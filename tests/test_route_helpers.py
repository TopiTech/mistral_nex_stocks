"""
Tests for route_helpers.py — rate limiting, stock request parsing, cache helpers, text extraction.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from route_helpers import (
    _cleanup_rate_limit_store,
    _rate_limit_store,
    _rate_limit_window_by_key,
    _rate_limit_env_name,
    _seconds_until,
    _extract_text_from_mistral_content,
    _stock_display_name,
)


class RateLimitEnvNameTestCase(unittest.TestCase):
    """_rate_limit_env_name のテスト"""

    def test_basic_endpoint(self):
        name = _rate_limit_env_name("/api/stocks", "MAX")
        self.assertEqual(name, "MNS_RATE_LIMIT__API_STOCKS_MAX")

    def test_none_or_empty_endpoint(self):
        name = _rate_limit_env_name("", "WINDOW")
        self.assertTrue(name.startswith("MNS_RATE_LIMIT_"))
        self.assertTrue(name.endswith("_WINDOW"))


class SecondsUntilTestCase(unittest.TestCase):
    """_seconds_until のテスト"""

    def test_future_timestamp(self):
        future = time.time() + 60
        secs = _seconds_until(future)
        self.assertGreater(secs, 59)
        self.assertLessEqual(secs, 60)

    def test_past_timestamp_returns_zero(self):
        past = time.time() - 60
        secs = _seconds_until(past)
        self.assertEqual(secs, 0.0)

    def test_none_returns_zero(self):
        secs = _seconds_until(None)
        self.assertEqual(secs, 0.0)

    def test_zero_returns_zero(self):
        secs = _seconds_until(0.0)
        self.assertEqual(secs, 0.0)


class ExtractTextFromMistralContentTestCase(unittest.TestCase):
    """_extract_text_from_mistral_content のテスト"""

    def test_plain_string(self):
        result = _extract_text_from_mistral_content("Hello world")
        self.assertEqual(result, "Hello world")

    def test_string_with_whitespace(self):
        result = _extract_text_from_mistral_content("  Hello world  ")
        self.assertEqual(result, "Hello world")

    def test_list_with_text_chunks(self):
        chunks = [
            {"type": "text", "text": " First part"},
            {"type": "text", "text": " Second part"},
        ]
        result = _extract_text_from_mistral_content(chunks)
        self.assertEqual(result, "First part\nSecond part")

    def test_thinking_chunks_are_skipped(self):
        chunks = [
            {"type": "thinking", "thinking": "I think..."},
            {"type": "text", "text": "Final answer"},
        ]
        result = _extract_text_from_mistral_content(chunks)
        self.assertEqual(result, "Final answer")

    def test_none_returns_empty(self):
        result = _extract_text_from_mistral_content(None)
        self.assertEqual(result, "")

    def test_empty_list_returns_empty(self):
        result = _extract_text_from_mistral_content([])
        self.assertEqual(result, "")


class StockDisplayNameTestCase(unittest.TestCase):
    """_stock_display_name のテスト"""

    @patch("route_helpers._get_stock_container")
    def test_name_from_container_string(self, mock_container):
        mock_container.return_value = {"AAPL": "Apple Inc."}
        name = _stock_display_name("AAPL", "us")
        self.assertEqual(name, "Apple Inc.")

    @patch("route_helpers._get_stock_container")
    def test_name_from_container_dict(self, mock_container):
        mock_container.return_value = {"AAPL": {"name": "Apple Inc."}}
        name = _stock_display_name("AAPL", "us")
        self.assertEqual(name, "Apple Inc.")

    @patch("route_helpers._get_stock_container")
    @patch("route_helpers._default_stock_names")
    def test_name_from_default(self, mock_defaults, mock_container):
        mock_container.return_value = {}
        mock_defaults.return_value = {"^N225": "Nikkei 225"}
        name = _stock_display_name("^N225", "idx")
        self.assertEqual(name, "Nikkei 225")

    @patch("route_helpers._get_stock_container")
    @patch("route_helpers._default_stock_names")
    def test_fallback_to_symbol(self, mock_defaults, mock_container):
        mock_container.return_value = {}
        mock_defaults.return_value = {}
        name = _stock_display_name("UNKNOWN", "us")
        self.assertEqual(name, "UNKNOWN")


class CleanupRateLimitStoreTestCase(unittest.TestCase):
    """_cleanup_rate_limit_store のテスト"""

    def setUp(self):
        _rate_limit_store.clear()
        _rate_limit_window_by_key.clear()

    def tearDown(self):
        _rate_limit_store.clear()
        _rate_limit_window_by_key.clear()

    def test_fresh_entry_preserved(self):
        _rate_limit_store["fresh"] = [time.time()]
        _rate_limit_window_by_key["fresh"] = 300
        _cleanup_rate_limit_store()
        self.assertIn("fresh", _rate_limit_store)

    def test_stale_entry_removed(self):
        _rate_limit_store["stale"] = [time.time() - 600]
        _rate_limit_window_by_key["stale"] = 300
        _cleanup_rate_limit_store()
        self.assertNotIn("stale", _rate_limit_store)

    def test_empty_store_stays_empty(self):
        _rate_limit_store.clear()
        _cleanup_rate_limit_store()
        self.assertEqual(len(_rate_limit_store), 0)


if __name__ == "__main__":
    unittest.main()
