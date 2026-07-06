import json
import logging
import time
import queue

from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

import requests
from flask import Blueprint, request, jsonify, current_app, g, Response, stream_with_context

from app_state import app_state
from services.stock_service import (
    fetch_history_sync_impl,
    fetch_history_async_task,
)
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
    is_market_open,
    get_cached,
    _parse_json_request,
    _stock_is_default_or_user,
    _get_stock_container,
    parse_non_negative_float,
    clear_cache_prefix,
    VALID_HISTORY_PERIODS,
    error_response,
    _is_local_request,
)
from utils.storage import save_user_stocks
from route_helpers import (
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
    POPULAR_US,
    POPULAR_JP,
    SSE_HEARTBEAT_INTERVAL,
    HISTORY_CACHE_DURATION_OPEN,
    HISTORY_CACHE_DURATION_OPEN_LONG,
    HISTORY_CACHE_DURATION_CLOSED,
    HISTORY_CACHE_DURATION_CLOSED_LONG,
    CACHE_DURATION_SEARCH,
    CACHE_DURATION_HEATMAP,
)

from app_helpers import require_trusted_state_changing_request
from config_utils import get_or_create_extension_api_token

api_stocks_bp = Blueprint("api_stocks", __name__)


@api_stocks_bp.route("/api/indices")
@rate_limit(max_requests=60, window_seconds=60)
def api_indices():
    """指数データAPIエンドポイント"""
    force = request.args.get("force") == "true"
    if force:
        schedule_sync_all_stocks_now()
    # キャッシュ済みのデータを即座に返す（バックグラウンドスレッドで更新される）
    with app_state.cache.sse_data_lock:
        data = _resolve_indices_for_response()
    if not data:
        _wait_for_initial_market_snapshot("indices", timeout_sec=6.0)
        with app_state.cache.sse_data_lock:
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
    with app_state.cache.sse_data_lock:
        stocks = _resolve_stocks_for_response()
        indices = _resolve_indices_for_response()
    if not any(stocks.get(m) for m in ("us", "jp", "idx")) and not indices:
        _wait_for_initial_market_snapshot("stocks", timeout_sec=6.0)
        with app_state.cache.sse_data_lock:
            stocks = _resolve_stocks_for_response()
            indices = _resolve_indices_for_response()
    yf_limited = app_state.is_yf_rate_limited()
    yf_until = None
    if yf_limited:
        from app_state import yf_session_manager
        rl_until = yf_session_manager.get_rate_limit_until("yfinance")
        if rl_until:
            yf_until = datetime.fromtimestamp(rl_until).isoformat()

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
    with app_state.market.history_circuit_lock:
        state: Any = app_state.market.history_circuit_state.get(symbol, {})
        if state.get("status") == "HALF_OPEN":
            is_half_open = True

    if is_open:
        logger.info("stock-history circuit open symbol=%s - failing fast", symbol)
        return error_response(ErrorCode.CIRCUIT_BREAKER_OPEN, status_code=200)

    cache_key = f"hist_{symbol}_{period}"
    
    # 市場が開いているかどうかでキャッシュ時間を動的に変更する
    if is_market_open(market):
        duration = HISTORY_CACHE_DURATION_OPEN if period in ["1d", "5d"] else HISTORY_CACHE_DURATION_OPEN_LONG
    else:
        duration = HISTORY_CACHE_DURATION_CLOSED if period in ["1d", "5d"] else HISTORY_CACHE_DURATION_CLOSED_LONG

    if is_half_open:
        logger.info("stock-history circuit HALF_OPEN symbol=%s - running sync fetch", symbol)
        res = fetch_history_sync_impl(symbol, market, period)
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
                fetch_history_async_task,
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

    # 4. ディスクキャッシュからフォールバック（再起動後も直近のデータを表示）
    disk_data = app_state.stock_disk_cache.get(cache_key)
    if disk_data and isinstance(disk_data, dict) and "error" not in disk_data:
        logger.info("Serving disk-cached history for %s period=%s", symbol, period)
        return jsonify({**disk_data, "stale": True, "message": "キャッシュ済みデータを表示中です。最新データを取得中..."})

    # 5. フェッチ中は一時的な空データを返す
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

    return jsonify(get_cached(f"search_{q}", _search, duration=CACHE_DURATION_SEARCH))


@api_stocks_bp.route("/api/stocks/add", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
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

    with app_state.market.user_stocks_lock:
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

    from app_bg import announce_current_market_state
    announce_current_market_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/delete", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
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

    with app_state.market.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        container.pop(symbol, None)

    save_user_stocks()
    invalidate_stock_caches(symbol)
    remove_stock_from_caches(symbol, market)

    from app_bg import announce_current_market_state
    announce_current_market_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/portfolio", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
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

    with app_state.market.user_stocks_lock:
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
    with app_state.cache.sse_data_lock:
        for cache in (app_state.market.current_stocks_cache, app_state.market.target_stocks_cache):
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
    from app_bg import announce_current_market_state
    announce_current_market_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/add_ext", methods=["POST", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_add_stock_ext():
    """拡張機能用銘柄追加APIエンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Validate raw socket IP to protect against proxy-override headers spoofing
    raw_remote = request.environ.get("REMOTE_ADDR", "").strip()
    from app_helpers import _is_loopback_ip
    if raw_remote and not _is_loopback_ip(raw_remote):
        current_app.logger.warning(
            "Add-ext request rejected: WSGI REMOTE_ADDR %s is not loopback", raw_remote
        )
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # CSRF protection: require BOTH custom header AND trusted origin for defense-in-depth.
    # Security model:
    # 1. X-MNS-Extension-Request header: cannot be set cross-origin without CORS preflight.
    # 2. _is_allowed_shutdown_origin: validates Origin/Referer against allow-list.
    auth_header = request.headers.get("Authorization")
    expected_token = get_or_create_extension_api_token()
    
    is_valid_token = False
    if auth_header and auth_header.startswith("Bearer "):
        import secrets
        token = auth_header[len("Bearer "):].strip()
        is_valid_token = secrets.compare_digest(token, expected_token)

    if not is_valid_token:
        current_app.logger.warning(
            "api_add_stock_ext: security rejection id=%s remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(ErrorCode.UNSAFE_INPUT, details={"reason": "invalid or missing extension token"}, status_code=403)

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
    with app_state.market.user_stocks_lock:
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

        from app_bg import announce_current_market_state
        announce_current_market_state()
        schedule_sync_all_stocks_now()
        return jsonify({"ok": True, "message": f"Added {symbol} to {market}"})
    return jsonify({"ok": True, "message": f"{symbol} already exists in {market}"})


@api_stocks_bp.route("/api/stocks/reset", methods=["POST"])
@rate_limit(max_requests=5, window_seconds=60)
def api_reset_stocks():
    """銘柄リセットAPIエンドポイント"""
    ok, reason = require_trusted_state_changing_request(request)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    with app_state.market.user_stocks_lock:
        app_state.market.user_us, app_state.market.user_jp, app_state.market.user_idx = {}, {}, {}
    save_user_stocks()
    with app_state.cache.sse_data_lock:
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.current_indices_cache = {}
        app_state.market.target_indices_cache = {}
    clear_cache_prefix("stocks")
    from app_bg import announce_current_market_state
    announce_current_market_state()
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
    return jsonify(get_cached(cache_key, _fetch_heatmap, duration=CACHE_DURATION_HEATMAP))


@api_stocks_bp.route("/api/stocks/stream", methods=["GET"])
@api_stocks_bp.route("/api/stream", methods=["GET"])
@rate_limit(max_requests=10, window_seconds=60)
def api_stocks_stream():
    """SSEストリームエンドポイント（接続数制限付き）"""
    request_id = getattr(g, "request_id", "-")

    def stream():
        try:
            with app_state.sse_announcer.listener_context() as q:
                sse_event_id = 0
                
                # 初回接続時に即座に現在のキャッシュ状態を送信する
                with app_state.cache.sse_data_lock:
                    initial_payload = json.dumps(
                        {
                            "stream_event": "initial_snapshot",
                            "stocks": app_state.market.current_stocks_cache,
                            "indices": app_state.market.current_indices_cache,
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
                        if msg is None:
                            current_app.logger.warning("SSE listener dropped due to backpressure id=%s", request_id)
                            break
                        sse_event_id += 1
                        yield f"id: {sse_event_id}\n{msg}"
                    except queue.Empty:
                        # 15秒間何もデータが来なかった場合、ハートビート送信
                        heartbeat_data = json.dumps(
                            {"type": "heartbeat", "timestamp": time.time()}
                        )
                        sse_event_id += 1
                        yield f"id: {sse_event_id}\nevent: heartbeat\ndata: {heartbeat_data}\n\n"
        except RuntimeError:
            current_app.logger.warning("SSE listener limit exceeded id=%s", request_id)
            err_data = json.dumps({"error": "too many SSE connections"})
            yield f"event: error\ndata: {err_data}\n\n"
            return
        except GeneratorExit:
            # クライアントが接続を切った
            current_app.logger.info("SSE client disconnected id=%s", request_id)

    response = Response(stream_with_context(stream()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
