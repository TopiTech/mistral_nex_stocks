# routes/stocks/quotes.py
"""Stock quotes, indices, history, search, and details endpoints."""

from __future__ import annotations

import logging
import queue
from datetime import UTC, datetime
from typing import Any

import requests
from flask import Blueprint, current_app, jsonify, request

from app_bg import schedule_sync_all_stocks_now
from app_state import app_state
from constants import (
    CACHE_DURATION_SEARCH,
    HISTORY_CACHE_DURATION_CLOSED,
    HISTORY_CACHE_DURATION_CLOSED_LONG,
    HISTORY_CACHE_DURATION_OPEN,
    HISTORY_CACHE_DURATION_OPEN_LONG,
    HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
    HISTORY_CIRCUIT_BREAKER_THRESHOLD,
    VALID_HISTORY_INTERVALS,
    VALID_HISTORY_PERIODS,
)
from error_codes import ErrorCode, get_error_message
from route_helpers import rate_limit
from routes.stocks.common import (
    _get_api_stocks_attr,
    is_market_open,
    require_trusted_or_admin,
    resolve_indices_for_response,
    resolve_stocks_for_response,
)
from services.stock_service import fetch_history_async_task
from utils.caching import (
    CACHE_FETCHING,
    _get_cached_value,
    _has_cached_key,
    get_cached,
)
from utils.env_helpers import _env_bool as _stocks_env_bool
from utils.env_helpers import _is_testing as _stocks_is_testing
from utils.normalization import (
    is_valid_symbol,
    normalize_market,
    normalize_optional_number,
    normalize_symbol,
    normalize_symbol_for_market,
)
from utils.stock_payload import (
    _wait_for_initial_market_snapshot,
    error_response,
    fetch_stock_info_async,
)

logger = logging.getLogger(__name__)

quotes_bp = Blueprint("quotes", __name__)


def _submit_async_info_fetch(symbol: str) -> None:
    """Offload a stock-info fetch to data_executor (see H-2)."""
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
    probe: bool = False,
) -> bool:
    """バックグラウンドexecutorに履歴データ非同期フェッチを送信する共通ヘルパー。"""
    with app_state.history_fetch_lock:
        if cache_key in app_state.history_fetch_inflight:
            return False
        app_state.history_fetch_inflight.add(cache_key)

    try:
        future = app_state.execution.data_executor.submit(
            fetch_history_async_task,
            symbol,
            market,
            period,
            cache_key,
            duration,
            interval=interval,
            probe=probe,
        )
        if probe:
            circuit_key = f"{market}:{symbol}"

            def _handle_cancelled_probe(done_future: Any) -> None:
                if not done_future.cancelled():
                    return
                app_state.market.report_circuit_result(
                    "yfinance_history",
                    success=False,
                    symbol=circuit_key,
                    threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
                    open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
                )
                with app_state.history_fetch_lock:
                    app_state.history_fetch_inflight.discard(cache_key)

            future.add_done_callback(_handle_cancelled_probe)
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


@quotes_bp.route("/api/indices")
@rate_limit(max_requests=60, window_seconds=60)
def api_indices() -> Any:
    """指数データAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)
    force = request.args.get("force") == "true"
    if force:
        schedule_sync_all_stocks_now()
    with app_state.cache.sse_data_lock:
        data = resolve_indices_for_response()
    if not data:
        _wait_for_initial_market_snapshot("indices", timeout_sec=6.0)
        with app_state.cache.sse_data_lock:
            data = resolve_indices_for_response()
    if not data:
        return jsonify({"fetching": True})
    return jsonify(data)


@quotes_bp.route("/api/stocks")
@rate_limit(max_requests=60, window_seconds=60)
def api_stocks() -> Any:
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
    if (
        _stocks_env_bool("MNS_SKIP_BOOTSTRAP")
        and not app_state.bootstrap_ready.is_set()
        and not _stocks_is_testing()
    ):
        return jsonify(
            {
                "ok": False,
                "error": "bootstrap skipped (MNS_SKIP_BOOTSTRAP=1)",
                "bootstrap_skipped": True,
                "stocks": {"us": [], "jp": [], "idx": []},
                "indices": {},
                "fetching": False,
            }
        ), 503
    with app_state.cache.sse_data_lock:
        stocks = resolve_stocks_for_response(include_portfolio=False)
        indices = resolve_indices_for_response()
    if not any(stocks.get(m) for m in ("us", "jp", "idx")) and not indices:
        _wait_for_initial_market_snapshot("stocks", timeout_sec=6.0)
        with app_state.cache.sse_data_lock:
            stocks = resolve_stocks_for_response(include_portfolio=False)
            indices = resolve_indices_for_response()
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


@quotes_bp.route("/api/stock-details")
@rate_limit(max_requests=60, window_seconds=60)
def api_stock_details() -> Any:
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

    short_cache_key = f"info_short_{symbol}"
    with app_state.yfinance_short_cache_lock:
        cached_short = app_state.yfinance_short_cache.get(short_cache_key)
    if isinstance(cached_short, dict) and cached_short:
        if cached_short.get("failed") or cached_short.get("error"):
            return jsonify(
                {
                    "symbol": symbol,
                    "failed": True,
                    "sector": None,
                    "industry": None,
                    "market_cap": None,
                    "pe_ratio": None,
                }
            )
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


@quotes_bp.route("/api/stock-history")
@rate_limit(max_requests=120, window_seconds=60)
def api_stock_history() -> Any:
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
        return error_response(
            ErrorCode.INVALID_INTERVAL,
            details={"allowed": sorted(VALID_HISTORY_INTERVALS)},
        )
    symbol = normalize_symbol_for_market(symbol, market)
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    circuit_key = f"{market}:{symbol}"
    is_open = app_state.market.is_circuit_open("yfinance_history", symbol=circuit_key)

    cache_key = (
        f"hist_{symbol}_{market}_{period}_{interval}"
        if interval != "auto"
        else f"hist_{symbol}_{market}_{period}"
    )

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

    def make_history_response(payload: Any, is_cacheable: bool = True) -> Any:
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

    with app_state.market.history_circuit_lock:
        state: Any = app_state.market.history_circuit_state.get(circuit_key) or {}
        if state.get("status") == "HALF_OPEN":
            is_probing = state.get("probing")
            if not is_probing:
                claimed = app_state.market.try_claim_circuit_probe(
                    "yfinance_history", symbol=circuit_key
                )
                is_probing = not claimed
            logger.info(
                "stock-history circuit HALF_OPEN symbol=%s - scheduling async fetch", circuit_key
            )
            if not is_probing:
                try:
                    submitted = _submit_async_history_fetch(
                        cache_key,
                        symbol,
                        market,
                        period,
                        duration,
                        "HALF_OPEN",
                        interval=interval,
                        probe=True,
                    )
                except queue.Full:
                    submitted = False
                    logger.warning("History fetch queue full during HALF_OPEN symbol=%s", circuit_key)
                if not submitted:
                    app_state.market.report_circuit_result(
                        "yfinance_history",
                        success=False,
                        symbol=circuit_key,
                        threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
                        open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
                    )
            return make_history_response(FETCHING_RESPONSE, is_cacheable=False)

    if is_open:
        logger.info("stock-history circuit open symbol=%s - failing fast", circuit_key)
        return error_response(ErrorCode.CIRCUIT_BREAKER_OPEN, status_code=503)

    if _has_cached_key(cache_key, duration):
        cached_data = _get_cached_value(cache_key, duration)
        if cached_data:
            return make_history_response(cached_data)

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

    disk_data = app_state.stock_disk_cache.get(cache_key)
    if disk_data and isinstance(disk_data, dict) and "error" not in disk_data:
        logger.info("Serving disk-cached history for %s period=%s", symbol, period)
        return make_history_response(
            {
                **disk_data,
                "stale": True,
                "fetching": True,
                "message": "キャッシュ済みデータを表示中です。最新データを取得中...",
            },
            is_cacheable=False,
        )

    return make_history_response(
        {
            "symbol": symbol,
            "history": [],
            "fetching": True,
            "message": "履歴データを取得中です。しばらくしてから再ロードしてください。",
        },
        is_cacheable=False,
    )


@quotes_bp.route("/api/search")
@rate_limit(max_requests=90, window_seconds=60)
def api_search() -> Any:
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

    def _search() -> Any:
        try:
            results = app_state.stock_provider.search(q, max_results=10)
            return {"results": results}
        except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
            current_app.logger.error("Search API failed (%s): %s", q, exc)
            return {
                "error": get_error_message(ErrorCode.API_SERVICE_ERROR, lang="ja"),
                "error_code": int(ErrorCode.API_SERVICE_ERROR),
            }

    get_cached_fn = _get_api_stocks_attr("get_cached", get_cached)
    result = get_cached_fn(
        f"search_{q}",
        _search,
        duration=CACHE_DURATION_SEARCH,
        valid_func=lambda payload: isinstance(payload, dict) and isinstance(payload.get("results"), list),
    )
    if result is CACHE_FETCHING or not isinstance(result, dict):
        result = {"results": []}
    return jsonify(result)
