import logging
import re
import threading
from typing import Any
from cachetools import TTLCache

from constants import CACHE_DURATION, STOCK_HISTORY_CACHE_MAXSIZE

logger = logging.getLogger(__name__)

class CacheState:
    """Global TTLCache and fetch event manager.

    Lock Hierarchy & Deadlock Prevention Rules:
    -------------------------------------------
    Lock ordering (ascending granularity):
      1. self.stats_lock       - lightweight, short duration only
      2. self.fetch_events_lock - guard for concurrent fetch Events
      3. self.cache_lock        - protects in-memory cache dicts (TTLCache)
      4. self.file_lock         - file storage writes (I/O bound)
      5. self.sse_data_lock     - RLock for SSE shared memory (broadest scope)

    To prevent deadlocks:
    - Always acquire locks in the order listed above (ascending).
    - Never acquire multiple locks concurrently (no nested lock holds).
    - Use locks in a short, localized scope.
    - Prefer RLock over Lock when the same thread may re-enter.
    """

    caches: dict[int, TTLCache]
    cache_lock: threading.Lock
    file_lock: threading.Lock
    fetch_events: dict[str, threading.Event]
    fetch_events_lock: threading.Lock
    sse_data_lock: threading.RLock
    stats_lock: threading.Lock
    cache_hits: int
    cache_misses: int

    def __init__(self) -> None:
        self.caches = {}  # Map of duration -> TTLCache
        self.cache_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.fetch_events = {}
        self.fetch_events_lock = threading.Lock()
        self.sse_data_lock = threading.RLock()
        self.stats_lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0

    def record_hit(self) -> None:
        with self.stats_lock:
            self.cache_hits += 1

    def record_miss(self) -> None:
        with self.stats_lock:
            self.cache_misses += 1

    def get_stats(self) -> dict[str, Any]:
        with self.stats_lock:
            total = self.cache_hits + self.cache_misses
            hit_rate = (self.cache_hits / total * 100) if total > 0 else 0.0
            return {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "total": total,
                "hit_rate_pct": round(hit_rate, 2),
            }

    def reset_stats(self) -> None:
        with self.stats_lock:
            self.cache_hits = 0
            self.cache_misses = 0

global_cache = CacheState()

def sanitize_cache_key(key):
    """キャッシュキーを安全にサニタイズ"""
    if not isinstance(key, str):
        key = str(key)
    # 危険な文字を削除
    sanitized = re.sub(r"[^\w\-:._]", "_", key)
    # 長すぎるキーを制限
    return sanitized[:256]


def get_cached(key, fetch_func, duration=CACHE_DURATION, valid_func=None):
    """キャッシュ取得かつスタンペード防止"""
    safe_key = sanitize_cache_key(key)

    with global_cache.cache_lock:
        if duration not in global_cache.caches:
            global_cache.caches[duration] = TTLCache(maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration)
        if safe_key in global_cache.caches[duration]:
            global_cache.record_hit()
            return global_cache.caches[duration][safe_key]

    global_cache.record_miss()

    with global_cache.fetch_events_lock:
        if safe_key in global_cache.fetch_events:
            ev = global_cache.fetch_events[safe_key]
            is_fetcher = False
        else:
            ev = threading.Event()
            global_cache.fetch_events[safe_key] = ev
            is_fetcher = True

    if not is_fetcher:
        ev.wait(timeout=10)
        with global_cache.cache_lock:
            cache = global_cache.caches.get(duration)
            if cache is not None and safe_key in cache:
                return cache[safe_key]
        # Timed out and cache still empty: return None to avoid re-executing
        # fetch_func here (that would defeat the stampede-prevention purpose).
        # The fetcher thread will populate the cache on its own schedule.
        return None

    try:
        result = fetch_func()
        if valid_func is None or valid_func(result):
            with global_cache.cache_lock:
                if duration not in global_cache.caches:
                    global_cache.caches[duration] = TTLCache(maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration)
                global_cache.caches[duration][safe_key] = result
        return result
    finally:
        with global_cache.fetch_events_lock:
            global_cache.fetch_events.pop(safe_key, None)
        ev.set()


def clear_cache_prefix(prefix):
    """Clears all cached items starting with the given prefix."""
    prefix_text = sanitize_cache_key(str(prefix))
    with global_cache.cache_lock:
        for _duration, cache in global_cache.caches.items():
            keys_to_delete = [
                k
                for k in list(cache.keys())
                if isinstance(k, str)
                and (k == prefix_text or k.startswith(prefix_text))
            ]
            for k in keys_to_delete:
                cache.pop(k, None)


def _ensure_cache_bucket(duration):
    """Ensures a TTLCache bucket exists for the given duration."""
    with global_cache.cache_lock:
        if duration not in global_cache.caches:
            global_cache.caches[duration] = TTLCache(maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration)
        return global_cache.caches[duration]


def _has_cached_key(key, duration):
    """Check if a specific key is present in the cache for a given duration."""
    with global_cache.cache_lock:
        cache = global_cache.caches.get(duration)
        return bool(cache and key in cache)


def _set_cached_value(key, value, duration):
    """Explicitly set a value in the cache bucket."""
    cache = _ensure_cache_bucket(duration)
    with global_cache.cache_lock:
        cache[key] = value


def _get_cached_value(key, duration, default=None):
    """Retrieve a value from the cache bucket without triggering a fetch."""
    with global_cache.cache_lock:
        cache = global_cache.caches.get(duration)
        if cache is None:
            return default
        return cache.get(key, default)


def get_cached_context_with_negative_cache(
    key, fetch_func, success_ttl=600, negative_ttl=90, bypass_negative_cache=False
):
    """ネガティブキャッシュ付きでコンテキストを取得する。"""
    neg_key = f"{key}__negative"
    if not bypass_negative_cache and _has_cached_key(neg_key, negative_ttl):
        return ""

    result = get_cached(
        key,
        fetch_func,
        duration=success_ttl,
        valid_func=lambda x: bool(isinstance(x, str) and x.strip()),
    )
    text = result if isinstance(result, str) else ""
    if text.strip():
        return text

    if not bypass_negative_cache and negative_ttl > 0:
        _set_cached_value(neg_key, True, negative_ttl)
    return text
