"""Regression tests for H5 / P3: /api/search must never return JSON `null`.

get_cached() can return None (genuine absence) or CACHE_FETCHING (a concurrent
fetcher is still running and the stampede-prevention waiter timed out). The
endpoint must fall back to an empty result set (a dict with "results": []) so
the client contract (data.results) is preserved instead of returning "null".
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from app_state import app_state
from utils.caching import global_cache

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SearchNullResponseTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_search_returns_dict_when_cache_misses_with_none(self):
        # Simulate get_cached returning None (genuine absence).
        with patch("routes.api_stocks.get_cached", return_value=None):
            response = self.client.get(
                "/api/search?q=NVDA",
                headers={"Origin": "http://localhost:5000"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsInstance(payload, dict, "response must be a JSON object, not null")
        self.assertIn("results", payload)
        self.assertEqual(payload["results"], [])

    def test_search_returns_dict_when_fetch_in_progress_sentinel(self):
        # P3: the sentinel (stampede-waiter timeout) must also fall back to an
        # empty result dict instead of serializing the sentinel object.
        from utils.caching import CACHE_FETCHING

        with patch("routes.api_stocks.get_cached", return_value=CACHE_FETCHING):
            response = self.client.get(
                "/api/search?q=NVDA",
                headers={"Origin": "http://localhost:5000"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsInstance(payload, dict, "response must be a JSON object, not null")
        self.assertIn("results", payload)
        self.assertEqual(payload["results"], [])

    def test_search_returns_results_on_success(self):
        fake = {"results": [{"symbol": "NVDA", "name": "NVIDIA"}]}
        with patch("routes.api_stocks.get_cached", return_value=fake):
            response = self.client.get(
                "/api/search?q=NVDA",
                headers={"Origin": "http://localhost:5000"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["results"][0]["symbol"], "NVDA")

    def test_long_distinct_queries_do_not_share_cached_results(self):
        """Long multibyte queries must retain their own one-minute cache entry."""
        query_a = ("😀" * 42) + "A"
        query_b = ("😀" * 42) + "B"
        with global_cache.cache_lock:
            global_cache.caches.clear()

        with patch.object(
            app_state.stock_provider,
            "search",
            side_effect=[
                [{"symbol": "FIRST", "name": "First result"}],
                [{"symbol": "SECOND", "name": "Second result"}],
            ],
        ) as search:
            first = self.client.get("/api/search", query_string={"q": query_a})
            second = self.client.get("/api/search", query_string={"q": query_b})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()["results"][0]["symbol"], "FIRST")
        self.assertEqual(second.get_json()["results"][0]["symbol"], "SECOND")
        self.assertEqual(search.call_count, 2)


if __name__ == "__main__":
    unittest.main()
