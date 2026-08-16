"""Regression tests for R5 (UTIL-1), R6 (UTIL-2), R7 (UTIL-3) fixes.

These tests verify that the `utils/` fixes handle edge cases that previously
caused runtime errors or data corruption:
  - R5: ``parse_retry_after`` clamps inf/NaN/negative/overlarge values.
  - R6: ``sanitize_cache_key`` produces injective (collision-free) keys.
  - R7: ``StockDiskCache.get`` returns ``None`` for list-shaped cache files
        instead of raising ``AttributeError``.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# =========================================================================
# R5 (UTIL-1): parse_retry_after clamps non-finite / negative / huge values
# =========================================================================


class R5ParseRetryAfterClampTest(unittest.TestCase):
    """parse_retry_after must reject or clamp inf/NaN/negative/overlarge values."""

    def _resp(self, headers: dict) -> SimpleNamespace:
        return SimpleNamespace(headers=headers)

    # --- Second-based Retry-After ---

    def test_retry_after_inf_returns_none(self):
        from utils.http_utils import parse_retry_after

        result = parse_retry_after(self._resp({"Retry-After": "inf"}))
        self.assertIsNone(result, f"Expected None for 'inf', got {result!r}")

    def test_retry_after_nan_returns_none(self):
        from utils.http_utils import parse_retry_after

        result = parse_retry_after(self._resp({"Retry-After": "NaN"}))
        self.assertIsNone(result, f"Expected None for 'NaN', got {result!r}")

    def test_retry_after_negative_returns_zero(self):
        from utils.http_utils import parse_retry_after

        result = parse_retry_after(self._resp({"Retry-After": "-30"}))
        self.assertEqual(result, 0.0, f"Expected 0.0 for '-30', got {result!r}")

    def test_retry_after_overlarge_clamped(self):
        from utils.http_utils import _MAX_RETRY_AFTER_SEC, parse_retry_after

        # 999999 seconds exceeds max (86400) → should be clamped to max
        result = parse_retry_after(self._resp({"Retry-After": "999999"}))
        self.assertEqual(result, _MAX_RETRY_AFTER_SEC)

    def test_retry_after_normal_seconds_passthrough(self):
        from utils.http_utils import parse_retry_after

        result = parse_retry_after(self._resp({"Retry-After": "120"}))
        self.assertEqual(result, 120.0)

    # --- HTTP-date based Retry-After ---

    def test_retry_after_http_date_negative_delta(self):
        """A past HTTP-date yields a negative delta → clamped to 0.0."""
        from email.utils import formatdate

        from utils.http_utils import parse_retry_after

        # 2000-01-01 is far in the past relative to test run time
        past_date = formatdate(timeval=946684800, usegmt=True)  # 2000-01-01
        with patch("utils.http_utils.time.time", return_value=1000000000.0):
            result = parse_retry_after(self._resp({"Retry-After": past_date}))
            self.assertEqual(result, 0.0)

    def test_retry_after_http_date_future_clamped(self):
        """A far-future HTTP-date yielding >max delta → clamped to max."""
        from email.utils import formatdate

        from utils.http_utils import _MAX_RETRY_AFTER_SEC, parse_retry_after

        # Year 2200 (epoch 7258118400) → with current time ~1.7B, delta ~5.5B >> max
        far_future = formatdate(timeval=7_258_118_400, usegmt=True)
        with patch("utils.http_utils.time.time", return_value=1_700_000_000.0):
            result = parse_retry_after(self._resp({"Retry-After": far_future}))
            self.assertEqual(result, _MAX_RETRY_AFTER_SEC)

    # --- Edge cases ---

    def test_retry_after_infinity_case_variants(self):
        """Case variations of Infinity/NaN should all be rejected."""
        from utils.http_utils import parse_retry_after

        for variant in ("Infinity", "infinity", "-inf", "+inf"):
            result = parse_retry_after(self._resp({"Retry-After": variant}))
            self.assertIsNone(result, f"Expected None for {variant!r}, got {result!r}")

    def test_retry_after_nan_case_variants(self):
        from utils.http_utils import parse_retry_after

        for variant in ("nan", "NAN", "NaN"):
            result = parse_retry_after(self._resp({"Retry-After": variant}))
            self.assertIsNone(result, f"Expected None for {variant!r}, got {result!r}")


# =========================================================================
# R6 (UTIL-2): sanitize_cache_key collision avoidance
# =========================================================================


class R6SanitizeCacheKeyCollisionTest(unittest.TestCase):
    """sanitize_cache_key must produce distinct keys for different inputs."""

    def test_special_chars_do_not_collide_with_underscore(self):
        """Characters that previously all mapped to '_' must now be distinct."""
        from utils.caching import sanitize_cache_key

        key_a = "search_a!b"
        key_b = "search_a_b"
        sanitized_a = sanitize_cache_key(key_a)
        sanitized_b = sanitize_cache_key(key_b)
        self.assertNotEqual(
            sanitized_a,
            sanitized_b,
            f"sanitize_cache_key({key_a!r}) == {sanitized_a!r} "
            f"collides with sanitize_cache_key({key_b!r}) == {sanitized_b!r}",
        )

    def test_hash_plus_collision_resolved(self):
        """'#' and '+' no longer collide with '_'."""
        from utils.caching import sanitize_cache_key

        keys = ["search_a#b", "search_a+b", "search_a_b"]
        sanitized = [sanitize_cache_key(k) for k in keys]
        self.assertEqual(
            len(set(sanitized)), len(keys), f"Sanitized keys {sanitized} have collisions"
        )

    def test_percent_encoded(self):
        """'%' must be encoded as '%25' to avoid ambiguity."""
        from utils.caching import sanitize_cache_key

        result = sanitize_cache_key("test%key")
        self.assertIn("%25", result, f"Expected %%25 in sanitized key, got {result!r}")
        self.assertNotEqual(result, "test_key", "%% must not collapse to '_'")

    def test_query_string_chars_distinct(self):
        """Common query characters need distinct encodings."""
        from utils.caching import sanitize_cache_key

        keys = [
            "search?q=hello",
            "search&q=hello",
            "search/q=hello",
            "search_q=hello",
        ]
        sanitized = [sanitize_cache_key(k) for k in keys]
        self.assertEqual(
            len(set(sanitized)), len(keys), f"Sanitized keys {sanitized} have collisions"
        )

    def test_key_truncation(self):
        """Keys longer than 256 characters are truncated."""
        from utils.caching import sanitize_cache_key

        long_key = "a" * 300
        result = sanitize_cache_key(long_key)
        self.assertLessEqual(len(result), 256)

    def test_negative_cache_key_still_works(self):
        """The '__negative' suffix pattern must continue to work."""
        from utils.caching import sanitize_cache_key

        raw_key = "market_news_context_us_ddgs/evil?key"
        neg_key = f"{raw_key}__negative"
        sanitized = sanitize_cache_key(neg_key)
        # The suffix should be preserved
        self.assertTrue(
            sanitized.endswith("__negative"),
            f"Expected __negative suffix, got {sanitized!r}",
        )


# =========================================================================
# R7 (UTIL-3): StockDiskCache.get tolerates list-shaped cache files
# =========================================================================


class R7DiskCacheListShapeTest(unittest.TestCase):
    """StockDiskCache.get must return None for list-shaped (non-dict) cache files."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    # --- Helper to create a cache file with arbitrary content ---

    def _write_cache_file(self, key: str, content: object) -> Path:
        """Write a cache file under *key* with the given JSON-serializable *content*."""
        from utils.disk_cache import StockDiskCache

        cache = StockDiskCache(cache_dir=self._cache_dir, enable_cleanup=False)
        path = cache._entry_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def test_list_shape_returns_none(self):
        """A list-shaped cache file ([...]) must return None, not raise AttributeError."""
        from utils.disk_cache import StockDiskCache

        cache = StockDiskCache(cache_dir=self._cache_dir, enable_cleanup=False)
        self._write_cache_file("list_key", [{"value": 123}])
        result = cache.get("list_key")
        self.assertIsNone(result, f"Expected None for list-shaped cache, got {result!r}")

    def test_empty_list_returns_none(self):
        """An empty list [] must return None, not raise AttributeError."""
        from utils.disk_cache import StockDiskCache

        cache = StockDiskCache(cache_dir=self._cache_dir, enable_cleanup=False)
        self._write_cache_file("empty_list", [])
        result = cache.get("empty_list")
        self.assertIsNone(result, f"Expected None for empty list, got {result!r}")

    def test_string_shape_returns_none(self):
        """A string-shaped cache file (\"...\") must return None."""
        from utils.disk_cache import StockDiskCache

        cache = StockDiskCache(cache_dir=self._cache_dir, enable_cleanup=False)
        self._write_cache_file("str_key", "not_a_dict")
        result = cache.get("str_key")
        self.assertIsNone(result, f"Expected None for string-shaped cache, got {result!r}")

    def test_number_shape_returns_none(self):
        """A number-shaped cache file (42) must return None."""
        from utils.disk_cache import StockDiskCache

        cache = StockDiskCache(cache_dir=self._cache_dir, enable_cleanup=False)
        self._write_cache_file("num_key", 42)
        result = cache.get("num_key")
        self.assertIsNone(result, f"Expected None for number-shaped cache, got {result!r}")

    def test_dict_shape_works_normally(self):
        """A normal dict-shaped cache file must still return the value."""
        from utils.disk_cache import StockDiskCache

        cache = StockDiskCache(cache_dir=self._cache_dir, enable_cleanup=False)
        self._write_cache_file("good_key", {"value": "hello", "stored_at": 1000.0})
        result = cache.get("good_key")
        self.assertEqual(result, "hello")

    def test_corrupt_json_returns_none(self):
        """Corrupt JSON (not parseable) still returns None (existing behavior)."""
        from utils.disk_cache import StockDiskCache

        cache = StockDiskCache(cache_dir=self._cache_dir, enable_cleanup=False)
        path = self._write_cache_file("corrupt_key", {"value": "x"})
        path.write_text("{corrupt json!!", encoding="utf-8")
        result = cache.get("corrupt_key")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()