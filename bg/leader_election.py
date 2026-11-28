# bg/leader_election.py
"""Leader election mechanism across multiple WSGI/worker processes."""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import config_store
from app_state import app_state
from bg._app_bridge import _get_app_bg_attr, _set_app_bg_attr

fcntl: Any
msvcrt: Any
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

logger = logging.getLogger(__name__)

APP_DATA_DIR = config_store.APP_DATA_DIR
_ATOMIC_LOCK_STALE_SEC: float = 60.0

_LEADER_LOCK_MUTEX = threading.RLock()
_LEADER_LOCK_FILE: Any = None
_LEADER_LOCK_PID: int | None = None
# Which acquisition mechanism owns leadership: ``"advisory"`` keeps a locked
# file descriptor for the whole term, ``"atomic"`` only owns a pid marker file
# (no descriptor). Ownership verification differs per mode: requiring a handle
# from the atomic mode demoted a healthy leader on every tick (the election
# loop then re-won the same marker forever, running as follower half the
# time), while the advisory mode needs the handle plus file-identity checks.
_LEADER_LOCK_MODE: str | None = None
_LEADER_LOCK_PATH: Path | None = None
_LEADER_RELEASE_GENERATION: int = 0
# LIFECYCLE-7: "follower until elected" — a process must not claim leadership
# before the election loop has won the leader lock, otherwise every worker in
# multi-process mode fetches as leader during the startup window (the exact
# failure the leader lock exists to prevent). The election loop promotes within
# milliseconds of thread start.
_is_sync_leader: bool = False


def _detach_inherited_leader_lock_locked() -> None:
    """Detach an inherited parent leader-lock handle after fork without closing it."""
    global _LEADER_LOCK_FILE, _LEADER_LOCK_PID
    if _LEADER_LOCK_PID is None or _LEADER_LOCK_PID == os.getpid():
        return
    # A differing PID alone does not prove inheritance: the bookkeeping marker
    # can be stale after a partial reset that cleared ``_LEADER_LOCK_FILE``
    # without going through ``_set_leader_lock_file``. Revoking leadership in
    # that state silently demotes a healthy master on the next ``is_leader()``
    # call, so only detach when this process actually owns an inherited handle.
    if _get_leader_lock_file() is None:
        _LEADER_LOCK_PID = None
        return
    _LEADER_LOCK_FILE = None
    _LEADER_LOCK_PID = None
    _set_app_bg_attr("_LEADER_LOCK_FILE", None)
    _set_leader_lock_mode(None)
    _set_is_sync_leader(False)


def _reset_leader_lock_in_child() -> None:
    """Reset inherited leader-lock state immediately in a forked child process."""
    global _LEADER_LOCK_FILE, _LEADER_LOCK_PID, _LEADER_LOCK_MUTEX, _LEADER_RELEASE_GENERATION
    _LEADER_LOCK_MUTEX = threading.RLock()
    _LEADER_RELEASE_GENERATION += 1
    _LEADER_LOCK_FILE = None
    _LEADER_LOCK_PID = None
    _set_app_bg_attr("_LEADER_LOCK_FILE", None)
    _set_leader_lock_mode(None)
    _set_is_sync_leader(False)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_leader_lock_in_child)


def _get_leader_lock_file() -> Any:
    with _LEADER_LOCK_MUTEX:
        return _get_app_bg_attr("_LEADER_LOCK_FILE", _LEADER_LOCK_FILE)


def _set_leader_lock_file(f: Any) -> None:
    global _LEADER_LOCK_FILE, _LEADER_LOCK_PID
    with _LEADER_LOCK_MUTEX:
        _LEADER_LOCK_FILE = f
        _LEADER_LOCK_PID = os.getpid() if f is not None else None
        _set_app_bg_attr("_LEADER_LOCK_FILE", f)


def _get_leader_lock_mode() -> str | None:
    with _LEADER_LOCK_MUTEX:
        return _get_app_bg_attr("_LEADER_LOCK_MODE", _LEADER_LOCK_MODE)


def _get_leader_lock_path() -> Path | None:
    with _LEADER_LOCK_MUTEX:
        return _get_app_bg_attr("_LEADER_LOCK_PATH", _LEADER_LOCK_PATH)


def _set_leader_lock_mode(mode: str | None, path: Path | None = None) -> None:
    """Record which acquisition mechanism currently owns leadership."""
    global _LEADER_LOCK_MODE, _LEADER_LOCK_PATH
    with _LEADER_LOCK_MUTEX:
        _LEADER_LOCK_MODE = mode
        if path is not None:
            _LEADER_LOCK_PATH = path
        elif mode is None:
            _LEADER_LOCK_PATH = None
        _set_app_bg_attr("_LEADER_LOCK_MODE", mode)
        _set_app_bg_attr("_LEADER_LOCK_PATH", _LEADER_LOCK_PATH)


def _set_is_sync_leader(val: bool) -> None:
    global _is_sync_leader
    _is_sync_leader = val
    _set_app_bg_attr("_is_sync_leader", val)


# ``_get_app_bg_attr`` / ``_set_app_bg_attr`` are owned by ``bg._app_bridge`` (M-3).


def _pid_is_alive(pid: int) -> bool:
    """Return True when a process with *pid* exists.

    LIFECYCLE-1: the fallback probe must never *signal* the process it checks.
    On Windows, ``os.kill(pid, sig)`` with any signal other than
    ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` calls ``TerminateProcess`` (documented
    CPython behavior), so ``os.kill(pid, 0)`` would terminate the lock owner —
    possibly the current sync leader or a recycled PID owned by an innocent
    process. psutil is therefore the only safe liveness probe on Windows; when
    it cannot be imported there is no way to probe liveness without signaling,
    so the PID is unverifiable and reported as not alive. The stale-reclaim
    path may then recycle the lock file, but — unlike the old fallback — it
    never signals the recorded owner. On POSIX, ``os.kill(pid, 0)`` is the
    standard non-destructive existence probe (it delivers no signal) and is
    kept unchanged.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        # psutil is the documented safe probe (LIFECYCLE-1). ctypes was
        # tried as a fallback but would silently change the contract pinned
        # by tests/test_review_2026_10_31_lifecycle.py (Fall-through to
        # conservative False + warning). Keep the conservative contract.
        logger.warning(
            "psutil unavailable: cannot verify pid %d liveness on Windows without "
            "risking TerminateProcess via os.kill. Treating the process as not "
            "alive so stale-lock reclaim proceeds without signaling the recorded "
            "owner. Install psutil for multi-process leader election on Windows.",
            pid,
        )
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: the process exists but is owned by another user.
        return True
    return True


def _release_leader_lock() -> None:
    """Close the leader lock file handle on process exit without unlinking advisory lock files."""
    global _LEADER_RELEASE_GENERATION
    with _LEADER_LOCK_MUTEX:
        _LEADER_RELEASE_GENERATION += 1
        _detach_inherited_leader_lock_locked()
        _lock_file = _get_leader_lock_file()
        _lock_mode = _get_leader_lock_mode()
        _lock_path = _get_leader_lock_path()
        _set_leader_lock_file(None)
        _set_leader_lock_mode(None)
        _set_is_sync_leader(False)
        if _lock_file is not None:
            try:
                _lock_file.close()
            except OSError:
                pass
        if _lock_mode == "atomic" and _lock_path is not None:
            try:
                if _lock_path.exists() and _lock_path.read_text(encoding="utf-8").strip() == str(
                    os.getpid()
                ):
                    _lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def release_leader_lock() -> None:
    """Close the leader lock file handle and reset leader status.

    Safe to call during shutdown or in test teardown to release any open
    leader lock file handle cleanly.
    """
    _release_leader_lock()


def close_leader_lock_file() -> None:
    """Close the leader lock file handle and reset the reference to None.

    Safe to call during shutdown or in test teardown.
    """
    _release_leader_lock()


atexit.register(_release_leader_lock)


def _try_acquire_atomic_lock(lock_path: Path, pid: int) -> bool:
    """Attempt to acquire the leader lock via atomic file creation (iterative)."""
    for _ in range(8):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        except FileExistsError:
            try:
                st_before = lock_path.stat()
            except FileNotFoundError:
                continue
            except OSError:
                st_before = None
            raw_bytes: bytes | None = None
            raw = ""
            owner_pid: int | None = None
            try:
                raw_bytes = lock_path.read_bytes()
                raw = raw_bytes.decode("utf-8", errors="replace").strip()
                owner_pid = int(raw) if (raw.isascii() and raw.isdigit()) else None
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                owner_pid = None
            if owner_pid == pid:
                logger.debug("Atomic leader lock already held by pid=%d", pid)
                _set_leader_lock_mode("atomic", lock_path)
                return True
            stale = False
            try:
                if owner_pid is not None:
                    stale = not _pid_is_alive(owner_pid)
                elif (
                    st_before is not None
                    and time.time() - st_before.st_mtime > _ATOMIC_LOCK_STALE_SEC
                ):
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
                if st_before is not None:
                    st_now = lock_path.stat()
                    if (st_now.st_dev, st_now.st_ino) != (
                        st_before.st_dev,
                        st_before.st_ino,
                    ) or (raw_bytes is not None and lock_path.read_bytes() != raw_bytes):
                        continue
                lock_path.unlink()
            except FileNotFoundError:
                continue
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
        prev_f = _get_leader_lock_file()
        if prev_f is not None and prev_f is not f:
            try:
                prev_f.close()
            except OSError:
                pass
        _set_leader_lock_file(f)
        _set_leader_lock_mode("atomic", lock_path)
        logger.debug("Acquired atomic leader lock at %s (pid=%d)", lock_path, pid)
        return True
    return False


def _try_acquire_advisory_file_lock(
    lock_path: Path,
    pid: int,
    lock_handle: Callable[[Any], None],
) -> bool:
    """Acquire or refresh an OS advisory file lock under ``_LEADER_LOCK_MUTEX``."""
    with _LEADER_LOCK_MUTEX:
        lock_f = _get_leader_lock_file()
        if lock_f is not None:
            try:
                lock_f.seek(0)
                lock_f.truncate(0)
                lock_f.write(str(pid))
                lock_f.flush()
                _set_leader_lock_mode("advisory", lock_path)
                return True
            except (ValueError, OSError):
                try:
                    lock_f.close()
                except (OSError, ValueError):
                    pass
                _set_leader_lock_file(None)

        lock_path.touch(exist_ok=True)
        with contextlib.ExitStack() as stack:
            lock_f = stack.enter_context(open(lock_path, "r+", encoding="utf-8"))
            lock_handle(lock_f)
            lock_f.seek(0)
            lock_f.truncate(0)
            lock_f.write(str(pid))
            lock_f.flush()
            _set_leader_lock_file(lock_f)
            _set_leader_lock_mode("advisory", lock_path)
            stack.pop_all()
            return True


def _verify_leader_ownership() -> None:
    """Re-verify that we still own the leader lock (R17-01).

    Ownership differs by acquisition mode:

    * ``advisory``: valid only while the locked descriptor stays open AND the
      path still names the same file. If the fd was closed (GC/shutdown race)
      or the lock file was unlink+recreated by another process, our flock lives
      on an orphaned inode while a peer locks the new file - the cached
      ``_is_sync_leader`` bool would keep saying "leader" and two masters would
      fetch concurrently. The file-identity check (``st_dev``/``st_ino``,
      populated on POSIX and NTFS) detects that replacement.

    * ``atomic``: ownership is the pid written in the marker file, and there
      is intentionally no descriptor. Requiring a handle here demoted a
      healthy leader on every tick, after which the loop immediately re-won
      the same marker - effectively running as follower for half of every
      10-second cycle. The marker content is the authoritative check.
    """
    with _LEADER_LOCK_MUTEX:
        mode = _get_leader_lock_mode()
        lock_path = _get_leader_lock_path()
        lock_f = _get_leader_lock_file()
        pid = os.getpid()

        def _demote(reason: str) -> None:
            logger.warning("Leader lock ownership lost (%s); demoting to follower.", reason)
            if lock_f is not None:
                try:
                    lock_f.close()
                except (OSError, ValueError):
                    pass
            _set_leader_lock_file(None)
            _set_leader_lock_mode(None)
            _set_is_sync_leader(False)

        if mode == "atomic":
            if lock_path is None:
                _demote("atomic lock path unknown")
                return
            try:
                owner_raw = lock_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                _demote(f"atomic lock file unreadable: {exc}")
                return
            if owner_raw != str(pid):
                _demote("atomic lock marker no longer owned")
                return
            return

        if lock_f is None:
            _demote("lock handle missing")
            return
        try:
            lock_f.seek(0)
            lock_f.truncate(0)
            lock_f.write(str(pid))
            lock_f.flush()
            if not lock_f.tell():
                raise OSError("empty leader lock handle")
            if lock_path is not None:
                try:
                    held_stat = os.fstat(lock_f.fileno())
                    current_stat = os.stat(lock_path)
                except FileNotFoundError as exc:
                    raise OSError("leader lock file was removed") from exc
                if (held_stat.st_dev, held_stat.st_ino) != (
                    current_stat.st_dev,
                    current_stat.st_ino,
                ):
                    raise OSError("leader lock file was replaced")
        except (ValueError, OSError) as exc:
            _demote(str(exc))
            return


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

                def _lock_win(handle: Any) -> None:
                    fd = handle.fileno()
                    handle.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

                try:
                    return _try_acquire_advisory_file_lock(lock_path, pid, _lock_win)
                except OSError:
                    return False
            with _LEADER_LOCK_MUTEX:
                return _try_acquire_atomic_lock(lock_path, pid)
        else:  # Unix
            if fcntl is not None:

                def _lock_unix(handle: Any) -> None:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

                try:
                    return _try_acquire_advisory_file_lock(lock_path, pid, _lock_unix)
                except OSError:
                    return False
            with _LEADER_LOCK_MUTEX:
                return _try_acquire_atomic_lock(lock_path, pid)
    except (OSError, ValueError) as exc:
        logger.debug("Failed to acquire sync leader lock: %s", exc)
        return False


def is_leader() -> bool:
    """Check if current worker is sync leader."""
    with _LEADER_LOCK_MUTEX:
        _detach_inherited_leader_lock_locked()
    return _get_app_bg_attr("_is_sync_leader", _is_sync_leader)


def _try_acquire_and_promote_leader(*, demote_on_failure: bool) -> bool:
    """Acquire the leader lock and promote only if no concurrent release intervened."""
    with _LEADER_LOCK_MUTEX:
        release_gen = _LEADER_RELEASE_GENERATION
    acquired = _try_acquire_leader_lock()
    with _LEADER_LOCK_MUTEX:
        if acquired and _LEADER_RELEASE_GENERATION == release_gen:
            _set_is_sync_leader(True)
            return True
        if acquired:
            _release_leader_lock()
        if demote_on_failure or acquired:
            _set_is_sync_leader(False)
        return False


def bg_leader_election_loop() -> None:
    """Periodically check and run leader election."""
    target = _get_app_bg_attr("bg_leader_election_loop", None)
    if target is not None and target is not bg_leader_election_loop:
        return target()

    acquired = _try_acquire_and_promote_leader(demote_on_failure=True)
    if acquired:
        logger.info("This process has acquired the sync leader lock. Running as MASTER.")
    else:
        logger.debug("This process failed to acquire the sync leader lock. Running as FOLLOWER.")

    while not app_state.execution.shutdown_event.is_set():
        if not is_leader():
            if _try_acquire_and_promote_leader(demote_on_failure=False):
                logger.info("Sync leader changed: this process is now the MASTER.")
        else:
            # R17-01: re-verify ownership every tick. Previously the leader
            # state was a one-time cached bool: if the advisory lock fd was
            # closed / the lock file was unlink+recreated by another process,
            # this process stayed MASTER and two leaders fetched concurrently.
            _verify_leader_ownership()
        app_state.execution.shutdown_event.wait(10.0)
