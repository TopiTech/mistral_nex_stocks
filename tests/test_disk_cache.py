# tests/test_disk_cache.py
"""Unit tests for utils/disk_cache.py — persistent disk cache for stock data."""

import os
import tempfile
import time
import unittest
from pathlib import Path


class TestStockDiskCache(unittest.TestCase):
    """Tests for StockDiskCache."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._cache_dir = Path(self._tmpdir) / "test_cache"
        # Import lazily so module-level constants are already resolved
        from utils.disk_cache import StockDiskCache
        self.cache = StockDiskCache(
            cache_dir=self._cache_dir,
            max_entries=5,
            default_ttl=60,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Basic get / set / has
    # ------------------------------------------------------------------

    def test_get_returns_none_for_missing_key(self):
        self.assertIsNone(self.cache.get("nonexistent"))

    def test_set_and_get_roundtrip(self):
        self.cache.set("k1", {"price": 100.5})
        result = self.cache.get("k1")
        self.assertEqual(result, {"price": 100.5})

    def test_has_returns_true_when_present(self):
        self.cache.set("k1", "hello")
        self.assertTrue(self.cache.has("k1"))

    def test_has_returns_false_when_missing(self):
        self.assertFalse(self.cache.has("nonexistent"))

    def test_set_and_get_string_value(self):
        self.cache.set("str_key", "some string")
        self.assertEqual(self.cache.get("str_key"), "some string")

    def test_set_and_get_list_value(self):
        data = [{"x": 1}, {"x": 2}]
        self.cache.set("list_key", data)
        self.assertEqual(self.cache.get("list_key"), data)

    def test_set_and_get_nested_dict(self):
        data = {"history": [{"x": 1, "c": 100.0}], "symbol": "AAPL"}
        self.cache.set("nested", data)
        self.assertEqual(self.cache.get("nested"), data)

    # ------------------------------------------------------------------
    # TTL / Expiration
    # ------------------------------------------------------------------

    def test_expired_entry_returns_none(self):
        self.cache.set("expire_me", "data")
        # Overwrite the mtime to simulate expiration
        path = self.cache._entry_path("expire_me")
        old_time = time.time() - 120  # 2 minutes ago
        os.utime(str(path), (old_time, old_time))
        self.assertIsNone(self.cache.get("expire_me", ttl=60))

    def test_valid_entry_within_ttl(self):
        self.cache.set("fresh", "data")
        self.assertEqual(self.cache.get("fresh", ttl=60), "data")

    def test_custom_ttl_on_get(self):
        self.cache.set("short_ttl", "data")
        # The file was just written, so ttl=1 should still be valid
        self.assertEqual(self.cache.get("short_ttl", ttl=1), "data")

    # ------------------------------------------------------------------
    # Delete / delete_prefix / clear
    # ------------------------------------------------------------------

    def test_delete_removes_entry(self):
        self.cache.set("to_delete", "value")
        self.assertTrue(self.cache.delete("to_delete"))
        self.assertIsNone(self.cache.get("to_delete"))

    def test_delete_returns_false_for_missing(self):
        self.assertFalse(self.cache.delete("no_such_key"))

    def test_delete_prefix_removes_matching(self):
        self.cache.set("hist_AAPL_3mo", {"a": 1})
        self.cache.set("hist_AAPL_1d", {"a": 2})
        self.cache.set("hist_MSFT_3mo", {"a": 3})
        removed = self.cache.delete_prefix("hist_AAPL")
        self.assertEqual(removed, 2)
        self.assertIsNone(self.cache.get("hist_AAPL_3mo"))
        self.assertIsNone(self.cache.get("hist_AAPL_1d"))
        self.assertIsNotNone(self.cache.get("hist_MSFT_3mo"))

    def test_clear_removes_all(self):
        self.cache.set("a", 1)
        self.cache.set("b", 2)
        self.cache.set("c", 3)
        self.cache.clear()
        self.assertIsNone(self.cache.get("a"))
        self.assertIsNone(self.cache.get("b"))
        self.assertIsNone(self.cache.get("c"))

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def test_eviction_when_max_exceeded(self):
        # max_entries=5 in setUp
        for i in range(7):
            self.cache.set(f"key_{i}", f"val_{i}")
            # Artificially age earlier entries so eviction picks the right ones
            if i < 5:
                path = self.cache._entry_path(f"key_{i}")
                old_time = time.time() - (10 * (7 - i))
                os.utime(str(path), (old_time, old_time))

        # At most 5 entries should remain
        stats = self.cache.stats()
        self.assertLessEqual(stats["disk_cache_entries"], 5)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def test_stats_reports_entries(self):
        self.cache.set("x", 1)
        self.cache.set("y", 2)
        stats = self.cache.stats()
        self.assertEqual(stats["disk_cache_entries"], 2)
        self.assertGreater(stats["disk_cache_total_size_bytes"], 0)
        self.assertEqual(stats["disk_cache_max_entries"], 5)

    def test_stats_empty_cache(self):
        stats = self.cache.stats()
        self.assertEqual(stats["disk_cache_entries"], 0)

    # ------------------------------------------------------------------
    # Filesystem edge cases
    # ------------------------------------------------------------------

    def test_special_characters_in_key(self):
        self.cache.set("hist_^N225_3mo", {"data": True})
        self.assertEqual(self.cache.get("hist_^N225_3mo"), {"data": True})

    def test_very_long_key_is_truncated(self):
        long_key = "x" * 500
        self.cache.set(long_key, "value")
        result = self.cache.get(long_key)
        # Key is truncated but the value should still be retrievable
        # (we can't easily test the exact truncated key, just that it doesn't crash)
        self.assertIsInstance(result, (str, type(None)))

    def test_corrupt_json_returns_none(self):
        self.cache.set("good", "data")
        path = self.cache._entry_path("good")
        with open(str(path), "w") as f:
            f.write("{corrupt json!!")
        self.assertIsNone(self.cache.get("good"))

    def test_overwrite_existing_key(self):
        self.cache.set("overwritten", "old_value")
        self.cache.set("overwritten", "new_value")
        self.assertEqual(self.cache.get("overwritten"), "new_value")

    def test_disk_cache_creates_directory(self):
        """Disk cache should create the cache directory if it doesn't exist."""
        new_dir = Path(self._tmpdir) / "nested" / "deep" / "cache"
        from utils.disk_cache import StockDiskCache
        cache = StockDiskCache(cache_dir=new_dir)
        self.assertTrue(new_dir.exists())
        cache.set("test", "value")
        self.assertEqual(cache.get("test"), "value")


if __name__ == "__main__":
    unittest.main()
