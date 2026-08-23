# bg/leader_election.py
"""Leader election mechanism across multiple WSGI/worker processes."""

from __future__ import annotations

import atexit
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import config_store
from app_state import app_state

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

APP_DATA_DIR = config_store.APP_DATA_DIR
_ATOMIC_LOCK_STALE_SEC: float = 60.0

_LEADER_LOCK_FILE: Any = None
_is_sync_leader: bool = True


def _get_leader_lock_file() -> Any:
    mod = sys.modules.get("app_bg")
    if mod is not None and "_LEADER_LOCK_FILE" in mod.__dict__:
        return mod.__dict__["_LEADER_LOCK_FILE"]
    return _LEADER_LOCK_FILE


def _set_leader_lock_file(f: Any) -> None:
    global _LEADER_LOCK_FILE
    _LEADER_LOCK_FILE = f
    mod = sys.modules.get("app_bg")
    if mod is not None:
        mod.__dict__["_LEADER_LOCK_FILE"] = f


def _get_app_bg_attr(name: str, fallback: Any) -> Any:
    mod = sys.modules.get("app_bg")
    if mod is not None and name in mod.__dict__:
        target = mod.__dict__[name]
        return target
    return fallback


def _pid_is_alive(pid: int) -> bool:
    """Return True when a process with *pid* exists."""
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _release_leader_lock() -> None:
    """Close the leader lock file handle on process exit."""
    _lock_file = _get_leader_lock_file()
    _set_leader_lock_file(None)
    if _lock_file is not None:
        try:
            _lock_file.close()
        except OSError:
            pass

    base_dir = _get_app_bg_attr("APP_DATA_DIR", APP_DATA_DIR)
    try:
        lock_path = Path(base_dir) / ".mns_sync_leader.lock"
        if lock_path.exists():
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            raw = lock_path.read_text(encoding="utf-8").strip()
            if raw.isdigit() and int(raw) == os.getpid():
                lock_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        legacy_lock = Path(__file__).resolve().parent.parent / ".mns_sync_leader.lock"
        if legacy_lock.exists():
            legacy_lock.unlink(missing_ok=True)
    except OSError:
        pass


atexit.register(_release_leader_lock)


def _try_acquire_atomic_lock(lock_path: Path, pid: int) -> bool:
    """Attempt to acquire the leader lock via atomic file creation (iterative)."""
    for _ in range(8):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        except FileExistsError:
            try:
                raw = lock_path.read_text(encoding="utf-8").strip()
                owner_pid = int(raw) if raw.isdigit() else None
            except (OSError, ValueError):
                owner_pid = None
            if owner_pid == pid:
                logger.debug("Atomic leader lock already held by pid=%d", pid)
                return True
            stale = False
            try:
                if owner_pid is not None:
                    stale = not _pid_is_alive(owner_pid)
                elif time.time() - lock_path.stat().st_mtime > _ATOMIC_LOCK_STALE_SEC:
                    stale = True
            except OSError:
                stale = False
            if not stale:
                return False
            logger.warning("Reclaiming stale atomic leader lock at %s", lock_path)
            prev_f = _get_leader_lock_file()
            if prev_f is not None:
                try:
                    prev_f.close()
                except OSError:
                    pass
                _set_leader_lock_file(None)
            try:
                lock_path.unlink()
            except OSError:
                return False
            continue
        except OSError as exc:
            logger.debug("Failed to acquire atomic leader lock: %s", exc)
            return False

        f = None
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
            f.write(str(pid))
            f.flush()
            f.seek(0)
        except OSError as exc:
            logger.debug("Failed to write atomic leader lock: %s", exc)
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
            else:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        _set_leader_lock_file(f)
        logger.debug("Acquired atomic leader lock at %s (pid=%d)", lock_path, pid)
        return True
    return False


def _try_acquire_leader_lock() -> bool:
    """Try to acquire a non-blocking lock on the leader lock file."""
    target = _get_app_bg_attr("_try_acquire_leader_lock", None)
    if target is not None and target is not _try_acquire_leader_lock:
        return target()

    base_dir = Path(_get_app_bg_attr("APP_DATA_DIR", APP_DATA_DIR))
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lock_path = base_dir / ".mns_sync_leader.lock"
    pid = os.getpid()

    try:
        if os.name == "nt":  # Windows
            if msvcrt is not None:
                lock_f = _get_leader_lock_file()
                if lock_f is None:
                    lock_path.touch(exist_ok=True)
                    lock_f = open(lock_path, "r+", encoding="utf-8")  # noqa: SIM115
                    _set_leader_lock_file(lock_f)
                fd = lock_f.fileno()
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    lock_f.seek(0)
                    lock_f.truncate(0)
                    lock_f.write(str(pid))
                    lock_f.flush()
                    return True
                except OSError:
                    return False
            return _try_acquire_atomic_lock(lock_path, pid)
        else:  # Unix
            if fcntl is not None:
                lock_f = _get_leader_lock_file()
                if lock_f is None:
                    lock_path.touch(exist_ok=True)
                    lock_f = open(lock_path, "r+", encoding="utf-8")  # noqa: SIM115
                    _set_leader_lock_file(lock_f)
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                    lock_f.seek(0)
                    lock_f.truncate(0)
                    lock_f.write(str(pid))
                    lock_f.flush()
                    return True
                except OSError:
                    return False
            return _try_acquire_atomic_lock(lock_path, pid)
    except (OSError, ValueError) as exc:
        logger.debug("Failed to acquire sync leader lock: %s", exc)
        return False


def is_leader() -> bool:
    """Check if current worker is sync leader."""
    return _get_app_bg_attr("_is_sync_leader", _is_sync_leader)


def bg_leader_election_loop() -> None:
    """Periodically check and run leader election."""
    global _is_sync_leader
    acquired = _try_acquire_leader_lock()
    _is_sync_leader = acquired
    if acquired:
        logger.info("This process has acquired the sync leader lock. Running as MASTER.")
    else:
        logger.debug("This process failed to acquire the sync leader lock. Running as FOLLOWER.")

    while not app_state.execution.shutdown_event.is_set():
        if not _is_sync_leader:
            acquired = _try_acquire_leader_lock()
            if acquired:
                _is_sync_leader = True
                logger.info("Sync leader changed: this process is now the MASTER.")
        app_state.execution.shutdown_event.wait(10.0)
