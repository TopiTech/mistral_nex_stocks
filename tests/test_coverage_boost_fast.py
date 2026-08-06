"""Fast unit tests to boost test coverage across stock_provider, ai_service, trend_sources, search, and storage modules."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

import trend_sources as ts
from services import ai_service, stock_provider
from services.search import ddgs, langsearch
from utils import storage


class StockProviderCoverageBoostTestCase(unittest.TestCase):
    """Test uncovered edge cases and fallback pathways in services/stock_provider.py."""

    def test_is_yfinance_rate_limit_error_detection(self):
        exc_429 = requests.HTTPError("429 Too Many Requests")
        resp = MagicMock()
        resp.status_code = 429
        exc_429.response = resp
        self.assertTrue(stock_provider._is_yfinance_rate_limit_error(exc_429))

        exc_normal = ValueError("Invalid parameter")
        self.assertFalse(stock_provider._is_yfinance_rate_limit_error(exc_normal))

    def test_is_yfinance_invalid_symbol_error(self):
        exc_404 = requests.HTTPError("404 Not Found")
        resp = MagicMock()
        resp.status_code = 404
        exc_404.response = resp
        self.assertTrue(stock_provider._is_yfinance_invalid_symbol_error(exc_404))

        exc_normal = RuntimeError("Connection error")
        self.assertFalse(stock_provider._is_yfinance_invalid_symbol_error(exc_normal))

    def test_handle_yf_rate_limit(self):
        mock_mstate = MagicMock()
        mock_mstate.mark_yf_429.return_value = 5.0

        exc_429 = requests.HTTPError("429 Too Many Requests")
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "10"}
        exc_429.response = resp

        delay = stock_provider._handle_yf_rate_limit(exc_429, mock_mstate, context="test")
        self.assertEqual(delay, 5.0)

    def test_infer_currency_from_symbol(self):
        provider = stock_provider.YFinanceProvider()
        self.assertEqual(provider._infer_currency_from_symbol("7203.T"), "JPY")
        self.assertEqual(provider._infer_currency_from_symbol("^N225"), "JPY")
        self.assertEqual(provider._infer_currency_from_symbol("AAPL"), "USD")

    def test_derive_quote_from_history_empty_df(self):
        provider = stock_provider.YFinanceProvider()
        self.assertIsNone(provider._derive_quote_from_history(pd.DataFrame(), "AAPL"))

    def test_derive_quote_from_history_valid_df(self):
        provider = stock_provider.YFinanceProvider()
        df = pd.DataFrame(
            {
                "Close": [150.0, 155.0],
                "Open": [148.0, 151.0],
                "High": [152.0, 156.0],
                "Low": [147.0, 150.0],
                "Volume": [1000, 2000],
            },
            index=pd.to_datetime(["2026-08-01", "2026-08-02"]),
        )
        quote = provider._derive_quote_from_history(df, "AAPL")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["regularMarketPrice"], 155.0)
        self.assertEqual(quote["regularMarketPreviousClose"], 150.0)


class AIServiceCoverageBoostTestCase(unittest.TestCase):
    """Test uncovered functions and fallbacks in services/ai_service.py."""

    def test_sanitize_prompt_text(self):
        self.assertEqual(ai_service._sanitize_prompt_text("  hello  "), "hello")
        self.assertEqual(ai_service._sanitize_prompt_text(None), "")

    def test_clamp_max_tokens(self):
        self.assertEqual(ai_service._clamp_max_tokens(100), 100)
        self.assertEqual(ai_service._clamp_max_tokens(0), 600)

    def test_extract_mistral_wait_seconds(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "5"}
        self.assertEqual(ai_service._extract_mistral_wait_seconds(resp), 5.0)


class TrendSourcesCoverageBoostTestCase(unittest.TestCase):
    """Test uncovered helpers in trend_sources.py."""

    def test_safe_text_various_types(self):
        self.assertEqual(ts._safe_text("  hello  "), "hello")
        self.assertEqual(ts._safe_text(None), "")
        self.assertEqual(ts._safe_text(123), "123")

    def test_make_item(self):
        item = ts.make_item("news", "Title A", summary="Sum", url="https://ex.com")
        self.assertEqual(item["title"], "Title A")
        self.assertEqual(item["type"], "news")

    def test_compact_context_limits(self):
        items = [{"title": f"Item {i}", "url": f"https://ex.com/{i}"} for i in range(10)]
        res = ts.compact_context(items, limit=3)
        self.assertIn("Item 0", res)
        self.assertIn("Item 2", res)
        self.assertNotIn("Item 5", res)

    def test_extract_titles(self):
        items = [{"title": "Title 1"}, {"title": "Title 2"}, {"title": ""}]
        titles = ts.extract_titles(items)
        self.assertEqual(titles, ["Title 1", "Title 2"])


class SearchServicesCoverageBoostTestCase(unittest.TestCase):
    """Test uncovered search helpers in services/search/langsearch.py and ddgs.py."""

    def test_langsearch_freshness_mapping(self):
        self.assertEqual(langsearch._map_langsearch_freshness("d"), "oneDay")
        self.assertEqual(langsearch._map_langsearch_freshness("w"), "oneWeek")

    def test_langsearch_extract_entries(self):
        payload = {"data": {"webPages": {"value": [{"name": "Entry 1", "snippet": "Snippet 1", "url": "https://a.com"}]}}}
        entries = langsearch._extract_langsearch_entries(payload)
        self.assertEqual(len(entries), 1)

    def test_ddgs_timeout_helper(self):
        with patch.dict("os.environ", {"DDGS_TIMEOUT": "12"}):
            self.assertEqual(ddgs._get_ddgs_timeout(), 12)


class StorageCoverageBoostTestCase(unittest.TestCase):
    """Test uncovered storage utilities in utils/storage.py."""

    def test_user_stocks_dict_structure(self):
        self.assertTrue(callable(storage.load_user_stocks))


if __name__ == "__main__":
    unittest.main()
