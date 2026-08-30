# bg/common.py
"""Shared helpers, constants, payload generation, and SSE broadcasting."""

from __future__ import annotations

import copy
import json
import logging
import sys
import threading
from typing import Any

from app_state import app_state
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
_sse_prev_stocks: dict[str, dict[str, Any]] = {"us": {}, "jp": {}, "idx": {}}
_sse_full_snapshot_counter: int = 0


def _get_app_bg_attr(name: str, fallback: Any) -> Any:
    mod = sys.modules.get("app_bg")
    if mod is not None and name in mod.__dict__:
        target = mod.__dict__[name]
        return target
    return fallback


def _set_app_bg_attr(name: str, val: Any) -> None:
    mod = sys.modules.get("app_bg")
    if mod is not None:
        mod.__dict__[name] = val


def _build_sse_light_stocks_payload(stocks: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Lightweight stock payload for SSE streaming without detailed history."""
    light: dict[str, list[dict[str, Any]]] = {}
    for market, items in stocks.items():
        light[market] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    light[market].append(_strip_portfolio_fields(item))
    return light


def _build_sse_diff(
    new_stocks: dict[str, list[dict[str, Any]]],
    prev_map: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Compute the diff between the previous and current stock snapshots."""
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
    return diff


def _invalidate_sse_payload_cache() -> None:
    """Invalidate the SSE payload cache, forcing re-serialization on next announce."""
    global _sse_payload_generation
    with _sse_payload_lock:
        gen = int(_get_app_bg_attr("_sse_payload_generation", _sse_payload_generation)) + 1
        _sse_payload_generation = gen
        _set_app_bg_attr("_sse_payload_generation", gen)


def _announce_frame(announcer: Any, frame: str, mode: int) -> None:
    """Broadcast a complete SSE frame with a single replay-log sequence id."""
    seq = sse_event_log.next_id()
    sse_event_log.record(seq, mode, "frame", frame)
    announcer.announce((seq, frame))


def _raw_announce_current_market_state() -> None:
    """現在のインメモリキャッシュ状態をシリアライズしてSSE配信する実体関数"""
    global _sse_payload_cache, _sse_payload_cached_generation
    global _sse_payload_yf_limited, _sse_payload_us_open, _sse_payload_jp_open, _sse_full_snapshot_counter, _sse_prev_stocks
    yf_limited = app_state.market.is_yf_rate_limited()
    us_open = is_market_open("us")
    jp_open = is_market_open("jp")
    with app_state.cache.sse_data_lock:
        stocks = copy.deepcopy(app_state.market.current_stocks_cache)
        indices = copy.deepcopy(app_state.market.current_indices_cache)

    with _sse_payload_lock:
        current_gen = int(_get_app_bg_attr("_sse_payload_generation", _sse_payload_generation))
        cached_gen = int(_get_app_bg_attr("_sse_payload_cached_generation", _sse_payload_cached_generation))
        cached_yf = bool(_get_app_bg_attr("_sse_payload_yf_limited", _sse_payload_yf_limited))
        cached_us = bool(_get_app_bg_attr("_sse_payload_us_open", _sse_payload_us_open))
        cached_jp = bool(_get_app_bg_attr("_sse_payload_jp_open", _sse_payload_jp_open))

    announcer1 = getattr(app_state, "sse_announcer", None) or app_state.sse_announcer_mode1
    if (
        current_gen == cached_gen
        and yf_limited == cached_yf
        and us_open == cached_us
        and jp_open == cached_jp
    ):
        announcer1.announce(_SSE_KEEPALIVE_FRAME)
        return

    with _sse_payload_lock:
        counter = int(_get_app_bg_attr("_sse_full_snapshot_counter", _sse_full_snapshot_counter)) + 1
        _sse_full_snapshot_counter = counter
        _set_app_bg_attr("_sse_full_snapshot_counter", counter)
        send_full_snapshot = counter % FULL_SNAPSHOT_INTERVAL == 0

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
            prev_stocks = _get_app_bg_attr("_sse_prev_stocks", _sse_prev_stocks)
            diff = _build_sse_diff(stocks, prev_stocks)
            diff_size = sum(len(v) for v in diff.values())
            if diff_size > 0:
                payload = json.dumps(
                    {
                        "stream_event": "diff",
                        "stocks": diff,
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
        _sse_prev_stocks = new_prev
        _set_app_bg_attr("_sse_prev_stocks", new_prev)

        _sse_payload_cache = f"data: {payload}\n\n"
        _sse_payload_cached_generation = current_gen
        _sse_payload_yf_limited = yf_limited
        _sse_payload_us_open = us_open
        _sse_payload_jp_open = jp_open

        _set_app_bg_attr("_sse_payload_cache", _sse_payload_cache)
        _set_app_bg_attr("_sse_payload_cached_generation", current_gen)
        _set_app_bg_attr("_sse_payload_yf_limited", yf_limited)
        _set_app_bg_attr("_sse_payload_us_open", us_open)
        _set_app_bg_attr("_sse_payload_jp_open", jp_open)

    announce_fn = _get_app_bg_attr("_announce_frame", _announce_frame)
    announce_fn(announcer1, _sse_payload_cache, 1)


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
