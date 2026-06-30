"""Unit tests for trend_sources.py utility functions.

Tests helper functions that don't require network access.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from trend_sources import (
    normalize_url,
    _safe_text,
    make_item,
    dedupe_items,
    compact_context,
    extract_titles,
    QueryTemplates,
)


class NormalizeUrlTestCase(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(normalize_url(None), "")

    def test_empty_returns_empty(self):
        self.assertEqual(normalize_url(""), "")

    def test_strips_trailing_slash(self):
        self.assertEqual(normalize_url("https://example.com/"), "https://example.com")

    def test_preserves_url_without_trailing_slash(self):
        url = "https://example.com/path"
        self.assertEqual(normalize_url(url), url)

    def test_strips_whitespace(self):
        self.assertEqual(normalize_url("  https://example.com  "), "https://example.com")


class SafeTextTestCase(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(_safe_text(None), "")

    def test_string_stripped(self):
        self.assertEqual(_safe_text("  hello  "), "hello")

    def test_non_string_converted(self):
        self.assertEqual(_safe_text(42), "42")
        self.assertEqual(_safe_text(3.14), "3.14")
        self.assertEqual(_safe_text(True), "True")


class MakeItemTestCase(unittest.TestCase):
    def test_basic_item(self):
        item = make_item("news", "Test Title", summary="Test Summary", url="https://example.com", source="Test Source", date="2026-01-01")
        self.assertEqual(item["type"], "news")
        self.assertEqual(item["title"], "Test Title")
        self.assertEqual(item["summary"], "Test Summary")
        self.assertEqual(item["url"], "https://example.com")
        self.assertEqual(item["source"], "Test Source")
        self.assertEqual(item["date"], "2026-01-01")
        self.assertEqual(item["metadata"], {})

    def test_empty_title(self):
        item = make_item("news", "")
        self.assertEqual(item["title"], "")

    def test_with_metadata(self):
        item = make_item("trend", "Test", metadata={"views": 100})
        self.assertEqual(item["metadata"]["views"], 100)


class DedupeItemsTestCase(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(dedupe_items([]), [])

    def test_dedupe_by_url(self):
        items = [
            {"url": "https://a.com", "title": "A"},
            {"url": "https://a.com", "title": "A dup"},
            {"url": "https://b.com", "title": "B"},
        ]
        result = dedupe_items(items)
        self.assertEqual(len(result), 2)

    def test_dedupe_by_title_when_url_empty(self):
        items = [
            {"url": "", "title": "Same Title"},
            {"url": "", "title": "Same Title"},
            {"url": "", "title": "Different"},
        ]
        result = dedupe_items(items)
        self.assertEqual(len(result), 2)

    def test_preserves_first_occurrence(self):
        items = [
            {"url": "https://a.com", "title": "First"},
            {"url": "https://a.com", "title": "Second"},
        ]
        result = dedupe_items(items)
        # First occurrence should be preserved
        self.assertEqual(result[0]["title"], "First")

    def test_case_insensitive_title_dedup(self):
        items = [
            {"url": "", "title": "Hello"},
            {"url": "", "title": "hello"},
        ]
        result = dedupe_items(items)
        self.assertEqual(len(result), 1)

    def test_empty_title_with_url(self):
        items = [
            {"url": "https://a.com", "title": ""},
            {"url": "https://a.com", "title": ""},
        ]
        result = dedupe_items(items)
        self.assertEqual(len(result), 1)

    def test_custom_key_names(self):
        items = [
            {"link": "https://a.com", "name": "A"},
            {"link": "https://a.com", "name": "A dup"},
        ]
        result = dedupe_items(items, url_key="link", title_key="name")
        self.assertEqual(len(result), 1)


class CompactContextTestCase(unittest.TestCase):
    def test_empty_items(self):
        self.assertEqual(compact_context([]), "")

    def test_single_item(self):
        items = [
            {"title": "Test", "summary": "Summary", "source": "Src", "date": "2026-01-01", "url": "https://example.com"}
        ]
        result = compact_context(items)
        self.assertIn("Test", result)
        self.assertIn("Summary", result)
        self.assertIn("Src", result)
        self.assertIn("https://example.com", result)

    def test_respects_limit(self):
        items = [{"title": f"Item {i}", "url": f"https://example.com/{i}"} for i in range(20)]
        result = compact_context(items, limit=5)
        # Should only include 5 items
        self.assertIn("Item 1", result)
        self.assertIn("Item 4", result)
        # Item 10+ should not appear
        self.assertNotIn("Item 10", result)

    def test_truncates_long_title(self):
        long_title = "A" * 300
        items = [{"title": long_title, "url": "https://example.com"}]
        result = compact_context(items)
        # Title should be truncated to 180 chars
        self.assertIn("A" * 180, result)
        self.assertNotIn("A" * 250, result)

    def test_truncates_long_summary(self):
        long_summary = "B" * 500
        items = [{"title": "Test", "summary": long_summary, "url": "https://example.com"}]
        result = compact_context(items)
        # Summary should be truncated to 320 chars
        self.assertIn("B" * 320, result)
        self.assertNotIn("B" * 400, result)

    def test_deduplicates_items(self):
        items = [
            {"title": "Same", "url": "https://example.com/1"},
            {"title": "Same", "url": "https://example.com/1"},  # dup by url
        ]
        result = compact_context(items)
        self.assertEqual(result.count("Same"), 1)


class ExtractTitlesTestCase(unittest.TestCase):
    def test_empty_items(self):
        self.assertEqual(extract_titles([]), [])

    def test_extracts_titles(self):
        items = [
            {"title": "First"},
            {"title": "Second"},
            {"title": ""},
        ]
        result = extract_titles(items)
        self.assertEqual(result, ["First", "Second"])

    def test_respects_limit(self):
        items = [{"title": f"Item {i}"} for i in range(20)]
        result = extract_titles(items, limit=3)
        self.assertEqual(len(result), 3)

    def test_skips_empty_titles(self):
        items = [
            {"title": "First"},
            {"title": ""},
            {"title": "Second"},
        ]
        result = extract_titles(items)
        self.assertEqual(len(result), 2)
        self.assertIn("First", result)
        self.assertIn("Second", result)

    def test_empty_title_skipped(self):
        items = [{"title": ""}, {"title": "  "}, {"title": "Real"}]
        result = extract_titles(items)
        self.assertEqual(result, ["Real"])


class MarketKeyTestCase(unittest.TestCase):
    """_market_key tests"""

    def test_us_returns_us(self):
        from trend_sources import _market_key
        self.assertEqual(_market_key("us"), "us")

    def test_uppercase_us_returns_us(self):
        from trend_sources import _market_key
        self.assertEqual(_market_key("US"), "us")

    def test_jp_returns_jp(self):
        from trend_sources import _market_key
        self.assertEqual(_market_key("jp"), "jp")

    def test_unknown_returns_us(self):
        from trend_sources import _market_key
        self.assertEqual(_market_key("unknown"), "us")


class GoogleTrendsRssUrlTestCase(unittest.TestCase):
    """_google_trends_rss_url tests"""

    def test_us_url(self):
        from trend_sources import _google_trends_rss_url
        url = _google_trends_rss_url("us")
        self.assertIn("geo=US", url)

    def test_jp_url(self):
        from trend_sources import _google_trends_rss_url
        url = _google_trends_rss_url("jp")
        self.assertIn("geo=JP", url)


class MarketQueriesFunctionTestCase(unittest.TestCase):
    """market_queries function tests"""

    def test_us_returns_queries(self):
        from trend_sources import market_queries
        queries = market_queries("us")
        self.assertTrue(len(queries) > 0)

    def test_jp_returns_queries(self):
        from trend_sources import market_queries
        queries = market_queries("jp")
        self.assertTrue(len(queries) > 0)


class SymbolQueriesFunctionTestCase(unittest.TestCase):
    """symbol_queries function tests"""

    def test_us_returns_queries(self):
        from trend_sources import symbol_queries
        queries = symbol_queries("AAPL", "Apple", "us")
        self.assertTrue(len(queries) > 0)

    def test_jp_returns_queries(self):
        from trend_sources import symbol_queries
        queries = symbol_queries("7203.T", "トヨタ自動車", "jp")
        self.assertTrue(len(queries) > 0)


class DataframeFirstColumnValuesTestCase(unittest.TestCase):
    """_dataframe_first_column_values tests"""

    def test_none_returns_empty(self):
        from trend_sources import _dataframe_first_column_values
        self.assertEqual(_dataframe_first_column_values(None), [])

    def test_empty_df_returns_empty(self):
        from trend_sources import _dataframe_first_column_values
        import pandas as pd
        self.assertEqual(_dataframe_first_column_values(pd.DataFrame()), [])

    def test_valid_df_returns_values(self):
        from trend_sources import _dataframe_first_column_values
        import pandas as pd
        df = pd.DataFrame({"col": ["val1", "val2", "val3"]})
        result = _dataframe_first_column_values(df, limit=2)
        self.assertEqual(result, ["val1", "val2"])

    def test_empty_cells_skipped(self):
        from trend_sources import _dataframe_first_column_values
        import pandas as pd
        df = pd.DataFrame({"col": ["val1", "", "val3"]})
        result = _dataframe_first_column_values(df, limit=5)
        self.assertEqual(result, ["val1", "val3"])


class GdeltQueryUrlTestCase(unittest.TestCase):
    """_gdelt_query_url tests"""

    def test_us_url_contains_english(self):
        from trend_sources import _gdelt_query_url
        url = _gdelt_query_url("test query", "us")
        self.assertIn("lang=english", url)
        self.assertIn("test+query", url)

    def test_jp_url_contains_japanese(self):
        from trend_sources import _gdelt_query_url
        url = _gdelt_query_url("test query", "jp")
        self.assertIn("lang=japanese", url)


class WikipediaProjectTestCase(unittest.TestCase):
    """_wikipedia_project tests"""

    def test_us_returns_en(self):
        from trend_sources import _wikipedia_project
        self.assertEqual(_wikipedia_project("us"), "en.wikipedia.org")

    def test_jp_returns_ja(self):
        from trend_sources import _wikipedia_project
        self.assertEqual(_wikipedia_project("jp"), "ja.wikipedia.org")


class RequestJsonTestCase(unittest.TestCase):
    """_request_json tests (mocked)"""

    @patch("trend_sources.requests.get")
    def test_makes_request_with_user_agent(self, mock_get):
        from trend_sources import _request_json
        mock_response = MagicMock()
        mock_response.json.return_value = {"key": "value"}
        mock_get.return_value = mock_response

        result = _request_json("https://example.com/api")
        self.assertEqual(result, {"key": "value"})
        mock_get.assert_called_once()
        headers = mock_get.call_args[1].get("headers", {})
        self.assertIn("User-Agent", headers)

    @patch("trend_sources.requests.get")
    def test_non_dict_response_returns_empty(self, mock_get):
        from trend_sources import _request_json
        mock_response = MagicMock()
        mock_response.json.return_value = "not a dict"
        mock_get.return_value = mock_response

        result = _request_json("https://example.com/api")
        self.assertEqual(result, {})

    @patch("trend_sources.requests.get")
    def test_passes_custom_headers(self, mock_get):
        from trend_sources import _request_json
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_get.return_value = mock_response

        result = _request_json(
            "https://example.com/api",
            headers={"Authorization": "Bearer test"}
        )
        self.assertEqual(result, {"ok": True})
        call_headers = mock_get.call_args[1]["headers"]
        self.assertEqual(call_headers["Authorization"], "Bearer test")

    @patch("trend_sources.requests.get")
    def test_passes_params(self, mock_get):
        from trend_sources import _request_json
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_get.return_value = mock_response

        _request_json("https://example.com/api", params={"q": "test"})
        call_params = mock_get.call_args[1].get("params", {})
        self.assertEqual(call_params["q"], "test")


class QueryTemplatesTestCase(unittest.TestCase):
    def test_get_market_queries_us(self):
        queries = QueryTemplates.get_market_queries("us")
        self.assertTrue(len(queries) > 0)
        self.assertTrue(any("US stock market" in q for q in queries))

    def test_get_market_queries_jp(self):
        queries = QueryTemplates.get_market_queries("jp")
        self.assertTrue(len(queries) > 0)
        self.assertTrue(any("日本株" in q for q in queries))

    def test_get_market_queries_unknown(self):
        queries = QueryTemplates.get_market_queries("unknown")
        self.assertEqual(queries, [])

    def test_get_symbol_queries_us(self):
        queries = QueryTemplates.get_symbol_queries("AAPL", "Apple", "us")
        self.assertTrue(len(queries) > 0)
        self.assertTrue(any("AAPL" in q for q in queries))
        self.assertTrue(any("Apple" in q for q in queries))

    def test_get_symbol_queries_jp(self):
        queries = QueryTemplates.get_symbol_queries("7203.T", "トヨタ自動車", "jp")
        self.assertTrue(len(queries) > 0)
        self.assertTrue(any("7203.T" in q for q in queries))
        self.assertTrue(any("トヨタ自動車" in q for q in queries))

    def test_get_symbol_queries_unknown_market(self):
        queries = QueryTemplates.get_symbol_queries("A", "B", "unknown")
        self.assertEqual(queries, [])


if __name__ == "__main__":
    unittest.main()
