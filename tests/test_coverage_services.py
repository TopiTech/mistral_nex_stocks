"""Coverage tests for services: stock_provider, ai_service, trend_sources, fallback_provider, search."""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import trend_sources as ts
from services import ai_service, stock_provider
from services.fallback_provider import (
    CompositeFallbackProvider,
    Nikkei225JPProvider,
    YahooWebScraperProvider,
)
from services.search import ddgs, langsearch


class StockProviderCoverageBoostTestCase(unittest.TestCase):
    """Test uncovered edge cases and fallback pathways in services/stock_provider.py."""

    def test_is_yfinance_rate_limit_error_detection(self):
        import requests

        exc_429 = requests.HTTPError("429 Too Many Requests")
        resp = MagicMock()
        resp.status_code = 429
        exc_429.response = resp
        self.assertTrue(stock_provider._is_yfinance_rate_limit_error(exc_429))

        exc_normal = ValueError("Invalid parameter")
        self.assertFalse(stock_provider._is_yfinance_rate_limit_error(exc_normal))

    def test_is_yfinance_invalid_symbol_error(self):
        import requests

        exc_404 = requests.HTTPError("404 Not Found")
        resp = MagicMock()
        resp.status_code = 404
        exc_404.response = resp
        self.assertTrue(stock_provider._is_yfinance_invalid_symbol_error(exc_404))

    def test_handle_yf_rate_limit(self):
        import requests

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

    def test_df_to_records_empty_and_none(self):
        provider = stock_provider.YFinanceProvider()
        self.assertEqual(provider._df_to_records(None), [])
        self.assertEqual(provider._df_to_records(pd.DataFrame()), [])

    def test_df_to_records_datetime_index_and_nan(self):
        provider = stock_provider.YFinanceProvider()
        dates = pd.date_range("2026-01-01", periods=3, freq="D")
        df = pd.DataFrame(
            {"val": [1.0, np.nan, 3.0], "text": ["a", "b", None]},
            index=dates,
        )
        records = provider._df_to_records(df, limit=2)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["val"], 1.0)
        self.assertIsNone(records[1]["val"])

    def test_merge_quote_into_history(self):
        provider = stock_provider.YFinanceProvider()
        self.assertTrue(provider._merge_quote_into_history(None, {}, "TEST").empty)
        ts = pd.Timestamp("2026-01-01 12:00:00", tz="America/New_York")
        df = pd.DataFrame(
            {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
            index=pd.DatetimeIndex([ts.strftime("%Y-%m-%d")]),
        )
        res1 = provider._merge_quote_into_history(df, {}, "TEST")
        self.assertEqual(len(res1), 1)

    def test_auxiliary_methods_handle_rate_limit_exceptions(self):
        provider = stock_provider.YFinanceProvider()
        mock_ticker = MagicMock()
        rate_err = Exception("429 Too Many Requests Rate limit exceeded")
        mock_ticker.get_earnings_dates.side_effect = rate_err
        mock_ticker.get_recommendations.side_effect = rate_err
        mock_ticker.get_institutional_holders.side_effect = rate_err
        mock_ticker.get_major_holders.side_effect = rate_err
        mock_ticker.get_analyst_price_targets.side_effect = rate_err
        mock_ticker.get_calendar.side_effect = rate_err
        mock_ticker.get_news.side_effect = rate_err
        mock_ticker.option_chain.side_effect = rate_err

        with patch.object(provider, "get_ticker", return_value=mock_ticker):
            self.assertEqual(provider.get_earnings_dates("AAPL"), [])
            self.assertEqual(provider.get_recommendations("AAPL"), [])
            self.assertEqual(provider.get_institutional_holders("AAPL"), [])
            self.assertEqual(provider.get_major_holders("AAPL"), {})
            self.assertEqual(provider.get_analyst_targets("AAPL"), {})
            self.assertEqual(provider.get_calendar("AAPL"), {})
            self.assertEqual(provider.get_news("AAPL"), [])
            self.assertEqual(provider.get_option_chain("AAPL"), {})

    def test_search_and_fallback(self):
        provider = stock_provider.YFinanceProvider()
        self.assertEqual(provider.search("a"), [])

        m_state = MagicMock()
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "quotes": [
                {"symbol": "AAPL", "shortname": "Apple Inc", "exchange": "NMS"},
                {"symbol": "MSFT", "longname": "Microsoft Corp", "exchDisp": "NASDAQ"},
            ]
        }
        mock_session.get.return_value = mock_resp
        with patch("session_manager.yf_session_manager.get_session", return_value=mock_session):
            res = provider._search_fallback("Apple", 5, m_state)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0]["symbol"], "AAPL")


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

    def test_wikipedia_top_and_search_items(self):
        sample_top_json = {
            "items": [
                {
                    "articles": [
                        {"article": "Artificial_intelligence", "views": 50000},
                        {"article": "Main_Page", "views": 100000},
                    ]
                }
            ]
        }
        with patch.object(ts, "_request_json", return_value=sample_top_json):
            items = ts.collect_wikipedia_top_items("us", limit=5)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Artificial intelligence")

    def test_reddit_search_items(self):
        sample_reddit_json = {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "NVIDIA earnings discussion",
                            "selftext": "Record revenues reported",
                            "permalink": "/r/stocks/comments/123/nvidia/",
                            "score": 500,
                            "num_comments": 120,
                        }
                    }
                ]
            }
        }
        with patch.object(ts, "_request_json_retry_on_429", return_value=sample_reddit_json):
            items = ts.collect_reddit_search_items(["NVDA"], "us", limit_per_query=2)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "NVIDIA earnings discussion")


class SearchServicesCoverageBoostTestCase(unittest.TestCase):
    """Test uncovered search helpers in services/search/langsearch.py and ddgs.py."""

    def test_langsearch_freshness_mapping(self):
        self.assertEqual(langsearch._map_langsearch_freshness("d"), "oneDay")
        self.assertEqual(langsearch._map_langsearch_freshness("w"), "oneWeek")

    def test_langsearch_extract_entries(self):
        payload = {
            "data": {
                "webPages": {
                    "value": [{"name": "Entry 1", "snippet": "Snippet 1", "url": "https://a.com"}]
                }
            }
        }
        entries = langsearch._extract_langsearch_entries(payload)
        self.assertEqual(len(entries), 1)

    def test_ddgs_timeout_helper(self):
        with patch.dict("os.environ", {"DDGS_TIMEOUT": "12"}):
            self.assertEqual(ddgs._get_ddgs_timeout(), 12)


class FallbackProviderCoverageTestCase(unittest.TestCase):
    """Tests for fallback_provider.py."""

    def test_yahoo_web_scraper_with_root_app_main(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <html>
        <script>
        root.App.main = {"context":{"dispatcher":{"stores":{"QuoteSummaryStore":{"price":{"regularMarketPrice":{"raw":182.5},"regularMarketPreviousClose":{"raw":180.0},"regularMarketVolume":{"raw":54000000}}}}}}}; (function(){})();
        </script>
        </html>
        """
        mock_session.get.return_value = mock_resp
        provider = YahooWebScraperProvider()
        provider.session = mock_session
        quote = provider.get_latest_quote("AAPL")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["regularMarketPrice"], 182.5)

    def test_yahoo_web_scraper_429_or_error(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Too Many Requests"
        mock_session.get.return_value = mock_resp
        provider = YahooWebScraperProvider()
        provider.session = mock_session
        quote = provider.get_latest_quote("AAPL")
        self.assertIsNone(quote)

    def test_nikkei225jp_provider_adr_parsing(self):
        mock_session = MagicMock()
        adr_resp = MagicMock()
        adr_resp.status_code = 200
        parts = [
            "7203",
            "Toyota",
            "150.0",
            "152.0",
            "148.0",
            "151.0",
            "1000",
            "0",
            "2500.0",
            "+50.0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
        ]
        adr_resp.text = f'A0[0]="{"_".join(parts)}"\n'
        mock_session.get.return_value = adr_resp
        provider = Nikkei225JPProvider()
        provider.session = mock_session
        quote = provider.get_latest_quote("7203.T")
        self.assertIsNotNone(quote)

    def test_composite_fallback_provider_close_delegates(self):
        composite = CompositeFallbackProvider()
        composite.yahoo_web = MagicMock()
        composite.yahoo_jp = MagicMock()
        composite.nikkei225jp = MagicMock()
        composite.minkabu = MagicMock()
        composite.close()
        composite.yahoo_web.close.assert_called_once()
        composite.yahoo_jp.close.assert_called_once()
        composite.nikkei225jp.close.assert_called_once()
        composite.minkabu.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
