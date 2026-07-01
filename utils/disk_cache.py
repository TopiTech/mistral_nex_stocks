# utils/disk_cache.py
"""Persistent disk cache for stock data to survive server restarts.

yfinance does **not** cache stock price data internally — every call triggers
a fresh HTTP request.  This module provides a lightweight JSON-based disk cache
so that:
  1. Cold-start after a server restart can serve recent data immediately.
  2. History endpoints have a fallback when yfinance is rate-limited.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class StockDiskCache:
    """Thread-safe disk cache for stock history and payload data.

    Each entry is stored as a separate JSON file under *cache_dir*.
    Freshness is determined by the file's mtime — no extra metadata files
    are needed.

    Parameters
    ----------
    cache_dir : Path
        Directory where cached JSON files are written.
    max_entries : int
        Hard cap on the number of files kept.  Oldest (by mtime) are evicted
        when the cap is exceeded.
    default_ttl : int
        Default time-to-live in seconds for ``get`` calls.
    """

    def __init__(
        self,
        cache_dir: Path,
        max_entries: int = 500,
        default_ttl: int = 7200,
    ):
        self._cache_dir = cache_dir
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._lock = threading.Lock()
        self._ensure_cache_dir()

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
        # Guard against excessively long filenames
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, ttl: Optional[int] = None) -> Optional[Any]:
        """Return cached value for *key*, or ``None`` if missing / expired."""
        effective_ttl = ttl if ttl is not None else self._default_ttl
        path = self._entry_path(key)

        with self._lock:
            if not path.exists():
                return None
            try:
                age = time.time() - path.stat().st_mtime
                if age > effective_ttl:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    return None
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return data.get("value")
            except (json.JSONDecodeError, IOError, OSError, KeyError) as exc:
                logger.debug("Disk cache read error for %s: %s", key, exc)
                return None

    def has(self, key: str, ttl: Optional[int] = None) -> bool:
        """Return ``True`` if a valid (non-expired) entry exists."""
        return self.get(key, ttl) is not None

    def set(self, key: str, value: Any) -> None:
        """Store *value* under *key* on disk."""
        path = self._entry_path(key)
        try:
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"value": value, "stored_at": time.time()},
                    fh,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            # Atomic rename for thread safety
            tmp_path.replace(path)
        except (IOError, OSError, TypeError) as exc:
            logger.debug("Disk cache write error for %s: %s", key, exc)
            return

        self._evict_if_needed()

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

    def stats(self) -> dict:
        """Return lightweight cache statistics."""
        with self._lock:
            try:
                entries = list(self._cache_dir.glob("*.json"))
                total_size = sum(e.stat().st_size for e in entries)
                return {
                    "disk_cache_entries": len(entries),
                    "disk_cache_total_size_bytes": total_size,
                    "disk_cache_max_entries": self._max_entries,
                }
            except OSError:
                return {
                    "disk_cache_entries": 0,
                    "disk_cache_total_size_bytes": 0,
                    "disk_cache_max_entries": self._max_entries,
                }
