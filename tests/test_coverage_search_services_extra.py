"""Coverage tests for services/search/ddgs.py and services/search/langsearch.py."""

import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from app_state import app_state
from services.search import ddgs, langsearch


class SearchServicesCoverageTests(unittest.TestCase):
    def setUp(self):
        from tests import reset_app_state_internals

        reset_app_state_internals()

    def test_ddgs_rate_limit_and_negative_cache(self):
        # 1. Test normal DDGS search with context manager mock
        mock_ddgs_inst = MagicMock()
        mock_ddgs_inst.news.return_value = [
            {"title": "Test Title", "url": "https://example.com", "body": "Snippet"}
        ]
        mock_ddgs_cls = MagicMock()
        mock_ddgs_cls.return_value.__enter__.return_value = mock_ddgs_inst

        with patch.object(ddgs, "DDGS", mock_ddgs_cls):
            res = ddgs.ddgs_news_search("test query")
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["title"], "Test Title")

        # 2. Test DDGS search exception branch
        with patch.object(ddgs, "DDGS", side_effect=Exception("DDGS network error")):
            res = ddgs.ddgs_news_search("test query 2")
            self.assertEqual(res, [])

    def test_langsearch_circuit_breaker_and_error_branches(self):
        # 1. Missing API key branch raises ValueError
        with self.assertRaises(ValueError):
            langsearch.langsearch_search("test query", "")

        # 2. Circuit breaker open branch
        c_state = app_state.market.circuit_states["langsearch"]
        c_state.status = "OPEN"
        c_state.open_until = time.time() + 3600.0

        with patch(
            "services.search.langsearch._langsearch_post_json",
            side_effect=requests.HTTPError("LangSearch circuit is OPEN"),
        ), self.assertRaises(requests.HTTPError):
            langsearch.langsearch_search("test query", "dummy_key_1234567890")

        # Reset circuit breaker
        c_state.status = "CLOSED"
        c_state.open_until = 0.0

        # 3. HTTP 429 rate limit response branch
        err_429 = requests.HTTPError("Rate limited")
        err_429.response = MagicMock(status_code=429)

        with patch("services.search.langsearch._langsearch_post_json", side_effect=err_429):
            errors = []
            with self.assertRaises(requests.HTTPError):
                langsearch.langsearch_search(
                    "test query", "valid_key_12345678901234567890", errors_out=errors
                )
            self.assertEqual(len(errors), 1)
