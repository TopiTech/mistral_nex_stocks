# routes/stocks/common.py
"""Common helpers, types, and utilities for stock route handlers."""

from __future__ import annotations

import json
import logging
import math
import numbers
import sys
from typing import Any

from flask import current_app, request

import app_bg as _bg_mod
import services.market_data_service as _mkt_service_mod
import utils.caching as _caching_mod
import utils.market_utils as _market_utils_mod
import utils.networking as _net_mod
import utils.stock_payload as _payload_mod
import utils.storage as _storage_mod
from app_bg import fetch_stocks_batch
from app_state import app_state
from constants import (
    CACHE_DURATION_HEATMAP,
    MAX_USER_WATCHLIST_ITEMS,
)
from error_codes import ErrorCode
from services.market_data_service import build_heatmap_payload
from utils.caching import get_cached
from utils.normalization import normalize_symbol_for_market
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    error_response,
)

logger = logging.getLogger(__name__)

_WATCHLIST_MARKET_LABELS = {"us": "米国", "jp": "日本", "idx": "インデックス/ETF"}


def _get_api_stocks_attr(name: str, fallback: Any) -> Any:
    mod = sys.modules.get("routes.api_stocks")
    if mod is not None and name in mod.__dict__:
        target = mod.__dict__[name]
        return target
    return fallback


def require_trusted_or_admin(req: Any, require_origin: bool = True) -> tuple[bool, str]:
    target = _get_api_stocks_attr("require_trusted_or_admin", _net_mod.require_trusted_or_admin)
    return target(req, require_origin=require_origin)


def require_sse_auth(req: Any) -> tuple[bool, str]:
    target = _get_api_stocks_attr("require_sse_auth", _net_mod.require_sse_auth)
    return target(req)


def save_user_stocks() -> None:
    target = _get_api_stocks_attr("save_user_stocks", _storage_mod.save_user_stocks)
    return target()


def is_market_open(market: str) -> bool:
    target = _get_api_stocks_attr("is_market_open", _market_utils_mod.is_market_open)
    return target(market)


def resolve_stocks_for_response(include_portfolio: bool = True, real_data_only: bool = False) -> dict[str, list[Any]]:
    target = _get_api_stocks_attr("_resolve_stocks_for_response", _payload_mod._resolve_stocks_for_response)
    try:
        return target(include_portfolio=include_portfolio, real_data_only=real_data_only)
    except TypeError:
        return target(include_portfolio=include_portfolio)


def resolve_indices_for_response() -> dict[str, Any]:
    target = _get_api_stocks_attr("_resolve_indices_for_response", _payload_mod._resolve_indices_for_response)
    return target()


def get_stock_info_cached(*args: Any, **kwargs: Any) -> dict[str, Any]:
    target = _get_api_stocks_attr("get_stock_info_cached", _payload_mod.get_stock_info_cached)
    return target(*args, **kwargs)


def fetch_stocks_batch_dispatch(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    target = _get_api_stocks_attr("fetch_stocks_batch", _bg_mod.fetch_stocks_batch)
    return target(*args, **kwargs)


def schedule_sync_all_stocks_now(force: bool = False) -> None:
    target = _get_api_stocks_attr("schedule_sync_all_stocks_now", _bg_mod.schedule_sync_all_stocks_now)
    return target(force=force)


def get_cached_dispatch(key: str, fetch_func: Any, duration: float = 300, valid_func: Any = None) -> Any:
    target = _get_api_stocks_attr("get_cached", _caching_mod.get_cached)
    return target(key, fetch_func, duration=duration, valid_func=valid_func)


def build_screener_base_rows_dispatch(stocks_data: dict[str, Any], market_filter: str) -> list[dict[str, Any]]:
    target = _get_api_stocks_attr("build_screener_base_rows", _mkt_service_mod.build_screener_base_rows)
    return target(stocks_data, market_filter)


def build_screener_enrichment_dispatch(
    pop_unseen_items: list[tuple[str, str, str]],
    q_symbol: str | None,
    fetch_batch_fn: Any = None,
    get_info_fn: Any = None,
) -> dict[str, dict[str, Any]]:
    target = _get_api_stocks_attr("build_screener_enrichment", _mkt_service_mod.build_screener_enrichment)
    return target(
        pop_unseen_items,
        q_symbol,
        fetch_batch_fn=fetch_batch_fn or fetch_stocks_batch_dispatch,
        get_info_fn=get_info_fn or get_stock_info_cached,
    )


def build_popular_symbol_items_dispatch(
    market_filter: str,
    q: str,
    seen_symbols: set[str],
    pop_sources: list[tuple[str, Any]],
) -> list[tuple[str, str, str]]:
    target = _get_api_stocks_attr("build_popular_symbol_items", _mkt_service_mod.build_popular_symbol_items)
    return target(market_filter, q, seen_symbols, pop_sources)


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats (NaN/±Inf) and pandas NA/NaT with None,
    and convert numpy/scalar types to standard Python types for strict JSON serialization."""
    if isinstance(value, bool):
        return value
    if type(value).__name__ in ("bool_", "bool"):
        return bool(value)
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "tolist"):
        try:
            return [_json_safe(v) for v in value.tolist()]
        except Exception:
            pass
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _parse_last_event_id() -> int:
    """Parse the client's last-seen SSE event id (query param or standard header)."""
    raw = request.headers.get("Last-Event-ID") or request.args.get("last_event_id")
    if not raw:
        return 0
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _replay_frame_for_entry(seq: int, kind: str, payload: Any, sse_mode: int) -> str | None:
    """Rebuild an SSE frame for a buffered event during Last-Event-ID replay."""
    target = _get_api_stocks_attr("_replay_frame_for_entry", None)
    if target is not None and target is not _replay_frame_for_entry:
        return target(seq, kind, payload, sse_mode)

    try:
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
    except Exception as exc:
        logger.warning(
            "Failed to rebuild SSE replay frame seq=%s kind=%s (mode=%d): %s",
            seq,
            kind,
            sse_mode,
            exc,
        )
        return None


def _sync_realtime_symbol(symbol: str, market: str, register: bool) -> None:
    """Keep the realtime market engine's subscription list in sync with the watchlist."""
    target = _get_api_stocks_attr("_sync_realtime_symbol", None)
    if target is not None and target is not _sync_realtime_symbol:
        return target(symbol, market, register=register)

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


def _stored_symbol_aliases(symbol: str, market: str) -> tuple[str, ...]:
    """Return all persisted spellings for one logical watchlist symbol."""
    if market != "jp":
        return (symbol,)

    canonical = normalize_symbol_for_market(symbol, market)
    aliases = [canonical]
    if canonical.endswith(".T") and canonical[:-2].isdigit():
        aliases.append(canonical[:-2])
    return tuple(aliases)


def _watchlist_has_capacity(market: str, extra: int = 1) -> bool:
    """Return True if ``extra`` more user entries fit in the market's watchlist."""
    container = _get_stock_container(market)
    if container is None:
        return False
    default_symbols = _default_stock_names(market)
    user_entry_count = sum(symbol not in default_symbols for symbol in container)
    return user_entry_count + extra <= MAX_USER_WATCHLIST_ITEMS


def _watchlist_capacity_error(market: str) -> Any:
    """Build the fixed 400 error used when a market's watchlist cap is exceeded."""
    label = _WATCHLIST_MARKET_LABELS.get(market, market)
    return error_response(
        ErrorCode.INVALID_INPUT,
        details={
            "reason": f"{label}市場の銘柄は最大 {MAX_USER_WATCHLIST_ITEMS} 件まで登録できます"
        },
        status_code=400,
    )


def _announce_watchlist_state() -> None:
    """Notify both SSE modes after a watchlist membership mutation."""
    target = _get_api_stocks_attr("_announce_watchlist_state", None)
    if target is not None and target is not _announce_watchlist_state:
        return target()

    from app_bg import (
        _invalidate_sse_payload_cache,
        announce_current_market_state,
        announce_real_market_state,
    )

    _invalidate_sse_payload_cache()
    announce_current_market_state()
    announce_real_market_state()


def _fetch_heatmap_cached(cache_key: str, market: str, symbols: list[str]) -> None:
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
