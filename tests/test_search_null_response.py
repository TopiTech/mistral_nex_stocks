"""Regression tests for H5: /api/search must never return JSON `null`.

get_cached() can return None when a concurrent fetcher is still running and the
stampede-prevention waiter times out. The endpoint must fall back to an empty
result set (a dict with "results": []) so the client contract (data.results)
is preserved instead of returning "null".
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SearchNullResponseTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_search_returns_dict_when_cache_misses_with_none(self):
        # Simulate get_cached returning None (stampede-waiter timeout).
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


if __name__ == "__main__":
    unittest.main()
