# bg/sync/state.py
"""Shared sync state, staleness guards, and app_bg mirror accessors.

Pure move from `bg.sync_worker`. The module-level counters are mirrored into
`app_bg.__dict__` by the setters; readers prefer the mirror via
`_get_app_bg_attr` so conftest stubs and test overrides keep working.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app_state import app_state

logger = logging.getLogger("bg.sync_worker")



def _get_logger() -> Any:
    # Lazy import: app_bg imports bg.*, so an eager import would be circular.
    try:
        import app_bg as app_bg_mod
    except ImportError:
        return logging.getLogger("app_bg")
    return app_bg_mod.__dict__.get("logger", logging.getLogger("app_bg"))


SYNC_STALE_TIMEOUT_SEC: float = 120.0


SYNC_STALE_LOCK_WAIT_SEC: float = 15.0


_sync_start_time: float = 0.0


_sync_generation: int = 0


_last_loaded_mtimes: dict[str, float] = {}


_sync_execution_lock = threading.Lock()


_BATCH_INVALID_MARKER = "__INVALID_SYMBOL__"


# Marker attribute set on threads spawned by start_background_worker so a
# re-entry (bootstrap retry / first-request fallback) can detect still-running
# loops idempotently instead of spawning duplicates.
_BG_LOOP_MARKER_ATTR = "_mns_bg_loop_marker"


def _get_app_bg_attr(name: str, fallback: Any) -> Any:
    try:
        import app_bg as app_bg_mod
    except ImportError:
        return fallback
    return app_bg_mod.__dict__.get(name, fallback)


def _get_sync_start_time() -> float:
    return float(_get_app_bg_attr("_sync_start_time", _sync_start_time))


def _set_sync_start_time(val: float) -> None:
    global _sync_start_time
    _sync_start_time = val
    try:
        import app_bg as app_bg_mod
    except ImportError:
        return
    app_bg_mod.__dict__["_sync_start_time"] = val


def _set_sync_generation(val: int) -> None:
    global _sync_generation
    _sync_generation = val
    try:
        import app_bg as app_bg_mod
    except ImportError:
        return
    app_bg_mod.__dict__["_sync_generation"] = val


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
