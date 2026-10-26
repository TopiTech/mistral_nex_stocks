# bg/sync/process.py
"""Cache preparation, merge/publish, indices update, and invalid-symbol removal.

Pure move from `bg.sync_worker`. `load_user_stocks` /
`ensure_stock_placeholder_in_caches` and the cross-module
`schedule_sync_all_stocks_now` call are resolved at call time through the
`bg.sync_worker` facade so `patch("bg.sync_worker.X")` keeps working in
tests. Lock order (`invalid_symbol_lock` -> `user_stocks_lock`, persist
outside locks) is unchanged.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any

from requests.exceptions import RequestException

from app_state import app_state
from bg.common import announce_real_market_state
from bg.sync import _shim
from bg.sync import state as _state
from bg.sync.fetch import _is_batch_result_invalid, fetch_index_data
from bg.sync.state import _get_app_bg_attr, _get_sync_generation
from route_helpers import (
    invalidate_stock_caches,
    remove_stock_from_caches,
)
from services.stock_provider import YFTickerMissingError
from utils.market_utils import is_market_open
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
)
from utils.storage import load_user_stocks, save_user_stocks

logger = logging.getLogger("bg.sync_worker")



def _warm_payload_cache_from_disk() -> None:
    """Load cached stock payloads from disk into target cache on cold start or follower sync."""
    try:
        try:
            cached_indices = app_state.payload_disk_cache.get("indices_cache", ignore_ttl=True)
            if isinstance(cached_indices, dict) and cached_indices:
                with app_state.cache.sse_data_lock:
                    app_state.market.current_indices_cache.update(cached_indices)
                logger.info("Warmed indices cache from disk cache")
        except Exception as exc:
            logger.debug("Failed to warm indices cache from disk: %s", exc)

        load_fn = _get_app_bg_attr("load_user_stocks", load_user_stocks)
        load_fn(force=True)
        warmed = 0
        for market in ("us", "jp", "idx"):
            user_map = {}
            with app_state.market.user_stocks_lock:
                if market == "us":
                    user_map = dict(app_state.market.user_us)
                elif market == "jp":
                    user_map = dict(app_state.market.user_jp)
                elif market == "idx":
                    user_map = dict(app_state.market.user_idx)

            symbols_to_warm = set(user_map.keys())
            for symbol in _default_stock_names(market):
                symbols_to_warm.add(symbol)

            for symbol in symbols_to_warm:
                key = f"payload_{symbol}_{market}"
                cache_file = app_state.payload_disk_cache._entry_path(key)

                try:
                    mtime = os.path.getmtime(cache_file) if cache_file.exists() else 0.0
                except OSError:
                    mtime = 0.0

                if mtime != 0.0 and _state._last_loaded_mtimes.get(key) == mtime:
                    continue

                cached = app_state.payload_disk_cache.get(key, ignore_ttl=True)
                if cached and isinstance(cached, dict) and cached.get("symbol"):
                    with app_state.cache.sse_data_lock:
                        target_list = app_state.market.target_stocks_cache.get(market, [])
                        found = False
                        for i, s in enumerate(target_list):
                            if isinstance(s, dict) and s.get("symbol") == symbol:
                                target_list[i] = cached
                                found = True
                                break
                        if not found:
                            target_list.append(cached)
                        app_state.market.target_stocks_cache[market] = target_list

                    _state._last_loaded_mtimes[key] = mtime
                    warmed += 1
        if warmed > 0:
            logger.info(
                "Warmed/Updated %d stock payloads from disk cache (including defaults)", warmed
            )
            current_empty = not any(
                app_state.market.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
            )
            if current_empty:
                with app_state.cache.sse_data_lock:
                    app_state.market.current_stocks_cache = copy.deepcopy(
                        app_state.market.target_stocks_cache
                    )
    except (OSError, TypeError, AttributeError, RuntimeError) as exc:
        logger.debug("Disk cache warm-up failed (non-critical): %s", exc)


def _prepare_sync_items(
    force_load: bool = True, force_fetch: bool = False
) -> list[tuple[str, str, str]]:
    """Loads user stocks and default stocks, and prepares the items list for batch fetch."""
    if force_load:
        _shim().load_user_stocks(force=True)

    for market in ("us", "jp", "idx"):
        with app_state.market.user_stocks_lock:
            if market == "us":
                user_set = set(app_state.market.user_us.keys())
            elif market == "jp":
                user_set = set(app_state.market.user_jp.keys())
            else:
                user_set = set(app_state.market.user_idx.keys())
        for symbol, name in _default_stock_names(market).items():
            if symbol not in user_set:
                _shim().ensure_stock_placeholder_in_caches(symbol, name, market)

    us_open = is_market_open("us")
    jp_open = is_market_open("jp")

    def _is_cache_incomplete(market: str) -> bool:
        with app_state.cache.sse_data_lock:
            target_list = list(
                app_state.market.target_stocks_cache.get(market, [])
                if isinstance(app_state.market.target_stocks_cache, dict)
                else []
            )
        cached_symbols = {
            s.get("symbol")
            for s in target_list
            if isinstance(s, dict) and s.get("symbol") and s.get("price") not in (None, "--", "")
        }
        with app_state.market.user_stocks_lock:
            if market == "us":
                user_set = set(app_state.market.user_us.keys())
            elif market == "jp":
                user_set = set(app_state.market.user_jp.keys())
            else:
                user_set = set(app_state.market.user_idx.keys())
        required_symbols = set(_default_stock_names(market).keys()) | user_set
        return not required_symbols.issubset(cached_symbols)

    fetch_us = us_open or _is_cache_incomplete("us") or force_fetch
    fetch_jp = jp_open or _is_cache_incomplete("jp") or force_fetch

    def _placeholder_symbols(market: str) -> set[str]:
        with app_state.cache.sse_data_lock:
            target_list = list(
                app_state.market.target_stocks_cache.get(market, [])
                if isinstance(app_state.market.target_stocks_cache, dict)
                else []
            )
        return {
            str(s["symbol"])
            for s in target_list
            if isinstance(s, dict) and s.get("symbol") and s.get("price") in (None, "--", "")
        }

    us_placeholders = _placeholder_symbols("us")
    jp_placeholders = _placeholder_symbols("jp")

    items = []
    with app_state.market.user_stocks_lock:
        user_us_snapshot = dict(app_state.market.user_us)
        user_jp_snapshot = dict(app_state.market.user_jp)
        user_idx_snapshot = dict(app_state.market.user_idx)

    user_us_set = set(user_us_snapshot.keys())
    user_jp_set = set(user_jp_snapshot.keys())
    user_idx_set = set(user_idx_snapshot.keys())

    if fetch_us:
        for s, n in user_us_snapshot.items():
            items.append((s, n, "us"))
    else:
        for s, n in user_us_snapshot.items():
            if s in us_placeholders:
                items.append((s, n, "us"))
    if fetch_jp:
        for s, n in user_jp_snapshot.items():
            items.append((s, n, "jp"))
    else:
        for s, n in user_jp_snapshot.items():
            if s in jp_placeholders:
                items.append((s, n, "jp"))
    for s, n in user_idx_snapshot.items():
        items.append((s, n, "idx"))

    for market_name, user_set in (
        ("us", user_us_set),
        ("jp", user_jp_set),
        ("idx", user_idx_set),
    ):
        should_fetch = (
            fetch_us if market_name == "us" else (fetch_jp if market_name == "jp" else True)
        )
        m_placeholders = (
            us_placeholders
            if market_name == "us"
            else (jp_placeholders if market_name == "jp" else set())
        )

        for symbol, name in _default_stock_names(market_name).items():
            if symbol not in user_set and (should_fetch or symbol in m_placeholders):
                items.append((symbol, name, market_name))
    return items


@dataclass(frozen=True)
class ProcessedStocks:
    """Explicit result contract for ``_process_fetched_stocks`` (M-4).

    ``us`` / ``jp`` / ``idx`` hold the per-market merged cache rows (freshly
    fetched rows merged over preserved rows). ``is_empty`` makes the
    "all-dead -> preserve" guard explicit instead of relying on the implicit
    truthiness of a bare 3-tuple: publishing is skipped only when *every* market
    is empty. Partial success (e.g. US freshly updated while JP is preserved) is
    intentional -- each market is updated independently in ``merge_cache``.
    """

    us: list[dict[str, Any]] = field(default_factory=list)
    jp: list[dict[str, Any]] = field(default_factory=list)
    idx: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when no market holds any rows (fresh or preserved)."""
        return not (self.us or self.jp or self.idx)


def _process_fetched_stocks(
    fetched_items: list[dict[str, Any] | None],
    sync_generation: int | None = None,
) -> ProcessedStocks:
    """Splits fetched items into US, JP, and IDX results and updates caches."""
    us_res, jp_res, idx_res = [], [], []
    for item in fetched_items:
        if not isinstance(item, dict) or not item:
            continue
        m = item.get("market")
        if m == "us":
            us_res.append(item)
        elif m == "jp":
            jp_res.append(item)
        else:
            idx_res.append(item)

    with app_state.cache.sse_data_lock:
        if sync_generation is not None:
            with app_state.market.is_syncing_lock:
                current_sync_gen = _get_sync_generation()
                if current_sync_gen != 0 and sync_generation != current_sync_gen:
                    logger.info(
                        "Discarding stale stock sync generation %s (current: %s)",
                        sync_generation,
                        current_sync_gen,
                    )
                    return ProcessedStocks()
        prev_us = (
            app_state.market.target_stocks_cache.get("us", [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )
        prev_jp = (
            app_state.market.target_stocks_cache.get("jp", [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )
        prev_idx = (
            app_state.market.target_stocks_cache.get("idx", [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )

        def merge_cache(prev_list: list[Any], res_list: list[Any]) -> list[Any]:
            if not res_list:
                return prev_list
            res_dict = {item["symbol"]: item for item in res_list if item and "symbol" in item}
            merged = []
            seen = set()
            for item in prev_list:
                if not item or "symbol" not in item:
                    continue
                sym = item["symbol"]
                if sym in res_dict:
                    merged.append(res_dict[sym])
                    seen.add(sym)
                else:
                    merged.append(item)
            for item in res_list:
                if not item or "symbol" not in item:
                    continue
                sym = item["symbol"]
                if sym not in seen:
                    merged.append(item)
            return merged

        new_us = merge_cache(prev_us, us_res)
        new_jp = merge_cache(prev_jp, jp_res)
        new_idx = merge_cache(prev_idx, idx_res)

        app_state.market.target_stocks_cache = {"us": new_us, "jp": new_jp, "idx": new_idx}

        for market_rows in (new_us, new_jp, new_idx):
            for row in market_rows:
                if not isinstance(row, dict):
                    continue
                sym = row.get("symbol")
                if not sym:
                    continue
                prev_close = row.get("previous_close")
                if prev_close is None:
                    price = row.get("price")
                    change = row.get("change")
                    if price is not None and change is not None:
                        try:
                            prev_close = float(price) - float(change)
                        except (TypeError, ValueError):
                            prev_close = None
                app_state.market.update_previous_close_cache(sym, prev_close)

        announce_real_market_state()
        current_empty = not any(
            app_state.market.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if current_empty:
            app_state.market.current_stocks_cache = copy.deepcopy(
                app_state.market.target_stocks_cache
            )
    return ProcessedStocks(us=new_us, jp=new_jp, idx=new_idx)


def _update_indices_data(
    idx_res: list[dict[str, Any]],
    us_res: list[dict[str, Any]],
    jp_res: list[dict[str, Any]],
) -> None:
    """Updates the current indices cache and market status cache with fresh values."""
    header_mapping = {
        "^N225": "N225",
        "^DJI": "DJI",
        "USDJPY=X": "USDJPY",
        "JPY=X": "USDJPY",
        "EURJPY=X": "EURJPY",
        "^IXIC": "NASDAQ",
        "^GSPC": "SP500",
        "^VIX": "VIX",
    }
    new_header_data = {}
    for item in idx_res + us_res + jp_res:
        if not isinstance(item, dict) or not item:
            continue
        sym = item.get("symbol")
        if sym in header_mapping:
            h_key = header_mapping[sym]
            new_header_data[h_key] = {
                "price": item.get("price"),
                "change": item.get("change"),
                "percent": item.get("change_percent") or item.get("percent"),
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "volume": item.get("volume"),
                "market_state": item.get("market_state", "UNKNOWN"),
                "market": item.get("market"),
            }

    critical_indices = {
        "N225": "^N225",
        "DJI": "^DJI",
        "USDJPY": "USDJPY=X",
        "EURJPY": "EURJPY=X",
        "VIX": "^VIX",
        "NASDAQ": "^IXIC",
        "SP500": "^GSPC",
    }
    _safety_net_budget = 2
    for key, sym in critical_indices.items():
        if key not in new_header_data or new_header_data[key].get("price") == "--":
            if app_state.market.is_yf_rate_limited():
                continue
            if _safety_net_budget <= 0:
                logger.debug("Safety net budget exhausted for this cycle; deferring %s", key)
                continue
            _safety_net_budget -= 1
            try:
                logger.debug(
                    "Safety net trigger: fetching %s (%s) individually",
                    key,
                    sym,
                )
                fetch_fn = _get_app_bg_attr("fetch_index_data", fetch_index_data)
                res = fetch_fn(key, sym)
                if res and res[1]:
                    new_header_data[key] = res[1]
            except (
                RequestException,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                YFTickerMissingError,
            ) as safety_exc:
                logger.warning("Safety net failed for %s: %s", key, safety_exc)
    if new_header_data:
        with app_state.cache.sse_data_lock:
            app_state.market.current_indices_cache.update(new_header_data)

        try:
            app_state.payload_disk_cache.set(
                "indices_cache", app_state.market.current_indices_cache
            )
        except Exception as e:
            logger.debug("Failed to cache current_indices_cache to disk: %s", e)

        with app_state.market.market_status_lock:
            if "N225" in new_header_data:
                app_state.market.market_status_cache["jp"] = new_header_data["N225"].get(
                    "market_state"
                )
            if "SP500" in new_header_data:
                st = new_header_data["SP500"].get("market_state")
                app_state.market.market_status_cache["us"] = st
                app_state.market.market_status_cache["idx"] = st

        if "USDJPY" in new_header_data:
            rate_dict = new_header_data["USDJPY"]
            price_val: object = rate_dict.get("price")
            if price_val not in (None, "--", ""):
                try:
                    rate_float = float(price_val)  # type: ignore[arg-type]
                    if math.isfinite(rate_float) and rate_float > 0:
                        with app_state.market.user_stocks_lock:
                            old_rate = getattr(app_state.market, "last_usdjpy_rate", None)
                            old_saved_ts = getattr(
                                app_state.market, "last_usdjpy_persisted_ts", 0.0
                            )
                            time_mod = _get_app_bg_attr("time", time)
                            now_ts = float(time_mod.time())
                            app_state.market.last_usdjpy_rate = rate_float
                            app_state.market.last_usdjpy_rate_ts = now_ts

                            should_persist = (
                                old_rate is None
                                or abs(rate_float - old_rate) >= 0.01
                                or (now_ts - old_saved_ts) >= 300.0
                            )
                        # C-03: persist outside user_stocks_lock so slow disk
                        # I/O (encrypt + fsync) never blocks SSE/readers.
                        if should_persist:
                            try:
                                save_fn = _get_app_bg_attr("save_user_stocks", save_user_stocks)
                                save_fn()
                            except Exception as persist_exc:
                                logger.warning(
                                    "Failed to persist fresh USDJPY state: %s",
                                    persist_exc,
                                )
                            else:
                                with app_state.market.user_stocks_lock:
                                    app_state.market.last_usdjpy_persisted_ts = now_ts
                except (ValueError, TypeError) as save_exc:
                    logger.debug("Failed to parse USDJPY rate: %s", save_exc)


def _auto_remove_invalid_symbols(
    items: list[tuple[str, str, str]],
    fetched_items: list[dict[str, Any] | None],
) -> None:
    """Track consecutive fetch failures for user-added symbols and auto-remove
    those that exceed the removal threshold."""
    if not items or not fetched_items or len(items) != len(fetched_items):
        return
    if app_state.market.is_yf_rate_limited():
        logger.debug("yfinance rate limited; skipping invalid symbol cleanup.")
        return
    if all(f is None for f in fetched_items):
        logger.debug("Entire batch fetch failed; skipping invalid symbol cleanup.")
        return

    threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD
    default_symbols: set[str] = set()
    for m in ("us", "jp", "idx"):
        default_symbols.update(_default_stock_names(m).keys())

    removed_any = False
    with app_state.market.invalid_symbol_lock:
        for (symbol, _name_or_dict, market), result in zip(items, fetched_items, strict=True):
            if symbol in default_symbols or market == "idx":
                continue
            if _is_batch_result_invalid(result):
                app_state.market.record_symbol_fetch_result(symbol, failed=True)
            else:
                app_state.market.record_symbol_fetch_result(symbol, failed=False)

    symbols_to_remove = app_state.market.get_symbols_to_remove(threshold)
    if not symbols_to_remove:
        return

    removed: list[tuple[str, str, Any, int]] = []
    persist_error: Exception | None = None
    with app_state.market.invalid_symbol_lock, app_state.market.user_stocks_lock:
        get_cont_fn = _get_app_bg_attr("_get_stock_container", _get_stock_container)
        for symbol in symbols_to_remove:
            if app_state.market.invalid_symbol_streak.get(symbol, 0) < threshold:
                continue
            for market in ("us", "jp"):
                container = get_cont_fn(market)
                if container and symbol in container:
                    original_stock = copy.deepcopy(container[symbol])
                    del container[symbol]
                    streak = app_state.market.invalid_symbol_streak.pop(symbol, 0)
                    logger.warning(
                        "Auto-removed invalid symbol %s from %s (consecutive failures: %d)",
                        symbol,
                        market,
                        streak,
                    )
                    removed.append((symbol, market, original_stock, streak))
                    removed_any = True
                    break

    # C-03: persist outside the locks so slow disk I/O (encrypt + fsync)
    # never blocks SSE/readers. Roll back under the same lock order on
    # failure, preserving the previous in-memory state.
    if removed_any:
        try:
            save_fn = _get_app_bg_attr("save_user_stocks", save_user_stocks)
            save_fn()
        except Exception as exc:
            with app_state.market.invalid_symbol_lock, app_state.market.user_stocks_lock:
                get_cont_fn = _get_app_bg_attr("_get_stock_container", _get_stock_container)
                for symbol, market, original_stock, streak in removed:
                    container = get_cont_fn(market)
                    if container is not None:
                        container[symbol] = original_stock
                    app_state.market.invalid_symbol_streak[symbol] = streak
            persist_error = exc

    if persist_error is not None:
        logger.error(
            "Failed to persist auto-removed invalid symbols; restored %s: %s",
            [(symbol, market) for symbol, market, _stock, _streak in removed],
            persist_error,
        )
        schedule_fn = _get_app_bg_attr("schedule_sync_all_stocks_now", None)
        if schedule_fn is None:
            schedule_fn = _shim().schedule_sync_all_stocks_now
        schedule_fn()
        return

    if removed_any:
        from services.realtime_engine import realtime_market_engine

        for symbol, market, _stock, _streak in removed:
            invalidate_stock_caches(symbol)
            remove_stock_from_caches(symbol, market)
            realtime_market_engine.unregister_symbol(symbol, market)
        announce_real_market_state()
        _shim().schedule_sync_all_stocks_now()
