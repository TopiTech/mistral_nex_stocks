# bg/sync_worker.py
"""Stock data fetching, batch processing, background loop, and synchronization."""

from __future__ import annotations

import concurrent.futures
import copy
import logging
import math
import os
import queue
import sys
import threading
import time
from typing import Any

import pandas as pd
from requests.exceptions import RequestException

from app_state import app_state
from bg.common import (
    _invalidate_sse_payload_cache,
    announce_current_market_state,
    announce_real_market_state,
)
from bg.leader_election import bg_leader_election_loop, is_leader
from bg.sse_interpolator import bg_interpolate_loop
from constants import (
    SSE_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP,
    SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_NO_LISTENER_SLEEP,
)
from route_helpers import (
    ensure_stock_placeholder_in_caches,
    invalidate_stock_caches,
    remove_stock_from_caches,
)
from services.stock_provider import YFTickerMissingError
from utils.http_utils import parse_retry_after
from utils.market_utils import acquire_yfinance_slot, is_market_open
from utils.normalization import (
    _fmt,
    _fmt_vol,
    normalize_history_frame,
)
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    _strip_portfolio_fields,
    build_stock_payload,
)
from utils.storage import load_user_stocks, save_user_stocks

logger = logging.getLogger(__name__)


def _get_logger() -> Any:
    import sys
    app_bg_mod = sys.modules.get("app_bg")
    if app_bg_mod is not None and "logger" in app_bg_mod.__dict__:
        return app_bg_mod.__dict__["logger"]
    return logging.getLogger("app_bg")

SYNC_STALE_TIMEOUT_SEC: float = 120.0
SYNC_STALE_LOCK_WAIT_SEC: float = 15.0

_sync_start_time: float = 0.0
_sync_generation: int = 0
_last_loaded_mtimes: dict[str, float] = {}
_sync_execution_lock = threading.Lock()
_BATCH_INVALID_MARKER = "__INVALID_SYMBOL__"


def _get_app_bg_attr(name: str, fallback: Any) -> Any:
    mod = sys.modules.get("app_bg")
    if mod is not None and name in mod.__dict__:
        target = mod.__dict__[name]
        return target
    return fallback


def _invalid_tuple_if_applicable(symbol: str, exc: Exception) -> Any:
    from services.stock_provider import _is_yfinance_invalid_symbol_error

    if _is_yfinance_invalid_symbol_error(exc):
        return (_BATCH_INVALID_MARKER, symbol)
    return None


def _is_batch_result_invalid(result: Any) -> bool:
    return isinstance(result, tuple) and len(result) == 2 and result[0] == _BATCH_INVALID_MARKER


def _get_sync_start_time() -> float:
    return float(_get_app_bg_attr("_sync_start_time", _sync_start_time))


def _set_sync_start_time(val: float) -> None:
    global _sync_start_time
    _sync_start_time = val
    mod = sys.modules.get("app_bg")
    if mod is not None:
        mod.__dict__["_sync_start_time"] = val


def _get_sync_execution_lock() -> Any:
    return _get_app_bg_attr("_sync_execution_lock", _sync_execution_lock)


def _recover_stale_sync_state_if_needed() -> bool:
    target = _get_app_bg_attr("_recover_stale_sync_state_if_needed", None)
    if target is not None and target is not _recover_stale_sync_state_if_needed:
        return target()

    with app_state.market.is_syncing_lock:
        if not app_state.market.is_syncing:
            return False
        start_time = _get_sync_start_time()
        elapsed = time.time() - start_time if start_time > 0 else 0.0
        if elapsed <= SYNC_STALE_TIMEOUT_SEC:
            return False
        logger.critical(
            "Sync is stale (running %.0fs, threshold %.0fs). Resetting sync state "
            "so scheduling, the UI, and lock takeovers can recover.",
            elapsed,
            SYNC_STALE_TIMEOUT_SEC,
        )
        app_state.market.is_syncing = False
        _set_sync_start_time(0.0)
        return True


def _handle_yfinance_error(exc: Exception, symbol: str = "") -> None:
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    exc_str_lower = str(exc).lower()

    if (
        status_code in (401, 429, 402, 439)
        or "too many requests" in exc_str_lower
        or "payment required" in exc_str_lower
        or "invalid crumb" in exc_str_lower
        or "unauthorized" in exc_str_lower
    ):
        backoff_time = app_state.market.mark_yf_429(retry_after=parse_retry_after(exc))
        logger.warning(
            "yfinance rate limit / block hit (%s) for symbol=%s; backing off for %d seconds.",
            status_code if status_code else "unknown",
            symbol,
            int(backoff_time),
        )
    elif "timeout" in exc_str_lower:
        logger.debug("yfinance timeout detected. symbol=%s", symbol)
    else:
        with app_state.market.yfinance_lock:
            app_state.market.yfinance_429_streak = 0


def fetch_stock(
    symbol: str,
    name_or_dict: Any,
    market: str,
    snapshot_ts_ms: int | None = None,
) -> dict[str, Any] | tuple[str, str] | None:
    """単一銘柄のデータを取得する"""
    if not acquire_yfinance_slot():
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for symbol=%s",
                symbol,
            )
        return None

    try:
        period = "3mo" if is_market_open(market) else "1mo"
        hist = pd.DataFrame()
        try:
            hist = app_state.stock_provider.get_history(symbol, period=period)
        except (
            RequestException,
            ValueError,
            KeyError,
            IndexError,
            OSError,
            YFTickerMissingError,
        ) as e:
            logger.debug("Fetch failed for %s with period %s: %s", symbol, period, e)
            invalid = _invalid_tuple_if_applicable(symbol, e)
            if invalid is not None:
                return invalid

        if hist.empty or "Close" not in hist.columns or len(hist) < 1:
            logger.warning(
                "No valid history data found for %s after period %s",
                symbol,
                period,
            )
            return None

        build_fn = _get_app_bg_attr("build_stock_payload", build_stock_payload)
        payload = build_fn(
            symbol, name_or_dict, market, hist, snapshot_ts_ms=snapshot_ts_ms
        )
        if isinstance(payload, dict):
            try:
                app_state.payload_disk_cache.set(
                    f"payload_{symbol}_{market}", _strip_portfolio_fields(payload)
                )
            except (OSError, TypeError):
                logger.debug("Failed to cache payload for %s", symbol)
            return payload
        return None
    except (
        RequestException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OSError,
        YFTickerMissingError,
    ) as exc:
        _handle_yfinance_error(exc, symbol)
        logger.exception("Stock fetch failed (%s)", symbol)
        invalid = _invalid_tuple_if_applicable(symbol, exc)
        if invalid is not None:
            return invalid
        return None


def extract_batch_history(downloaded: Any, symbol: str, single_symbol: bool = False) -> pd.DataFrame:
    """バッチ取得されたDataFrameから単一銘柄の履歴を抽出"""
    if downloaded is None or getattr(downloaded, "empty", True):
        return pd.DataFrame()
    try:
        if not isinstance(downloaded, pd.DataFrame):
            return pd.DataFrame()

        if isinstance(downloaded.columns, pd.MultiIndex):
            try:
                return normalize_history_frame(downloaded.xs(symbol, axis=1, level=1))
            except (KeyError, IndexError, ValueError):
                pass

            try:
                return normalize_history_frame(downloaded[symbol])
            except (KeyError, IndexError, ValueError):
                pass

            try:
                matching_cols = [
                    col for col in downloaded.columns if isinstance(col, tuple) and symbol in col
                ]
                if matching_cols:
                    extracted = downloaded[matching_cols].copy()
                    extracted.columns = [
                        next(part for part in col if part != symbol) for col in matching_cols
                    ]
                    return normalize_history_frame(extracted)
            except (KeyError, IndexError, TypeError, StopIteration, ValueError):
                pass

            return pd.DataFrame()
        elif single_symbol or ("Close" in downloaded.columns or "close" in downloaded.columns):
            return normalize_history_frame(downloaded)
        else:
            return pd.DataFrame()
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        logger.debug("extract_batch_history error for %s: %s", symbol, exc)
        return pd.DataFrame()


def fetch_stocks_batch(
    items: list[tuple[str, str, str]],
    snapshot_ts_ms: int | None = None,
    lightweight: bool = False,
    period: str = "3mo",
) -> list[Any]:
    """複数銘柄をバッチで取得。"""
    target = _get_app_bg_attr("fetch_stocks_batch", None)
    if target is not None and target is not fetch_stocks_batch:
        return target(items, snapshot_ts_ms=snapshot_ts_ms, lightweight=lightweight, period=period)

    if not items:
        return []

    from utils.disk_cache import is_disk_cache_degraded

    if is_disk_cache_degraded():
        logger.warning("Disk cache is in degraded state; skipping batch stock fetch")
        return [None] * len(items)

    symbols = [item[0] for item in items]
    logger.info("Batch stock fetch starting: count=%d", len(symbols))

    max_batch_size = len(symbols)
    if app_state.market.is_yf_rate_limited():
        max_batch_size = max(5, min(len(symbols), 10))
        if len(symbols) > max_batch_size:
            logger.info(
                "Rate limit active: reducing batch from %d to %d symbols",
                len(symbols),
                max_batch_size,
            )
            symbols = symbols[:max_batch_size]
            items = items[:max_batch_size]

    downloaded = None
    if acquire_yfinance_slot():
        try:
            downloaded = app_state.stock_provider.download_batch(
                symbols, period=period, lightweight=lightweight
            )
        except (RequestException, ValueError, TypeError, KeyError, OSError) as exc:
            _handle_yfinance_error(exc, "batch_fetch")
            logger.warning(
                "Batch fetch failed with exception: %s.",
                exc,
                exc_info=True,
            )
    else:
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for batch fetch."
            )

    if downloaded is None or downloaded.empty:
        logger.warning("Batch fetch completely failed or empty. Preserving previous state.")
        return [None] * len(items)

    results_map: dict[str, Any] = {}
    fallback_items = []
    max_fallbacks = 2

    for symbol, name, market in items:
        payload = None
        if downloaded is not None and not downloaded.empty:
            try:
                extract_fn = _get_app_bg_attr("extract_batch_history", extract_batch_history)
                hist = extract_fn(downloaded, symbol, single_symbol=(len(symbols) == 1))
                if not hist.empty and len(hist) >= 1:
                    build_fn = _get_app_bg_attr("build_stock_payload", build_stock_payload)
                    payload = build_fn(
                        symbol,
                        name,
                        market,
                        hist,
                        snapshot_ts_ms=snapshot_ts_ms,
                        lightweight=lightweight,
                    )
            except (KeyError, IndexError, ValueError, TypeError) as extract_exc:
                logger.debug("Failed to extract %s from batch: %s", symbol, extract_exc)

        if payload is not None:
            results_map[symbol] = payload
        else:
            fallback_items.append((symbol, name, market))

    if lightweight:
        logger.debug("Lightweight mode: skipping all %d fallbacks", len(fallback_items))
        for symbol, _name, _market in fallback_items:
            results_map[symbol] = None
        results = [results_map.get(item[0]) for item in items]
        return results

    if app_state.market.is_yf_rate_limited():
        logger.warning(
            "yfinance rate-limited: skipping %d batch fallbacks.",
            len(fallback_items),
        )
        results = [results_map.get(item[0]) for item in items]
        return results

    to_fetch = fallback_items[:max_fallbacks]
    skipped_items = fallback_items[max_fallbacks:]

    for symbol, _, _ in skipped_items:
        logger.debug("Skipping fallback for %s: limit reached", symbol)
        results_map[symbol] = None

    if to_fetch:
        futures_map: dict[concurrent.futures.Future[Any], str] = {}

        logger.info(
            "Fallback parallel single queries triggered for %d stocks (limit %d)",
            len(to_fetch),
            max_fallbacks,
        )

        for symbol, name, market in to_fetch:
            try:
                fut = app_state.execution.data_executor.submit(
                    fetch_stock, symbol, name, market, snapshot_ts_ms
                )
            except (queue.Full, RuntimeError) as submit_exc:
                logger.warning("Fallback fetch submission skipped for %s: %s", symbol, submit_exc)
                results_map[symbol] = None
                continue
            futures_map[fut] = symbol
        from utils.env_helpers import _env_float

        fallback_timeout = _env_float("MNS_FALLBACK_FETCH_TIMEOUT", 10.0, 1.0, 60.0)
        done, not_done = concurrent.futures.wait(
            futures_map.keys(),
            timeout=fallback_timeout,
        )

        for fut in done:
            symbol = futures_map[fut]
            try:
                payload = fut.result()
                results_map[symbol] = payload
            except (RequestException, ValueError, TypeError, RuntimeError) as exc:
                logger.warning("Parallel fallback fetch failed for %s: %s", symbol, exc)
                results_map[symbol] = _invalid_tuple_if_applicable(symbol, exc)
        for fut in not_done:
            symbol = futures_map[fut]
            fut.cancel()
            logger.warning("Parallel fallback fetch timed out for %s", symbol)
            results_map[symbol] = None

            def _log_late_failure(f: Any, _sym: str = symbol) -> None:
                try:
                    exc = f.exception()
                except Exception as log_exc:
                    exc = log_exc
                if exc is not None:
                    _get_logger().warning("Parallel fallback fetch failed late for %s: %s", _sym, exc)

            fut.add_done_callback(_log_late_failure)

    results = [results_map.get(item[0]) for item in items]
    return results


def fetch_index_data(key: str, symbol: str) -> tuple[str, dict[str, Any]] | None:
    """指数データ取得（シングルピリオド、フォールバック無し）"""
    if not acquire_yfinance_slot():
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for index=%s", key
            )
        return None

    try:
        hist = app_state.stock_provider.get_history(symbol, period="1mo")

        if len(hist) < 2:
            return None

        last_row = hist.iloc[-1]
        prev_close = hist["Close"].iloc[-2]

        price = float(last_row["Close"])
        change = price - float(prev_close)
        pct = (change / float(prev_close) * 100) if prev_close else 0.0

        market_type = "jp" if key == "N225" else "us"
        is_open = is_market_open(market_type)
        market_state = "REGULAR" if is_open else "CLOSED"

        return key, {
            "price": _fmt(price),
            "change": _fmt(change),
            "percent": _fmt(pct),
            "high": _fmt(last_row.get("High")),
            "low": _fmt(last_row.get("Low")),
            "open": _fmt(last_row.get("Open")),
            "volume": _fmt_vol(last_row.get("Volume")),
            "market_state": market_state,
        }
    except (RequestException, ValueError, TypeError, KeyError, IndexError, OSError):
        logger.exception("Index fetch failed for %s", key)
        return None


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

                if mtime != 0.0 and _last_loaded_mtimes.get(key) == mtime:
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

                    _last_loaded_mtimes[key] = mtime
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
        load_user_stocks(force=True)

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
                ensure_stock_placeholder_in_caches(symbol, name, market)

    us_open = is_market_open("us")
    jp_open = is_market_open("jp")

    def _is_cache_incomplete(market: str) -> bool:
        target_list = (
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
        target_list = (
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


def _process_fetched_stocks(
    fetched_items: list[dict[str, Any] | None],
    sync_generation: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
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
                current_sync_gen = _get_app_bg_attr("_sync_generation", _sync_generation)
                if current_sync_gen != 0 and sync_generation != current_sync_gen:
                    logger.info("Discarding stale stock sync generation %s (current: %s)", sync_generation, current_sync_gen)
                    return [], [], []
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
    return new_us, new_jp, new_idx


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
                            old_saved_ts = getattr(app_state.market, "last_usdjpy_persisted_ts", 0.0)
                            time_mod = _get_app_bg_attr("time", time)
                            now_ts = float(time_mod.time())
                            app_state.market.last_usdjpy_rate = rate_float
                            app_state.market.last_usdjpy_rate_ts = now_ts

                            should_persist = (
                                old_rate is None
                                or abs(rate_float - old_rate) >= 0.01
                                or (now_ts - old_saved_ts) >= 300.0
                            )
                            if should_persist:
                                try:
                                    save_fn = _get_app_bg_attr("save_user_stocks", save_user_stocks)
                                    save_fn()
                                    app_state.market.last_usdjpy_persisted_ts = now_ts
                                except Exception as persist_exc:
                                    logger.warning(
                                        "Failed to persist fresh USDJPY state: %s",
                                        persist_exc,
                                    )
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
        for (symbol, _name_or_dict, market), result in zip(items, fetched_items):
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

        if removed_any:
            try:
                save_fn = _get_app_bg_attr("save_user_stocks", save_user_stocks)
                save_fn()
            except Exception as exc:
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
        schedule_fn = _get_app_bg_attr("schedule_sync_all_stocks_now", schedule_sync_all_stocks_now)
        schedule_fn()
        return

    if removed_any:
        from services.realtime_engine import realtime_market_engine

        for symbol, market, _stock, _streak in removed:
            invalidate_stock_caches(symbol)
            remove_stock_from_caches(symbol, market)
            realtime_market_engine.unregister_symbol(symbol, market)
        announce_real_market_state()
        schedule_sync_all_stocks_now()


def sync_all_stocks_now(force_fetch: bool = False) -> None:
    """Yahoo Financeから全銘柄を一括同期し、ターゲットキャッシュを更新する"""
    target = _get_app_bg_attr("sync_all_stocks_now", None)
    if target is not None and target is not sync_all_stocks_now:
        return target(force_fetch=force_fetch)

    global _sync_generation
    lock = _get_sync_execution_lock()
    if not lock.acquire(blocking=False):
        if not _recover_stale_sync_state_if_needed():
            logger.info("Sync already in progress, skipping.")
            return
        lock = _get_sync_execution_lock()
        if not lock.acquire(timeout=SYNC_STALE_LOCK_WAIT_SEC):
            logger.critical(
                "Sync lock takeover timed out after %.0fs; the previous sync is still wedged.",
                SYNC_STALE_LOCK_WAIT_SEC,
            )
            return

    try:
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
            _set_sync_start_time(time.time())
            _sync_generation += 1
            sync_generation = _sync_generation

        is_leader_flag = bool(_get_app_bg_attr("_is_sync_leader", is_leader()))
        if not is_leader_flag:
            logger.debug("Follower process: reloading cache from disk payloads")
            warm_fn = _get_app_bg_attr("_warm_payload_cache_from_disk", _warm_payload_cache_from_disk)
            warm_fn()
            inval_fn = _get_app_bg_attr("_invalidate_sse_payload_cache", _invalidate_sse_payload_cache)
            inval_fn()
            ann_fn = _get_app_bg_attr("announce_current_market_state", announce_current_market_state)
            ann_fn()
            return
        with app_state.cache.sse_data_lock:
            if getattr(app_state.market, "current_indices_cache", None) is None:
                app_state.market.current_indices_cache = {}

        target_empty = not any(
            app_state.market.target_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if target_empty:
            warm_fn = _get_app_bg_attr("_warm_payload_cache_from_disk", _warm_payload_cache_from_disk)
            warm_fn()

        if any(app_state.market.target_stocks_cache.get(m) for m in ("us", "jp", "idx")):
            app_state.market.first_sync_attempted = True
            if not getattr(app_state.market, "first_sync_completed_at", 0.0):
                app_state.market.first_sync_completed_at = time.time()

        prep_fn = _get_app_bg_attr("_prepare_sync_items", _prepare_sync_items)
        items = prep_fn(force_load=not target_empty, force_fetch=force_fetch)

        snapshot_ts_ms = int(time.time() * 1000)
        fetch_batch_fn = _get_app_bg_attr("fetch_stocks_batch", fetch_stocks_batch)
        fetched_items = fetch_batch_fn(items, snapshot_ts_ms=snapshot_ts_ms)

        auto_rm_fn = _get_app_bg_attr("_auto_remove_invalid_symbols", _auto_remove_invalid_symbols)
        auto_rm_fn(items, fetched_items)

        proc_fn = _get_app_bg_attr("_process_fetched_stocks", _process_fetched_stocks)
        us_res, jp_res, idx_res = proc_fn(fetched_items, sync_generation)

        if items and not (us_res or jp_res or idx_res):
            logger.warning("Stock sync produced no valid items; preserving previous target cache.")
            return

        with app_state.market.is_syncing_lock:
            current_sync_gen = _get_app_bg_attr("_sync_generation", _sync_generation)
            is_current_generation = (current_sync_gen == 0 or sync_generation == current_sync_gen)
        if not is_current_generation:
            logger.info(
                "Discarding stale stock sync generation %s before cache publish", sync_generation
            )
            return

        update_idx_fn = _get_app_bg_attr("_update_indices_data", _update_indices_data)
        update_idx_fn(idx_res, us_res, jp_res)
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = copy.deepcopy(
                app_state.market.target_stocks_cache
            )
        try:
            from constants import POPULAR_JP, POPULAR_US
            from routes.api_stocks import _fetch_heatmap_cached

            app_state.execution.data_executor.submit(
                _fetch_heatmap_cached, "heatmap_us", "us", POPULAR_US
            )
            app_state.execution.data_executor.submit(
                _fetch_heatmap_cached, "heatmap_jp", "jp", POPULAR_JP
            )
        except Exception as prewarm_exc:
            logger.debug("Heatmap pre-warm submission skipped: %s", prewarm_exc)

        _invalidate_sse_payload_cache()
        announce_current_market_state()
        logger.info("Sync completed.")
    except (
        RequestException,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        RuntimeError,
        YFTickerMissingError,
    ):
        logger.exception("sync_all_stocks_now error")
        raise
    finally:
        try:
            app_state.market.first_sync_attempted = True
            app_state.market.first_sync_completed_at = time.time()
            with app_state.market.is_syncing_lock:
                if sync_generation == _sync_generation:
                    app_state.market.is_syncing = False
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass


def _run_scheduled_sync_job() -> None:
    """スケジュールされた同期ジョブを実行"""
    forced = False
    with app_state.market.sync_schedule_lock:
        if getattr(app_state.market, "sync_forced", False):
            forced = True
            app_state.market.sync_forced = False
    try:
        sync_all_stocks_now(force_fetch=forced)
    finally:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
            pending = app_state.market.sync_pending
            if pending:
                app_state.market.sync_pending = False
        if pending:
            logger.info("Triggering pending stock sync.")
            schedule_sync_all_stocks_now()


def schedule_sync_all_stocks_now(force: bool = False) -> bool:
    """同期ジョブをスケジュール"""
    target = _get_app_bg_attr("schedule_sync_all_stocks_now", None)
    if target is not None and target is not schedule_sync_all_stocks_now:
        return target(force=force)

    recover_fn = _get_app_bg_attr("_recover_stale_sync_state_if_needed", _recover_stale_sync_state_if_needed)
    with app_state.market.sync_schedule_lock:
        if force:
            app_state.market.sync_forced = True
    with app_state.market.is_syncing_lock:
        if app_state.market.is_syncing and not recover_fn():
            with app_state.market.sync_schedule_lock:
                app_state.market.sync_pending = True
            return False

    with app_state.market.sync_schedule_lock:
        if app_state.market.sync_scheduled:
            app_state.market.sync_pending = True
            return False
        app_state.market.sync_scheduled = True

    try:
        app_state.execution.sync_refresh_executor.submit(_run_scheduled_sync_job)
        return True
    except queue.Full as exc:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
        logger.warning("Failed to schedule stock sync (queue full): %s", exc)
        return False
    except (RuntimeError, AttributeError, ValueError) as exc:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
        logger.warning("Failed to schedule stock sync: %s", exc)
        return False


def bg_yahoo_fetch_loop() -> None:
    """Yahoo Financeデータの定期取得ループ"""
    target = _get_app_bg_attr("bg_yahoo_fetch_loop", None)
    if target is not None and target is not bg_yahoo_fetch_loop:
        return target()

    app_state.execution.shutdown_event.wait(SSE_MARKET_OPEN_SLEEP)

    while not app_state.execution.shutdown_event.is_set():
        try:
            sync_all_stocks_now()
        except Exception:
            logger.exception("sync_all_stocks_now failed")

        try:
            listener_count = (
                app_state.sse_announcer_mode1.listener_count()
                + app_state.sse_announcer_mode2.listener_count()
            )
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_NO_LISTENER_SLEEP)
            elif not is_market_open("us") and not is_market_open("jp"):
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP)
            else:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP)
        except Exception:
            logger.exception("Error in market check")
            app_state.execution.shutdown_event.wait(60.0)


def _watchdog_restart_dead_realtime_engine(engine: Any = None) -> list[str]:
    """Restart the realtime engine when any internal producer thread died."""
    target = _get_app_bg_attr("_watchdog_restart_dead_realtime_engine", None)
    if target is not None and target is not _watchdog_restart_dead_realtime_engine:
        return target(engine=engine)

    if engine is None:
        from services.realtime_engine import realtime_market_engine

        engine = realtime_market_engine
    try:
        dead = [t for t in engine.worker_threads() if not t.is_alive()]
        if dead:
            logger.warning(
                "Watchdog detected %d dead realtime producer thread(s): %s — restarting engine.",
                len(dead),
                ", ".join(t.name for t in dead),
            )
            engine.restart()
        return [t.name for t in dead]
    except Exception as exc:
        logger.debug("Realtime engine watchdog check failed: %s", exc)
        return []


def start_background_worker() -> None:
    """バックグラウンドスレッドを安全に開始（クラッシュ時に指数バックオフで再起動）"""
    target = _get_app_bg_attr("start_background_worker", None)
    if target is None:
        target = _get_app_bg_attr("_start_background_threads", None)
    if target is not None and target is not start_background_worker and target is not _start_background_threads:
        return target()

    def wrapped_loop(func: Any, name: str) -> None:
        consecutive_errors = 0
        max_consecutive_errors = 10
        while not app_state.execution.shutdown_event.is_set():
            try:
                func()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors > max_consecutive_errors:
                    logger.critical(
                        "%s thread stopped after %d consecutive errors. Restarting...",
                        name,
                        max_consecutive_errors,
                    )
                    consecutive_errors = 0
                    app_state.execution.shutdown_event.wait(60.0)
                    continue
                sleep_time = min(2**consecutive_errors, 600)
                logger.error(
                    "%s thread crashed (consecutive=%d/%d). Retrying in %ds. Error: %s",
                    name,
                    consecutive_errors,
                    max_consecutive_errors,
                    sleep_time,
                    e,
                )
                app_state.execution.shutdown_event.wait(sleep_time)

    yahoo_loop = _get_app_bg_attr("bg_yahoo_fetch_loop", bg_yahoo_fetch_loop)
    t1 = threading.Thread(target=wrapped_loop, args=(yahoo_loop, "Yahoo"), daemon=True)
    app_state.execution.background_threads.append(t1)
    t1.start()

    leader_loop = _get_app_bg_attr("bg_leader_election_loop", bg_leader_election_loop)
    t_leader = threading.Thread(
        target=wrapped_loop, args=(leader_loop, "LeaderElection"), daemon=True
    )
    app_state.execution.background_threads.append(t_leader)
    t_leader.start()

    from session_manager import bg_session_reap_loop

    reap_loop = _get_app_bg_attr("bg_session_reap_loop", bg_session_reap_loop)
    t_reap = threading.Thread(
        target=wrapped_loop, args=(reap_loop, "SessionReap"), daemon=True
    )
    app_state.execution.background_threads.append(t_reap)
    t_reap.start()

    try:
        from constants import POPULAR_JP, POPULAR_US
        from services.realtime_engine import realtime_market_engine
        from utils.storage import load_user_stocks

        load_fn = _get_app_bg_attr("load_user_stocks", load_user_stocks)
        load_fn(force=True)
        with app_state.market.user_stocks_lock:
            user_us = list(app_state.market.user_us.keys())
            user_jp = list(app_state.market.user_jp.keys())

        us_defaults = list(dict.fromkeys(POPULAR_US + user_us + ["INDEX:SPX", "INDEX:IUXX"]))
        jp_all_symbols = list(dict.fromkeys(POPULAR_JP + user_jp))

        realtime_market_engine.register_symbols(us_defaults, jp_all_symbols)
        realtime_market_engine.start()
        logger.info(
            "RealtimeMarketEngine started successfully with %d US and %d JP symbols.",
            len(us_defaults),
            len(jp_all_symbols),
        )
    except Exception as e:
        logger.info("Failed to start RealtimeMarketEngine: %s", e)

    interp_loop = _get_app_bg_attr("bg_interpolate_loop", bg_interpolate_loop)
    t_interp = threading.Thread(
        target=wrapped_loop, args=(interp_loop, "Interpolate"), daemon=True
    )
    app_state.execution.background_threads.append(t_interp)
    t_interp.start()

    def bg_threads_watchdog_loop() -> None:
        while not app_state.execution.shutdown_event.is_set():
            app_state.execution.shutdown_event.wait(60.0)
            if app_state.execution.shutdown_event.is_set():
                break
            dead_threads = [
                t for t in list(app_state.execution.background_threads) if not t.is_alive()
            ]
            if dead_threads:
                logger.warning(
                    "Watchdog detected %d dead thread(s): %s",
                    len(dead_threads),
                    ", ".join(t.name for t in dead_threads),
                )
            watchdog_fn = _get_app_bg_attr(
                "_watchdog_restart_dead_realtime_engine", _watchdog_restart_dead_realtime_engine
            )
            watchdog_fn()

    t_watchdog = threading.Thread(target=bg_threads_watchdog_loop, name="Watchdog", daemon=True)
    app_state.execution.background_threads.append(t_watchdog)
    t_watchdog.start()


_start_background_threads = start_background_worker
