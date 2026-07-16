"""Unit tests for services/search/tavily.py (Tavily search provider)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.search.tavily import (
    _get_tavily_client,
    tavily_search,
    _format_tavily_items,
    _collect_tavily_items,
)


class GetTavilyClientTestCase(unittest.TestCase):
    def test_creates_client_with_key(self):
        """Creates client with key using sys.modules mock to avoid import error."""
        mock_tavily_mod = MagicMock()
        with patch.dict("sys.modules", {"tavily": mock_tavily_mod}):
            _get_tavily_client("test-key")
            mock_tavily_mod.TavilyClient.assert_called_once_with(api_key="test-key")

    def test_raises_import_error_when_tavily_not_installed(self):
        with patch.dict("sys.modules", {"tavily": None}):
            with self.assertRaises(ImportError):
                _get_tavily_client("test-key")


class TavilySearchTestCase(unittest.TestCase):
    def test_empty_query_returns_empty(self):
        result = tavily_search("", api_key="key")
        self.assertEqual(result, [])

    def test_whitespace_query_returns_empty(self):
        result = tavily_search("   ", api_key="key")
        self.assertEqual(result, [])

    def test_missing_api_key_raises(self):
        with self.assertRaises(ValueError):
            tavily_search("test", api_key="")

    def test_search_with_minimal_params(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "results": [
                {
                    "title": "Test Article",
                    "content": "Content here",
                    "url": "https://example.com",
                    "source": "example",
                    "published_date": "2026-01-01",
                }
            ]
        }

        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            result = tavily_search(
                "test query",
                api_key="test-key",
                max_results=3,
                timelimit="d",
                topic="news",
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["title"], "Test Article")
            mock_client.search.assert_called_once()
            kwargs = mock_client.search.call_args[1]
            self.assertEqual(kwargs["search_depth"], "basic")  # max_results <= 5
            self.assertEqual(kwargs["topic"], "news")
            self.assertEqual(kwargs["time_range"], "day")
            self.assertEqual(kwargs["max_results"], 3)

    def test_search_with_advanced_depth(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}

        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            tavily_search("query", api_key="key", max_results=10)
            kwargs = mock_client.search.call_args[1]
            self.assertEqual(kwargs["search_depth"], "advanced")
            self.assertEqual(kwargs["max_results"], 10)

    def test_search_with_week_timelimit(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}

        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            tavily_search("query", api_key="key", timelimit="w")
            kwargs = mock_client.search.call_args[1]
            self.assertEqual(kwargs["time_range"], "week")

    def test_search_with_month_timelimit(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}

        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            tavily_search("query", api_key="key", timelimit="m")
            kwargs = mock_client.search.call_args[1]
            self.assertEqual(kwargs["time_range"], "month")

    def test_search_with_year_timelimit(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            tavily_search("query", api_key="key", timelimit="y")
            kwargs = mock_client.search.call_args[1]
            self.assertEqual(kwargs["time_range"], "year")

    def test_search_with_unknown_timelimit_no_time_range(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            tavily_search("query", api_key="key", timelimit="x")
            kwargs = mock_client.search.call_args[1]
            self.assertNotIn("time_range", kwargs)

    def test_search_clamps_max_results(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            tavily_search("query", api_key="key", max_results=100)
            self.assertEqual(mock_client.search.call_args[1]["max_results"], 20)

    def test_search_handles_non_dict_response(self):
        mock_client = MagicMock()
        mock_client.search.return_value = "not a dict"
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            result = tavily_search("query", api_key="key")
            self.assertEqual(result, [])

    def test_search_handles_non_list_results(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": "not a list"}
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            result = tavily_search("query", api_key="key")
            self.assertEqual(result, [])

    def test_search_handles_import_error_gracefully(self):
        with patch(
            "services.search.tavily._get_tavily_client", side_effect=ImportError("no package")
        ):
            result = tavily_search("query", api_key="key")
            self.assertEqual(result, [])

    def test_search_handles_generic_exception_gracefully(self):
        mock_client = MagicMock()
        mock_client.search.side_effect = RuntimeError("API error")
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            result = tavily_search("query", api_key="key")
            self.assertEqual(result, [])

    def test_search_with_general_topic(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            tavily_search("query", api_key="key", topic="general")
            self.assertEqual(mock_client.search.call_args[1]["topic"], "general")

    def test_search_none_query_handled(self):
        """None query should be normalized to empty string"""
        result = tavily_search(None, api_key="key")
        self.assertEqual(result, [])

    def test_search_non_string_query(self):
        """Non-string query should be normalized"""
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}
        with patch("services.search.tavily._get_tavily_client", return_value=mock_client):
            result = tavily_search(12345, api_key="key")
            # 12345 is non-empty so it should proceed
            self.assertEqual(result, [])


class FormatTavilyItemsTestCase(unittest.TestCase):
    def test_non_list_returns_empty(self):
        self.assertEqual(_format_tavily_items("not a list"), [])
        self.assertEqual(_format_tavily_items(None), [])

    def test_empty_list_returns_empty(self):
        self.assertEqual(_format_tavily_items([]), [])

    def test_skips_non_dict_entries(self):
        result = _format_tavily_items(["string", 42, {"title": "ok", "url": "u"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "ok")

    def test_uses_content_as_summary(self):
        items = [
            {
                "title": "Test",
                "content": "Content body",
                "url": "https://example.com",
                "source": "tavily",
                "published_date": "2026-01-01",
            }
        ]
        result = _format_tavily_items(items)
        self.assertEqual(result[0]["summary"], "Content body")
        self.assertEqual(result[0]["source"], "tavily")
        self.assertEqual(result[0]["date"], "2026-01-01")

    def test_falls_back_to_body_for_summary(self):
        items = [
            {
                "title": "Test",
                "body": "Body text",
                "url": "https://example.com",
                "published_date": "2026-01-01",
            }
        ]
        result = _format_tavily_items(items)
        self.assertEqual(result[0]["summary"], "Body text")

    def test_falls_back_to_date_for_date(self):
        items = [
            {
                "title": "Test",
                "content": "Content",
                "url": "https://example.com",
                "date": "2026-01-02",
            }
        ]
        result = _format_tavily_items(items)
        self.assertEqual(result[0]["date"], "2026-01-02")

    def test_missing_fields_get_empty_defaults(self):
        items: list[dict[str, Any]] = [{}]
        result = _format_tavily_items(items)
        self.assertEqual(result[0]["title"], "")
        self.assertEqual(result[0]["summary"], "")
        self.assertEqual(result[0]["url"], "")
        self.assertEqual(result[0]["source"], "tavily")
        self.assertEqual(result[0]["date"], "")


class CollectTavilyItemsTestCase(unittest.TestCase):
    def test_empty_api_key_returns_empty(self):
        result = _collect_tavily_items(["query"], api_key="", timelimit="d")
        self.assertEqual(result, [])

    @patch("services.search.tavily.tavily_search")
    @patch("services.search.tavily.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_collects_items_from_queries(self, mock_dedup, mock_search):
        # Each query returns the same item, so with 2 queries we get 2 items
        # (dedupe is mocked to pass through, so both copies are preserved)
        mock_search.return_value = [
            {
                "title": "Result 1",
                "content": "Content",
                "url": "https://example.com/1",
                "source": "tavily",
                "published_date": "2026-01-01",
            }
        ]
        result = _collect_tavily_items(
            ["query1", "query2"],
            api_key="test-key",
            timelimit="d",
            max_results=6,
            limit=10,
            query_limit=2,
        )
        # _format_tavily_items creates 1 item per result, tavily_search returns 1 result
        # per query, 2 queries = 2 items (dedupe mocked to pass through)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "Result 1")
        self.assertEqual(mock_search.call_count, 2)

    @patch("services.search.tavily.tavily_search")
    @patch("services.search.tavily.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_respects_limit(self, mock_dedup, mock_search):
        mock_search.return_value = [
            {
                "title": f"Result {i}",
                "content": "Content",
                "url": f"https://example.com/{i}",
                "source": "tavily",
                "published_date": "2026-01-01",
            }
            for i in range(10)
        ]
        result = _collect_tavily_items(
            ["query1"],
            api_key="test-key",
            timelimit="d",
            max_results=6,
            limit=5,
            query_limit=1,
        )
        self.assertEqual(len(result), 5)

    @patch("services.search.tavily.tavily_search")
    def test_handles_search_failure_gracefully(self, mock_search):
        mock_search.side_effect = ValueError("bad request")
        result = _collect_tavily_items(
            ["query1"],
            api_key="test-key",
            timelimit="d",
            max_results=6,
            limit=10,
            query_limit=1,
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
