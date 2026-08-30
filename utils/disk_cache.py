# utils/disk_cache.py
"""Persistent disk cache for stock data to survive server restarts.

yfinance does **not** cache stock price data internally — every call triggers
a fresh HTTP request.  This module provides a lightweight JSON-based disk cache
so that:
  1. Cold-start after a server restart can serve recent data immediately.
  2. History endpoints have a fallback when yfinance is rate-limited.
  3. Process-safe operations via platform-native file locking.

File Locking Strategy
---------------------
- **Unix**: ``fcntl.flock`` (stdlib, POSIX)
- **Windows**: ``msvcrt.locking`` (stdlib, Win32)

The lock is advisory and scoped to the open file descriptor, so it correctly
serialises concurrent reads/writes even across threads and processes.
"""

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cleanup interval (seconds) — periodic stale file removal
# ---------------------------------------------------------------------------
_STALE_CLEANUP_INTERVAL = 600.0  # 10 minutes

# Maximum time (seconds) to wait for the cross-process cache lock before
# treating the cache as unavailable. Bounded so a wedged peer process can never
# block cache access (and, transitively, the stock-sync path) indefinitely.
_PROCESS_LOCK_TIMEOUT_SEC = 10.0
_PROCESS_LOCK_POLL_SEC = 0.05


class DiskCacheLockTimeout(OSError):
    """Raised when the cross-process cache lock cannot be acquired within
    ``_PROCESS_LOCK_TIMEOUT_SEC``.

    Subclasses ``OSError`` so the existing broad exception handlers around cache
    call sites (``except OSError`` / ``except Exception``) keep working
    unchanged. Public cache methods degrade gracefully on this error (missing
    entry / skipped write) instead of blocking forever.
    """


_DEGRADED_RETRY_AFTER_SEC = 10.0
_last_lock_timeout_ts: float = 0.0


def _note_lock_timeout() -> None:
    global _last_lock_timeout_ts
    try:
        _last_lock_timeout_ts = time.time()
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.debug("Failed to record lock timeout timestamp: %s", exc)


def is_disk_cache_degraded(within_sec: float = _DEGRADED_RETRY_AFTER_SEC) -> bool:
    try:
        return (time.time() - _last_lock_timeout_ts) < within_sec
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.debug("is_disk_cache_degraded check failed: %s", exc)
        return False


def _read_stale_payload(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("value")
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.debug("Failed to read stale payload %s: %s", path, exc)
        return None


def _remove_fields_recursive(value: Any, fields: frozenset[str]) -> tuple[Any, bool]:
    """Return a JSON-compatible copy with matching mapping keys removed."""
    if isinstance(value, dict):
        changed = False
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key in fields:
                changed = True
                continue
            cleaned_item, item_changed = _remove_fields_recursive(item, fields)
            cleaned[key] = cleaned_item
            changed = changed or item_changed
        return cleaned, changed
    if isinstance(value, list):
        changed = False
        cleaned_list = []
        for item in value:
            cleaned_item, item_changed = _remove_fields_recursive(item, fields)
            cleaned_list.append(cleaned_item)
            changed = changed or item_changed
        return cleaned_list, changed
    return value, False


class StockDiskCache:
    """Thread-safe and process-safe disk cache for stock history and payload data.

    Each entry is stored as a separate JSON file under *cache_dir*.
    Freshness is determined by a ``stored_at`` timestamp embedded in the file
    (fallback: file mtime).

    Parameters
    ----------
    cache_dir : Path
        Directory where cached JSON files are written.
    max_entries : int
        Hard cap on the number of files kept.  Oldest (by mtime) are evicted
        when the cap is exceeded.
    default_ttl : int
        Default time-to-live in seconds for ``get`` calls.
    enable_cleanup : bool
        If True, runs periodic cleanup of stale entries every ~10 minutes.
    """

    def __init__(
        self,
        cache_dir: Path,
        max_entries: int = 500,
        default_ttl: int = 7200,
        enable_cleanup: bool = True,
    ):
        self._cache_dir = cache_dir
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        # Cleanup is triggered by operations that already hold this lock.
        self._lock = threading.RLock()
        self._last_cleanup_ts: float = 0.0
        self._enable_cleanup = enable_cleanup
        self._initialized = False
        self._process_lock_path = self._cache_dir / ".cache.lock"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_cache_dir(self) -> None:
        if not self._initialized:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._initialized = True
            except OSError as exc:
                logger.warning("Failed to create disk cache directory %s: %s", self._cache_dir, exc)

    @contextmanager
    def _process_lock(self):
        """Hold a persistent advisory lock shared by all cache processes.

        Acquisition is bounded: the lock is polled with non-blocking calls and
        ``DiskCacheLockTimeout`` is raised after ``_PROCESS_LOCK_TIMEOUT_SEC``,
        so a wedged peer process degrades the cache to unavailable instead of
        blocking every cache user (and the stock-sync path) indefinitely.
        """
        self._ensure_cache_dir()
        deadline = time.monotonic() + _PROCESS_LOCK_TIMEOUT_SEC
        if os.name == "nt":
            import msvcrt

            msvcrt_module = cast(Any, msvcrt)
            fd = os.open(str(self._process_lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            locked = False
            try:
                if os.fstat(fd).st_size == 0:
                    try:
                        os.write(fd, b"L")
                        os.lseek(fd, 0, os.SEEK_SET)
                    except OSError:
                        pass
                while True:
                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt_module.locking(fd, msvcrt_module.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise DiskCacheLockTimeout(
                                f"Could not acquire disk cache lock within "
                                f"{_PROCESS_LOCK_TIMEOUT_SEC}s: {self._process_lock_path}"
                            ) from None
                        time.sleep(_PROCESS_LOCK_POLL_SEC)
                if os.fstat(fd).st_size == 0:
                    try:
                        os.write(fd, b"L")
                        os.lseek(fd, 0, os.SEEK_SET)
                    except OSError:
                        pass
                yield
            finally:
                if locked:
                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt_module.locking(fd, msvcrt_module.LK_UNLCK, 1)
                    except OSError:
                        pass
                try:
                    os.close(fd)
                except OSError:
                    pass
        else:
            import fcntl

            fcntl_module = cast(Any, fcntl)
            with self._process_lock_path.open("a+", encoding="utf-8") as handle:
                while True:
                    try:
                        fcntl_module.flock(
                            handle.fileno(), fcntl_module.LOCK_EX | fcntl_module.LOCK_NB
                        )
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise DiskCacheLockTimeout(
                                f"Could not acquire disk cache lock within "
                                f"{_PROCESS_LOCK_TIMEOUT_SEC}s: {self._process_lock_path}"
                            ) from None
                        time.sleep(_PROCESS_LOCK_POLL_SEC)
                try:
                    yield
                finally:
                    fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)

    def _entry_path(self, key: str) -> Path:
        """Map *key* to a filesystem-safe filename."""
        # Keep a readable prefix but include the complete-key digest. Pure
        # sanitisation is not injective (``stock/a`` and ``stock_a`` collide),
        # and truncating long keys creates another collision class.
        import hashlib

        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        safe_key = safe_key[:120]
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{safe_key}_{digest}.json"

    def _evict_if_needed(self) -> None:
        """Remove oldest files when the entry count exceeds *max_entries*."""
        try:
            entries: list[tuple[Path, float]] = []
            for p in self._cache_dir.glob("*.json"):
                try:
                    entries.append((p, p.stat().st_mtime))
                except OSError:
                    continue
            entries.sort(key=lambda x: x[1], reverse=True)
            if len(entries) <= self._max_entries:
                return
            for entry, _ in entries[self._max_entries :]:
                try:
                    entry.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _remove_stale_entries(self) -> int:
        """Remove all entries whose age exceeds the maximum allowed TTL.

        Returns the number of entries removed.
        """
        removed = 0
        now = time.time()
        # 2026-07 Refactor: Use the maximum potential TTL (86400 seconds / 24h for stock details)
        # to ensure that files with custom TTLs longer than default_ttl (7200s) are not
        # prematurely unlinked by the background cleanup task.
        max_ttl = max(self._default_ttl, 86400)
        try:
            for entry in self._cache_dir.glob("*.json"):
                try:
                    age = now - entry.stat().st_mtime
                    if age > max_ttl:
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            pass
        if removed:
            logger.debug("Disk cache: removed %d stale entries from %s", removed, self._cache_dir)
        return removed

    def _maybe_run_cleanup(self, force: bool = False) -> None:
        """Run stale entry cleanup if enough time has passed since last run."""
        now = time.time()
        if not force and (now - self._last_cleanup_ts < _STALE_CLEANUP_INTERVAL):
            return
        self._last_cleanup_ts = now
        with self._lock:
            self._remove_stale_entries()
            self._evict_if_needed()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, ttl: int | None = None, ignore_ttl: bool = False) -> Any | None:
        """Return cached value for *key*, or ``None`` if missing / expired.

        The entire check-and-read sequence is performed inside the lock to
        prevent TOCTOU (time-of-check / time-of-use) race conditions between
        threads and processes.

        If the cross-process lock cannot be acquired within
        ``_PROCESS_LOCK_TIMEOUT_SEC`` (a wedged peer process), the read is
        degraded to ``None`` instead of blocking indefinitely.
        """
        effective_ttl = ttl if ttl is not None else self._default_ttl
        path = self._entry_path(key)

        try:
            with self._lock, self._process_lock():
                self._maybe_run_cleanup()
                if not path.exists():
                    return None
                try:
                    age = time.time() - path.stat().st_mtime
                    if not ignore_ttl and age > effective_ttl:
                        path.unlink(missing_ok=True)
                        return None
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        logger.debug(
                            "Disk cache entry %s has unexpected shape (expected dict, got %s); "
                            "treating as corrupt",
                            key,
                            type(data).__name__,
                        )
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return None
                    return data.get("value")
                except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
                    logger.warning("Disk cache corrupt entry detected for %s; unlinking: %s", key, exc)
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return None
                except OSError as exc:
                    logger.debug("Disk cache read error for %s: %s", key, exc)
                    return None
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            # R9: degraded window tracked via _note_lock_timeout; callers check
            # is_disk_cache_degraded() to suppress yfinance re-fetch amplification.
            # get() still returns None (original contract) so callers explicitly
            # decide to serve stale via get_stale_or_degraded(); stale-while-revalidate
            # is opt-in to keep test semantics stable.
            logger.warning(
                "Disk cache read degraded (lock busy) for %s: %s (Retry-After %ds)",
                key,
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )
            return None
        except OSError as exc:
            logger.warning(
                "Disk cache read degraded due to filesystem I/O (%s)",
                type(exc).__name__,
            )
            return None

    def get_stale(self, key: str) -> object:
        """Best-effort stale read without acquiring the cross-process lock (R9).

        Used during degraded window to avoid treating lock contention as cache
        miss and triggering immediate yfinance re-fetch loop.
        """
        try:
            path = self._entry_path(key)
            return _read_stale_payload(path)
        except Exception:
            return None

    def has(self, key: str, ttl: int | None = None) -> bool:
        """Return ``True`` if a valid (non-expired) entry exists.

        Performs a lightweight check (file existence + mtime) without reading
        or parsing the JSON content. More efficient than ``get() is not None``
        when only presence is needed.
        """
        effective_ttl = ttl if ttl is not None else self._default_ttl
        path = self._entry_path(key)
        try:
            with self._lock, self._process_lock():
                if not path.exists():
                    return False
                try:
                    return time.time() - path.stat().st_mtime <= effective_ttl
                except OSError:
                    return False
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            logger.warning(
                "Disk cache existence check degraded (lock busy) for %s: %s (Retry-After %ds)",
                key,
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )
            return False

    def set(self, key: str, value: Any) -> None:
        """Store *value* under *key* on disk.

        If the cross-process lock cannot be acquired within
        ``_PROCESS_LOCK_TIMEOUT_SEC``, the write is skipped (logged) instead of
        blocking indefinitely.
        """
        path = self._entry_path(key)
        # Use UUID for temp file to avoid potential thread-ID reuse collisions
        # (threading.get_ident() IDs can be recycled by the OS).
        tmp_path = path.with_suffix(f".{uuid.uuid4().hex[:12]}.tmp")
        try:
            with self._lock, self._process_lock():
                try:
                    # Ensure cache directory exists before writing
                    self._cache_dir.mkdir(parents=True, exist_ok=True)
                    with open(tmp_path, "w", encoding="utf-8") as fh:
                        json.dump(
                            {"value": value, "stored_at": time.time()},
                            fh,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        fh.flush()
                        try:
                            os.fsync(fh.fileno())
                        except OSError:
                            pass
                    for attempt in range(4):
                        try:
                            os.replace(str(tmp_path), str(path))
                            break
                        except PermissionError:
                            if os.name == "nt" and attempt < 3:
                                time.sleep(0.015 * (attempt + 1))
                                continue
                            raise
                    o_dir = getattr(os, "O_DIRECTORY", None)
                    if os.name != "nt" and o_dir is not None:
                        try:
                            dir_fd = os.open(str(path.parent), o_dir)
                            try:
                                os.fsync(dir_fd)
                            finally:
                                os.close(dir_fd)
                        except OSError:
                            pass
                except (OSError, TypeError) as exc:
                    logger.debug("Disk cache write error for %s: %s", key, exc)
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                    return

                self._evict_if_needed()
                self._maybe_run_cleanup()
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            logger.warning(
                "Disk cache write skipped (lock busy) for %s: %s (Retry-After %ds)",
                key,
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def delete(self, key: str) -> bool:
        """Remove a specific entry.  Returns ``True`` if it existed.

        Returns ``False`` when the cross-process lock cannot be acquired within
        ``_PROCESS_LOCK_TIMEOUT_SEC`` (degraded; no blocking).
        """
        path = self._entry_path(key)
        try:
            with self._lock, self._process_lock():
                if path.exists():
                    try:
                        path.unlink()
                        return True
                    except OSError:
                        pass
            return False
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            logger.warning(
                "Disk cache delete skipped (lock busy) for %s: %s (Retry-After %ds)",
                key,
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )
            return False

    def delete_prefix(self, prefix: str) -> int:
        """Remove all entries whose key starts with *prefix*.

        Returns the number of files actually removed.
        """
        if not prefix or not str(prefix).strip():
            logger.warning("delete_prefix called with empty prefix; ignoring")
            return 0
        safe_prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(prefix))
        if not safe_prefix.strip("_"):
            logger.warning("delete_prefix called with non-alphanumeric prefix; ignoring")
            return 0
        removed = 0
        try:
            with self._lock, self._process_lock():
                for entry in self._cache_dir.glob("*.json"):
                    if entry.stem.startswith(safe_prefix):
                        try:
                            entry.unlink()
                            removed += 1
                        except OSError:
                            pass
            return removed
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            logger.warning(
                "Disk cache delete_prefix skipped (lock busy) for %s: %s (Retry-After %ds)",
                prefix,
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )
            return 0

    def clear(self) -> None:
        """Remove **all** cached entries.

        Degrades to a logged no-op when the cross-process lock cannot be
        acquired within ``_PROCESS_LOCK_TIMEOUT_SEC``.
        """
        try:
            with self._lock, self._process_lock():
                for entry in self._cache_dir.glob("*.json"):
                    try:
                        entry.unlink()
                    except OSError:
                        pass
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            logger.warning(
                "Disk cache clear skipped (lock busy): %s (Retry-After %ds)",
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )

    def remove_fields_recursive(self, field_names: Iterable[str]) -> int:
        """Atomically remove sensitive mapping fields from all JSON entries.

        This is used for cache-schema migrations where an older application
        version persisted fields that no longer belong on disk. Corrupt entries
        are deleted because cache data is disposable and may still contain a
        readable sensitive fragment.

        R9: migration is chunked so the cross-process lock is not held for the
        entire scan. Each entry is processed under a short lock acquisition and
        the critical section is kept minimal; periodic yields cap consecutive
        lock-hold time.
        """
        fields = frozenset(name for name in field_names if name)
        if not fields:
            return 0

        migrated = 0
        try:
            entries = list(self._cache_dir.glob("*.json"))
        except OSError:
            return 0
        CHUNK = 50
        for idx, entry in enumerate(entries):
            tmp_path = entry.with_suffix(f".{uuid.uuid4().hex[:12]}.tmp")
            try:
                with self._lock, self._process_lock():
                    try:
                        with entry.open("r", encoding="utf-8") as handle:
                            payload = json.load(handle)
                        cleaned, changed = _remove_fields_recursive(payload, fields)
                        if not changed:
                            continue
                        with tmp_path.open("w", encoding="utf-8") as handle:
                            json.dump(
                                cleaned,
                                handle,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            handle.flush()
                            try:
                                os.fsync(handle.fileno())
                            except OSError:
                                pass
                        os.replace(str(tmp_path), str(entry))
                        migrated += 1
                    except (TypeError, json.JSONDecodeError) as exc:
                        logger.debug("Deleting invalid disk cache entry %s: %s", entry, exc)
                        try:
                            entry.unlink(missing_ok=True)
                        except OSError:
                            pass
                    except DiskCacheLockTimeout as exc:
                        _note_lock_timeout()
                        logger.warning(
                            "Disk cache field migration skipped (lock busy) for %s: %s (Retry-After %ds)",
                            entry,
                            exc,
                            int(_DEGRADED_RETRY_AFTER_SEC),
                        )
                    except OSError as exc:
                        logger.debug("Disk cache field migration skipped for %s: %s", entry, exc)
                    finally:
                        if tmp_path.exists():
                            try:
                                tmp_path.unlink()
                            except OSError:
                                pass
            except DiskCacheLockTimeout as exc:
                _note_lock_timeout()
                logger.warning(
                    "Disk cache field migration skipped (lock busy): %s (Retry-After %ds)",
                    exc,
                    int(_DEGRADED_RETRY_AFTER_SEC),
                )
            except OSError as exc:
                logger.warning("Disk cache field migration skipped (cache unavailable): %s", exc)
            if (idx + 1) % CHUNK == 0:
                time.sleep(0.01)
        return migrated

    def cleanup(self) -> int:
        """Force an immediate cleanup of stale and excess entries.

        Returns the number of entries removed.

        Returns 0 when the cross-process lock cannot be acquired within
        ``_PROCESS_LOCK_TIMEOUT_SEC`` (degraded; no blocking).
        """
        removed = 0
        try:
            with self._lock, self._process_lock():
                removed += self._remove_stale_entries()
                before = len(list(self._cache_dir.glob("*.json")))
                self._evict_if_needed()
                after = len(list(self._cache_dir.glob("*.json")))
                removed += max(0, before - after)
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            logger.warning(
                "Disk cache cleanup skipped (lock busy): %s (Retry-After %ds)",
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )
            return 0
        self._last_cleanup_ts = time.time()
        return removed

    def stats(self) -> dict:
        """Return lightweight cache statistics.

        The returned dict contains:
        - disk_cache_entries
        - disk_cache_total_size_bytes
        - disk_cache_max_entries
        - disk_cache_default_ttl
        - disk_cache_last_cleanup_ts (epoch seconds, 0 if never run)
        """
        empty_stats = {
            "disk_cache_entries": 0,
            "disk_cache_total_size_bytes": 0,
            "disk_cache_max_entries": self._max_entries,
            "disk_cache_default_ttl": self._default_ttl,
            "disk_cache_last_cleanup_ts": self._last_cleanup_ts,
        }
        try:
            with self._lock, self._process_lock():
                try:
                    entries = list(self._cache_dir.glob("*.json"))
                    total_size = sum(e.stat().st_size for e in entries)
                    return {
                        "disk_cache_entries": len(entries),
                        "disk_cache_total_size_bytes": total_size,
                        "disk_cache_max_entries": self._max_entries,
                        "disk_cache_default_ttl": self._default_ttl,
                        "disk_cache_last_cleanup_ts": self._last_cleanup_ts,
                    }
                except OSError:
                    return empty_stats
        except DiskCacheLockTimeout as exc:
            _note_lock_timeout()
            logger.warning(
                "Disk cache stats degraded (lock busy): %s (Retry-After %ds)",
                exc,
                int(_DEGRADED_RETRY_AFTER_SEC),
            )
            return empty_stats
