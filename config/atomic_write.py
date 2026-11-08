"""原子書き込みヘルパー。`config_store` からの純粋な移動。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from config.paths import _ensure_runtime_dir

logger = logging.getLogger("config_store")

# POSIX write-lock retry budget (DATA-4): ~2.5 s worst case, mirroring the
# bounded LOCK_EX|LOCK_NB pattern of config/locks.py. Module-level constants
# so tests can shrink the budget.
_FCNTL_LOCK_MAX_RETRIES = 100
_FCNTL_LOCK_RETRY_DELAY_SEC = 0.025


def _migrate_legacy_runtime_file(source: Path, target: Path) -> None:
    """Copy a legacy source-tree file into the runtime directory if needed.

    After a successful copy the source file is removed so the project root
    does not accumulate runtime state that could be accidentally bundled,
    committed, or exposed.
    """
    if not source.exists():
        return
    _ensure_runtime_dir()
    try:
        # O_EXCL makes the existence check atomic: two processes starting
        # together cannot both stream into the same target and interleave a
        # half-written config that load_config() then classifies as corrupt.
        # Copying content (not metadata) also stops the legacy file's mode
        # (e.g. 0o644 from a checkout) from being propagated onto the runtime
        # config (CFG-11).
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        logger.warning("Failed to migrate legacy runtime file %s: %s", source, exc)
        return
    try:
        with os.fdopen(fd, "wb") as dst, open(source, "rb") as src:
            shutil.copyfileobj(src, dst)
        logger.info("Migrated legacy runtime file %s -> %s", source, target)
        try:
            source.unlink()
            logger.info("Removed legacy runtime file after migration: %s", source)
        except OSError as rm_exc:
            logger.warning("Failed to remove legacy runtime file %s: %s", source, rm_exc)
    except OSError as exc:
        logger.warning("Failed to migrate legacy runtime file %s: %s", source, exc)
        # Leave no partial target behind so a later attempt can retry.
        try:
            target.unlink()
        except OSError:
            pass


def _write_and_replace_with_lock(
    data: dict, tmp_file: Path, target_file: Path, lock_file: Path
) -> tuple[int, int] | None:
    """Write JSON data to tmp_file and replace target_file with platform-appropriate locking.

    Uses fcntl.flock on Unix/POSIX and msvcrt.locking on Windows.
    Falls back to lock-free write and replace if neither is available.
    Returns the (mtime_ns, size) cache key of the target file captured while
    the write lock is still held (or None when the key cannot be captured),
    so callers can key a cache entry to the exact content they persisted
    without a cross-process replacement slipping between the swap and a
    later stat call.
    """
    if os.name == "nt":  # Windows
        return _write_and_replace_with_msvcrt_lock(data, tmp_file, target_file, lock_file)
    return _write_and_replace_with_fcntl_lock(data, tmp_file, target_file, lock_file)


def _target_stat_key(target_file: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(target_file)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _durable_write_json(path: Path, data: dict) -> None:
    """Write JSON to path with fsync durability before atomic replace."""
    import config_store

    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        if config_store._is_windows():
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        else:
            old_umask = os.umask(0o077)
            try:
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except OSError:
                            pass
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
            finally:
                os.umask(old_umask)
        os.replace(tmp, path)
        o_dir = getattr(os, "O_DIRECTORY", None)
        if not config_store._is_windows() and o_dir is not None:
            try:
                dir_fd = os.open(str(path.parent), o_dir)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        if not config_store._is_windows() and path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _safe_write_json(tmp_file: Path, data: dict) -> None:
    """Write JSON data to tmp_file using restrictive permissions (0o600) with fsync."""
    old_umask = os.umask(0o077)
    try:
        fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    finally:
        os.umask(old_umask)


def _write_and_replace_with_fcntl_lock(
    data: dict, tmp_file: Path, target_file: Path, lock_file: Path
) -> tuple[int, int] | None:
    """Write and replace with POSIX fcntl.flock locking."""
    try:
        import fcntl  # type: ignore[import-untyped]

        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            # DATA-4: mirror config/locks.py — bounded LOCK_EX|LOCK_NB retries
            # instead of an unbounded blocking LOCK_EX. A hung holder (slow
            # disk, debugger stop, stuck NFS) must not freeze save_config
            # while it holds _CONFIG_LOCK (every load_config then queues
            # behind it); RuntimeError surfaces through save_config's
            # fail-fast branch, same contract as the Windows sibling.
            import errno as _errno

            for attempt in range(_FCNTL_LOCK_MAX_RETRIES):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                    break
                except OSError as fl_err:
                    if getattr(fl_err, "errno", None) not in (
                        _errno.EAGAIN,
                        _errno.EWOULDBLOCK,
                    ):
                        raise
                    if attempt == _FCNTL_LOCK_MAX_RETRIES - 1:
                        raise RuntimeError("config write lock busy") from fl_err
                    time.sleep(_FCNTL_LOCK_RETRY_DELAY_SEC)
            _safe_write_json(tmp_file, data)
            os.replace(tmp_file, target_file)
            o_dir = getattr(os, "O_DIRECTORY", None)
            if o_dir is not None:
                try:
                    dir_fd = os.open(str(target_file.parent), o_dir)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            # Capture the cache key while the lock is held: the content just
            # swapped in cannot be replaced by another process before this
            # stat, so a caller caching its payload can key it exactly.
            return _target_stat_key(target_file)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
    except ImportError as exc:
        logger.debug("fcntl is unavailable, writing without lock: %s", exc)
        _safe_write_json(tmp_file, data)
        os.replace(tmp_file, target_file)
        o_dir = getattr(os, "O_DIRECTORY", None)
        if o_dir is not None:
            try:
                dir_fd = os.open(str(target_file.parent), o_dir)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        return _target_stat_key(target_file)


def _write_and_replace_with_msvcrt_lock(
    data: dict, tmp_file: Path, target_file: Path, lock_file: Path
) -> tuple[int, int] | None:
    """Write and replace with Windows msvcrt.locking."""
    try:
        import msvcrt  # type: ignore[import-untyped]
        import random

        fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        locked = False
        max_lock_retries = 30
        try:
            # Ensure the lock file has at least 1 byte of data so msvcrt.locking succeeds.
            # Otherwise, locking a 0-byte file might fail or be ignored on Windows.
            if os.fstat(fd).st_size < 1:
                try:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass

            for attempt in range(max_lock_retries):
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    locked = True
                    break
                except OSError as err:
                    if attempt < max_lock_retries - 1:
                        base_delay = 0.05 + (0.01 * attempt)
                        jitter = random.SystemRandom().uniform(0.01, 0.05)
                        time.sleep(min(base_delay + jitter, 0.5))
                        continue
                    raise RuntimeError(
                        f"msvcrt lock busy, failed to acquire lock on: {lock_file}"
                    ) from err
            if os.fstat(fd).st_size < 1:
                try:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass
            _safe_write_json(tmp_file, data)
            os.replace(tmp_file, target_file)
            o_dir = getattr(os, "O_DIRECTORY", None)
            if o_dir is not None:
                try:
                    dir_fd = os.open(str(target_file.parent), o_dir)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            # Capture the cache key while the write lock is still held (see
            # _write_and_replace_with_lock).
            return _target_stat_key(target_file)
        finally:
            if locked:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
    except ImportError as exc:
        logger.debug("msvcrt is unavailable, writing without lock: %s", exc)
        # Only reached when msvcrt is genuinely unavailable (not contention),
        # so a lock-free write is the last-resort fallback.
        _safe_write_json(tmp_file, data)
        os.replace(tmp_file, target_file)
        o_dir = getattr(os, "O_DIRECTORY", None)
        if o_dir is not None:
            try:
                dir_fd = os.open(str(target_file.parent), o_dir)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        return _target_stat_key(target_file)
    except RuntimeError as exc:
        # Lock contention after retries: do NOT write lock-free (would risk a
        # partial/corrupted config). Surface the failure so the caller can
        # report it (e.g. a 503) instead of silently losing the modification.
        # Unlike OSError/TypeError, RuntimeError is NOT caught by save_config's
        # retry loop — propagate immediately to avoid endless spinning.
        logger.error("Config save skipped: Windows lock busy after retries (%s)", exc)
        raise
