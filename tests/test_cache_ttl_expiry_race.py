"""Regression tests for TTL-expiry races in ``utils.caching.get_cached``.

``cachetools.TTLCache`` evaluates expiry on access. The previous code used a
membership test followed by a separate value lookup:

    if safe_key in cache_bucket:
        cached_value = cache_bucket[safe_key]

When the TTL boundary falls between those two calls, the second lookup raises
``KeyError`` even though the cache lock is held. ``get_cached`` must perform a
single expiry-evaluating lookup (the same single-lookup rule already used by
``_get_cached_value_if_present``) on both the fast path and the
wait-for-fetcher path.
"""

import threading
import unittest

from cachetools import TTLCache

from utils import caching


class _ExpiresBetweenLookupsCache(TTLCache):
    """Cache whose entry disappears between a membership test and getitem."""

    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        raise KeyError(key)


class CacheTtlExpiryRaceTests(unittest.TestCase):
    def setUp(self):
        self.duration = 864000  # dedicated bucket; not used by application code
        self.raw_key = "expiry-race-key"
        self.safe_key = caching.sanitize_cache_key(self.raw_key)
        self.fetch_key = (self.safe_key, self.duration)
        self._remove_state()

    def tearDown(self):
        self._remove_state()

    def _remove_state(self):
        with caching.global_cache.cache_lock:
            caching.global_cache.caches.pop(self.duration, None)
        with caching.global_cache.fetch_events_lock:
            caching.global_cache.fetch_events.pop(self.fetch_key, None)

    def _install_racy_cache(self):
        bucket = _ExpiresBetweenLookupsCache(maxsize=8, ttl=self.duration)
        with caching.global_cache.cache_lock:
            caching.global_cache.caches[self.duration] = bucket
        return bucket

    def test_expired_entry_between_membership_and_getitem_fetches_instead_of_raising(self):
        """A cache hit that expires mid-check must fall through to the fetch."""
        self._install_racy_cache()
        calls = []

        def fetch():
            calls.append(1)
            return {"value": 42}

        result = caching.get_cached(self.raw_key, fetch, duration=self.duration)

        self.assertEqual(result, {"value": 42})
        self.assertEqual(len(calls), 1)

    def test_waiting_caller_survives_expiry_between_membership_and_getitem(self):
        """The single-flight wait path must not raise if the value expires."""
        self._install_racy_cache()
        event = threading.Event()
        event.set()
        with caching.global_cache.fetch_events_lock:
            caching.global_cache.fetch_events[self.fetch_key] = event

        result = caching.get_cached(
            self.raw_key, lambda: "unused", duration=self.duration
        )

        self.assertIsNone(result)

    def test_peek_cached_treats_expiry_as_a_miss(self):
        """peek_cached must return None instead of raising at the TTL boundary."""
        self._install_racy_cache()

        self.assertIsNone(caching.peek_cached(self.raw_key, duration=self.duration))

    def test_get_cached_value_treats_expiry_as_default(self):
        """_get_cached_value must not let cachetools' get() raise KeyError."""
        self._install_racy_cache()

        self.assertEqual(
            caching._get_cached_value(self.raw_key, self.duration, default="fallback"),
            "fallback",
        )

    def test_get_cached_value_if_present_treats_expiry_as_absent(self):
        """The presence helper must report a miss for an expired entry."""
        self._install_racy_cache()

        self.assertEqual(
            caching._get_cached_value_if_present(
                self.raw_key, self.duration, default="fallback"
            ),
            (False, "fallback"),
        )


if __name__ == "__main__":
    unittest.main()
