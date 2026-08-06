import copy
import json
import logging
import os
import queue
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

import requests
from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from app_bg import (
    fetch_stocks_batch,
    schedule_sync_all_stocks_now,
)
from app_state import app_state
from constants import (
    CACHE_DURATION_HEATMAP,
    CACHE_DURATION_SEARCH,
    HISTORY_CACHE_DURATION_CLOSED,
    HISTORY_CACHE_DURATION_CLOSED_LONG,
    HISTORY_CACHE_DURATION_OPEN,
    HISTORY_CACHE_DURATION_OPEN_LONG,
    POPULAR_JP,
    POPULAR_US,
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
    SSE_GET_TIMEOUT,
    SSE_HEARTBEAT_INTERVAL,
    VALID_HISTORY_INTERVALS,
    VALID_HISTORY_PERIODS,
)
from credential_manager import get_or_create_extension_api_token
from error_codes import ErrorCode, get_error_message
from route_helpers import (
    _parse_stock_request,
    _stock_display_name,
    ensure_stock_placeholder_in_caches,
    invalidate_stock_caches,
    rate_limit,
    remove_stock_from_caches,
)
from services.market_data_service import (
    build_heatmap_payload,
    build_popular_symbol_items,
    build_screener_base_rows,
    build_screener_enrichment,
)
from services.stock_service import (
    fetch_history_async_task,
)
from utils.caching import (
    CACHE_FETCHING,
    _get_cached_value,
    _has_cached_key,
    clear_cache_prefix,
    get_cached,
)
from utils.market_utils import is_market_open
from utils.networking import (
    _is_local_request,
    require_trusted_or_admin,
)
from utils.normalization import (
    is_valid_symbol,
    normalize_market,
    normalize_optional_number,
    normalize_symbol,
    normalize_symbol_for_market,
)
from utils.stock_payload import (
    _get_stock_container,
    _resolve_indices_for_response,
    _resolve_stocks_for_response,
    _stock_is_default_or_user,
    _wait_for_initial_market_snapshot,
    error_response,
    fetch_stock_info_async,
    get_stock_info_cached,
)
from utils.storage import UserStocksPersistError, save_user_stocks
from utils.text_utils import _parse_json_request, parse_non_negative_float
from utils.validators import validate_portfolio_input

_HEATMAP_FETCH_START_TIMES: dict[str, float] = {}


def _sync_realtime_symbol(symbol: str, market: str, register: bool) -> None:
    """Keep the realtime market engine's subscription list in sync with the watchlist.

    Symbols added to the watchlist after startup are registered with the realtime
    engine (TradingView WS / Yahoo JP) so they receive live updates; deletions are
    unregistered and their stored quote state is purged.
    """
    try:
        from services.realtime_engine import realtime_market_engine

        if register:
            realtime_market_engine.register_symbol(symbol, market)
        else:
            realtime_market_engine.unregister_symbol(symbol, market)
    except Exception:
        current_app.logger.debug(
            "Realtime engine symbol sync skipped for %s/%s (register=%s)",
            market,
            symbol,
            register,
        )


def _fetch_heatmap_cached(cache_key: str, market: str, symbols: list[str]):
    """バックグラウンドexecutorから呼ばれ、ヒートマップを取得してキャッシュに格納する。"""
    try:
        res = get_cached(
            cache_key,
            lambda: build_heatmap_payload(market, symbols, fetch_batch_fn=fetch_stocks_batch),
            duration=CACHE_DURATION_HEATMAP,
        )
        if res and isinstance(res, dict) and res.get("stocks"):
            try:
                app_state.payload_disk_cache.set(cache_key, res)
            except Exception as exc:
                logger.debug("Failed to save heatmap payload to disk cache: %s", exc)
    except Exception:
        logger.exception("Failed to fetch heatmap cached for key %s", cache_key)
    finally:
        with app_state.heatmap_fetch_lock:
            app_state.heatmap_fetch_inflight.discard(cache_key)
            _HEATMAP_FETCH_START_TIMES.pop(cache_key, None)


api_stocks_bp = Blueprint("api_stocks", __name__)


@api_stocks_bp.route("/api/indices")
@rate_limit(max_requests=60, window_seconds=60)
def api_indices():
    """指数データAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)
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
    if not data:
        return jsonify({"fetching": True})
    return jsonify(data)


@api_stocks_bp.route("/api/stocks")
@rate_limit(max_requests=60, window_seconds=60)
def api_stocks():
    """銘柄データAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )
    force = request.args.get("force") == "true"
    if force:
        schedule_sync_all_stocks_now(force=True)
    # キャッシュ済みのデータを即座に返す（バックグラウンドスレッドで更新される）
    with app_state.cache.sse_data_lock:
        stocks = _resolve_stocks_for_response()
        indices = _resolve_indices_for_response()
    if not any(stocks.get(m) for m in ("us", "jp", "idx")) and not indices:
        _wait_for_initial_market_snapshot("stocks", timeout_sec=6.0)
        with app_state.cache.sse_data_lock:
            stocks = _resolve_stocks_for_response()
            indices = _resolve_indices_for_response()
    yf_limited = app_state.market.is_yf_rate_limited()
    yf_until = None
    if yf_limited:
        rl_until = app_state.yf_session_manager.get_rate_limit_until("yfinance")
        if rl_until:
            yf_until = datetime.fromtimestamp(rl_until, tz=UTC).isoformat()

    is_empty = not any(stocks.get(m) for m in ("us", "jp", "idx")) and not indices
    return jsonify(
        {
            "stocks": stocks,
            "indices": indices,
            "is_yfinance_rate_limited": yf_limited,
            "yfinance_rate_limit_until": yf_until,
            "fetching": is_empty,
        }
    )


@api_stocks_bp.route("/api/stock-details")
@rate_limit(max_requests=60, window_seconds=60)
def api_stock_details():
    """銘柄詳細情報APIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)
    symbol = normalize_symbol(request.args.get("symbol"))
    market = normalize_market(request.args.get("market"), default="us")
    if not symbol:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)

    symbol = normalize_symbol_for_market(symbol, market)
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    # Serve from the short cache (populated by the background sync and by
    # on-demand async fetches) WITHOUT blocking the request thread on a yfinance
    # network call. On a cold cache miss we offload the fetch to data_executor
    # and return fetching:True so the client can poll (H-2).
    short_cache_key = f"info_short_{symbol}"
    with app_state.yfinance_short_cache_lock:
        cached_short = app_state.yfinance_short_cache.get(short_cache_key)
    if isinstance(cached_short, dict) and cached_short:
        info = cached_short
    else:
        _submit_async_info_fetch(symbol)
        return jsonify(
            {
                "symbol": symbol,
                "fetching": True,
                "message": "銘柄詳細を取得中です。しばらくしてから再読み込みしてください。",
            }
        )
    return jsonify(
        {
            "symbol": symbol,
            "sector": info.get("sector") or None,
            "industry": info.get("industry") or None,
            "market_cap": normalize_optional_number(info.get("marketCap")),
            "pe_ratio": normalize_optional_number(info.get("trailingPE")),
        }
    )


def _submit_async_info_fetch(symbol: str) -> None:
    """Offload a stock-info fetch to data_executor (see H-2).

    Reuses the inflight guard pattern from history fetches to avoid spawning
    duplicate background jobs for the same symbol.
    """
    info_key = f"info_{symbol}"
    with app_state.info_fetch_lock:
        if info_key in app_state.info_fetch_inflight:
            return
        app_state.info_fetch_inflight.add(info_key)
    try:
        app_state.execution.data_executor.submit(_run_async_info_fetch, symbol)
    except queue.Full:
        current_app.logger.warning("Info fetch queue is full symbol=%s", symbol)
        with app_state.info_fetch_lock:
            app_state.info_fetch_inflight.discard(info_key)
    except (RuntimeError, AttributeError, ValueError) as exc:
        # Do not leave the symbol permanently marked in-flight when the executor
        # has been shut down or cannot accept work.
        current_app.logger.warning("Failed to submit info fetch symbol=%s: %s", symbol, exc)
        with app_state.info_fetch_lock:
            app_state.info_fetch_inflight.discard(info_key)


def _run_async_info_fetch(symbol: str) -> None:
    try:
        fetch_stock_info_async(symbol)
    finally:
        with app_state.info_fetch_lock:
            app_state.info_fetch_inflight.discard(f"info_{symbol}")


def _submit_async_history_fetch(
    cache_key: str,
    symbol: str,
    market: str,
    period: str,
    duration: int,
    log_label: str = "",
    interval: str = "auto",
) -> bool:
    """
    バックグラウンドexecutorに履歴データ非同期フェッチを送信する共通ヘルパー。

    既に同一cache_keyのフェッチが進行中かをチェックし、重複送信を防止する。
    送信成功時は True、失敗（重複含む）時は False を返す。
    """
    with app_state.history_fetch_lock:
        if cache_key in app_state.history_fetch_inflight:
            return False
        app_state.history_fetch_inflight.add(cache_key)

    try:
        # Route market-data fetches to data_executor so AI-bound work on the
        # general executor cannot starve history/price refreshes (H3).
        app_state.execution.data_executor.submit(
            fetch_history_async_task,
            symbol,
            market,
            period,
            cache_key,
            duration,
            interval=interval,
        )
        if log_label:
            logger.info("Async history fetch submitted: %s key=%s", log_label, cache_key)
        return True
    except queue.Full:
        with app_state.history_fetch_lock:
            app_state.history_fetch_inflight.discard(cache_key)
        raise
    except Exception as exc:
        with app_state.history_fetch_lock:
            app_state.history_fetch_inflight.discard(cache_key)
        logger.warning(
            "Failed to submit async history fetch %s symbol=%s: %s",
            log_label,
            symbol,
            exc,
        )
        return False


@api_stocks_bp.route("/api/stock-history")
@rate_limit(max_requests=120, window_seconds=60)
def api_stock_history():
    """銘柄履歴データAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)
    symbol = normalize_symbol(request.args.get("symbol"))
    market = normalize_market(request.args.get("market"), default="us")
    period = (request.args.get("period") or "3mo").strip().lower()
    interval = (request.args.get("interval") or "auto").strip().lower()

    if not symbol:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if period not in VALID_HISTORY_PERIODS:
        return error_response(ErrorCode.INVALID_PERIOD)
    if interval not in VALID_HISTORY_INTERVALS:
        interval = "auto"
    symbol = normalize_symbol_for_market(symbol, market)

    # 0. サーキットブレーカーの状態をチェック (Fail-Fast & HALF-OPEN 同期実行)
    is_open = app_state.market.is_circuit_open("yfinance_history", symbol=symbol)

    is_half_open = False
    with app_state.market.history_circuit_lock:
        state: Any = app_state.market.history_circuit_state.get(symbol, {})
        if state.get("status") == "HALF_OPEN":
            is_half_open = True

    if is_open:
        logger.info("stock-history circuit open symbol=%s - failing fast", symbol)
        return error_response(ErrorCode.CIRCUIT_BREAKER_OPEN, status_code=503)

    cache_key = f"hist_{symbol}_{period}_{interval}" if interval != "auto" else f"hist_{symbol}_{period}"

    # 市場が開いているかどうかでキャッシュ時間を動的に変更する
    if is_market_open(market):
        duration = (
            HISTORY_CACHE_DURATION_OPEN
            if period in ["1d", "5d"]
            else HISTORY_CACHE_DURATION_OPEN_LONG
        )
    else:
        duration = (
            HISTORY_CACHE_DURATION_CLOSED
            if period in ["1d", "5d"]
            else HISTORY_CACHE_DURATION_CLOSED_LONG
        )

    def make_history_response(payload, is_cacheable=True):
        resp = jsonify(payload)
        if is_cacheable and "error" not in payload and not payload.get("fetching"):
            if is_market_open(market):
                resp.headers["Cache-Control"] = "public, max-age=60"
            else:
                resp.headers["Cache-Control"] = "public, max-age=3600"
        else:
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    FETCHING_RESPONSE = {
        "symbol": symbol,
        "history": [],
        "fetching": True,
        "message": "履歴データを取得中です。しばらくしてから再ロードしてください。",
    }

    if is_half_open:
        logger.info("stock-history circuit HALF_OPEN symbol=%s - scheduling async fetch", symbol)
        try:
            _submit_async_history_fetch(cache_key, symbol, market, period, duration, "HALF_OPEN", interval=interval)
        except queue.Full:
            logger.warning("History fetch queue full during HALF_OPEN symbol=%s", symbol)
        return make_history_response(FETCHING_RESPONSE, is_cacheable=False)

    # 1. すでにキャッシュが存在する場合は即座に返却
    if _has_cached_key(cache_key, duration):
        cached_data = _get_cached_value(cache_key, duration)
        if cached_data:
            return make_history_response(cached_data)

    # 2. キャッシュがない場合、バックグラウンドフェッチを開始
    try:
        submitted = _submit_async_history_fetch(
            cache_key, symbol, market, period, duration, "cache_miss", interval=interval
        )
    except queue.Full:
        current_app.logger.warning("History fetch queue is full symbol=%s", symbol)
        return error_response(
            ErrorCode.TOO_MANY_REQUESTS,
            details={
                "reason": "履歴取得の処理容量を超えました。しばらくしてから再試行してください。"
            },
            status_code=503,
        )
    if submitted:
        logger.info("Triggered async background history fetch for key=%s", cache_key)

    # 4. ディスクキャッシュからフォールバック（再起動後も直近のデータを表示）
    disk_data = app_state.stock_disk_cache.get(cache_key)
    if disk_data and isinstance(disk_data, dict) and "error" not in disk_data:
        logger.info("Serving disk-cached history for %s period=%s", symbol, period)
        return make_history_response(
            {
                **disk_data,
                "stale": True,
                "message": "キャッシュ済みデータを表示中です。最新データを取得中...",
            },
            is_cacheable=False,
        )

    # 5. フェッチ中は一時的な空データを返す
    return make_history_response(
        {
            "symbol": symbol,
            "history": [],
            "fetching": True,
            "message": "履歴データを取得中です。しばらくしてから再ロードしてください。",
        },
        is_cacheable=False,
    )


@api_stocks_bp.route("/api/search")
@rate_limit(max_requests=90, window_seconds=60)
def api_search():
    """銘柄検索APIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return error_response(ErrorCode.INVALID_INPUT, details={"reason": "検索ワードは2文字以上"})
    if len(q) > 200:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "検索ワードは200文字以内で入力してください"},
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

    result = get_cached(f"search_{q}", _search, duration=CACHE_DURATION_SEARCH)
    # get_cached() returns CACHE_FETCHING when a concurrent fetcher is still
    # running and the waiter timed out (stampede prevention). Never jsonify
    # the sentinel — that would serialize a useless object and break the
    # client contract (the frontend reads data.results). Fall back to an
    # empty result set so the endpoint always returns a dict. (Mirrors the
    # guard already present in get_trending.)
    if result is CACHE_FETCHING or not isinstance(result, dict):
        result = {"results": []}
    return jsonify(result)


@api_stocks_bp.route("/api/screener")
@rate_limit(max_requests=60, window_seconds=60)
def api_screener():
    """簡易株式スクリーナーAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    market_filter = (request.args.get("market") or "all").strip().lower()
    sector_filter = (request.args.get("sector") or "all").strip()
    q = (request.args.get("q") or "").strip().lower()
    sort_by = (request.args.get("sort_by") or "market_cap").strip().lower()
    sort_order = (request.args.get("sort_order") or "desc").strip().lower()

    def _parse_float(val):
        if val is None or str(val).strip() == "":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    min_price = _parse_float(request.args.get("min_price"))
    max_price = _parse_float(request.args.get("max_price"))
    min_change = _parse_float(request.args.get("min_change"))
    max_change = _parse_float(request.args.get("max_change"))

    # Collect stock records from the active market snapshot.
    stocks_data = _resolve_stocks_for_response(include_portfolio=False)
    all_stocks = build_screener_base_rows(stocks_data, market_filter)
    seen_symbols = {item["symbol"] for item in all_stocks}

    # Include popular stocks & fetch/enrich stock data for unregistered symbols
    pop_sources = []
    if market_filter in ("all", "us"):
        pop_sources.append(("us", POPULAR_US))
    if market_filter in ("all", "jp"):
        pop_sources.append(("jp", POPULAR_JP))

    pop_unseen_items = build_popular_symbol_items(market_filter, q, seen_symbols, pop_sources)

    q_symbol = None
    if q and is_valid_symbol(q.upper()):
        q_symbol = q.upper()
        mkt_q = "jp" if (q_symbol.endswith(".T") or q_symbol.isdigit()) else "us"
        if market_filter == "all" or market_filter == mkt_q:
            # The queried symbol keeps full-fetch privilege on batch failure even
            # when it is already present via the popular list (seen check only
            # prevents a duplicate enrichment entry, not the network allowance).
            if q_symbol not in seen_symbols:
                seen_symbols.add(q_symbol)
                pop_unseen_items.append((q_symbol, q_symbol, mkt_q))
        else:
            q_symbol = None

    if pop_unseen_items:
        # Cache the enrichment result so repeated page loads / filter changes
        # within the TTL do not re-trigger a yfinance batch download on every
        # request. The per-request symbol set (pop_unseen_items) is still
        # recomputed against the live watchlist, so cached rows for symbols that
        # later join the watchlist are simply not merged in (no duplicates).
        enrich_key = f"screener_enrich_{market_filter}_{q}"
        enriched = get_cached(
            enrich_key,
            lambda: build_screener_enrichment(
                pop_unseen_items,
                q_symbol,
                fetch_batch_fn=fetch_stocks_batch,
                get_info_fn=get_stock_info_cached,
            ),
            duration=CACHE_DURATION_SEARCH,
        )
        # get_cached() returns CACHE_FETCHING when a concurrent fetcher is still
        # running and the waiter times out (stampede prevention); treat as "no data".
        if enriched is CACHE_FETCHING or not isinstance(enriched, dict):
            enriched = {}
        for sym, _fallback_name, _mkt in pop_unseen_items:
            row = enriched.get(sym)
            if row is not None:
                all_stocks.append(row)

    # Apply Filtering
    filtered = []
    for item in all_stocks:
        if sector_filter != "all" and item["sector"].lower() != sector_filter.lower():
            continue
        if min_price is not None and item["price"] > 0 and item["price"] < min_price:
            continue
        if max_price is not None and item["price"] > 0 and item["price"] > max_price:
            continue
        if min_change is not None and item["change_percent"] < min_change:
            continue
        if max_change is not None and item["change_percent"] > max_change:
            continue
        if q:
            matched_q = (
                q in item["symbol"].lower()
                or q in item["name"].lower()
                or q in item["sector"].lower()
            )
            if not matched_q:
                continue
        filtered.append(item)

    # Apply Sorting
    reverse = (sort_order != "asc")
    if sort_by == "price":
        filtered.sort(key=lambda x: x["price"], reverse=reverse)
    elif sort_by == "change_percent":
        filtered.sort(key=lambda x: x["change_percent"], reverse=reverse)
    elif sort_by == "volume":
        filtered.sort(key=lambda x: x["volume"], reverse=reverse)
    elif sort_by == "symbol":
        filtered.sort(key=lambda x: x["symbol"], reverse=reverse)
    else:  # market_cap
        filtered.sort(key=lambda x: x["market_cap"], reverse=reverse)

    return jsonify({
        "ok": True,
        "total": len(filtered),
        "stocks": filtered[:150],
    })


@api_stocks_bp.route("/api/stocks/add", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def api_add_stock():

    """銘柄追加APIエンドポイント"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )

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
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"}
        )
    name = parsed["name"]
    market = parsed["market"]
    symbol = parsed["symbol"]

    with app_state.market.user_stocks_lock:
        if _stock_is_default_or_user(symbol, market):
            return error_response(ErrorCode.INVALID_INPUT, details={"reason": "既に追加済み"})

        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        container[symbol] = name

        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            container.pop(symbol, None)
            current_app.logger.error("Failed to persist added stock %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
    invalidate_stock_caches(symbol)
    ensure_stock_placeholder_in_caches(symbol, name, market)

    from app_bg import announce_current_market_state

    announce_current_market_state()
    _sync_realtime_symbol(symbol, market, register=True)
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/delete", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def api_delete_stock():
    """銘柄削除APIエンドポイント"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )

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
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"}
        )
    market = parsed["market"]
    symbol = parsed["symbol"]

    with app_state.market.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        previous_value = container.pop(symbol, None)

        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            if previous_value is not None:
                container[symbol] = previous_value
            current_app.logger.error("Failed to persist deleted stock %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
    invalidate_stock_caches(symbol)
    remove_stock_from_caches(symbol, market)

    _sync_realtime_symbol(symbol, market, register=False)

    from app_bg import announce_current_market_state

    announce_current_market_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/portfolio", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_update_portfolio():
    """ポートフォリオ更新APIエンドポイント"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )

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
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"}
        )
    market = parsed["market"]
    symbol = parsed["symbol"]

    try:
        shares_raw = data.get("shares")
        avg_price_raw = data.get("avg_price")
        avg_fx_rate_raw = data.get("avg_fx_rate")
        if shares_raw is None or str(shares_raw).strip() == "":
            return error_response(ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["shares"]})
        if avg_price_raw is None or str(avg_price_raw).strip() == "":
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["avg_price"]}
            )

        shares = parse_non_negative_float(shares_raw, "shares", max_value=PORTFOLIO_SHARES_MAX)
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
            return error_response(ErrorCode.INVALID_INPUT, details={"reason": portfolio_errors[0]})
    except ValueError as exc:
        return error_response(ErrorCode.INVALID_INPUT, details={"reason": str(exc)})

    with app_state.market.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)

        previous_value = copy.deepcopy(container.get(symbol))
        # MNS-003: a portfolio update must target an already-tracked symbol.
        # Creating an entry for an unregistered symbol would persist an orphan
        # holding that never appears in the watch list / SSE and cannot be
        # managed through the normal UI flow. Require the symbol to exist first.
        if symbol not in container:
            current_app.logger.warning(
                "Portfolio update rejected: symbol %s not in %s watch list", symbol, market
            )
            return error_response(
                ErrorCode.SYMBOL_NOT_FOUND,
                details={"reason": "symbol not in watch list; add it before setting holdings"},
                status_code=404,
            )
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

        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            if previous_value is None:
                container.pop(symbol, None)
            else:
                container[symbol] = previous_value
            current_app.logger.error("Failed to persist portfolio update for %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "ポートフォリオの保存に失敗しました。再試行してください。"},
                status_code=503,
            )

        # Hold user_stocks_lock across the SSE cache patch so a concurrent
        # background sync cannot interleave between the persisted write and
        # the in-memory cache update (which would briefly publish stale
        # shares/avg_price over SSE). save_user_stocks() already acquires this
        # RLock, so this nesting is reentrant and deadlock-free.
        invalidate_stock_caches(symbol)

        # フロントエンドの fetchInitialStocks や SSE に即座に反映させるため両方のキャッシュを更新する
        ensure_stock_placeholder_in_caches(
            symbol, _stock_display_name(symbol, market), market
        )
        with app_state.cache.sse_data_lock:
            for cache in (
                app_state.market.current_stocks_cache,
                app_state.market.target_stocks_cache,
            ):
                if market not in cache:
                    cache[market] = []
                target_list = cache.get(market, [])
                for s in target_list:
                    if s.get("symbol") == symbol:
                        s["shares"] = shares
                        s["avg_price"] = avg_price
                        if avg_fx_rate is not None:
                            s["avg_fx_rate"] = avg_fx_rate
                        else:
                            s.pop("avg_fx_rate", None)
                        break
    from app_bg import announce_current_market_state

    announce_current_market_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/stocks/portfolio/snapshot", methods=["POST"])
@rate_limit(max_requests=30, window_seconds=60)
def api_portfolio_snapshot():
    """Return holdings only to the trusted local UI.

    Public market-data endpoints and SSE intentionally omit holdings. Keeping a
    separate CSRF-protected endpoint prevents a local unauthenticated process
    from recovering portfolio data while allowing a page reload to restore it.
    """
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )
    with app_state.cache.sse_data_lock:
        stocks = _resolve_stocks_for_response(include_portfolio=True)
    return jsonify({"stocks": stocks})


@api_stocks_bp.route("/api/stocks/add_ext", methods=["POST", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_add_stock_ext():
    """拡張機能用銘柄追加APIエンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Validate raw socket IP to protect against proxy-override headers spoofing
    raw_remote = request.environ.get("RAW_REMOTE_ADDR") or request.environ.get("REMOTE_ADDR", "")
    raw_remote = str(raw_remote).strip()
    from utils.networking import _is_loopback_ip

    if raw_remote and not _is_loopback_ip(raw_remote):
        current_app.logger.warning(
            "Add-ext request rejected: WSGI REMOTE_ADDR %s is not loopback", raw_remote
        )
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Security model (defense-in-depth):
    # 1. WSGI REMOTE_ADDR must be loopback (enforced earlier in this handler).
    # 2. Bearer extension API token (constant-time compare) must match the
    #    server-managed token from get_or_create_extension_api_token().
    # 3. Origin MUST be present and pass _is_allowed_shutdown_origin() (allow-list).
    #    Missing Origin is rejected so a leaked extension token cannot be replayed
    #    from an arbitrary local process without a trusted extension/browser origin.
    # The extension token is the sole authenticator; there is no CSRF token
    # here because the trusted-origin + loopback checks already block
    # cross-origin/cross-host abuse, and the endpoint is CSRF-exempt by design.
    auth_header = request.headers.get("Authorization")
    expected_token = get_or_create_extension_api_token()

    from utils.networking import _is_allowed_shutdown_origin

    if not _is_allowed_shutdown_origin(request):
        current_app.logger.warning(
            "api_add_stock_ext: missing or untrusted origin id=%s remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "untrusted origin"}, status_code=403
        )

    is_valid_token = False
    if auth_header and auth_header.startswith("Bearer "):
        import secrets

        token = auth_header.removeprefix("Bearer ").strip()
        is_valid_token = secrets.compare_digest(token, expected_token)

    if not is_valid_token:
        current_app.logger.warning(
            "api_add_stock_ext: security rejection id=%s remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"reason": "invalid or missing extension token"},
            status_code=403,
        )

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
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"}
        )
    market = parsed["market"]
    symbol = parsed["symbol"]

    name = parsed["name"] or symbol
    with app_state.market.user_stocks_lock:
        if _stock_is_default_or_user(symbol, market):
            return jsonify({"ok": True, "message": f"{symbol} already exists in {market}"})

        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        container[symbol] = name

        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            container.pop(symbol, None)
            current_app.logger.error("Failed to persist extension-added stock %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
        invalidate_stock_caches(symbol)
        ensure_stock_placeholder_in_caches(symbol, name, market)

        from app_bg import announce_current_market_state

        announce_current_market_state()
        _sync_realtime_symbol(symbol, market, register=True)
        schedule_sync_all_stocks_now()
        return jsonify({"ok": True, "message": f"Added {symbol} to {market}"})


@api_stocks_bp.route("/api/stocks/reset", methods=["POST"])
@rate_limit(max_requests=5, window_seconds=60)
def api_reset_stocks():
    """銘柄リセットAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )

    with app_state.market.user_stocks_lock:
        previous_us = app_state.market.user_us
        previous_jp = app_state.market.user_jp
        previous_idx = app_state.market.user_idx
        app_state.market.user_us, app_state.market.user_jp, app_state.market.user_idx = {}, {}, {}
        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            (
                app_state.market.user_us,
                app_state.market.user_jp,
                app_state.market.user_idx,
            ) = previous_us, previous_jp, previous_idx
            current_app.logger.error("Failed to persist stock reset: %s", exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.current_indices_cache = {}
            app_state.market.target_indices_cache = {}
    try:
        app_state.payload_disk_cache.delete("indices_cache")
    except Exception as exc:
        current_app.logger.debug("Failed to delete indices_cache from disk cache: %s", exc)
    clear_cache_prefix("stocks")
    from app_bg import announce_current_market_state

    announce_current_market_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@api_stocks_bp.route("/api/heatmap")
@rate_limit(max_requests=30, window_seconds=60)
def api_heatmap():
    """ヒートマップデータAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)
    market = normalize_market(request.args.get("market"), default="us")
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if market not in ("us", "jp"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "heatmap market は us/jp のみ対応です"},
        )
    symbols = POPULAR_US if market == "us" else POPULAR_JP

    cache_key = f"heatmap_{market}"

    # キャッシュがあれば即座に返す（バックグラウンドで更新される）
    if _has_cached_key(cache_key, CACHE_DURATION_HEATMAP):
        cached = _get_cached_value(cache_key, CACHE_DURATION_HEATMAP)
        if cached:
            return jsonify(cached)

    # SWR (Stale-While-Revalidate): ディスクキャッシュがあれば即座に引き出して返却対象に保存
    disk_cached = None
    try:
        disk_cached = app_state.payload_disk_cache.get(cache_key, ignore_ttl=True)
    except Exception as exc:
        logger.debug("Failed to read heatmap disk cache: %s", exc)

    # キャッシュミス時: リクエストスレッドで同期的に yfinance を呼ぶと最大数十秒
    # ワーカーが固まり、429 バーストの原因になる。バックグラウンドexecutorへオフロードし、
    # キャッシュができるまで (ディスクキャッシュが無ければ) fetching:True を返す。
    with app_state.heatmap_fetch_lock:
        now = time.time()
        if cache_key in app_state.heatmap_fetch_inflight:
            start_time = _HEATMAP_FETCH_START_TIMES.get(cache_key, 0.0)
            if now - start_time > 30.0:
                app_state.heatmap_fetch_inflight.discard(cache_key)
                _HEATMAP_FETCH_START_TIMES.pop(cache_key, None)

        already_fetching = cache_key in app_state.heatmap_fetch_inflight
        if not already_fetching:
            app_state.heatmap_fetch_inflight.add(cache_key)
            _HEATMAP_FETCH_START_TIMES[cache_key] = now

    if not already_fetching:
        try:
            # Route market-data work to data_executor (H3).
            app_state.execution.data_executor.submit(
                _fetch_heatmap_cached, cache_key, market, symbols
            )
        except queue.Full:
            with app_state.heatmap_fetch_lock:
                app_state.heatmap_fetch_inflight.discard(cache_key)
            current_app.logger.warning("Heatmap fetch queue is full market=%s", market)
            if not disk_cached:
                return error_response(
                    ErrorCode.TOO_MANY_REQUESTS,
                    details={
                        "reason": "ヒートマップ取得の処理容量を超えました。しばらくしてから再試行してください。"
                    },
                    status_code=503,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            with app_state.heatmap_fetch_lock:
                app_state.heatmap_fetch_inflight.discard(cache_key)
            logger.warning("Failed to submit heatmap fetch for %s: %s", market, exc)

    if disk_cached and isinstance(disk_cached, dict) and disk_cached.get("stocks"):
        return jsonify(disk_cached)

    return jsonify(
        {
            "stocks": [],
            "fetching": True,
            "message": "ヒートマップデータを取得中です。しばらくしてから再読み込みしてください。",
        }
    )


_SSE_TICKETS: dict[str, float] = {}
_SSE_TICKET_TTL_SEC = 120.0
_SSE_TICKETS_LOCK = threading.Lock()


@api_stocks_bp.route("/api/stocks/stream/ticket", methods=["POST"])
@rate_limit(max_requests=30, window_seconds=60)
def api_create_sse_ticket():
    """Issue a short-lived SSE connection ticket for browser clients.

    ``EventSource`` cannot set custom headers, so long-lived bearer tokens must
    not travel in the URL. A CSRF-protected POST can issue a one-time ticket
    that the subsequent GET-based SSE connection presents instead.
    """
    ok, reason = require_trusted_or_admin(request, require_origin=False, allow_query_token=False)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    ticket = secrets.token_urlsafe(24)
    now = time.time()
    with _SSE_TICKETS_LOCK:
        _SSE_TICKETS[ticket] = now + _SSE_TICKET_TTL_SEC
    return jsonify({"ok": True, "ticket": ticket, "expires_in": _SSE_TICKET_TTL_SEC})


@api_stocks_bp.route("/api/stocks/stream", methods=["GET"])
@rate_limit(max_requests=10, window_seconds=60)
def api_stocks_stream():
    """SSEストリームエンドポイント（接続数・モード切替対応）

    ``EventSource`` はカスタムヘッダーを送れないため、SSE専用の短寿命チケットで
    認証する。長期の管理者トークンはURLクエリへ載せない。
    """
    ok, reason = require_trusted_or_admin(request, require_origin=False, allow_query_token=False)
    if not ok:
        return jsonify({"error": reason}), 403

    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    if admin_token:
        provided_header = (request.headers.get("X-MNS-Admin-Token") or "").strip()
        provided_ticket = (
            request.args.get("sse_ticket")
            or request.args.get("ticket")
            or ""
        ).strip()
        if not provided_header and not provided_ticket:
            return jsonify({"error": "SSE requires admin header or ticket"}), 403

        if provided_ticket and not provided_header:
            now = time.time()
            with _SSE_TICKETS_LOCK:
                expires_at = _SSE_TICKETS.pop(provided_ticket, None)
            if expires_at is None or now > expires_at:
                return jsonify({"error": "invalid or expired SSE ticket"}), 403

    request_id = getattr(g, "request_id", "-")

    # Mode parameter evaluation: 0 = disabled, 1 = complementary, 2 = tradingview_realtime
    raw_mode = str(request.args.get("mode", "2")).strip().lower()
    if raw_mode in ("0", "disabled", "off"):
        return jsonify({"status": "disabled", "sse_mode": 0, "message": "SSE streaming disabled by client"}), 200

    sse_mode = 2 if raw_mode in ("2", "tradingview", "tradingview_realtime") else 1

    from constants import MAX_SSE_LISTENERS

    if app_state.sse_announcer.listener_count() >= MAX_SSE_LISTENERS:
        current_app.logger.warning("SSE listener limit exceeded id=%s", request_id)
        return error_response(
            ErrorCode.TOO_MANY_REQUESTS,
            status_code=429,
            details={"reason": "too many SSE connections"},
        )

    def stream():
        # Use a context manager explicitly so the listener queue is always
        # released, even if this generator is closed via GeneratorExit (client
        # disconnect) or garbage-collected without an explicit close.
        try:
            with app_state.sse_announcer.listener_context() as q:
                # Realtime deltas are consumed per-connection (mode 2 only): a
                # dedicated engine cursor guarantees every connected client
                # receives each price change rather than only whichever one
                # polls first.
                rt_client_id = None
                if sse_mode == 2:
                    from services.realtime_engine import realtime_market_engine

                    rt_client_id = realtime_market_engine.register_client()
                try:
                    sse_event_id = 0

                    from utils.market_utils import is_market_open
                    from utils.tradingview_mapper import (
                        get_tradingview_symbol,
                        get_tradingview_ticker_tape_symbols,
                    )

                    current_app.logger.info("SSE Stream client connected id=%s (mode=%d)", request_id, sse_mode)
                    stocks_payload = _resolve_stocks_for_response(include_portfolio=False)
                    stocks_payload.pop("idx", None)
                    for market in ("us", "jp"):
                        if market in stocks_payload and isinstance(stocks_payload[market], list):
                            for s in stocks_payload[market]:
                                if isinstance(s, dict) and "symbol" in s:
                                    s["tv_symbol"] = s.get("tv_symbol") or get_tradingview_symbol(
                                        s["symbol"], exchange=s.get("exchange")
                                    )

                    indices_payload = _resolve_indices_for_response()
                    all_stocks_list = stocks_payload.get("us", []) + stocks_payload.get("jp", [])
                    tv_ticker_tape = get_tradingview_ticker_tape_symbols(
                        indices=indices_payload,
                        stocks=all_stocks_list,
                    )

                    with app_state.cache.sse_data_lock:
                        initial_payload = json.dumps(
                            {
                                "stream_event": "initial_snapshot",
                                "sse_mode": sse_mode,
                                "stocks": stocks_payload,
                                "indices": indices_payload,
                                "tv_ticker_tape": tv_ticker_tape,
                                "is_us_market_open": is_market_open("us"),
                                "is_jp_market_open": is_market_open("jp"),
                            },
                            allow_nan=False,
                        )
                    sse_event_id += 1
                    yield f"id: {sse_event_id}\ndata: {initial_payload}\n\n"

                    # 15秒ハートビート（クライアント側でタイムアウト検出用）
                    heartbeat_interval = SSE_HEARTBEAT_INTERVAL
                    last_heartbeat_time = time.time()

                    while True:
                        try:
                            # Use a short timeout of 2.0s to detect disconnects quickly.
                            # This prevents thread starvation by releasing resources when the client disconnects.
                            msg = q.get(timeout=SSE_GET_TIMEOUT)
                            if msg is None:
                                current_app.logger.info(
                                    "SSE listener dropped due to backpressure id=%s", request_id
                                )
                                break
                            sse_event_id += 1
                            yield f"id: {sse_event_id}\n{msg}"
                        except queue.Empty:
                            now = time.time()
                            # Check realtime engine deltas (TradingView WS / Yahoo JP)
                            # Enabled ONLY when sse_mode == 2 (TradingView Realtime Mode)
                            if sse_mode == 2:
                                try:
                                    deltas = realtime_market_engine.get_market_deltas(rt_client_id)
                                    if deltas:
                                        sse_event_id += 1
                                        current_app.logger.debug(
                                            "SSE sending realtime_update to client id=%s with %d symbol(s): %s",
                                            request_id,
                                            len(deltas),
                                            list(deltas.keys()),
                                        )
                                        delta_data = json.dumps({"stream_event": "realtime_update", "deltas": deltas})
                                        yield f"id: {sse_event_id}\nevent: realtime_update\ndata: {delta_data}\n\n"
                                    # PTS (after-hours) quote deltas: Yahoo JP first,
                                    # SBI fallback — dispatched as a separate event so
                                    # the regular session price is never overwritten.
                                    pts_deltas = realtime_market_engine.get_pts_deltas(rt_client_id)
                                    if pts_deltas:
                                        sse_event_id += 1
                                        pts_data = json.dumps({"stream_event": "pts_update", "deltas": pts_deltas})
                                        yield f"id: {sse_event_id}\nevent: pts_update\ndata: {pts_data}\n\n"
                                except Exception as e:
                                    current_app.logger.debug("Failed fetching realtime engine deltas: %s", e)

                            if now - last_heartbeat_time >= heartbeat_interval:
                                # 15秒間何もデータが来なかった場合、ハートビート送信
                                heartbeat_data = json.dumps({"type": "heartbeat", "timestamp": now})
                                sse_event_id += 1
                                yield f"id: {sse_event_id}\nevent: heartbeat\ndata: {heartbeat_data}\n\n"
                                last_heartbeat_time = now
                            else:
                                # Otherwise yield a lightweight keep-alive comment to probe socket health
                                yield ": keepalive\n\n"
                finally:
                    if rt_client_id is not None:
                        try:
                            realtime_market_engine.unregister_client(rt_client_id)
                        except Exception:
                            current_app.logger.debug(
                                "Failed to unregister realtime client id=%s", rt_client_id
                            )
        except GeneratorExit:
            raise
        except RuntimeError as exc:
            if (
                "too many" in str(exc).lower()
                or "limit" in str(exc).lower()
                or app_state.sse_announcer.listener_count() >= MAX_SSE_LISTENERS
            ):
                current_app.logger.warning(
                    "SSE listener limit exceeded concurrently id=%s: %s", request_id, exc
                )
                err_data = json.dumps({"error": "too many SSE connections"})
                yield f"event: error\ndata: {err_data}\n\n"
                return
            current_app.logger.exception("SSE stream error id=%s", request_id)
            try:
                err_data = json.dumps({"error": "stream error"})
                yield f"event: error\ndata: {err_data}\n\n"
            except Exception:  # nosec B110
                pass
        except Exception:
            current_app.logger.exception("SSE stream error id=%s", request_id)
            try:
                err_data = json.dumps({"error": "stream error"})
                yield f"event: error\ndata: {err_data}\n\n"
            except Exception:  # nosec B110
                pass

    response = Response(stream_with_context(stream()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
