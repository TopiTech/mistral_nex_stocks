import logging
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
    fetch_events: dict[tuple[str, int], threading.Event]
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


def history_short_cache_key(symbol: str, period: str, interval: str) -> str:
    """Canonical in-memory short-cache key for a yfinance history fetch.

    Single source of truth for the ``history_short_{symbol}_{period}_{interval}``
    key used by both ``services.stock_service`` and ``services.stock_provider``.
    Keeping the format in one place prevents drift when the key scheme changes.
    """
    return f"history_short_{symbol}_{period}_{interval}"


def history_short_payload_cache_key(symbol: str, period: str, interval: str = "auto") -> str:
    """Canonical short-cache key for a serialized history payload (API response)."""
    return f"history_short_payload_{symbol}_{period}_{interval}"


def sanitize_cache_key(key):
    """キャッシュキーを安全にサニタイズ（衝突を避けるため可逆エンコード）

    Description: 安全文字（英数字と ``_`` ``-`` ``.`` ``:``）はそのまま残し、
    それ以外の文字は ``%XX``（大文字 hex）にパーセントエンコードする。
    アンダースコア ``_`` はそのまま残すため ``search_a!b`` → ``search_a%21b`` と
    ``search_a_b`` → ``search_a_b`` が衝突せず、異なる実キーが同一キャッシュキーに
    正規化される問題（UTIL-2）を解消する。``%`` 自体は ``%25`` にエンコードして
    可逆性を保つ。文字列長は従来どおり 256 文字に制限する。
    """
    if not isinstance(key, str):
        key = str(key)
    sanitized: list[str] = []
    for ch in key:
        if ch.isalnum() or ch in "_.-:":
            sanitized.append(ch)
        elif ch == "%":
            sanitized.append("%25")
        else:
            sanitized.append(f"%{ord(ch):02X}")
    # 長すぎるキーを制限
    return "".join(sanitized)[:256]


class _CacheFetching:
    """Sentinel returned by get_cached() when a concurrent fetcher is still
    running and the waiting caller timed out.

    Unlike ``None`` (which callers historically treated as "empty data" and
    converted into empty result sets), this sentinel lets callers distinguish
    "fetch in progress" from "no data". It is falsy so existing
    ``if result:`` / ``bool(result)`` checks keep working.
    """

    def __bool__(self):
        return False


CACHE_FETCHING = _CacheFetching()


def get_cached(key, fetch_func, duration=CACHE_DURATION, valid_func=None):
    """キャッシュ取得かつスタンペード防止"""
    safe_key = sanitize_cache_key(key)

    with global_cache.cache_lock:
        if duration not in global_cache.caches:
            global_cache.caches[duration] = TTLCache(
                maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration
            )
        if safe_key in global_cache.caches[duration]:
            global_cache.record_hit()
            return global_cache.caches[duration][safe_key]

    global_cache.record_miss()

    # The fetch-event must be keyed by (safe_key, duration): two callers asking
    # for the same key with DIFFERENT TTLs must not share one in-flight event,
    # otherwise the second caller waits on the first but then finds its own
    # duration bucket empty and returns None (silently dropping the fetch).
    fetch_key = (safe_key, duration)

    with global_cache.fetch_events_lock:
        if fetch_key in global_cache.fetch_events:
            ev = global_cache.fetch_events[fetch_key]
            is_fetcher = False
        else:
            ev = threading.Event()
            global_cache.fetch_events[fetch_key] = ev
            is_fetcher = True

    if not is_fetcher:
        signaled = ev.wait(timeout=10)
        with global_cache.cache_lock:
            cache = global_cache.caches.get(duration)
            if cache is not None and safe_key in cache:
                return cache[safe_key]
        # Timed out and cache still empty: return the CACHE_FETCHING sentinel
        # (not None) so callers can distinguish "fetch in progress" from
        # "no data". If the event was signaled by the fetcher and the cache is
        # still empty, the fetch completed and produced no valid data, so return None.
        return CACHE_FETCHING if not signaled else None

    try:
        result = fetch_func()
        if valid_func is None or valid_func(result):
            with global_cache.cache_lock:
                if duration not in global_cache.caches:
                    global_cache.caches[duration] = TTLCache(
                        maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration
                    )
                global_cache.caches[duration][safe_key] = result
        return result
    finally:
        with global_cache.fetch_events_lock:
            global_cache.fetch_events.pop(fetch_key, None)
        ev.set()


def peek_cached(key, duration=CACHE_DURATION):
    """Retrieve a value from the cache if it exists, without triggering stampede prevention or blocking."""
    safe_key = sanitize_cache_key(key)
    with global_cache.cache_lock:
        cache = global_cache.caches.get(duration)
        if cache is not None and safe_key in cache:
            return cache[safe_key]
    return None


def clear_cache_prefix(prefix):
    """Clears all cached items starting with the given prefix."""
    prefix_text = sanitize_cache_key(str(prefix))
    with global_cache.cache_lock:
        for cache in global_cache.caches.values():
            keys_to_delete = [
                k
                for k in list(cache.keys())
                if isinstance(k, str) and (k == prefix_text or k.startswith(prefix_text))
            ]
            for k in keys_to_delete:
                cache.pop(k, None)


def _ensure_cache_bucket(duration):
    """Ensures a TTLCache bucket exists for the given duration."""
    with global_cache.cache_lock:
        if duration not in global_cache.caches:
            global_cache.caches[duration] = TTLCache(
                maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration
            )
        return global_cache.caches[duration]


def _has_cached_key(key, duration):
    """Check if a specific key is present in the cache for a given duration."""
    safe = sanitize_cache_key(key)
    with global_cache.cache_lock:
        cache = global_cache.caches.get(duration)
        return bool(cache and safe in cache)


def _set_cached_value(key, value, duration):
    """Explicitly set a value in the cache bucket."""
    safe = sanitize_cache_key(key)
    cache = _ensure_cache_bucket(duration)
    with global_cache.cache_lock:
        cache[safe] = value


def _get_cached_value(key, duration, default=None):
    """Retrieve a value from the cache bucket without triggering a fetch."""
    safe = sanitize_cache_key(key)
    with global_cache.cache_lock:
        cache = global_cache.caches.get(duration)
        if cache is None:
            return default
        return cache.get(safe, default)


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
    if result is CACHE_FETCHING:
        # A concurrent fetch for this key is still running and this caller
        # timed out waiting for it. Do NOT write the negative cache here: the
        # in-flight fetch may succeed right after and populate the positive
        # cache, and poisoning the negative entry would suppress that success
        # for the whole negative TTL.
        return ""
    text = result if isinstance(result, str) else ""
    if text.strip():
        return text

    if not bypass_negative_cache and negative_ttl > 0:
        _set_cached_value(neg_key, True, negative_ttl)
    return text
