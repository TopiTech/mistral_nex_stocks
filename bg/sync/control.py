# bg/sync/control.py
"""Orchestration: full sync, scheduling, fetch loop, watchdog, worker startup.

Pure move from `bg.sync_worker`. The app_bg mirror delegation
(`target is not <self>` guards) is unchanged, so conftest stubs and test
overrides on `app_bg` keep working.
"""

from __future__ import annotations

import copy
import logging
import queue
import threading
import time
from typing import Any

from requests.exceptions import RequestException

from app_state import app_state
from bg.common import (
    _invalidate_sse_payload_cache,
    announce_current_market_state,
)
from bg.leader_election import bg_leader_election_loop, is_leader
from bg.sse_interpolator import bg_interpolate_loop
from bg.sync import state as _state
from bg.sync.fetch import fetch_stocks_batch
from bg.sync.process import (
    _auto_remove_invalid_symbols,
    _prepare_sync_items,
    _process_fetched_stocks,
    _update_indices_data,
    _warm_payload_cache_from_disk,
)
from bg.sync.state import (
    _BG_LOOP_MARKER_ATTR,
    SYNC_STALE_LOCK_WAIT_SEC,
    _get_app_bg_attr,
    _get_sync_execution_lock,
    _recover_stale_sync_state_if_needed,
    _set_sync_generation,
    _set_sync_start_time,
)
from constants import (
    SSE_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP,
    SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_NO_LISTENER_SLEEP,
)
from services.stock_provider import YFTickerMissingError
from utils.market_utils import is_market_open

logger = logging.getLogger("bg.sync_worker")



def sync_all_stocks_now(force_fetch: bool = False) -> None:
    """Yahoo Financeから全銘柄を一括同期し、ターゲットキャッシュを更新する"""
    target = _get_app_bg_attr("sync_all_stocks_now", None)
    if target is not None and target is not sync_all_stocks_now:
        return target(force_fetch=force_fetch)

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
            _set_sync_generation(_state._sync_generation + 1)
            sync_generation = _state._sync_generation

        is_leader_flag = bool(_get_app_bg_attr("_is_sync_leader", is_leader()))
        if not is_leader_flag:
            logger.debug("Follower process: reloading cache from disk payloads")
            warm_fn = _get_app_bg_attr(
                "_warm_payload_cache_from_disk", _warm_payload_cache_from_disk
            )
            warm_fn()
            inval_fn = _get_app_bg_attr(
                "_invalidate_sse_payload_cache", _invalidate_sse_payload_cache
            )
            inval_fn()
            ann_fn = _get_app_bg_attr(
                "announce_current_market_state", announce_current_market_state
            )
            ann_fn()
            return
        with app_state.cache.sse_data_lock:
            if getattr(app_state.market, "current_indices_cache", None) is None:
                app_state.market.current_indices_cache = {}
            target_empty = not any(
                app_state.market.target_stocks_cache.get(m) for m in ("us", "jp", "idx")
            )
        if target_empty:
            warm_fn = _get_app_bg_attr(
                "_warm_payload_cache_from_disk", _warm_payload_cache_from_disk
            )
            warm_fn()

        with app_state.cache.sse_data_lock:
            has_targets = any(
                app_state.market.target_stocks_cache.get(m) for m in ("us", "jp", "idx")
            )
        if has_targets:
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
            current_sync_gen = _get_app_bg_attr("_sync_generation", _state._sync_generation)
            is_current_generation = current_sync_gen == 0 or sync_generation == current_sync_gen
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
            from routes.stocks.common import _fetch_heatmap_cached

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
                if sync_generation == _state._sync_generation:
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

    recover_fn = _get_app_bg_attr(
        "_recover_stale_sync_state_if_needed", _recover_stale_sync_state_if_needed
    )
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
    if (
        target is not None
        and target is not start_background_worker
        and target is not _start_background_threads
    ):
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

    from session_manager import bg_session_reap_loop

    yahoo_loop = _get_app_bg_attr("bg_yahoo_fetch_loop", bg_yahoo_fetch_loop)
    leader_loop = _get_app_bg_attr("bg_leader_election_loop", bg_leader_election_loop)
    reap_loop = _get_app_bg_attr("bg_session_reap_loop", bg_session_reap_loop)
    interp_loop = _get_app_bg_attr("bg_interpolate_loop", bg_interpolate_loop)

    def _spawn_loop(loop_fn: Any, loop_name: str) -> None:
        """Start *loop_fn* unless a live thread for the same loop already exists.

        ``start_background_worker`` can be re-entered after a partial start
        (bootstrap retries while ``_app_bootstrap_done`` is still False, and
        the first-request fallback can re-invoke it). Spawning a second copy
        of a still-running loop would double-poll upstream APIs and double-
        write shared state, so match on an explicit marker attribute rather
        than closure identity (``wrapped_loop`` is re-created on every call).
        """
        for existing in app_state.execution.get_background_threads():
            if (
                getattr(existing, _BG_LOOP_MARKER_ATTR, None) == loop_name
                and existing.is_alive()
            ):
                logger.debug("Background loop %s already running; not spawning again", loop_name)
                return
        t = threading.Thread(target=wrapped_loop, args=(loop_fn, loop_name), daemon=True)
        setattr(t, _BG_LOOP_MARKER_ATTR, loop_name)
        app_state.execution.register_background_thread(t)
        t.start()

    _spawn_loop(yahoo_loop, "Yahoo")
    _spawn_loop(leader_loop, "LeaderElection")
    _spawn_loop(reap_loop, "SessionReap")

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

    _spawn_loop(interp_loop, "Interpolate")

    def bg_threads_watchdog_loop() -> None:
        while not app_state.execution.shutdown_event.is_set():
            app_state.execution.shutdown_event.wait(60.0)
            if app_state.execution.shutdown_event.is_set():
                break
            dead_threads = [
                t for t in app_state.execution.get_background_threads() if not t.is_alive()
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

    watchdog_running = any(
        getattr(existing, _BG_LOOP_MARKER_ATTR, None) == "Watchdog" and existing.is_alive()
        for existing in app_state.execution.get_background_threads()
    )
    if not watchdog_running:
        t_watchdog = threading.Thread(target=bg_threads_watchdog_loop, name="Watchdog", daemon=True)
        setattr(t_watchdog, _BG_LOOP_MARKER_ATTR, "Watchdog")
        app_state.execution.register_background_thread(t_watchdog)
        t_watchdog.start()


_start_background_threads = start_background_worker
