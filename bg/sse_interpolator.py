# bg/sse_interpolator.py
"""SSE price interpolation and simulated fluctuation engine."""

from __future__ import annotations

import copy
import logging
import math
import random
import time
from typing import Any

import constants as _const_mod
from app_state import app_state
from bg.common import (
    _get_app_bg_attr,
    _invalidate_sse_payload_cache,
    announce_current_market_state,
)
from constants import (
    SSE_MARKET_CLOSED_SLEEP,
    SSE_MARKET_OPEN_SLEEP,
)
from utils.market_utils import is_market_open as _orig_is_market_open
from utils.normalization import normalize_optional_number

logger = logging.getLogger(__name__)


def _is_market_open(market: str) -> bool:
    target = _get_app_bg_attr("is_market_open", _orig_is_market_open)
    return target(market)


def _get_simulate_fluctuation() -> bool:
    return bool(_get_app_bg_attr("SIMULATE_FLUCTUATION", _const_mod.SIMULATE_FLUCTUATION))


def _interpolate_and_fluctuate_market(
    target_list: list[dict[str, Any]],
    current_list: list[dict[str, Any]],
    is_open: bool,
    market: str,
) -> list[dict[str, Any]]:
    """ターゲットキャッシュから現在キャッシュの価格を補間し、市場オープン時は微小変動を加える。"""
    if not target_list:
        return []

    current_map = {}
    for item in current_list:
        if isinstance(item, dict) and item.get("symbol"):
            current_map[item["symbol"]] = item

    new_current = []
    now_ms = int(time.time() * 1000)

    for t_item in target_list:
        if not isinstance(t_item, dict) or not t_item.get("symbol"):
            continue

        sym = t_item["symbol"]
        non_price_changed = False
        if sym in current_map:
            prev_entry = current_map[sym]
            c_item = prev_entry.copy()
            for k in (
                "name",
                "market",
                "currency",
                "sector",
                "industry",
                "high",
                "low",
                "volume",
                "chart_data",
                "ohlc_data",
            ):
                if k in t_item:
                    if k not in prev_entry or t_item[k] != prev_entry.get(k):
                        non_price_changed = True
                    c_item[k] = t_item[k]
            for _pk in ("shares", "avg_price", "portfolio_value", "portfolio_pl", "avg_fx_rate"):
                c_item.pop(_pk, None)
        else:
            c_item = copy.deepcopy(t_item)
            non_price_changed = True
            for _pk in ("shares", "avg_price", "portfolio_value", "portfolio_pl", "avg_fx_rate"):
                c_item.pop(_pk, None)

        prev_market_state = c_item.get("market_state")
        prev_price = c_item.get("price")
        prev_change = c_item.get("change")
        prev_change_percent = c_item.get("change_percent")
        c_item["market_state"] = "REGULAR" if is_open else "CLOSED"

        target_price = normalize_optional_number(t_item.get("price"))
        if target_price is not None and target_price > 0:
            try:
                _target_change_val = normalize_optional_number(
                    t_item.get("change"), allow_negative=True
                )
                target_change = _target_change_val if _target_change_val is not None else 0.0
                _raw_prev_close = normalize_optional_number(t_item.get("previous_close"))
                previous_close = (
                    _raw_prev_close
                    if _raw_prev_close is not None and _raw_prev_close > 0
                    else target_price - target_change
                )

                if not math.isfinite(target_price) or not math.isfinite(previous_close):
                    raise ValueError("non-finite price from data source")

                current_price_val = normalize_optional_number(c_item.get("price"))
                current_price = (
                    current_price_val
                    if current_price_val is not None and current_price_val > 0
                    else target_price
                )
                diff = target_price - current_price
                step = diff * 0.25

                if is_open and _get_simulate_fluctuation() and random.random() < 0.25:  # nosec B311
                    volatility = 0.0002
                    step += target_price * random.uniform(-volatility, volatility)  # nosec B311

                new_price = current_price + step
                new_price = max(target_price * 0.99, min(target_price * 1.01, new_price))

                is_jpy = (c_item.get("currency") == "JPY") or (sym.endswith(".T"))
                decimals = 2 if is_jpy else 4

                # Derive change from the price actually published. Computing
                # it from the unrounded interpolation made ``price - prev_close``
                # disagree with ``change`` by up to one display unit (e.g. JPY
                # prices are rounded to 2 decimals before reaching the client).
                published_price = round(new_price, decimals)
                c_item["price"] = published_price
                if previous_close > 0 and math.isfinite(previous_close):
                    new_change = published_price - previous_close
                    new_change_percent = (new_change / previous_close) * 100
                    c_item["change"] = round(new_change, decimals)
                    c_item["change_percent"] = round(new_change_percent, 2)
                else:
                    c_item["change"] = round(target_change, decimals)
                    _raw_pct_val = normalize_optional_number(
                        t_item.get("change_percent"), allow_negative=True
                    )
                    raw_pct = _raw_pct_val if _raw_pct_val is not None else 0.0
                    c_item["change_percent"] = round(raw_pct, 2)
            except (ValueError, TypeError):
                pass

        if (
            non_price_changed
            or c_item.get("price") != prev_price
            or c_item.get("market_state") != prev_market_state
            or c_item.get("change") != prev_change
            or c_item.get("change_percent") != prev_change_percent
        ):
            c_item["snapshot_ts_ms"] = now_ms

        new_current.append(c_item)

    return new_current


def _fluctuate_indices(
    indices_dict: dict[str, Any],
    us_open: bool,
    jp_open: bool,
    target_indices: dict[str, Any] | None = None,
) -> None:
    """開場ステータスに応じて主要指数の価格を微小変動させる"""
    for key, info in indices_dict.items():
        if not isinstance(info, dict) or "price" not in info:
            continue
        price_val = info.get("price")
        try:
            price = normalize_optional_number(price_val)
            _change_val = normalize_optional_number(info.get("change"), allow_negative=True)
            change = _change_val if _change_val is not None else 0.0
        except (ValueError, TypeError):
            continue
        if price is None:
            continue

        if not math.isfinite(price) or not math.isfinite(change):
            continue

        target_price = price
        if isinstance(target_indices, dict):
            t_info = target_indices.get(key)
            if isinstance(t_info, dict):
                try:
                    t_price = normalize_optional_number(t_info.get("price"))
                except (ValueError, TypeError):
                    t_price = None
                if t_price is not None and math.isfinite(t_price) and t_price > 0:
                    target_price = t_price
                    _t_change = normalize_optional_number(
                        t_info.get("change"), allow_negative=True
                    )
                    change = (
                        _t_change if (_t_change is not None and math.isfinite(_t_change)) else 0.0
                    )

        prev_close = target_price - change if target_indices is not None else price - change
        if price <= 0 or not math.isfinite(target_price) or target_price <= 0:
            continue

        should_fluctuate = (
            (key == "N225" and jp_open)
            or (key in ("DJI", "SP500", "NASDAQ", "VIX") and us_open)
            or (key in ("USDJPY", "EURJPY") and (us_open or jp_open))
        )

        if should_fluctuate and _get_simulate_fluctuation() and random.random() < 0.3:  # nosec B311
            vol = 0.0001
            change_factor = 1.0 + random.uniform(-vol, vol)  # nosec B311
            diff_step = (target_price - price) * 0.25
            new_price = price * change_factor + diff_step
            new_price = max(target_price * 0.99, min(target_price * 1.01, new_price))

            is_fx = key in ("USDJPY", "EURJPY")
            published_price = round(new_price, 3 if is_fx else 2)
            if prev_close > 0 and math.isfinite(prev_close):
                # Change must be consistent with the rounded price that is
                # actually published (same rule as the stock interpolator).
                new_change = published_price - prev_close
                new_percent = (new_change / prev_close) * 100

                info["price"] = published_price
                info["change"] = round(new_change, 3 if is_fx else 2)
                info["percent"] = round(new_percent, 2)
            else:
                info["price"] = published_price


def bg_interpolate_loop() -> None:
    """全銘柄の現在値を目標値へ補間し、リアルタイム風の価格変動を模擬しながらSSE配信する"""
    target = _get_app_bg_attr("bg_interpolate_loop", None)
    if target is not None and target is not bg_interpolate_loop:
        return target()

    app_state.execution.shutdown_event.wait(2.0)

    while not app_state.execution.shutdown_event.is_set():
        try:
            listener_count = app_state.sse_announcer_mode1.listener_count()
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(5.0)
                continue

            us_open = _is_market_open("us")
            jp_open = _is_market_open("jp")
            idx_open = _is_market_open("idx")
            any_open = us_open or jp_open or idx_open

            with app_state.cache.sse_data_lock:
                target_us = list(app_state.market.target_stocks_cache.get("us", []))
                target_jp = list(app_state.market.target_stocks_cache.get("jp", []))
                target_idx = list(app_state.market.target_stocks_cache.get("idx", []))

                current_us = list(app_state.market.current_stocks_cache.get("us", []))
                current_jp = list(app_state.market.current_stocks_cache.get("jp", []))
                current_idx = list(app_state.market.current_stocks_cache.get("idx", []))

                indices_copy = copy.deepcopy(app_state.market.current_indices_cache)
                target_indices_snapshot = (
                    copy.deepcopy(app_state.market.target_indices_cache)
                    if isinstance(getattr(app_state.market, "target_indices_cache", None), dict)
                    else None
                )
                if not target_indices_snapshot and indices_copy:
                    app_state.market.target_indices_cache = copy.deepcopy(indices_copy)
                    target_indices_snapshot = copy.deepcopy(indices_copy)
                # M25 (2026-11-07 review): snapshot the exact container objects
                # read above. The compute below runs WITHOUT the lock; if a
                # concurrent publisher (sync_all_stocks_now / the reset
                # endpoint / _update_indices_data) replaces them meanwhile,
                # the write-back below silently reverts that fresher payload
                # and announces the stale one to every SSE listener.
                owned_target_us = app_state.market.target_stocks_cache.get("us")
                owned_target_jp = app_state.market.target_stocks_cache.get("jp")
                owned_target_idx = app_state.market.target_stocks_cache.get("idx")
                owned_us = app_state.market.current_stocks_cache.get("us")
                owned_jp = app_state.market.current_stocks_cache.get("jp")
                owned_idx = app_state.market.current_stocks_cache.get("idx")
                owned_indices = app_state.market.current_indices_cache

            if any((target_us, target_jp, target_idx, indices_copy)):
                new_current_stocks = {
                    "us": _interpolate_and_fluctuate_market(target_us, current_us, us_open, "us"),
                    "jp": _interpolate_and_fluctuate_market(target_jp, current_jp, jp_open, "jp"),
                    "idx": _interpolate_and_fluctuate_market(
                        target_idx, current_idx, idx_open, "idx"
                    ),
                }

                fluctuated_indices = indices_copy
                if any_open:
                    # Fluctuate a private copy so indices_copy stays a pristine
                    # snapshot of what was read — the write-back compares it
                    # against the live cache to detect concurrent in-place
                    # publishers (_update_indices_data) as well as whole-dict
                    # replacements (sync publish / reset).
                    fluctuated_indices = copy.deepcopy(indices_copy)
                    _fluctuate_indices(
                        fluctuated_indices,
                        us_open,
                        jp_open,
                        target_indices=target_indices_snapshot,
                    )

                with app_state.cache.sse_data_lock:
                    still_ours = (
                        app_state.market.target_stocks_cache.get("us") is owned_target_us
                        and app_state.market.target_stocks_cache.get("jp") is owned_target_jp
                        and app_state.market.target_stocks_cache.get("idx") is owned_target_idx
                        and app_state.market.current_stocks_cache.get("us") is owned_us
                        and app_state.market.current_stocks_cache.get("jp") is owned_jp
                        and app_state.market.current_stocks_cache.get("idx") is owned_idx
                        and app_state.market.current_indices_cache is owned_indices
                        and app_state.market.current_indices_cache == indices_copy
                    )
                    if still_ours:
                        # No concurrent publisher touched the caches during the
                        # compute window: publish the interpolated snapshot.
                        app_state.market.current_stocks_cache = new_current_stocks
                        app_state.market.current_indices_cache = fluctuated_indices

                if still_ours:
                    _invalidate_sse_payload_cache()
                    announce_current_market_state()

            if not any_open:
                app_state.execution.shutdown_event.wait(SSE_MARKET_CLOSED_SLEEP)
            else:
                app_state.execution.shutdown_event.wait(SSE_MARKET_OPEN_SLEEP)

        except Exception:
            logger.exception("bg_interpolate_loop error")
            app_state.execution.shutdown_event.wait(1.0)
