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
from bg._app_bridge import _get_app_bg_attr, _set_app_bg_attr

logger = logging.getLogger("bg.sync_worker")



def _get_logger() -> Any:
    # The app_bg lazy import lives in ``bg._app_bridge`` (M-3); the logger is
    # resolved through the shared bridge so it cannot drift per call path.
    return _get_app_bg_attr("logger", logging.getLogger("app_bg"))


SYNC_STALE_TIMEOUT_SEC: float = 120.0


SYNC_STALE_LOCK_WAIT_SEC: float = 15.0


_sync_start_time: float = 0.0


_sync_generation: int = 0

# Dedicated lock guarding the sync generation counter. The counter is a
# read-modify-write value (``next = current + 1``) that must never lose an
# increment: a lost update would let a stale sync pass the generation check and
# clear a newer sync's ``is_syncing`` flag (double sync). This lock is separate
# from ``is_syncing_lock`` so the atomic increment is self-contained.
_sync_generation_lock = threading.Lock()


_last_loaded_mtimes: dict[str, float] = {}


_sync_execution_lock = threading.Lock()


_BATCH_INVALID_MARKER = "__INVALID_SYMBOL__"


# Marker attribute set on threads spawned by start_background_worker so a
# re-entry (bootstrap retry / first-request fallback) can detect still-running
# loops idempotently instead of spawning duplicates.
_BG_LOOP_MARKER_ATTR = "_mns_bg_loop_marker"


# ``_get_app_bg_attr`` / ``_set_app_bg_attr`` are owned by ``bg._app_bridge``
# (M-3); they are imported above and re-exported here for existing callers.


def _get_sync_start_time() -> float:
    return float(_get_app_bg_attr("_sync_start_time", _sync_start_time))


def _set_sync_start_time(val: float) -> None:
    global _sync_start_time
    _sync_start_time = val
    _set_app_bg_attr("_sync_start_time", val)


def _get_sync_generation() -> int:
    """Return the current sync generation (mirror-preferred, consistent read)."""
    return int(_get_app_bg_attr("_sync_generation", _sync_generation))


def _set_sync_generation(val: int) -> None:
    global _sync_generation
    with _sync_generation_lock:
        _sync_generation = val
        _set_app_bg_attr("_sync_generation", val)


def _next_sync_generation() -> int:
    """Atomically increment the sync generation and return the new value (C-1).

    This replaces the former caller-side read-modify-write
    (``_set_sync_generation(_sync_generation + 1)``) which could lose an
    increment under concurrency. The increment, the mirror write, and the
    returned value are all produced under one lock so every caller observes a
    unique, monotonically increasing generation.
    """
    global _sync_generation
    with _sync_generation_lock:
        next_gen = _get_sync_generation() + 1
        _sync_generation = next_gen
        _set_app_bg_attr("_sync_generation", next_gen)
        return next_gen


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
