import logging
import re
import threading
from cachetools import TTLCache

from app_state import app_state
from constants import CACHE_DURATION, STOCK_HISTORY_CACHE_MAXSIZE

logger = logging.getLogger(__name__)


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

    with app_state.cache.cache_lock:
        if duration not in app_state.cache.caches:
            app_state.cache.caches[duration] = TTLCache(maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration)
        if safe_key in app_state.cache.caches[duration]:
            app_state.record_hit()
            return app_state.cache.caches[duration][safe_key]

    app_state.record_miss()

    with app_state.cache.fetch_events_lock:
        if safe_key in app_state.cache.fetch_events:
            ev = app_state.cache.fetch_events[safe_key]
            is_fetcher = False
        else:
            ev = threading.Event()
            app_state.cache.fetch_events[safe_key] = ev
            is_fetcher = True

    if not is_fetcher:
        ev.wait(timeout=10)
        with app_state.cache.cache_lock:
            cache = app_state.cache.caches.get(duration, {})
            if safe_key in cache:
                return cache[safe_key]
        # Re-check: another thread may have populated while we waited above
        with app_state.cache.cache_lock:
            cache = app_state.cache.caches.get(duration, {})
            if safe_key in cache:
                return cache[safe_key]
        return fetch_func()

    try:
        result = fetch_func()
        if valid_func is None or valid_func(result):
            with app_state.cache.cache_lock:
                if duration not in app_state.cache.caches:
                    app_state.cache.caches[duration] = TTLCache(maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration)
                app_state.cache.caches[duration][safe_key] = result
        return result
    finally:
        with app_state.cache.fetch_events_lock:
            app_state.cache.fetch_events.pop(safe_key, None)
        ev.set()


def clear_cache_prefix(prefix):
    """Clears all cached items starting with the given prefix."""
    prefix_text = sanitize_cache_key(str(prefix))
    with app_state.cache.cache_lock:
        for _duration, cache in app_state.cache.caches.items():
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
    with app_state.cache.cache_lock:
        if duration not in app_state.cache.caches:
            app_state.cache.caches[duration] = TTLCache(maxsize=STOCK_HISTORY_CACHE_MAXSIZE, ttl=duration)
        return app_state.cache.caches[duration]


def _has_cached_key(key, duration):
    """Check if a specific key is present in the cache for a given duration."""
    with app_state.cache.cache_lock:
        cache = app_state.cache.caches.get(duration)
        return bool(cache and key in cache)


def _set_cached_value(key, value, duration):
    """Explicitly set a value in the cache bucket."""
    cache = _ensure_cache_bucket(duration)
    with app_state.cache.cache_lock:
        cache[key] = value


def _get_cached_value(key, duration, default=None):
    """Retrieve a value from the cache bucket without triggering a fetch."""
    with app_state.cache.cache_lock:
        cache = app_state.cache.caches.get(duration)
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
