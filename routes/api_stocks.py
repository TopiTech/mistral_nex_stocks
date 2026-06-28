import json
import logging
import time
import queue

from datetime import datetime

logger = logging.getLogger(__name__)

import pandas as pd
import requests
from flask import Blueprint, request, jsonify, current_app, g, Response, stream_with_context
from requests.exceptions import Timeout as RequestsTimeout

try:
    from curl_cffi.requests.exceptions import Timeout as CurlRequestsTimeout
except ImportError:
    CurlRequestsTimeout = RequestsTimeout  # type: ignore[misc,assignment,unused-ignore]

from app_state import app_state
from app_bg import (
    schedule_sync_all_stocks_now,
    fetch_stocks_batch,
)
from app_helpers import (
    _resolve_indices_for_response,
    _wait_for_initial_market_snapshot,
    _resolve_stocks_for_response,
    normalize_symbol,
    normalize_market,
    normalize_symbol_for_market,
    is_valid_symbol,
    get_stock_info_cached,
    normalize_optional_number,
    safe_get_ticker,
    normalize_history_frame,
    is_market_open,
    get_cached,
    _parse_json_request,
    _stock_is_default_or_user,
    _get_stock_container,
    save_user_stocks,
    parse_non_negative_float,
    _is_allowed_shutdown_origin,
    clear_cache_prefix,
    VALID_HISTORY_PERIODS,
    error_response,
    _is_local_request,
)
from route_helpers import (
    cleanup_history_circuit_state,
    _parse_stock_request,
    invalidate_stock_caches,
    ensure_stock_placeholder_in_caches,
    remove_stock_from_caches,
    _stock_display_name,
    rate_limit,
)
from utils.validators import validate_portfolio_input
from error_codes import ErrorCode, get_error_message
from constants import (
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
    YFINANCE_TIMEOUT_SINGLE,
    HISTORY_CIRCUIT_BREAKER_THRESHOLD,
    HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
    POPULAR_US,
    POPULAR_JP,
    SSE_HEARTBEAT_INTERVAL,
)

from app_helpers import require_trusted_state_changing_request

api_stocks_bp = Blueprint("api_stocks", __name__)


@api_stocks_bp.route("/api/indices")
@rate_limit(max_requests=60, window_seconds=60)
def api_indices():
    """指数データAPIエンドポイント"""
    force = request.args.get("force") == "true"
    if force:
        schedule_sync_all_stocks_now()
    # キャッシュ済みのデータを即座に返す（バックグラウンドスレッドで更新される）
    with app_state.sse_data_lock:
        data = _resolve_indices_for_response()
    if not data:
        _wait_for_initial_market_snapshot("indices", timeout_sec=6.0)
        with app_state.sse_data_lock:
            data = _resolve_indices_for_response()
    return jsonify(data)


@api_stocks_bp.route("/api/stocks")
@rate_limit(max_requests=60, window_seconds=60)
def api_stocks():
    """銘柄データAPIエンドポイント"""
    force = request.args.get("force") == "true"
    if force:
        schedule_sync_all_stocks_now()
    # キャッシュ済みのデータを即座に返す（バックグラウンドスレッドで更新される）
    with app_state.sse_data_lock:
        stocks = _resolve_stocks_for_response()
        indices = _resolve_indices_for_response()
    if not any(stocks.get(m) for m in ("us", "jp", "idx")) and not indices:
        _wait_for_initial_market_snapshot("stocks", timeout_sec=6.0)
        with app_state.sse_data_lock:
            stocks = _resolve_stocks_for_response()
            indices = _resolve_indices_for_response()
    with app_state.yfinance_lock:
        yf_limited = app_state.is_yfinance_rate_limited and (
            time.time() < app_state.yfinance_rate_limit_until
        )
        yf_until = (
            datetime.fromtimestamp(app_state.yfinance_rate_limit_until).isoformat()
            if app_state.is_yfinance_rate_limited
            else None
        )

    return jsonify(
        {
            "stocks": stocks,
            "indices": indices,
            "is_yfinance_rate_limited": yf_limited,
            "yfinance_rate_limit_until": yf_until,
        }
    )


@api_stocks_bp.route("/api/stock-details")
@rate_limit(max_requests=60, window_seconds=60)
def api_stock_details():
    """銘柄詳細情報APIエンドポイント"""
    symbol = normalize_symbol(request.args.get("symbol"))
    market = normalize_market(request.args.get("market"), default="us")
    if not symbol:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)

    symbol = normalize_symbol_for_market(symbol, market)
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    info = get_stock_info_cached(symbol)
    return jsonify(
        {
            "symbol": symbol,
            "sector": info.get("sector") or None,
            "industry": info.get("industry") or None,
            "market_cap": normalize_optional_number(info.get("marketCap")),
            "pe_ratio": normalize_optional_number(info.get("trailingPE")),
        }
    )


def _history_with_timeout(ticker_obj, period_value, interval_value, symbol):
    now = time.time()
    # Clean up old circuit states occasionally
    cleanup_history_circuit_state(now_ts=now)

    if app_state.is_circuit_open("yfinance_history", symbol=symbol):
        logger.info("stock-history circuit open symbol=%s", symbol)
        return pd.DataFrame()

    # Acquire semaphore with timeout to protect Web threads from blocking
    acquired = app_state.yfinance_history_semaphore.acquire(blocking=True, timeout=6.0)
    if not acquired:
        logger.warning("Timeout acquiring history semaphore for symbol=%s", symbol)
        return pd.DataFrame()

    try:
        result = ticker_obj.history(
            period=period_value,
            interval=interval_value,
            auto_adjust=True,
            timeout=YFINANCE_TIMEOUT_SINGLE,
        )
        app_state.report_circuit_result(
            "yfinance_history", success=True, symbol=symbol
        )
        return result
    except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as timeout_exc:
        app_state.report_circuit_result(
            "yfinance_history",
            success=False,
            symbol=symbol,
            threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
            open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
        )
        logger.debug(
            "stock-history timeout symbol=%s err=%s", symbol, timeout_exc
        )
        return pd.DataFrame()
    finally:
        app_state.yfinance_history_semaphore.release()


def _fetch_history_sync_impl(symbol, market, period):
    try:
        t = safe_get_ticker(symbol)
        if not t:
            return {
                "error": "銘柄情報が取得できませんでした。",
                "symbol": symbol,
            }

        # 1d の場合は短いインターバルで取得を試みる
        interval = "5m" if period == "1d" else "1d"
        if period == "5d":
            interval = "15m"

        # MA25 計算のために日足では十分な期間を拡張して取得する
        extended_period_map = {
            "1mo": "6mo",
            "3mo": "6mo",
            "6mo": "1y",
            "1y": "2y",
            "2y": "5y",
            "5y": "10y",
        }
        extended_period = period
        if interval == "1d" and period in extended_period_map:
            extended_period = extended_period_map[period]

        hist = _history_with_timeout(t, extended_period, interval, symbol)
        hist = normalize_history_frame(hist)

        # フォールバック 1: 1d/5m が失敗 → 1d/1d を試す
        if hist.empty and period == "1d" and interval == "5m":
            logger.info(
                "Fallback 1 for %s: 1d/5m failed, trying 1d/1d", symbol
            )
            hist = _history_with_timeout(t, "1d", "1d", symbol)
            hist = normalize_history_frame(hist)
            interval = "1d"

        # フォールバック 2: 空またはデータが少なすぎる場合 → 5d/1d を試す
        if (hist.empty or len(hist) < 1) and period in ["1d", "5d"]:
            logger.info("%s: trying 5d/1d", symbol)
            hist = _history_with_timeout(t, "5d", "1d", symbol)
            hist = normalize_history_frame(hist)
            interval = "1d"

        if hist.empty:
            return {
                "error": "データが見つかりませんでした。銘柄が上場廃止されているか、選択した期間のデータが存在しない可能性があります。",
                "symbol": symbol,
                "interval_used": interval,
                "period_requested": period,
            }

        # MA計算 (日足の場合のみ)
        # 拡張取得した全データで MA を計算するため NaN になる先頭行が減る
        if interval == "1d":
            if len(hist) >= 5:
                hist["MA5"] = hist["Close"].rolling(window=5).mean()
            if len(hist) >= 25:
                hist["MA25"] = hist["Close"].rolling(window=25).mean()

            # 元のピリオドに対応するカレンダー期間でデータをトリミング
            period_offset_map = {
                "1mo": pd.DateOffset(months=1),
                "3mo": pd.DateOffset(months=3),
                "6mo": pd.DateOffset(months=6),
                "1y": pd.DateOffset(years=1),
                "2y": pd.DateOffset(years=2),
                "5y": pd.DateOffset(years=5),
            }
            if extended_period != period and period in period_offset_map:
                cutoff = hist.index[-1] - period_offset_map[period]
                hist = hist[hist.index >= cutoff]

        data_list = []
        for dt, row in hist.iterrows():
            try:
                vol = (
                    int(float(row["Volume"]))
                    if ("Volume" in row and pd.notna(row["Volume"]))
                    else 0
                )
            except (TypeError, ValueError, KeyError):
                vol = 0
            d = {
                "x": dt.timestamp() * 1000,
                "o": float(row["Open"]) if pd.notna(row["Open"]) else 0,
                "h": float(row["High"]) if pd.notna(row["High"]) else 0,
                "l": float(row["Low"]) if pd.notna(row["Low"]) else 0,
                "c": float(row["Close"]) if pd.notna(row["Close"]) else 0,
                "v": vol,
            }
            if "MA5" in row.index and pd.notna(row["MA5"]):
                d["ma5"] = float(row["MA5"])
            if "MA25" in row.index and pd.notna(row["MA25"]):
                d["ma25"] = float(row["MA25"])
            data_list.append(d)

        return {"symbol": symbol, "history": data_list, "interval_used": interval}
    except Exception as exc:
        logger.error(
            "Stock history fetch failed (%s, %s): %s", symbol, period, exc
        )
        return {
            "error": get_error_message(ErrorCode.FETCH_FAILED, lang="ja"),
            "error_code": int(ErrorCode.FETCH_FAILED),
            "symbol": symbol,
        }


def _fetch_history_async_task(symbol, market, period, cache_key, duration):
    try:
        res = _fetch_history_sync_impl(symbol, market, period)
        from app_helpers import _set_cached_value
        _set_cached_value(cache_key, res, duration)
    except Exception as e:
        logger.error("Async background history fetch failed for %s: %s", symbol, e)
    finally:
        with app_state.history_fetch_lock:
            app_state.history_fetch_inflight.discard(cache_key)


@api_stocks_bp.route("/api/stock-history")
@rate_limit(max_requests=120, window_seconds=60)
def api_stock_history():
    """銘柄履歴データAPIエンドポイント"""
    symbol = normalize_symbol(request.args.get("symbol"))
    market = normalize_market(request.args.get("market"), default="us")
    period = (request.args.get("period") or "3mo").strip().lower()

    if not symbol:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if period not in VALID_HISTORY_PERIODS:
        return error_response(ErrorCode.INVALID_PERIOD)
    symbol = normalize_symbol_for_market(symbol, market)

    # 0. サーキットブレーカーの状態をチェック (Fail-Fast & HALF-OPEN 同期実行)
    is_open = app_state.is_circuit_open("yfinance_history", symbol=symbol)
    
    is_half_open = False
    with app_state.history_circuit_lock:
        state = app_state.history_circuit_state.get(symbol, {})
        if state.get("status") == "HALF_OPEN":
            is_half_open = True

    if is_open:
        logger.info("stock-history circuit open symbol=%s - failing fast", symbol)
        return jsonify({
            "error": "サーキットブレーカーにより一時的にリクエストが制限されています。",
            "symbol": symbol,
            "error_code": int(ErrorCode.FETCH_FAILED)
        })

    cache_key = f"hist_{symbol}_{period}"
    
    # 市場が開いているかどうかでキャッシュ時間を動的に変更する
    if is_market_open(market):
        duration = 60 if period in ["1d", "5d"] else 3600
    else:
        duration = 3600 if period in ["1d", "5d"] else 43200

    if is_half_open:
        logger.info("stock-history circuit HALF_OPEN symbol=%s - running sync fetch", symbol)
        res = _fetch_history_sync_impl(symbol, market, period)
        if "error" not in res:
            from app_helpers import _set_cached_value
            _set_cached_value(cache_key, res, duration)
        return jsonify(res)
    from app_helpers import _get_cached_value, _has_cached_key

    # 1. すでにキャッシュが存在する場合は即座に返却
    if _has_cached_key(cache_key, duration):
        cached_data = _get_cached_value(cache_key, duration)
        if cached_data:
            return jsonify(cached_data)

    # 2. キャッシュがない場合、バックグラウンドフェッチが進行中か確認
    with app_state.history_fetch_lock:
        already_fetching = cache_key in app_state.history_fetch_inflight
        if not already_fetching:
            app_state.history_fetch_inflight.add(cache_key)

    # 3. 進行中でない場合は、新しくバックグラウンドスレッドで起動
    if not already_fetching:
        try:
            app_state.execution.executor.submit(
                _fetch_history_async_task,
                symbol,
                market,
                period,
                cache_key,
                duration
            )
            logger.info("Triggered async background history fetch for key=%s", cache_key)
        except Exception as exc:
            with app_state.history_fetch_lock:
                app_state.history_fetch_inflight.discard(cache_key)
            logger.warning("Failed to submit async history fetch for %s: %s", symbol, exc)

    # 4. フェッチ中は一時的な空データを返す
    return jsonify({
        "symbol": symbol,
        "history": [],
        "fetching": True,
        "message": "履歴データを取得中です。しばらくしてから再ロードしてください。"
    })


@api_stocks_bp.route("/api/search")
@rate_limit(max_requests=90, window_seconds=60)
def api_search():
    """銘柄検索APIエンドポイント"""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": "検索ワードは2文字以上"}
        )

    def _search():
        try:
            results = app_state.stock_provider.search(q, max_results=10)
            return {"results": results}
        except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
            current_app.logger.error("Search API failed (%s): %s", q, exc)
            return {
                "error": get_error_message(ErrorCode.API_SERVICE_ERROR, lang="ja"),
                "error_code": int(ErrorCode.API_SERVICE_ERROR),
            }

    return jsonify(get_cached(f"search_{q}", _search, duration=60))


@api_stocks_bp.route("/api/stocks/add", methods=["POST"])
def api_add_stock():
    """銘柄追加APIエンドポイント"""
    ok, reason = require_trusted_state_changing_request(request)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    parsed, error = _parse_stock_request(data, require_name=True, default_market="")
    if error:
        return error
    if parsed is None:
        return error_response(ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"})
    name = parsed["name"]
    market = parsed["market"]
    symbol = parsed["symbol"]

    with app_state.user_stocks_lock:
        if _stock_is_default_or_user(symbol, market):
            return error_response(
                ErrorCode.INVALID_INPUT, details={"reason": "既に追加済み"}
            )

        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        container[symbol] = name

    save_user_stocks()
    invalidate_stock_caches(symbol)
    ensure_stock_placeholder_in_caches(symbol, name, market)

    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/delete", methods=["POST"])
def api_delete_stock():
    """銘柄削除APIエンドポイント"""
    ok, reason = require_trusted_state_changing_request(request)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    parsed, error = _parse_stock_request(data, default_market="")
    if error:
        return error
    if parsed is None:
        return error_response(ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"})
    market = parsed["market"]
    symbol = parsed["symbol"]

    with app_state.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        container.pop(symbol, None)

    save_user_stocks()
    invalidate_stock_caches(symbol)
    remove_stock_from_caches(symbol, market)

    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/portfolio", methods=["POST"])
def api_update_portfolio():
    """ポートフォリオ更新APIエンドポイント"""
    ok, reason = require_trusted_state_changing_request(request)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    parsed, error = _parse_stock_request(data, default_market="")
    if error:
        return error
    if parsed is None:
        return error_response(ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"})
    market = parsed["market"]
    symbol = parsed["symbol"]

    try:
        shares_raw = data.get("shares")
        avg_price_raw = data.get("avg_price")
        avg_fx_rate_raw = data.get("avg_fx_rate")
        if shares_raw is None or str(shares_raw).strip() == "":
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["shares"]}
            )
        if avg_price_raw is None or str(avg_price_raw).strip() == "":
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["avg_price"]}
            )

        shares = parse_non_negative_float(
            shares_raw, "shares", max_value=PORTFOLIO_SHARES_MAX
        )
        avg_price = parse_non_negative_float(
            avg_price_raw, "avg_price", max_value=PORTFOLIO_AVG_PRICE_MAX
        )
        avg_fx_rate = None
        if avg_fx_rate_raw is not None and str(avg_fx_rate_raw).strip():
            avg_fx_rate = parse_non_negative_float(
                avg_fx_rate_raw, "avg_fx_rate", max_value=1_000_000.0
            )

        portfolio_errors = validate_portfolio_input(shares, avg_price, avg_fx_rate)
        if portfolio_errors:
            return error_response(
                ErrorCode.INVALID_INPUT, details={"reason": portfolio_errors[0]}
            )
    except ValueError as exc:
        return error_response(ErrorCode.INVALID_INPUT, details={"reason": str(exc)})

    with app_state.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)

        name = _stock_display_name(symbol, market)
        if symbol not in container:
            container[symbol] = {"name": name, "shares": shares, "avg_price": avg_price}
            if avg_fx_rate is not None:
                container[symbol]["avg_fx_rate"] = avg_fx_rate
        else:
            val = container[symbol]
            if isinstance(val, str):
                val = {
                    "name": val,
                    "shares": shares,
                    "avg_price": avg_price,
                }
            else:
                val["shares"] = shares
                val["avg_price"] = avg_price

            if avg_fx_rate is not None:
                val["avg_fx_rate"] = avg_fx_rate
            else:
                val.pop("avg_fx_rate", None)

            container[symbol] = val

    save_user_stocks()
    invalidate_stock_caches(symbol)

    # フロントエンドの fetchInitialStocks や SSE に即座に反映させるため両方のキャッシュを更新する
    with app_state.sse_data_lock:
        for cache in (app_state.current_stocks_cache, app_state.target_stocks_cache):
            if market not in cache:
                cache[market] = []
            target_list = cache.get(market, [])
            found = False
            for s in target_list:
                if s.get("symbol") == symbol:
                    s["shares"] = shares
                    s["avg_price"] = avg_price
                    if avg_fx_rate is not None:
                        s["avg_fx_rate"] = avg_fx_rate
                    else:
                        s.pop("avg_fx_rate", None)
                    found = True
                    break
            if not found:
                target_list.append(
                    {
                        "symbol": symbol,
                        "name": _stock_display_name(symbol, market),
                        "market": market,
                        "price": "--",
                        "change": "--",
                        "change_percent": "--",
                        "chart_data": [],
                        "shares": shares,
                        "avg_price": avg_price,
                    }
                )
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/add_ext", methods=["POST", "OPTIONS"])
def api_add_stock_ext():
    """拡張機能用銘柄追加APIエンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # CSRF protection: require BOTH custom header AND trusted origin for defense-in-depth.
    # Security model:
    # 1. X-MNS-Extension-Request header: cannot be set cross-origin without CORS preflight.
    # 2. _is_allowed_shutdown_origin: validates Origin/Referer against allow-list.
    has_header = request.headers.get("X-MNS-Extension-Request") == "true"
    has_trusted_origin = _is_allowed_shutdown_origin(request)

    if not (has_header and has_trusted_origin):
        current_app.logger.warning(
            "api_add_stock_ext: security rejection id=%s header=%s origin_ok=%s remote=%s",
            getattr(g, "request_id", "-"),
            has_header,
            has_trusted_origin,
            request.remote_addr,
        )
        return error_response(ErrorCode.UNSAFE_INPUT, details={"reason": "security rejection"}, status_code=403)

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    parsed, error = _parse_stock_request(data, require_name=False)
    if error:
        return error
    if parsed is None:
        return error_response(ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"})
    market = parsed["market"]
    symbol = parsed["symbol"]

    added = False
    with app_state.user_stocks_lock:
        if _stock_is_default_or_user(symbol, market):
            return jsonify(
                {"ok": True, "message": f"{symbol} already exists in {market}"}
            )

        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        container[symbol] = symbol
        added = True

    if added:
        save_user_stocks()
        invalidate_stock_caches(symbol)
        ensure_stock_placeholder_in_caches(symbol, symbol, market)

        schedule_sync_all_stocks_now()
        return jsonify({"ok": True, "message": f"Added {symbol} to {market}"})
    return jsonify({"ok": True, "message": f"{symbol} already exists in {market}"})


@api_stocks_bp.route("/api/stocks/reset", methods=["POST"])
def api_reset_stocks():
    """銘柄リセットAPIエンドポイント"""
    ok, reason = require_trusted_state_changing_request(request)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    with app_state.user_stocks_lock:
        app_state.user_us, app_state.user_jp, app_state.user_idx = {}, {}, {}
    save_user_stocks()
    with app_state.sse_data_lock:
        app_state.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.current_indices_cache = {}
        app_state.target_indices_cache = {}
    clear_cache_prefix("stocks")
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/heatmap")
@rate_limit(max_requests=30, window_seconds=60)
def api_heatmap():
    """ヒートマップデータAPIエンドポイント"""
    market = normalize_market(request.args.get("market"), default="us")
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if market not in ("us", "jp"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "heatmap market は us/jp のみ対応です"},
        )
    symbols = POPULAR_US if market == "us" else POPULAR_JP

    def _fetch_heatmap():
        items = []
        for s in symbols:
            items.append((s, "", market))  # fallback name is empty

        fetched = fetch_stocks_batch(items)
        results = []
        for item in fetched:
            if not item:
                continue

            # P2修正: build_stock_payload が既に sector/industry を含むため get_stock_info_cached の再呼び出しを削除
            # market_cap のみ別途必要なため info から取得（ただし build_stock_payload 経由でキャッシュ済み）
            info = get_stock_info_cached(
                item["symbol"]
            )  # ここではキャッシュHITのみ（再フェッチなし）
            price = (
                normalize_optional_number(item.get("price"))
                or normalize_optional_number(item.get("close"))
                or 0
            )
            volume = normalize_optional_number(item.get("volume")) or 0
            fallback_size = price * max(volume, 1)
            try:
                change_pct_raw = item.get("change_percent")
                change_pct = (
                    float(change_pct_raw) if change_pct_raw is not None else 0.0
                )
            except (ValueError, TypeError):
                change_pct = 0.0

            from sectors import PREDEFINED_SECTORS

            sector = (
                item.get("sector")
                or info.get("sector")
                or PREDEFINED_SECTORS.get(item["symbol"], "Other")
            )
            if sector == "Other":
                sector = PREDEFINED_SECTORS.get(item["symbol"], "Other")

            results.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "price": price,
                    "change_percent": change_pct,
                    "market_cap": (
                        normalize_optional_number(item.get("market_cap"))
                        or normalize_optional_number(item.get("marketCap"))
                        or (
                            (
                                normalize_optional_number(item.get("sharesOutstanding"))
                                * price
                            )
                            if normalize_optional_number(item.get("sharesOutstanding"))
                            is not None
                            else fallback_size
                        )
                        or fallback_size
                    ),
                    "sector": sector,
                }
            )
        results = [r for r in results if float(r.get("market_cap") or 0) > 0]
        results.sort(key=lambda r: r.get("market_cap", 0), reverse=True)
        return {"stocks": results}

    cache_key = f"heatmap_{market}"
    return jsonify(get_cached(cache_key, _fetch_heatmap, duration=300))


@api_stocks_bp.route("/api/stocks/stream")
@rate_limit(max_requests=10, window_seconds=60)
def api_stocks_stream():
    """SSEストリームエンドポイント（接続数制限付き）"""
    request_id = getattr(g, "request_id", "-")
    try:
        q = app_state.sse_announcer.listen()
    except RuntimeError:
        current_app.logger.warning("SSE listener limit exceeded id=%s", request_id)
        return jsonify({"ok": False, "error": "too many SSE connections"}), 429

    def stream():
        sse_event_id = 0
        try:
            # 初回接続時に即座に現在のキャッシュ状態を送信する
            with app_state.sse_data_lock:
                initial_payload = json.dumps(
                    {
                        "stream_event": "initial_snapshot",
                        "stocks": app_state.current_stocks_cache,
                        "indices": app_state.current_indices_cache,
                    }
                )
            sse_event_id += 1
            yield f"id: {sse_event_id}\ndata: {initial_payload}\n\n"

            # 15秒ハートビート（クライアント側でタイムアウト検出用）
            heartbeat_interval = SSE_HEARTBEAT_INTERVAL

            while True:
                try:
                    # タイムアウトを15秒に設定し、その間隔でハートビート送信
                    msg = q.get(timeout=heartbeat_interval)
                    sse_event_id += 1
                    yield f"id: {sse_event_id}\n{msg}"
                except queue.Empty:
                    # 15秒間何もデータが来なかった場合、ハートビート送信
                    heartbeat_data = json.dumps(
                        {"type": "heartbeat", "timestamp": time.time()}
                    )
                    sse_event_id += 1
                    yield f"id: {sse_event_id}\nevent: heartbeat\ndata: {heartbeat_data}\n\n"
        except GeneratorExit:
            # クライアントが接続を切った
            current_app.logger.info("SSE client disconnected id=%s", request_id)
        finally:
            app_state.sse_announcer.unlisten(q)

    response = Response(stream_with_context(stream()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
