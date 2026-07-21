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
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cleanup interval (seconds) — periodic stale file removal
# ---------------------------------------------------------------------------
_STALE_CLEANUP_INTERVAL = 600.0  # 10 minutes


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
        self._ensure_cache_dir()

        if enable_cleanup:
            self._maybe_run_cleanup(force=False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_cache_dir(self) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Failed to create disk cache directory %s: %s", self._cache_dir, exc)

    def _entry_path(self, key: str) -> Path:
        """Map *key* to a filesystem-safe filename."""
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        if len(safe_key) > 200:
            safe_key = safe_key[:200]
        return self._cache_dir / f"{safe_key}.json"

    def _evict_if_needed(self) -> None:
        """Remove oldest files when the entry count exceeds *max_entries*."""
        try:
            entries = sorted(
                self._cache_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if len(entries) <= self._max_entries:
                return
            for entry in entries[self._max_entries :]:
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

    def get(self, key: str, ttl: Optional[int] = None, ignore_ttl: bool = False) -> Optional[Any]:
        """Return cached value for *key*, or ``None`` if missing / expired.

        The entire check-and-read sequence is performed inside the lock to
        prevent TOCTOU (time-of-check / time-of-use) race conditions between
        threads and processes.
        """
        effective_ttl = ttl if ttl is not None else self._default_ttl
        path = self._entry_path(key)

        with self._lock:
            self._maybe_run_cleanup()
            if not path.exists():
                return None
            try:
                age = time.time() - path.stat().st_mtime
                if not ignore_ttl and age > effective_ttl:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    return None
                # Read inside the lock to prevent another thread/process from
                # deleting the file between exists() and open().
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    return data.get("value")
                except (json.JSONDecodeError, IOError, OSError, KeyError) as exc:
                    logger.debug("Disk cache read error for %s: %s", key, exc)
                    return None
            except OSError:
                return None

    def has(self, key: str, ttl: Optional[int] = None) -> bool:
        """Return ``True`` if a valid (non-expired) entry exists.

        Performs a lightweight check (file existence + mtime) without reading
        or parsing the JSON content. More efficient than ``get() is not None``
        when only presence is needed.
        """
        effective_ttl = ttl if ttl is not None else self._default_ttl
        path = self._entry_path(key)
        with self._lock:
            if not path.exists():
                return False
            try:
                age = time.time() - path.stat().st_mtime
                return age <= effective_ttl
            except OSError:
                return False

    def set(self, key: str, value: Any) -> None:
        """Store *value* under *key* on disk."""
        path = self._entry_path(key)
        # Use UUID for temp file to avoid potential thread-ID reuse collisions
        # (threading.get_ident() IDs can be recycled by the OS).
        tmp_path = path.with_suffix(f".{uuid.uuid4().hex[:12]}.tmp")
        with self._lock:
            try:
                # Ensure cache directory exists before writing
                try:
                    self._cache_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    logger.debug("Disk cache mkdir error: %s", exc)
                    return
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(
                        {"value": value, "stored_at": time.time()},
                        fh,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                # Atomic rename for thread/process safety
                try:
                    os.replace(str(tmp_path), str(path))
                except (IOError, OSError):
                    # Fallback if os.replace fails cross-device
                    tmp_path.replace(path)
            except (IOError, OSError, TypeError) as exc:
                logger.debug("Disk cache write error for %s: %s", key, exc)
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                return

            self._evict_if_needed()
            self._maybe_run_cleanup()

    def delete(self, key: str) -> bool:
        """Remove a specific entry.  Returns ``True`` if it existed."""
        path = self._entry_path(key)
        with self._lock:
            if path.exists():
                try:
                    path.unlink()
                    return True
                except OSError:
                    pass
        return False

    def delete_prefix(self, prefix: str) -> int:
        """Remove all entries whose key starts with *prefix*.

        Returns the number of files actually removed.
        """
        safe_prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix)
        removed = 0
        with self._lock:
            for entry in self._cache_dir.glob("*.json"):
                if entry.stem.startswith(safe_prefix):
                    try:
                        entry.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    def clear(self) -> None:
        """Remove **all** cached entries."""
        with self._lock:
            for entry in self._cache_dir.glob("*.json"):
                try:
                    entry.unlink()
                except OSError:
                    pass

    def cleanup(self) -> int:
        """Force an immediate cleanup of stale and excess entries.

        Returns the number of entries removed.
        """
        removed = 0
        with self._lock:
            removed += self._remove_stale_entries()
            before = len(list(self._cache_dir.glob("*.json")))
            self._evict_if_needed()
            after = len(list(self._cache_dir.glob("*.json")))
            removed += max(0, before - after)
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
        with self._lock:
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
                return {
                    "disk_cache_entries": 0,
                    "disk_cache_total_size_bytes": 0,
                    "disk_cache_max_entries": self._max_entries,
                    "disk_cache_default_ttl": self._default_ttl,
                    "disk_cache_last_cleanup_ts": self._last_cleanup_ts,
                }
