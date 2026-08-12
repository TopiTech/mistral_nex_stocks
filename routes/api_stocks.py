import copy
import hashlib
import json
import logging
import math
import queue
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

import requests
from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from app_bg import (
    fetch_stocks_batch,
    schedule_sync_all_stocks_now,
)
from app_state import app_state
from constants import (  # noqa: F401
    AI_PORTFOLIO_MARKETS,
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
    SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC,
    VALID_HISTORY_INTERVALS,
    VALID_HISTORY_PERIODS,
)
from credential_manager import get_or_create_extension_api_token
from error_codes import ErrorCode, get_error_message
from messaging import sse_event_log
from route_helpers import (
    _parse_stock_request,
    _stock_display_name,
    ensure_stock_placeholder_in_caches,
    extract_api_key,
    invalidate_stock_caches,
    rate_limit,
    remove_stock_from_caches,
)
from services.ai_portfolio_service import (
    DEFAULT_PRESET_CONFIGS,
    VIRTUAL_INITIAL_CAPITAL_JPY,
    delete_custom_ai_portfolio,
    generate_ai_portfolio_by_theme,
    load_saved_ai_portfolios,
    sanitize_ai_portfolio,
    save_custom_ai_portfolio,
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
    SSE_TICKET_TTL_SEC,
    SseTicketSessionUnavailable,
    _is_local_request,
    create_sse_ticket,
    require_sse_auth,
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

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats (NaN/±Inf) with None.

    SSE payloads are serialized with ``json.dumps(..., allow_nan=False)`` so a
    single NaN from an upstream source would raise ``ValueError`` and kill the
    whole stream (or, without that flag, emit an invalid ``NaN`` token that
    browsers reject on ``JSON.parse``). Normalizing at the SSE boundary keeps
    the stream alive and the JSON standards-compliant.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _parse_last_event_id() -> int:
    """Parse the client's last-seen SSE event id (query param or standard header).

    The client sends ``?last_event_id=N`` on every (re)connect (EventSource
    cannot set custom headers, and each reconnect mints a fresh ticket URL).
    The standard ``Last-Event-ID`` header is also accepted for native
    EventSource auto-reconnect scenarios.
    """
    raw = request.headers.get("Last-Event-ID") or request.args.get("last_event_id")
    if not raw:
        return 0
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _replay_frame_for_entry(seq: int, kind: str, payload: Any, sse_mode: int) -> str | None:
    """Rebuild an SSE frame for a buffered event during Last-Event-ID replay.

    - ``frame`` entries (any mode) are replayed verbatim: the payload is the
      original ``data: ...`` frame.
    - mode-2 ``delta`` / ``pts_delta`` entries store only the delta's symbol
      keys; the current engine values are resolved at replay time (state-based
      replay, so a stale cursor never resurrects an outdated quote).
    """
    if kind == "frame":
        return f"id: {seq}\n{payload}"
    if sse_mode != 2 or kind not in ("delta", "pts_delta"):
        return None
    from services.realtime_engine import realtime_market_engine

    if kind == "delta":
        store = realtime_market_engine.market_store
        event_name = "realtime_update"
    else:
        store = realtime_market_engine.pts_store
        event_name = "pts_update"
    resolved: dict[str, Any] = {}
    with realtime_market_engine.store_lock:
        for sym in payload:
            cur = store.get(sym)
            if cur is not None:
                resolved[sym] = cur
    if not resolved:
        return None
    data = json.dumps(
        _json_safe({"stream_event": event_name, "deltas": resolved}),
        allow_nan=False,
    )
    return f"id: {seq}\nevent: {event_name}\ndata: {data}\n\n"


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


def _announce_watchlist_state() -> None:
    """Notify both SSE modes after a watchlist membership mutation."""
    from app_bg import announce_current_market_state, announce_real_market_state

    announce_current_market_state()
    # Mode 2 intentionally does not receive Mode 1's interpolated ticks, but
    # it still needs the authoritative target snapshot for add/delete/reset
    # mutations so reconnects cannot preserve a removed card.
    announce_real_market_state()


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
            app_state.heatmap_fetch_start_times.pop(cache_key, None)


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
    # R8: reject invalid interval values explicitly instead of silently
    # coercing to "auto". The previous fallback hid client bugs and
    # produced inconsistent behaviour (period returned 400, interval did not).
    if interval not in VALID_HISTORY_INTERVALS:
        return error_response(
            ErrorCode.INVALID_INTERVAL,
            details={"allowed": sorted(VALID_HISTORY_INTERVALS)},
        )
    symbol = normalize_symbol_for_market(symbol, market)
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    # 0. サーキットブレーカーの状態をチェック (Fail-Fast & HALF-OPEN 同期実行)
    # Key the circuit by ``market:symbol`` so the same symbol string queried
    # under different markets does not share fail-fast state (collision class).
    circuit_key = f"{market}:{symbol}"
    is_open = app_state.market.is_circuit_open("yfinance_history", symbol=circuit_key)

    # Symbol-first ordering keeps the ``hist_{symbol}`` prefix invalidation used
    # by route_helpers.invalidate_* working while still separating markets so a
    # symbol string queried under ``us`` and ``jp`` does not share a cache entry
    # (or a disk-cache fallback serving the other market's payload).
    cache_key = (
        f"hist_{symbol}_{market}_{period}_{interval}"
        if interval != "auto"
        else f"hist_{symbol}_{market}_{period}"
    )

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

    # R4 fix: hold history_circuit_lock across both the HALF_OPEN check and the
    # async fetch submission so concurrent requests cannot all enter the
    # HALF_OPEN branch and submit duplicate fetches for the same symbol.
    with app_state.market.history_circuit_lock:
        state: Any = app_state.market.history_circuit_state.get(circuit_key, {})
        if state.get("status") == "HALF_OPEN":
            logger.info("stock-history circuit HALF_OPEN symbol=%s - scheduling async fetch", circuit_key)
            try:
                _submit_async_history_fetch(
                    cache_key, symbol, market, period, duration, "HALF_OPEN", interval=interval
                )
            except queue.Full:
                logger.warning("History fetch queue full during HALF_OPEN symbol=%s", circuit_key)
            return make_history_response(FETCHING_RESPONSE, is_cacheable=False)

    if is_open:
        logger.info("stock-history circuit open symbol=%s - failing fast", circuit_key)
        return error_response(ErrorCode.CIRCUIT_BREAKER_OPEN, status_code=503)

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

    result = get_cached(
        f"search_{q}",
        _search,
        duration=CACHE_DURATION_SEARCH,
        # Never cache upstream error payloads as success: a failed search would
        # otherwise keep returning {"error": ...} (no results) for the whole TTL.
        valid_func=lambda payload: bool(
            isinstance(payload, dict) and isinstance(payload.get("results"), list)
        ),
    )
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

    # Reject invalid filter values instead of silently returning "no stocks
    # match" (200) or falling back to a default sort for a typo'd parameter.
    if market_filter not in ("all", "us", "jp"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "market は all/us/jp のいずれかを指定してください"},
        )
    if sort_by not in ("market_cap", "price", "change_percent", "volume", "symbol"):
        return error_response(ErrorCode.INVALID_INPUT, details={"reason": "sort_by の値が不正です"})
    if sort_order not in ("asc", "desc"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "sort_order は asc/desc のいずれかを指定してください"},
        )

    def _parse_float(val):
        if val is None or str(val).strip() == "":
            return None
        try:
            res = float(val)
            return res if math.isfinite(res) else None
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
        # request. The per-request symbol set (pop_unseen_items) is recomputed
        # against the live watchlist, so the cache key MUST include that exact
        # symbol set: with a key of only market+q, a watchlist change within the
        # TTL would serve a stale payload that lacks rows for symbols which left
        # the watchlist (the merge loop only looks up current symbols).
        _enrich_symbols = ",".join(sorted({sym for sym, _n, _m in pop_unseen_items}))
        enrich_key = (
            f"screener_enrich_{market_filter}_{q}_"
            f"{hashlib.sha256(_enrich_symbols.encode('utf-8')).hexdigest()}"
        )
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
        if min_price is not None and item["price"] < min_price:
            continue
        if max_price is not None and (item["price"] > max_price or item["price"] <= 0):
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
    reverse = sort_order != "asc"
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

    return jsonify(
        {
            "ok": True,
            "total": len(filtered),
            "stocks": filtered[:150],
        }
    )


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

    _announce_watchlist_state()
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

    _announce_watchlist_state()
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

            if market == "jp":
                # Japanese domestic stocks are JPY-denominated; avg_fx_rate is not applicable
                val.pop("avg_fx_rate", None)
            elif avg_fx_rate is not None:
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
        ensure_stock_placeholder_in_caches(symbol, _stock_display_name(symbol, market), market)
        with app_state.cache.sse_data_lock:
            for cache in (
                app_state.market.target_stocks_cache,
                app_state.market.current_stocks_cache,
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
    """Return holdings to the CSRF-protected local UI.

    Public market-data endpoints and SSE intentionally omit holdings. Keeping a
    separate endpoint prevents cross-site browser reads while allowing a page
    reload to restore them. Loopback + Origin + CSRF is not authentication
    against native processes on the same host; see SECURITY.md.
    """
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )
    # The resolver snapshots holdings before taking the SSE cache lock. Do not
    # wrap it in an outer SSE lock here or the lock order becomes SSE -> user,
    # opposite to portfolio mutation paths (user -> SSE).
    stocks = _resolve_stocks_for_response(include_portfolio=True)
    return jsonify({"stocks": stocks})


@api_stocks_bp.route("/api/stocks/add_ext", methods=["POST", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_add_stock_ext():
    """拡張機能用銘柄追加APIエンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    # R4: Disable this endpoint in remote/reverse-proxy mode. The
    # check-the-token + trusted-origin model is designed for same-user /
    # same-machine use; behind a reverse proxy ``_is_local_request`` returns
    # True for every proxy-supplied peer address, which would let any
    # remote caller present a leaked extension token + a spoofed
    # ``chrome-extension://<extid>`` Origin and add stocks. The native host
    # can still start the backend locally for the extension.
    from utils.env_helpers import _is_remote_api_enabled
    if _is_remote_api_enabled():
        current_app.logger.warning(
            "api_add_stock_ext rejected: not available in remote API mode id=%s",
            getattr(g, "request_id", "-"),
        )
        return jsonify(
            {"ok": False, "error": "forbidden in remote API mode"}
        ), 403

    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # The browser extension marks its requests with X-MNS-Extension-Request: true
    # (background.js). Enforce it here so a locally reachable non-extension client
    # (e.g. a bookmarklet or a page on another localhost port) cannot use a
    # leaked extension token without also proving it is the extension's own
    # request path.
    if request.headers.get("X-MNS-Extension-Request", "").strip().lower() != "true":
        current_app.logger.warning(
            "api_add_stock_ext: missing extension marker header id=%s",
            getattr(g, "request_id", "-"),
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"reason": "invalid or missing extension marker"},
            status_code=403,
        )

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

    name = parsed["name"] or _stock_display_name(symbol, market)
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

        _announce_watchlist_state()
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
    for market, previous in (
        ("us", previous_us),
        ("jp", previous_jp),
        ("idx", previous_idx),
    ):
        for symbol in previous:
            _sync_realtime_symbol(symbol, market, register=False)
    try:
        app_state.payload_disk_cache.delete("indices_cache")
    except Exception as exc:
        current_app.logger.debug("Failed to delete indices_cache from disk cache: %s", exc)
    clear_cache_prefix("stocks")
    _announce_watchlist_state()
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
            start_time = app_state.heatmap_fetch_start_times.get(cache_key, 0.0)
            if now - start_time > 30.0:
                app_state.heatmap_fetch_inflight.discard(cache_key)
                app_state.heatmap_fetch_start_times.pop(cache_key, None)

        already_fetching = cache_key in app_state.heatmap_fetch_inflight
        if not already_fetching:
            app_state.heatmap_fetch_inflight.add(cache_key)
            app_state.heatmap_fetch_start_times[cache_key] = now

    if not already_fetching:
        try:
            # Route market-data work to data_executor (H3).
            app_state.execution.data_executor.submit(
                _fetch_heatmap_cached, cache_key, market, symbols
            )
        except queue.Full:
            with app_state.heatmap_fetch_lock:
                app_state.heatmap_fetch_inflight.discard(cache_key)
                app_state.heatmap_fetch_start_times.pop(cache_key, None)
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
                app_state.heatmap_fetch_start_times.pop(cache_key, None)
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


@api_stocks_bp.route("/api/stocks/stream/ticket", methods=["POST"])
@rate_limit(max_requests=30, window_seconds=60)
def api_create_sse_ticket():
    """Issue a short-lived SSE connection ticket for browser clients.

    ``EventSource`` cannot set custom headers, so long-lived bearer tokens must
    not travel in the URL. A CSRF-protected POST can issue a one-time ticket
    that the subsequent GET-based SSE connection presents instead.

    The ticket is returned in the response body AND set as a SameSite=Strict
    HttpOnly cookie so the frontend can avoid placing the ticket in the URL
    (which would expose it via Referer, browser history, and proxy logs).
    """
    # R7: the SSE ticket endpoint is a CSRF-protected POST that the
    # browser calls before opening the GET /api/stocks/stream connection.
    # It uses the standard admin/origin gate (no URL-borne token support).
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    try:
        ticket = create_sse_ticket(request)
    except SseTicketSessionUnavailable as exc:
        # R1: no browser session backs this request, so a ticket would be
        # bound to the shared peer address and redeemable by any other client
        # on the same host. Non-browser callers use the admin-token header.
        current_app.logger.warning("Refused to issue SSE ticket without a session: %s", exc)
        return jsonify({"ok": False, "error": "session required for SSE ticket"}), 403

    resp = jsonify({"ok": True, "ticket": ticket, "expires_in": SSE_TICKET_TTL_SEC})
    resp.set_cookie(
        "sse_ticket",
        ticket,
        max_age=int(SSE_TICKET_TTL_SEC),
        httponly=True,
        samesite="Strict",
        path="/api/stocks/stream",
    )
    return resp


@api_stocks_bp.route("/api/stocks/stream", methods=["GET"])
# 30/60s instead of the default 10/60s: EventSource clients reconnect with
# exponential backoff after any hiccup, and each reconnect consumes a slot.
# A tighter limit would turn a brief network blip into a 429 reconnect storm.
@rate_limit(max_requests=30, window_seconds=60)
def api_stocks_stream():
    """SSEストリームエンドポイント（接続数・モード切替対応）

    ``EventSource`` はカスタムヘッダーを送れないため、SSE専用の短寿命チケットで
    認証する。長期の管理者トークンはURLクエリへ載せない。
    """
    ok, reason = require_sse_auth(request)
    if not ok:
        return jsonify({"error": reason}), 403



    request_id = getattr(g, "request_id", "-")

    # Mode parameter evaluation: 0 = disabled, 1 = complementary, 2 = tradingview_realtime
    raw_mode = str(request.args.get("mode", "2")).strip().lower()
    if raw_mode in ("0", "disabled", "off"):
        return jsonify(
            {"status": "disabled", "sse_mode": 0, "message": "SSE streaming disabled by client"}
        ), 200

    sse_mode = 2 if raw_mode in ("2", "tradingview", "tradingview_realtime") else 1

    from constants import MAX_SSE_LISTENERS

    total_listeners = (
        app_state.sse_announcer_mode1.listener_count()
        + app_state.sse_announcer_mode2.listener_count()
    )
    if total_listeners >= MAX_SSE_LISTENERS:
        current_app.logger.warning("SSE listener limit exceeded id=%s", request_id)
        return error_response(
            ErrorCode.TOO_MANY_REQUESTS,
            status_code=429,
            details={"reason": "too many SSE connections"},
        )

    announcer = app_state.sse_announcer_mode2 if sse_mode == 2 else app_state.sse_announcer_mode1

    def stream():
        # Use a context manager explicitly so the listener queue is always
        # released, even if this generator is closed via GeneratorExit (client
        # disconnect) or garbage-collected without an explicit close.
        try:
            with announcer.listener_context() as q:
                # Realtime deltas are consumed per-connection (mode 2 only): a
                # dedicated engine cursor guarantees every connected client
                # receives each price change rather than only whichever one
                # polls first.
                rt_ctx: Any
                if sse_mode == 2:
                    from services.realtime_engine import realtime_market_engine

                    rt_ctx = realtime_market_engine.client_context()
                else:
                    rt_ctx = nullcontext(None)

                with rt_ctx as rt_client_id:
                    from utils.market_utils import is_market_open
                    from utils.tradingview_mapper import (
                        get_tradingview_symbol,
                        get_tradingview_ticker_tape_symbols,
                    )

                    current_app.logger.info(
                        "SSE Stream client connected id=%s (mode=%d)", request_id, sse_mode
                    )

                    # ---- Last-Event-ID replay (resume) -------------------------
                    # The client passes the id of the last event it processed via
                    # ``?last_event_id=N`` (EventSource cannot send headers, and
                    # every reconnect mints a fresh ticket URL). The sliding event
                    # log replays missed events so a reconnect whose gap is still
                    # covered does not need a full initial snapshot.
                    last_event_id = _parse_last_event_id()
                    replay_entries = None
                    replayed_frames_count = 0
                    replay_requires_initial = False
                    if last_event_id > 0:
                        # A cursor may have come from an older implementation
                        # that emitted an id without recording it. Treat that
                        # case as a gap instead of silently resuming with no
                        # authoritative state.
                        replay_requires_initial = not sse_event_log.contains(
                            last_event_id, sse_mode
                        )
                        replay_entries = sse_event_log.replay_after(last_event_id, sse_mode)
                        if replay_entries is not None:
                            for seq, kind, payload in replay_entries:
                                frame = _replay_frame_for_entry(seq, kind, payload, sse_mode)
                                if frame is not None:
                                    yield frame
                                    replayed_frames_count += 1
                                else:
                                    # Legacy state-based delta entries can no
                                    # longer be reconstructed after a symbol
                                    # was purged. A partial replay is unsafe;
                                    # send a complete snapshot instead.
                                    replay_requires_initial = True
                            current_app.logger.info(
                                "SSE Stream replayed %d event(s) (%d frame(s)) id=%s (mode=%d, last_event_id=%s)",
                                len(replay_entries),
                                replayed_frames_count,
                                request_id,
                                sse_mode,
                                last_event_id,
                            )
                    # ``None`` means the buffer no longer covers the gap → full
                    # initial snapshot below; ``[]``/entries mean resume live.
                    # If replay_entries was non-empty but produced 0 valid frames
                    # (e.g. all symbols were deleted), emit a snapshot to avoid stall.
                    send_initial = (
                        last_event_id <= 0
                        or replay_entries is None
                        or replay_requires_initial
                        or (len(replay_entries) > 0 and replayed_frames_count == 0)
                    )

                    if send_initial:
                        stocks_payload = _resolve_stocks_for_response(
                            include_portfolio=False, real_data_only=(sse_mode == 2)
                        )
                        for market in ("us", "jp", "idx"):
                            if market in stocks_payload and isinstance(
                                stocks_payload[market], list
                            ):
                                for s in stocks_payload[market]:
                                    if isinstance(s, dict) and "symbol" in s:
                                        s["tv_symbol"] = s.get(
                                            "tv_symbol"
                                        ) or get_tradingview_symbol(
                                            s["symbol"], exchange=s.get("exchange")
                                        )

                        indices_payload = _resolve_indices_for_response()
                        all_stocks_list = stocks_payload.get("us", []) + stocks_payload.get(
                            "jp", []
                        )
                        tv_ticker_tape = get_tradingview_ticker_tape_symbols(
                            indices=indices_payload,
                            stocks=all_stocks_list,
                        )

                        with app_state.cache.sse_data_lock:
                            initial_payload = json.dumps(
                                _json_safe(
                                    {
                                        "stream_event": "initial_snapshot",
                                        "sse_mode": sse_mode,
                                        "stocks": stocks_payload,
                                        "indices": indices_payload,
                                        "tv_ticker_tape": tv_ticker_tape,
                                        "is_us_market_open": is_market_open("us"),
                                        "is_jp_market_open": is_market_open("jp"),
                                    }
                                ),
                                allow_nan=False,
                            )
                        initial_frame = f"retry: 3000\ndata: {initial_payload}\n\n"
                        initial_seq = sse_event_log.next_id()
                        sse_event_log.record(initial_seq, sse_mode, "frame", initial_frame)
                        yield f"id: {initial_seq}\n{initial_frame}"
                        # Purge stale deltas queued before initial_snapshot was generated to prevent
                        # sending redundant/duplicate updates to the newly connected client.
                        if sse_mode == 2:
                            try:
                                realtime_market_engine.get_market_deltas(rt_client_id)
                                realtime_market_engine.get_pts_deltas(rt_client_id)
                            except Exception as purge_exc:
                                current_app.logger.debug("Failed to purge initial deltas: %s", purge_exc)

                    # 15秒ハートビート（クライアント側でタイムアウト検出用）
                    heartbeat_interval = SSE_HEARTBEAT_INTERVAL
                    last_heartbeat_time = time.time()
                    last_mode2_full_ts = 0.0

                    def _queued_frame(item: str | tuple[Any, Any]) -> str:
                        """Frame a queued announcer message with a global replay id.

                        Comment keepalives (``: ...``) are emitted as-is without
                        an id and are never recorded in the replay log.
                        Broadcast frames arrive pre-stamped as ``(seq, frame)``
                        tuples — the sequence id is allocated once at broadcast
                        time (see app_bg._announce_frame), not per listener, so
                        the replay log contains exactly one entry per broadcast
                        and Last-Event-ID resume stays consistent across
                        multiple connected clients. Plain strings are only
                        produced for keepalive comments.
                        """
                        if isinstance(item, tuple):
                            seq, msg = item
                            if isinstance(msg, str) and msg.startswith(":"):
                                return msg
                            return f"id: {seq}\n{msg}"
                        msg = item
                        if msg.startswith(":"):
                            return msg
                        seq = sse_event_log.next_id()
                        sse_event_log.record(seq, sse_mode, "frame", msg)
                        return f"id: {seq}\n{msg}"

                    while True:
                        msg = None
                        try:
                            msg = q.get_nowait()
                            if msg is None:
                                current_app.logger.info(
                                    "SSE listener dropped due to backpressure id=%s", request_id
                                )
                                break
                            yield _queued_frame(msg)
                        except queue.Empty:
                            pass

                        now = time.time()
                        # Check realtime engine deltas (TradingView WS / Yahoo JP)
                        # Enabled ONLY when sse_mode == 2 (TradingView Realtime Mode)
                        if sse_mode == 2:
                            try:
                                deltas = realtime_market_engine.get_market_deltas(rt_client_id)
                                if deltas:
                                    seq = sse_event_log.next_id()
                                    current_app.logger.debug(
                                        "SSE sending realtime_update to client id=%s with %d symbol(s): %s",
                                        request_id,
                                        len(deltas),
                                        list(deltas.keys()),
                                    )
                                    delta_data = json.dumps(
                                        _json_safe(
                                            {"stream_event": "realtime_update", "deltas": deltas}
                                        ),
                                        allow_nan=False,
                                    )
                                    delta_frame = (
                                        f"event: realtime_update\ndata: {delta_data}\n\n"
                                    )
                                    sse_event_log.record(seq, 2, "frame", delta_frame)
                                    yield f"id: {seq}\n{delta_frame}"
                                # PTS (after-hours) quote deltas: Yahoo JP first,
                                # SBI fallback — dispatched as a separate event so
                                # the regular session price is never overwritten.
                                pts_deltas = realtime_market_engine.get_pts_deltas(rt_client_id)
                                if pts_deltas:
                                    seq = sse_event_log.next_id()
                                    pts_data = json.dumps(
                                        _json_safe(
                                            {"stream_event": "pts_update", "deltas": pts_deltas}
                                        ),
                                        allow_nan=False,
                                    )
                                    pts_frame = f"event: pts_update\ndata: {pts_data}\n\n"
                                    sse_event_log.record(seq, 2, "frame", pts_frame)
                                    yield f"id: {seq}\n{pts_frame}"
                            except Exception as e:
                                current_app.logger.debug(
                                    "Failed fetching realtime engine deltas: %s", e
                                )

                            # Short-cycle full engine snapshot: with the cursor
                            # seeded at connect and incremental deltas after, a
                            # silently dropped frame could otherwise go unnoticed
                            # for a whole sync cycle. Re-emitting the full engine
                            # snapshot every N seconds bounds recovery latency.
                            if now - last_mode2_full_ts >= SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC:
                                last_mode2_full_ts = now
                                try:
                                    snapshot = realtime_market_engine.get_market_snapshot(rt_client_id)
                                    if snapshot:
                                        seq = sse_event_log.next_id()
                                        full_data = json.dumps(
                                            _json_safe(
                                                {
                                                    "stream_event": "realtime_update",
                                                    "deltas": snapshot,
                                                }
                                            ),
                                            allow_nan=False,
                                        )
                                        full_frame = (
                                            f"event: realtime_update\ndata: {full_data}\n\n"
                                        )
                                        sse_event_log.record(seq, 2, "frame", full_frame)
                                        yield f"id: {seq}\n{full_frame}"
                                except Exception as e:
                                    current_app.logger.debug(
                                        "Failed emitting mode-2 periodic snapshot: %s", e
                                    )

                        if now - last_heartbeat_time >= heartbeat_interval:
                            # 15秒間何もデータが来なかった場合、ハートビート送信
                            heartbeat_data = json.dumps({"type": "heartbeat", "timestamp": now})
                            heartbeat_seq = sse_event_log.next_id()
                            heartbeat_frame = (
                                f"event: heartbeat\ndata: {heartbeat_data}\n\n"
                            )
                            sse_event_log.record(heartbeat_seq, sse_mode, "frame", heartbeat_frame)
                            yield f"id: {heartbeat_seq}\n{heartbeat_frame}"
                            last_heartbeat_time = now

                        # Adaptive event wait:
                        # Mode 2 (Realtime): event-driven wait on realtime_market_engine (timeout 0.5s)
                        # Mode 1 (Complementary): blocking queue wait (0.5s during open market, 2.0s when closed)
                        if sse_mode == 2 and rt_client_id is not None:
                            realtime_market_engine.wait_for_updates(rt_client_id, timeout=0.5)
                        else:
                            market_open = is_market_open("us") or is_market_open("jp")
                            wait_msg = None
                            got_msg = False
                            try:
                                wait_msg = q.get(timeout=0.5 if market_open else 2.0)
                                got_msg = True
                            except queue.Empty:
                                pass
                            if got_msg:
                                if wait_msg is None:
                                    current_app.logger.info(
                                        "SSE listener dropped id=%s", request_id
                                    )
                                    break
                                yield _queued_frame(wait_msg)
                            elif not market_open:
                                yield ": keepalive\n\n"
        except GeneratorExit:
            raise
        except RuntimeError as exc:
            if (
                "too many" in str(exc).lower()
                or "limit" in str(exc).lower()
                or (
                    app_state.sse_announcer_mode1.listener_count()
                    + app_state.sse_announcer_mode2.listener_count()
                )
                >= MAX_SSE_LISTENERS
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


# #region AI Portfolio API Routes
@api_stocks_bp.route("/api/ai-portfolio", methods=["GET"])
@rate_limit(max_requests=60, window_seconds=60)
def api_get_ai_portfolios():
    """AIポートフォリオ一覧（プリセットおよび保存済みカスタムテーマ）を取得"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    saved = load_saved_ai_portfolios()
    return jsonify(
        {
            "ok": True,
            "presets": DEFAULT_PRESET_CONFIGS,
            "saved": saved,
        }
    )
import threading
from typing import TypedDict, cast

from cachetools import TTLCache
from flask import Flask


class FetchJob(TypedDict):
    result: Any
    error: BaseException | None
    done: threading.Event

ai_portfolio_fetch_lock = threading.Lock()
ai_portfolio_fetch_inflight: dict[str, Any] = {}

AI_PORTFOLIO_RESULT_CACHE_TTL = 300.0
ai_portfolio_result_cache: TTLCache[str, tuple[float, Any, BaseException | None]] = TTLCache(
    maxsize=128, ttl=AI_PORTFOLIO_RESULT_CACHE_TTL
)

def _submit_in_app_context(executor, job_fn, app=None):
    if app is None:
        _proxy: Any = current_app
        app = cast(Flask, _proxy._get_current_object())
    def _runner():
        with app.app_context():
            job_fn()
    executor.submit(_runner)


@api_stocks_bp.route("/api/ai-portfolio/generate", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_generate_ai_portfolio():
    """テーマに基づいてAIポートフォリオを生成"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request() or {}
    theme = str(data.get("theme", "")).strip()
    if not theme:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["theme"]}, status_code=400
        )

    api_key = extract_api_key(request)

    inflight_key = f"generate_{theme}"
    with ai_portfolio_fetch_lock:
        cached = ai_portfolio_result_cache.get(inflight_key)
    if cached is not None:
        _cached_ts, cached_result, cached_err = cached
        if cached_err is not None:
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, details={"reason": str(cached_err)}, status_code=500)
        if cached_result is not None:
            return jsonify({"ok": True, "portfolio": cached_result})

    with ai_portfolio_fetch_lock:
        if inflight_key in ai_portfolio_fetch_inflight:
            result_holder = ai_portfolio_fetch_inflight[inflight_key]
            already_fetching = True
        else:
            new_result_holder: FetchJob = {
                "result": None,
                "error": None,
                "done": threading.Event(),
            }
            ai_portfolio_fetch_inflight[inflight_key] = new_result_holder
            result_holder = new_result_holder
            already_fetching = False

    if not already_fetching:
        def _run_ai_portfolio_job() -> None:
            try:
                res = generate_ai_portfolio_by_theme(theme, api_key=api_key)
                result_holder["result"] = res
            except Exception as exc:
                result_holder["error"] = exc
            finally:
                with ai_portfolio_fetch_lock:
                    ai_portfolio_fetch_inflight.pop(inflight_key, None)
                    ai_portfolio_result_cache[inflight_key] = (
                        time.time(),
                        result_holder["result"],
                        result_holder["error"],
                    )
                result_holder["done"].set()

        try:
            _submit_in_app_context(app_state.execution.executor, _run_ai_portfolio_job)
        except Exception as exc:
            current_app.logger.error("Failed to schedule AI portfolio job: %s", exc)
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    # 短い時間だけ待機し、終わらなければクライアントにポーリングさせる
    finished = result_holder["done"].wait(timeout=3.0)
    if not finished:
        return jsonify({"fetching": True})

    if result_holder["error"] is not None:
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, details={"reason": str(result_holder["error"])}, status_code=500)

    return jsonify({"ok": True, "portfolio": result_holder["result"]})


@api_stocks_bp.route("/api/ai-portfolio/rebalance", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_rebalance_ai_portfolio():
    """指定されたAIポートフォリオのリバランス（再評価）を実行"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request()
    if data is None or not isinstance(data, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    theme = str(data.get("theme", "")).strip()
    if not theme:
        theme = "tech"

    api_key = extract_api_key(request)

    inflight_key = f"rebalance_{theme}"
    with ai_portfolio_fetch_lock:
        cached = ai_portfolio_result_cache.get(inflight_key)
    if cached is not None:
        _cached_ts, cached_result, cached_err = cached
        if cached_err is not None:
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, details={"reason": str(cached_err)}, status_code=500)
        if cached_result is not None:
            return jsonify({"ok": True, "portfolio": cached_result, "message": "リバランスが完了しました"})

    with ai_portfolio_fetch_lock:
        if inflight_key in ai_portfolio_fetch_inflight:
            result_holder = ai_portfolio_fetch_inflight[inflight_key]
            already_fetching = True
        else:
            new_result_holder: FetchJob = {
                "result": None,
                "error": None,
                "done": threading.Event(),
            }
            ai_portfolio_fetch_inflight[inflight_key] = new_result_holder
            result_holder = new_result_holder
            already_fetching = False

    if not already_fetching:
        def _run_ai_rebalance_job() -> None:
            try:
                res = generate_ai_portfolio_by_theme(theme, force_rebalance=True, api_key=api_key)
                result_holder["result"] = res
            except Exception as exc:
                result_holder["error"] = exc
            finally:
                with ai_portfolio_fetch_lock:
                    ai_portfolio_fetch_inflight.pop(inflight_key, None)
                    ai_portfolio_result_cache[inflight_key] = (
                        time.time(),
                        result_holder["result"],
                        result_holder["error"],
                    )
                result_holder["done"].set()

        try:
            _submit_in_app_context(app_state.execution.executor, _run_ai_rebalance_job)
        except Exception as exc:
            current_app.logger.error("Failed to schedule AI rebalance job: %s", exc)
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    # 短い時間だけ待機し、終わらなければクライアントにポーリングさせる
    finished = result_holder["done"].wait(timeout=3.0)
    if not finished:
        return jsonify({"fetching": True})

    if result_holder["error"] is not None:
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, details={"reason": str(result_holder["error"])}, status_code=500)

    return jsonify({"ok": True, "portfolio": result_holder["result"], "message": "リバランスが完了しました"})


@api_stocks_bp.route("/api/ai-portfolio/save", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_save_ai_portfolio():
    """カスタムAIポートフォリオを永続化保存"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request() or {}
    portfolio = data.get("portfolio")
    if not isinstance(portfolio, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "無効なポートフォリオデータです"},
            status_code=400,
        )

    canonical_portfolio = sanitize_ai_portfolio(portfolio)
    if not canonical_portfolio.get("title"):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "無効なポートフォリオデータです（タイトルがありません）"},
            status_code=400,
        )
    if portfolio.get("items") is not None and not canonical_portfolio.get("items"):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "有効な銘柄データがありません"},
            status_code=400,
        )

    success = save_custom_ai_portfolio(canonical_portfolio)
    if not success:
        return error_response(
            ErrorCode.INTERNAL_SERVER_ERROR,
            details={"reason": "保存に失敗しました"},
            status_code=500,
        )

    return jsonify({"ok": True, "portfolio": canonical_portfolio})


@api_stocks_bp.route("/api/ai-portfolio/custom", methods=["DELETE"])
@rate_limit(max_requests=20, window_seconds=60)
def api_delete_ai_portfolio():
    """保存済みカスタムAIポートフォリオを削除"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request() or {}
    portfolio_id = str(data.get("id", "")).strip()
    if not portfolio_id:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["id"]}, status_code=400
        )

    success = delete_custom_ai_portfolio(portfolio_id)
    if not success:
        return error_response(
            ErrorCode.NOT_FOUND,
            details={"reason": "対象ポートフォリオが見つかりません"},
            status_code=404,
        )

    return jsonify({"ok": True, "id": portfolio_id})


@api_stocks_bp.route("/api/ai-portfolio/copy-to-my", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_copy_ai_portfolio_to_my():
    """AIポートフォリオの構成銘柄をユーザーのマイポートフォリオへ複製"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request()
    if data is None or not isinstance(data, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "itemsリストが必要です"}, status_code=400
        )

    # Validate every item BEFORE touching any state so a malformed payload
    # returns a clean 400 without leaving a partially applied portfolio behind.
    parsed_items: list[tuple[str, str, float, float]] = []
    total_weight_pct = 0.0
    stale_warning: str | None = None
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return error_response(
                ErrorCode.MALFORMED_INPUT,
                details={"reason": f"items[{idx}] が無効です"},
                status_code=400,
            )
        symbol = str(item.get("symbol") or "").strip().upper()
        market = str(item.get("market") or "us").strip().lower()
        if market not in AI_PORTFOLIO_MARKETS or not is_valid_symbol(symbol):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] のシンボルまたは市場が無効です"},
                status_code=400,
            )
        target_raw = item.get("target_price")
        try:
            # A null/missing target price defaults to 100.0 (the same fallback
            # the single-add UI uses) so portfolios generated without explicit
            # target prices can still be bulk-copied.
            if target_raw is None or str(target_raw).strip() == "":
                target_price = 100.0
            else:
                target_price = float(target_raw)
            weight_pct = float(item.get("weight_pct") or 0.0)
        except (TypeError, ValueError):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] の数値が無効です"},
                status_code=400,
            )
        if (
            not math.isfinite(target_price)
            or not math.isfinite(weight_pct)
            or target_price <= 0
            or target_price > PORTFOLIO_AVG_PRICE_MAX
        ):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"items[{idx}] の target_price は 0 より大きく {PORTFOLIO_AVG_PRICE_MAX:,.0f} 以下の値が必要です"
                },
                status_code=400,
            )
        if not (0.0 < weight_pct <= 100.0):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] の weight_pct は 0〜100 の範囲が必要です"},
                status_code=400,
            )
        total_weight_pct += weight_pct
        if total_weight_pct > 100.0 + 1e-9:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": "items の weight_pct 合計は100以下である必要があります"},
                status_code=400,
            )
        allocated_val = VIRTUAL_INITIAL_CAPITAL_JPY * (weight_pct / 100.0)
        if market == "us":
            rate_is_valid = True
            try:
                usdjpy_rate = float(getattr(app_state.market, "last_usdjpy_rate", 150.0))
            except (TypeError, ValueError):
                usdjpy_rate = 150.0
                rate_is_valid = False
            if not math.isfinite(usdjpy_rate) or usdjpy_rate <= 0:
                usdjpy_rate = 150.0
                rate_is_valid = False
            # R7: surface a stale-rate warning when the last successful
            # USDJPY update is older than 24h. A backend that boots and
            # idles would otherwise silently compute shares with a stale
            # rate; surfacing the warning lets the UI prompt a refresh.
            try:
                usdjpy_rate_ts = float(
                    getattr(app_state.market, "last_usdjpy_rate_ts", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                usdjpy_rate_ts = 0.0
            now = time.time()
            if (
                not rate_is_valid
                or not math.isfinite(usdjpy_rate_ts)
                or usdjpy_rate_ts <= 0.0
                or usdjpy_rate_ts > now + 300.0
                or (now - usdjpy_rate_ts) > 24 * 3600
            ):
                stale_warning = (
                    "ドル円為替レートの更新日時が古いか確認できません（デフォルトレート 1ドル=150.0円 を適用しました）。"
                    "最新データでの再計算をお勧めします。"
                )
            allocated_val_usd = allocated_val / usdjpy_rate
            raw_shares = allocated_val_usd / target_price
        else:
            raw_shares = allocated_val / target_price

        if not math.isfinite(raw_shares) or raw_shares > PORTFOLIO_SHARES_MAX + 0.01:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"items[{idx}] の計算株数が上限（{PORTFOLIO_SHARES_MAX:,.0f}）を超過しています"
                },
                status_code=400,
            )

        # The portfolio model supports two decimal places. Floor instead of
        # round so the represented position can never exceed its allocation.
        shares = float(Decimal(str(raw_shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if shares < 0.01:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"items[{idx}] の割当額では最小株数（0.01株）を購入できません"
                },
                status_code=400,
            )

        if shares > PORTFOLIO_SHARES_MAX:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"items[{idx}] の計算株数が上限（{PORTFOLIO_SHARES_MAX:,.0f}）を超過しています"
                },
                status_code=400,
            )
        parsed_items.append((symbol, market, target_price, shares))

    added_count = 0
    skipped_symbols: list[str] = []
    added_symbols: list[tuple[str, str]] = []
    with app_state.market.user_stocks_lock:
        for symbol, market, target_price, shares in parsed_items:
            container = _get_stock_container(market)
            if container is None:
                continue
            # Never overwrite an existing holding: silently replacing the user's
            # real shares/avg_price with AI-simulated values would lose data.
            if symbol in container:
                skipped_symbols.append(f"{symbol} ({market})")
                continue
            container[symbol] = {
                "name": symbol,
                "shares": shares,
                "avg_price": target_price,
            }
            added_symbols.append((symbol, market))
            added_count += 1

        if added_count > 0:
            try:
                save_user_stocks()
            except Exception as exc:
                logger.warning("save_user_stocks during copy-to-my: %s", exc)
                # Roll back the in-memory additions so the UI and disk stay
                # consistent with the failed persistence (matches api_add_stock).
                for symbol, market in added_symbols:
                    container = _get_stock_container(market)
                    if container is not None:
                        container.pop(symbol, None)
                return error_response(
                    ErrorCode.FILE_ERROR,
                    details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                    status_code=503,
                )

            # Hold user_stocks_lock across the SSE cache patch to maintain consistency
            if added_symbols:
                with app_state.cache.sse_data_lock:
                    for sym, mkt in added_symbols:
                        invalidate_stock_caches(sym)
                        ensure_stock_placeholder_in_caches(sym, _stock_display_name(sym, mkt), mkt)
                        container = _get_stock_container(mkt)
                        holding_info = container.get(sym) if container else None
                        if holding_info and isinstance(holding_info, dict):
                            shares_val = holding_info.get("shares")
                            avg_price_val = holding_info.get("avg_price")
                            for cache in (
                                app_state.market.current_stocks_cache,
                                app_state.market.target_stocks_cache,
                            ):
                                if mkt not in cache:
                                    cache[mkt] = []
                                target_list = cache.get(mkt, [])
                                for s in target_list:
                                    if isinstance(s, dict) and s.get("symbol") == sym:
                                        if shares_val is not None:
                                            s["shares"] = shares_val
                                        if avg_price_val is not None:
                                            s["avg_price"] = avg_price_val
                                        break

    if added_symbols:
        for sym, mkt in added_symbols:
            _sync_realtime_symbol(sym, mkt, register=True)

        _announce_watchlist_state()
        schedule_sync_all_stocks_now()

    message = f"{added_count} 銘柄をマイポートフォリオに反映しました"
    if skipped_symbols:
        message += f"（既存保有のためスキップ: {', '.join(skipped_symbols[:5])}）"
    payload: dict = {
        "ok": True,
        "added_count": added_count,
        "skipped": skipped_symbols,
        "message": message,
    }
    # R7: surface the staleness warning to the UI when the cached USDJPY
    # rate is older than 24h so the operator can refresh before trusting the
    # computed share counts.
    if stale_warning:
        payload["stale_warning"] = stale_warning
    return jsonify(payload)


# #endregion AI Portfolio API Routes
