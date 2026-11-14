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
import weakref
from collections.abc import Callable
from typing import Any, TypeVar

from app_state import app_state
from bg._app_bridge import _get_app_bg_attr, _set_app_bg_attr

logger = logging.getLogger("bg.sync_worker")

TCallable = TypeVar("TCallable", bound=Callable[..., Any])



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


_APP_BG_DELEGATES: weakref.WeakSet[Any] = weakref.WeakSet()
_WEDGED_SYNC_LOCKS_LOCK = threading.Lock()
_WEDGED_SYNC_LOCKS: weakref.WeakSet[Any] = weakref.WeakSet()
_WEDGED_SYNC_LOCKS_STRONG: list[Any] = []


def _register_app_bg_delegate(fn: TCallable) -> TCallable:
    """Register a thin ``app_bg`` forwarding wrapper so canonical code won't recurse."""
    _APP_BG_DELEGATES.add(fn)
    return fn


def _is_app_bg_delegate(obj: Any) -> bool:
    """Return True when *obj* is a thin forwarding wrapper registered by ``app_bg``."""
    try:
        return obj in _APP_BG_DELEGATES
    except TypeError:
        return False


def _mark_wedged_sync_lock(lock: Any) -> None:
    """Record a lock whose stale-takeover acquire timed out (BG-001)."""
    with _WEDGED_SYNC_LOCKS_LOCK:
        try:
            _WEDGED_SYNC_LOCKS.add(lock)
        except TypeError:
            if lock not in _WEDGED_SYNC_LOCKS_STRONG:
                if len(_WEDGED_SYNC_LOCKS_STRONG) >= 16:
                    _WEDGED_SYNC_LOCKS_STRONG.pop(0)
                _WEDGED_SYNC_LOCKS_STRONG.append(lock)


def _is_wedged_sync_lock(lock: Any) -> bool:
    """Return True if *lock* previously timed out during a stale takeover."""
    with _WEDGED_SYNC_LOCKS_LOCK:
        try:
            in_weak = lock in _WEDGED_SYNC_LOCKS
        except TypeError:
            in_weak = False
        return in_weak or lock in _WEDGED_SYNC_LOCKS_STRONG


def _clear_wedged_sync_lock(lock: Any) -> None:
    """Clear *lock* from the wedged tracker once a caller acquires it cleanly."""
    with _WEDGED_SYNC_LOCKS_LOCK:
        try:
            _WEDGED_SYNC_LOCKS.discard(lock)
        except TypeError:
            pass
        if lock in _WEDGED_SYNC_LOCKS_STRONG:
            _WEDGED_SYNC_LOCKS_STRONG.remove(lock)


def _rotate_sync_execution_lock_if_default(timed_out_lock: Any) -> Any:
    """Replace the unpatched module sync lock after takeover timeout so new syncs proceed.

    Test mocks (e.g. FakeLock patched onto ``app_bg._sync_execution_lock``) are never
    replaced; they remain tracked via ``_WEDGED_SYNC_LOCKS`` instead.
    """
    global _sync_execution_lock
    with _WEDGED_SYNC_LOCKS_LOCK:
        if (
            timed_out_lock is _sync_execution_lock
            and _get_app_bg_attr("_sync_execution_lock", _sync_execution_lock) is timed_out_lock
        ):
            new_lock = threading.Lock()
            _sync_execution_lock = new_lock
            _set_app_bg_attr("_sync_execution_lock", new_lock)
            return new_lock
    return timed_out_lock


def _get_sync_execution_lock() -> Any:
    return _get_app_bg_attr("_sync_execution_lock", _sync_execution_lock)


def _recover_stale_sync_state_if_needed(
    *, facade_globals: dict[str, Any] | None = None
) -> bool:
    if facade_globals is None:
        target = _get_app_bg_attr("_recover_stale_sync_state_if_needed", None)
        if (
            target is not None
            and target is not _recover_stale_sync_state_if_needed
            and not _is_app_bg_delegate(target)
        ):
            return bool(target())

    with app_state.market.is_syncing_lock:
        if not app_state.market.is_syncing:
            return False
        if facade_globals is not None and "_sync_start_time" in facade_globals:
            start_time = float(facade_globals["_sync_start_time"])
        else:
            start_time = _get_sync_start_time()
        if facade_globals is not None and "SYNC_STALE_TIMEOUT_SEC" in facade_globals:
            timeout_sec = float(facade_globals["SYNC_STALE_TIMEOUT_SEC"])
        else:
            timeout_sec = float(
                _get_app_bg_attr("SYNC_STALE_TIMEOUT_SEC", SYNC_STALE_TIMEOUT_SEC)
            )
        elapsed = time.time() - start_time if start_time > 0 else 0.0
        if elapsed <= timeout_sec:
            return False
        logger.critical(
            "Sync is stale (running %.0fs, threshold %.0fs). Resetting sync state "
            "so scheduling, the UI, and lock takeovers can recover.",
            elapsed,
            timeout_sec,
        )
        app_state.market.is_syncing = False
        if facade_globals is not None:
            facade_globals["_sync_start_time"] = 0.0
        _set_sync_start_time(0.0)
        new_gen = _next_sync_generation()
        if facade_globals is not None:
            facade_globals["_sync_generation"] = new_gen
        return True
