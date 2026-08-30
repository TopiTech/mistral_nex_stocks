# app_bg.py
"""Backward-compatible facade for background worker, leader election, and market synchronization.

Notes on R9 degraded disk cache handling:
If is_disk_cache_degraded() is True, fetch_stocks_batch will return [None] early to avoid stalling.
"""

from __future__ import annotations

import queue
import time

from app_state import app_state
from bg.common import (
    _SSE_KEEPALIVE_FRAME,
    FULL_SNAPSHOT_INTERVAL,
    _announce_frame,
    _build_sse_diff,
    _build_sse_light_stocks_payload,
    _invalidate_sse_payload_cache,
    _raw_announce_current_market_state,
    _sse_full_snapshot_counter,
    _sse_payload_cache,
    _sse_payload_cached_generation,
    _sse_payload_generation,
    _sse_payload_jp_open,
    _sse_payload_lock,
    _sse_payload_us_open,
    _sse_payload_yf_limited,
    _sse_prev_stocks,
    announce_current_market_state,
    announce_real_market_state,
)
from bg.leader_election import (
    _ATOMIC_LOCK_STALE_SEC,
    _LEADER_LOCK_FILE,
    APP_DATA_DIR,
    _is_sync_leader,
    _pid_is_alive,
    _release_leader_lock,
    _try_acquire_atomic_lock,
    _try_acquire_leader_lock,
    bg_leader_election_loop,
    is_leader,
)
from bg.sse_interpolator import (
    _fluctuate_indices,
    _interpolate_and_fluctuate_market,
    bg_interpolate_loop,
)
from bg.sync_worker import (
    _BATCH_INVALID_MARKER,
    SYNC_STALE_LOCK_WAIT_SEC,
    SYNC_STALE_TIMEOUT_SEC,
    _auto_remove_invalid_symbols,
    _handle_yfinance_error,
    _invalid_tuple_if_applicable,
    _is_batch_result_invalid,
    _last_loaded_mtimes,
    _prepare_sync_items,
    _process_fetched_stocks,
    _run_scheduled_sync_job,
    _sync_execution_lock,
    _sync_generation,
    _sync_start_time,
    _update_indices_data,
    _warm_payload_cache_from_disk,
    _watchdog_restart_dead_realtime_engine,
    bg_yahoo_fetch_loop,
    extract_batch_history,
    fetch_index_data,
    fetch_stock,
    fetch_stocks_batch,
    start_background_worker,
    sync_all_stocks_now,
)
from constants import SIMULATE_FLUCTUATION
from route_helpers import (
    ensure_stock_placeholder_in_caches,
    invalidate_stock_caches,
    remove_stock_from_caches,
)
from utils.market_utils import acquire_yfinance_slot, is_market_open
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    _strip_portfolio_fields,
    build_stock_payload,
)
from utils.storage import load_user_stocks, save_user_stocks

_original_announce_current_market_state = _raw_announce_current_market_state
_start_background_threads = start_background_worker


def _recover_stale_sync_state_if_needed() -> bool:
    global _sync_start_time
    with app_state.market.is_syncing_lock:
        if not app_state.market.is_syncing:
            return False
        start_ts = float(globals().get("_sync_start_time", _sync_start_time))
        elapsed = time.time() - start_ts if start_ts > 0 else 0.0
        if elapsed <= SYNC_STALE_TIMEOUT_SEC:
            return False
        app_state.market.is_syncing = False
        globals()["_sync_start_time"] = 0.0
        _sync_start_time = 0.0
        return True


def schedule_sync_all_stocks_now(force: bool = False) -> bool:
    with app_state.market.sync_schedule_lock:
        if force:
            app_state.market.sync_forced = True
    with app_state.market.is_syncing_lock:
        if app_state.market.is_syncing and not _recover_stale_sync_state_if_needed():
            with app_state.market.sync_schedule_lock:
                app_state.market.sync_pending = True
            return False

    with app_state.market.sync_schedule_lock:
        if app_state.market.sync_scheduled:
            app_state.market.sync_pending = True
            return False
        app_state.market.sync_scheduled = True

    try:
        job = globals().get("_run_scheduled_sync_job", _run_scheduled_sync_job)
        app_state.execution.sync_refresh_executor.submit(job)
        return True
    except queue.Full:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
        return False
    except (RuntimeError, AttributeError, ValueError):
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
        return False


__all__ = [
    "APP_DATA_DIR",
    "FULL_SNAPSHOT_INTERVAL",
    "SIMULATE_FLUCTUATION",
    "SYNC_STALE_LOCK_WAIT_SEC",
    "SYNC_STALE_TIMEOUT_SEC",
    "_ATOMIC_LOCK_STALE_SEC",
    "_BATCH_INVALID_MARKER",
    "_LEADER_LOCK_FILE",
    "_SSE_KEEPALIVE_FRAME",
    "_announce_frame",
    "_auto_remove_invalid_symbols",
    "_build_sse_diff",
    "_build_sse_light_stocks_payload",
    "_default_stock_names",
    "_fluctuate_indices",
    "_get_stock_container",
    "_handle_yfinance_error",
    "_interpolate_and_fluctuate_market",
    "_invalid_tuple_if_applicable",
    "_invalidate_sse_payload_cache",
    "_is_batch_result_invalid",
    "_is_sync_leader",
    "_last_loaded_mtimes",
    "_original_announce_current_market_state",
    "_pid_is_alive",
    "_prepare_sync_items",
    "_process_fetched_stocks",
    "_recover_stale_sync_state_if_needed",
    "_release_leader_lock",
    "_run_scheduled_sync_job",
    "_sse_full_snapshot_counter",
    "_sse_payload_cache",
    "_sse_payload_cached_generation",
    "_sse_payload_generation",
    "_sse_payload_jp_open",
    "_sse_payload_lock",
    "_sse_payload_us_open",
    "_sse_payload_yf_limited",
    "_sse_prev_stocks",
    "_start_background_threads",
    "_strip_portfolio_fields",
    "_sync_execution_lock",
    "_sync_generation",
    "_sync_start_time",
    "_try_acquire_atomic_lock",
    "_try_acquire_leader_lock",
    "_update_indices_data",
    "_warm_payload_cache_from_disk",
    "_watchdog_restart_dead_realtime_engine",
    "acquire_yfinance_slot",
    "announce_current_market_state",
    "announce_real_market_state",
    "bg_interpolate_loop",
    "bg_leader_election_loop",
    "bg_yahoo_fetch_loop",
    "build_stock_payload",
    "ensure_stock_placeholder_in_caches",
    "extract_batch_history",
    "fetch_index_data",
    "fetch_stock",
    "fetch_stocks_batch",
    "invalidate_stock_caches",
    "is_leader",
    "is_market_open",
    "load_user_stocks",
    "remove_stock_from_caches",
    "save_user_stocks",
    "schedule_sync_all_stocks_now",
    "start_background_worker",
    "sync_all_stocks_now",
]
