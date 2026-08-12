"""
Tests for route_helpers.py — rate limiting, stock request parsing, cache helpers, text extraction.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import route_helpers
from route_helpers import (
    _RATE_LIMIT_CLEANUP_INTERVAL,
    _cleanup_rate_limit_store,
    _extract_text_from_mistral_content,
    _rate_limit_env_name,
    _rate_limit_last_cleanup,
    _rate_limit_store,
    _rate_limit_window_by_key,
    _seconds_until,
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
        _rate_limit_store["fresh"] = [time.monotonic()]
        _rate_limit_window_by_key["fresh"] = 300
        _cleanup_rate_limit_store()
        self.assertIn("fresh", _rate_limit_store)

    def test_stale_entry_removed(self):
        _rate_limit_store["stale"] = [time.monotonic() - 600]
        _rate_limit_window_by_key["stale"] = 300
        _cleanup_rate_limit_store()
        self.assertNotIn("stale", _rate_limit_store)

    def test_empty_store_stays_empty(self):
        _rate_limit_store.clear()
        _cleanup_rate_limit_store()
        self.assertEqual(len(_rate_limit_store), 0)


class RateLimitCleanupGateTestCase(unittest.TestCase):
    """The periodic-cleanup gate inside ``rate_limit`` must actually fire.

    Regression (R2): ``_rate_limit_last_cleanup`` was initialized with
    ``time.time()`` (wall-clock epoch) while every comparison uses
    ``time.monotonic()``. Because ``monotonic - epoch`` is always hugely
    negative, the ``current_time - _rate_limit_last_cleanup > interval`` gate
    never became true, so ``_cleanup_rate_limit_store()`` was dead code: stale
    rate-limit entries (including per-``request_token`` polling keys, which are
    inserted without any capacity eviction) were never pruned.
    """

    def setUp(self):
        import route_helpers as rh

        self._original_last_cleanup = rh._rate_limit_last_cleanup

    def tearDown(self):
        import route_helpers as rh

        rh._rate_limit_last_cleanup = self._original_last_cleanup

    def test_last_cleanup_marker_is_in_monotonic_domain(self):
        """``_rate_limit_last_cleanup`` must live on the monotonic clock.

        Fails if the module-level initializer regresses to ``time.time()``:
        ``time.monotonic() - time.time()`` is a huge negative number, while a
        monotonic-initialized value yields a small non-negative delta.
        """
        delta = time.monotonic() - _rate_limit_last_cleanup
        self.assertGreaterEqual(delta, 0.0)
        self.assertLess(delta, 3600.0)

    def test_periodic_cleanup_gate_fires_and_updates_marker(self):
        """Once the interval elapses, the gate must run cleanup and refresh the marker."""
        from flask import Flask, jsonify

        from route_helpers import rate_limit

        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/api/ping", methods=["GET"])
        @rate_limit(max_requests=10, window_seconds=60)
        def ping():
            return jsonify({"ok": True})

        client = app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.230"}

        with patch.object(route_helpers, "_cleanup_rate_limit_store") as mock_cleanup:
            # Force the marker far enough into the past that the gate must fire.
            route_helpers._rate_limit_last_cleanup = time.monotonic() - (
                _RATE_LIMIT_CLEANUP_INTERVAL + 1
            )
            resp = client.get("/api/ping", environ_base=env)
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            mock_cleanup.assert_called_once()
            # The marker is refreshed to the current monotonic time, so the gate
            # does not re-fire on the very next request.
            delta = time.monotonic() - route_helpers._rate_limit_last_cleanup
            self.assertGreaterEqual(delta, 0.0)
            self.assertLess(delta, 5.0)

    def test_cleanup_gate_does_not_fire_within_interval(self):
        """A fresh marker must NOT trigger cleanup on the next request."""
        from flask import Flask, jsonify

        from route_helpers import rate_limit

        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/api/ping", methods=["GET"])
        @rate_limit(max_requests=10, window_seconds=60)
        def ping():
            return jsonify({"ok": True})

        client = app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.231"}

        with patch.object(route_helpers, "_cleanup_rate_limit_store") as mock_cleanup:
            route_helpers._rate_limit_last_cleanup = time.monotonic()
            resp = client.get("/api/ping", environ_base=env)
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            mock_cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
