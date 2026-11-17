# bg/common.py
"""Shared helpers, constants, payload generation, and SSE broadcasting."""

from __future__ import annotations

import copy
import json
import logging
import threading
from typing import Any

from app_state import app_state
from bg._app_bridge import _get_app_bg_attr, _set_app_bg_attr
from messaging import sse_event_log
from utils.market_utils import is_market_open as _orig_is_market_open
from utils.stock_payload import _strip_portfolio_fields

logger = logging.getLogger(__name__)


def is_market_open(market: str) -> bool:
    target = _get_app_bg_attr("is_market_open", _orig_is_market_open)
    return target(market)


# Constants
_SSE_KEEPALIVE_FRAME = ": keepalive\n\n"
FULL_SNAPSHOT_INTERVAL: int = 6

# State tracking for SSE payload caching
_sse_payload_cache: str = (
    'data: {"stocks":[],"indices":[],'
    '"is_yfinance_rate_limited":false,'
    '"is_us_market_open":false,"is_jp_market_open":false}\n\n'
)
_sse_payload_generation: int = 0
_sse_payload_cached_generation: int = -1
_sse_payload_yf_limited: bool = False
_sse_payload_us_open: bool = False
_sse_payload_jp_open: bool = False
_sse_payload_lock = threading.Lock()
# Serializes state calculation + emit+announce inside _raw_announce_current_market_state
# and _announce_frame so concurrent broadcasters cannot allocate adjacent sequence ids
# and then deliver them in the opposite order (RLock allows safe re-entry on the same thread).
_sse_frame_publish_lock = threading.RLock()
_sse_prev_stocks: dict[str, dict[str, Any]] = {"us": {}, "jp": {}, "idx": {}}
_sse_prev_indices: dict[str, Any] = {}
_sse_full_snapshot_counter: int = 0


# ``_get_app_bg_attr`` / ``_set_app_bg_attr`` are owned by ``bg._app_bridge``
# (M-3) so the app_bg mirror has a single implementation. They are imported
# above and re-exported here for existing callers (``bg_common._get_app_bg_attr``
# etc.) that resolve them through this module.


def _build_sse_light_stocks_payload(
    stocks: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Lightweight stock payload for SSE streaming without detailed history."""
    light: dict[str, list[dict[str, Any]]] = {}
    for market, items in stocks.items():
        light[market] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    light[market].append(_strip_portfolio_fields(item))
    return light


def _build_indices_diff(
    new_indices: dict[str, Any],
    prev_indices: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compute changed/added/removed index entries between SSE snapshots."""
    cur_map = new_indices if isinstance(new_indices, dict) else {}
    old_map = prev_indices if isinstance(prev_indices, dict) else {}
    rows: list[dict[str, Any]] = []
    for sym, val in cur_map.items():
        if sym not in old_map or old_map[sym] != val:
            if isinstance(val, dict):
                entry = dict(val)
                entry.setdefault("symbol", sym)
                rows.append(entry)
            else:
                rows.append({"symbol": sym, "value": val})
    for sym in old_map:
        if sym not in cur_map:
            rows.append({"symbol": sym, "_removed": True})
    return rows


def _build_sse_diff(
    new_stocks: dict[str, list[dict[str, Any]]],
    prev_map: dict[str, dict[str, Any]],
    new_indices: dict[str, Any] | None = None,
    prev_indices: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Compute the diff between the previous and current market snapshots."""
    diff: dict[str, list[dict[str, Any]]] = {"us": [], "jp": [], "idx": []}
    for market in ("us", "jp", "idx"):
        current_list = new_stocks.get(market, [])
        current_map: dict[str, dict[str, Any]] = {}
        for item in current_list:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol")
            if not sym:
                continue
            safe_item = _strip_portfolio_fields(item)
            current_map[sym] = safe_item
            prev_item = prev_map.get(market, {}).get(sym)
            if prev_item is None:
                diff[market].append(safe_item)
            else:
                prev_ts = prev_item.get("snapshot_ts_ms") or 0
                curr_ts = safe_item.get("snapshot_ts_ms") or 0
                if curr_ts != prev_ts or safe_item.get("price") != prev_item.get("price"):
                    diff[market].append(safe_item)
        for sym in prev_map.get(market, {}):
            if sym not in current_map:
                diff[market].append({"symbol": sym, "_removed": True})
    if new_indices is not None:
        diff["indices"] = _build_indices_diff(new_indices, prev_indices or {})
    return diff


def _invalidate_sse_payload_cache() -> None:
    """Invalidate the SSE payload cache, forcing re-serialization on next announce."""
    global _sse_payload_generation
    with _sse_payload_lock:
        gen = int(_get_app_bg_attr("_sse_payload_generation", _sse_payload_generation)) + 1
        _sse_payload_generation = gen
        _set_app_bg_attr("_sse_payload_generation", gen)


def _announce_frame(announcer: Any, frame: str, mode: int) -> None:
    """Broadcast a complete SSE frame with a single replay-log sequence id.

    The emit and the announce are serialized process-wide so two broadcaster
    threads cannot invert frame order relative to the replay log (a listener
    that receives seq N+1 before seq N would apply a newer diff and then an
    older one, reverting values until the next frame).
    """
    with _sse_frame_publish_lock:
        seq = sse_event_log.emit(mode, "frame", frame)
        announcer.announce((seq, frame))


def _raw_announce_current_market_state() -> None:
    """現在のインメモリキャッシュ状態をシリアライズしてSSE配信する実体関数"""
    global _sse_payload_cache, _sse_payload_cached_generation
    global \
        _sse_payload_yf_limited, \
        _sse_payload_us_open, \
        _sse_payload_jp_open, \
        _sse_full_snapshot_counter, \
        _sse_prev_stocks, \
        _sse_prev_indices
    yf_limited = app_state.market.is_yf_rate_limited()
    us_open = is_market_open("us")
    jp_open = is_market_open("jp")
    with app_state.cache.sse_data_lock:
        stocks = copy.deepcopy(app_state.market.current_stocks_cache)
        indices = copy.deepcopy(app_state.market.current_indices_cache)

    announcer1 = getattr(app_state, "sse_announcer", None) or app_state.sse_announcer_mode1
    with _sse_payload_lock:
        current_gen = int(_get_app_bg_attr("_sse_payload_generation", _sse_payload_generation))
        cached_gen = int(
            _get_app_bg_attr("_sse_payload_cached_generation", _sse_payload_cached_generation)
        )
        cached_yf = bool(_get_app_bg_attr("_sse_payload_yf_limited", _sse_payload_yf_limited))
        cached_us = bool(_get_app_bg_attr("_sse_payload_us_open", _sse_payload_us_open))
        cached_jp = bool(_get_app_bg_attr("_sse_payload_jp_open", _sse_payload_jp_open))

        # R17-04: keep this critical section to the generation/state read.
        # The (potentially large) json.dumps + announce below are ordered by
        # _sse_frame_publish_lock, not by holding _sse_payload_lock across
        # serialization and queue operations (which blocked invalidations).
        _snapshot = (current_gen, cached_gen, cached_yf, cached_us, cached_jp)
        counter = (
            int(_get_app_bg_attr("_sse_full_snapshot_counter", _sse_full_snapshot_counter)) + 1
        )
        _sse_full_snapshot_counter = counter
        _set_app_bg_attr("_sse_full_snapshot_counter", counter)
        send_full_snapshot = counter % FULL_SNAPSHOT_INTERVAL == 0

    with _sse_frame_publish_lock:
        if send_full_snapshot:
            light_stocks = _build_sse_light_stocks_payload(stocks)
            payload = json.dumps(
                {
                    "stream_event": "full_snapshot",
                    "stocks": light_stocks,
                    "indices": indices,
                    "is_yfinance_rate_limited": yf_limited,
                    "is_us_market_open": us_open,
                    "is_jp_market_open": jp_open,
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        else:
            current_gen, cached_gen, cached_yf, cached_us, cached_jp = _snapshot
            prev_stocks = _get_app_bg_attr("_sse_prev_stocks", _sse_prev_stocks)
            prev_indices = _get_app_bg_attr("_sse_prev_indices", _sse_prev_indices)
            diff = _build_sse_diff(
                stocks,
                prev_stocks,
                new_indices=indices,
                prev_indices=prev_indices,
            )
            # L-1: the decision unit is an explicit *changed-entry count* (the
            # number of changed stock/index rows across markets), NOT bytes or chars.
            # ``_build_sse_diff`` returns ``dict[str, list[changed rows]]`` so
            # ``len(rows)`` counts rows; the quantity is named accordingly so
            # the unit cannot be misread as a serialized-size measure.  The
            # publish decision is therefore "is at least one row present?".
            changed_entry_count = sum(len(rows) for rows in diff.values())
            status_changed = (
                yf_limited != cached_yf or us_open != cached_us or jp_open != cached_jp
            )
            stock_diff = {market: diff[market] for market in ("us", "jp", "idx")}
            if changed_entry_count > 0 or status_changed:
                payload = json.dumps(
                    {
                        "stream_event": "diff",
                        "stocks": stock_diff,
                        "indices": indices,
                        "is_yfinance_rate_limited": yf_limited,
                        "is_us_market_open": us_open,
                        "is_jp_market_open": jp_open,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                )
            else:
                announcer1.announce(_SSE_KEEPALIVE_FRAME)
                with _sse_payload_lock:
                    _sse_payload_cached_generation = current_gen
                    _sse_payload_yf_limited = yf_limited
                    _sse_payload_us_open = us_open
                    _sse_payload_jp_open = jp_open
                    _set_app_bg_attr("_sse_payload_cached_generation", current_gen)
                    _set_app_bg_attr("_sse_payload_yf_limited", yf_limited)
                    _set_app_bg_attr("_sse_payload_us_open", us_open)
                    _set_app_bg_attr("_sse_payload_jp_open", jp_open)
                return

        new_prev: dict[str, dict[str, Any]] = {}
        for market in ("us", "jp", "idx"):
            current_list = stocks.get(market, [])
            new_prev[market] = {}
            for item in current_list:
                if isinstance(item, dict) and item.get("symbol"):
                    new_prev[market][item["symbol"]] = _strip_portfolio_fields(item)
        new_prev_indices = copy.deepcopy(indices) if isinstance(indices, dict) else {}
        frame = f"data: {payload}\n\n"

        # R17-04: state write under a short critical section. Order is
        # guaranteed by the surrounding _sse_frame_publish_lock.
        with _sse_payload_lock:
            _sse_prev_stocks = new_prev
            _set_app_bg_attr("_sse_prev_stocks", new_prev)
            _sse_prev_indices = new_prev_indices
            _set_app_bg_attr("_sse_prev_indices", new_prev_indices)
            _sse_payload_cache = frame
            _sse_payload_cached_generation = current_gen
            _sse_payload_yf_limited = yf_limited
            _sse_payload_us_open = us_open
            _sse_payload_jp_open = jp_open
            _set_app_bg_attr("_sse_payload_cache", frame)
            _set_app_bg_attr("_sse_payload_cached_generation", current_gen)
            _set_app_bg_attr("_sse_payload_yf_limited", yf_limited)
            _set_app_bg_attr("_sse_payload_us_open", us_open)
            _set_app_bg_attr("_sse_payload_jp_open", jp_open)

        # Publish inside _sse_frame_publish_lock (re-entrant RLock) so a second
        # broadcaster cannot overtake this thread between releasing the lock
        # and calling sse_event_log.emit / announcer.announce.
        announce_fn = _get_app_bg_attr("_announce_frame", _announce_frame)
        announce_fn(announcer1, frame, 1)


def announce_current_market_state() -> None:
    """現在のインメモリキャッシュ状態をシリアライズしてSSE配信する"""
    target = _get_app_bg_attr("announce_current_market_state", None)
    if target is not None and target is not announce_current_market_state:
        return target()
    return _raw_announce_current_market_state()


def announce_real_market_state() -> None:
    """Announce real market data (target_stocks_cache) via Mode 2 SSE."""
    target = _get_app_bg_attr("announce_real_market_state", None)
    if target is not None and target is not announce_real_market_state:
        return target()

    if app_state.sse_announcer_mode2.listener_count() == 0:
        return
    with app_state.cache.sse_data_lock:
        target_stocks = copy.deepcopy(app_state.market.target_stocks_cache)
        indices = copy.deepcopy(app_state.market.current_indices_cache)
    yf_limited = app_state.market.is_yf_rate_limited()
    us_open = is_market_open("us")
    jp_open = is_market_open("jp")
    light_stocks = _build_sse_light_stocks_payload(target_stocks)
    payload = json.dumps(
        {
            "stream_event": "full_snapshot",
            "stocks": light_stocks,
            "indices": indices,
            "is_yfinance_rate_limited": yf_limited,
            "is_us_market_open": us_open,
            "is_jp_market_open": jp_open,
        },
        ensure_ascii=False,
        allow_nan=False,
    )
    announce_fn = _get_app_bg_attr("_announce_frame", _announce_frame)
    announce_fn(app_state.sse_announcer_mode2, f"data: {payload}\n\n", 2)
