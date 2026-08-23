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
        if sym in current_map:
            c_item = current_map[sym].copy()
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
                    c_item[k] = t_item[k]
            for _pk in ("shares", "avg_price", "portfolio_value", "portfolio_pl", "avg_fx_rate"):
                c_item.pop(_pk, None)
        else:
            c_item = copy.deepcopy(t_item)
            for _pk in ("shares", "avg_price", "portfolio_value", "portfolio_pl", "avg_fx_rate"):
                c_item.pop(_pk, None)

        prev_market_state = c_item.get("market_state")
        prev_price = c_item.get("price")
        c_item["market_state"] = "REGULAR" if is_open else "CLOSED"

        target_price_val = t_item.get("price")
        if target_price_val is not None and target_price_val not in ("--", ""):
            try:
                target_price = float(target_price_val)
                target_change = float(t_item.get("change") or 0.0)
                previous_close = target_price - target_change

                if not math.isfinite(target_price) or not math.isfinite(previous_close):
                    raise ValueError("non-finite price from data source")

                current_price = float(c_item.get("price") or target_price)
                diff = target_price - current_price
                step = diff * 0.25

                if is_open and _get_simulate_fluctuation() and random.random() < 0.25:  # nosec B311
                    volatility = 0.0002
                    step += target_price * random.uniform(-volatility, volatility)  # nosec B311

                new_price = current_price + step
                new_price = max(target_price * 0.99, min(target_price * 1.01, new_price))

                is_jpy = (c_item.get("currency") == "JPY") or (sym.endswith(".T"))
                decimals = 2 if is_jpy else 4

                c_item["price"] = round(new_price, decimals)
                if previous_close != 0:
                    new_change = new_price - previous_close
                    new_change_percent = (new_change / previous_close) * 100
                    c_item["change"] = round(new_change, decimals)
                    c_item["change_percent"] = round(new_change_percent, 2)
            except (ValueError, TypeError):
                pass

        if c_item.get("price") != prev_price or c_item.get("market_state") != prev_market_state:
            c_item["snapshot_ts_ms"] = now_ms

        new_current.append(c_item)

    return new_current


def _fluctuate_indices(indices_dict: dict[str, Any], us_open: bool, jp_open: bool) -> None:
    """開場ステータスに応じて主要指数の価格を微小変動させる"""
    for key, info in indices_dict.items():
        if not isinstance(info, dict) or "price" not in info:
            continue
        price_val = info.get("price")
        try:
            price = normalize_optional_number(price_val)
            change = normalize_optional_number(info.get("change"), allow_negative=True) or 0.0
        except (ValueError, TypeError):
            continue
        if price is None:
            continue

        if not math.isfinite(price) or not math.isfinite(change):
            continue

        should_fluctuate = (
            (key == "N225" and jp_open)
            or (key in ("DJI", "SP500", "NASDAQ", "VIX") and us_open)
            or (key in ("USDJPY", "EURJPY") and (us_open or jp_open))
        )

        if should_fluctuate and _get_simulate_fluctuation() and random.random() < 0.3:  # nosec B311
            vol = 0.0001
            change_factor = 1.0 + random.uniform(-vol, vol)  # nosec B311
            new_price = price * change_factor
            prev_close = price - change

            if prev_close != 0:
                new_change = new_price - prev_close
                new_percent = (new_change / prev_close) * 100

                if key in ("USDJPY", "EURJPY"):
                    info["price"] = round(new_price, 3)
                    info["change"] = round(new_change, 3)
                else:
                    info["price"] = round(new_price, 2)
                    info["change"] = round(new_change, 2)
                info["percent"] = round(new_percent, 2)


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

            if any((target_us, target_jp, target_idx)):
                new_current_stocks = {
                    "us": _interpolate_and_fluctuate_market(target_us, current_us, us_open, "us"),
                    "jp": _interpolate_and_fluctuate_market(target_jp, current_jp, jp_open, "jp"),
                    "idx": _interpolate_and_fluctuate_market(
                        target_idx, current_idx, idx_open, "idx"
                    ),
                }

                if any_open:
                    _fluctuate_indices(indices_copy, us_open, jp_open)

                with app_state.cache.sse_data_lock:
                    app_state.market.current_stocks_cache = new_current_stocks
                    app_state.market.current_indices_cache = indices_copy

                _invalidate_sse_payload_cache()
                announce_current_market_state()

            if not any_open:
                app_state.execution.shutdown_event.wait(SSE_MARKET_CLOSED_SLEEP)
            else:
                app_state.execution.shutdown_event.wait(SSE_MARKET_OPEN_SLEEP)

        except Exception:
            logger.exception("bg_interpolate_loop error")
            app_state.execution.shutdown_event.wait(1.0)
