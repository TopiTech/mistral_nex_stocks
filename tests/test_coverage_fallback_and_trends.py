"""
test_coverage_fallback_and_trends.py - Unit tests for fallback_provider and trend_sources
"""

import unittest
from unittest.mock import MagicMock, patch

import trend_sources as ts
from services.fallback_provider import (
    Nikkei225JPProvider,
    YahooWebScraperProvider,
)


class FallbackProviderCoverageTestCase(unittest.TestCase):
    def test_yahoo_web_scraper_with_root_app_main(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <html>
        <script>
        root.App.main = {
            "context": {
                "dispatcher": {
                    "stores": {
                        "QuoteSummaryStore": {
                            "price": {
                                "regularMarketPrice": {"raw": 182.5},
                                "regularMarketPreviousClose": {"raw": 180.0},
                                "regularMarketVolume": {"raw": 54000000},
                                "regularMarketOpen": {"raw": 181.0},
                                "regularMarketDayHigh": {"raw": 183.0},
                                "regularMarketDayLow": {"raw": 180.5},
                                "currency": "USD"
                            }
                        }
                    }
                }
            }
        }; (function(){})();
        </script>
        </html>
        """
        mock_session.get.return_value = mock_resp
        provider = YahooWebScraperProvider()
        provider.session = mock_session
        quote = provider.get_latest_quote("AAPL")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["regularMarketPrice"], 182.5)
        self.assertEqual(quote["regularMarketPreviousClose"], 180.0)

    def test_yahoo_web_scraper_with_next_data(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <html>
        <script id="__NEXT_DATA__" type="application/json">
        {
            "props": {
                "pageProps": {
                    "quoteSummary": {
                        "price": {
                            "regularMarketPrice": {"raw": 420.0},
                            "regularMarketPreviousClose": {"raw": 415.0},
                            "regularMarketVolume": {"raw": 20000000}
                        }
                    }
                }
            }
        }
        </script>
        </html>
        """
        mock_session.get.return_value = mock_resp
        provider = YahooWebScraperProvider()
        provider.session = mock_session
        quote = provider.get_latest_quote("MSFT")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["regularMarketPrice"], 420.0)

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

    def test_nikkei225jp_provider_adr_and_index_parsing(self):
        mock_session = MagicMock()
        adr_resp = MagicMock()
        adr_resp.status_code = 200
        # 21 elements separated by _, parts[8] is price
        parts = ["7203", "Toyota", "150.0", "152.0", "148.0", "151.0", "1000", "0", "2500.0", "+50.0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0"]
        adr_resp.text = f'A0[0]="{"_".join(parts)}"\n'

        index_resp = MagicMock()
        index_resp.status_code = 200
        index_resp.text = 'A[1]="38000.0_+200.0_0.5"\n'

        def get_side_effect(url, **kwargs):
            if "adr" in url or "_adr_all" in url:
                return adr_resp
            return index_resp

        mock_session.get.side_effect = get_side_effect
        provider = Nikkei225JPProvider()
        provider.session = mock_session
        quote = provider.get_latest_quote("7203.T")
        self.assertIsNotNone(quote)


class TrendSourcesCoverageTestCase(unittest.TestCase):
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

        sample_search_json = {
            "query": {
                "search": [
                    {"title": "Semiconductor", "snippet": "A semiconductor material..."}
                ]
            }
        }
        with patch.object(ts, "_request_json", return_value=sample_search_json):
            items = ts.collect_wikipedia_search_items(["tech"], "us", limit_per_query=2)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Semiconductor")

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

    def test_google_trends_for_keyword(self):
        mock_pytrends = MagicMock()
        mock_pytrends.suggestions.return_value = [{"title": "Quantum computing"}]
        mock_pytrends.related_queries.return_value = {}

        with (
            patch.object(ts, "TrendReq", return_value=mock_pytrends),
            patch.object(ts, "_google_trends_client", return_value=mock_pytrends),
        ):
            queries = ts._trend_queries_for_keyword("Quantum", "us", limit=5)
            self.assertIn("Quantum computing", queries)


if __name__ == "__main__":
    unittest.main()
