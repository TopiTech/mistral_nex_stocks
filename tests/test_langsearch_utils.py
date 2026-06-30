"""Unit tests for services/search/langsearch.py utility functions."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.search.langsearch import (
    _map_langsearch_freshness,
    _extract_langsearch_entries,
    _format_langsearch_items,
    _summarize_http_error,
)


class MapLangSearchFreshnessTestCase(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(_map_langsearch_freshness("d"), "oneDay")
        self.assertEqual(_map_langsearch_freshness("w"), "oneWeek")
        self.assertEqual(_map_langsearch_freshness("m"), "oneMonth")
        self.assertEqual(_map_langsearch_freshness("y"), "oneYear")

    def test_unknown_defaults_to_no_limit(self):
        self.assertEqual(_map_langsearch_freshness("x"), "noLimit")
        self.assertEqual(_map_langsearch_freshness(""), "noLimit")
        self.assertEqual(_map_langsearch_freshness(None), "noLimit")


class ExtractLangSearchEntriesTestCase(unittest.TestCase):
    """Test _extract_langsearch_entries with various response shapes."""

    def _make_entry(self):
        return {"url": "https://example.com"}

    def test_webpages_value_path(self):
        payload = {"data": {"webPages": {"value": [{"url": "u"}]}}}
        entries = _extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u"}])

    def test_data_results_path(self):
        payload = {"data": {"results": [{"url": "u1"}]}}
        entries = _extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u1"}])

    def test_data_items_path(self):
        payload = {"data": {"items": [{"url": "u2"}]}}
        entries = _extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u2"}])

    def test_data_webpages_results_path(self):
        # data.webPages.value exists but data.webPages.results does not
        payload = {"data": {"webPages": {"results": [{"url": "u3"}]}}}
        entries = _extract_langsearch_entries(payload)
        self.assertEqual(entries, [])

    def test_root_results_path(self):
        payload = {"results": [{"url": "u4"}]}
        entries = _extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u4"}])

    def test_root_items_path(self):
        payload = {"items": [{"url": "u5"}]}
        entries = _extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u5"}])

    def test_root_webpages_value_path(self):
        payload = {"webPages": {"value": [{"url": "u6"}]}}
        entries = _extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u6"}])

    def test_empty_payload(self):
        entries = _extract_langsearch_entries({})
        self.assertEqual(entries, [])

    def test_non_dict_payload(self):
        entries = _extract_langsearch_entries("not a dict")
        self.assertEqual(entries, [])

    def test_data_is_not_dict(self):
        entries = _extract_langsearch_entries({"data": "string"})
        self.assertEqual(entries, [])

    def test_webpages_value_is_not_list(self):
        entries = _extract_langsearch_entries({"data": {"webPages": {"value": "not list"}}})
        self.assertEqual(entries, [])

    def test_no_matching_path(self):
        entries = _extract_langsearch_entries({"data": {"unrelated": "data"}})
        self.assertEqual(entries, [])


class FormatLangSearchItemsTestCase(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(_format_langsearch_items([]), [])

    def test_skips_non_dict(self):
        items = ["string", 42, None, {"title": "ok", "url": "u"}]
        result = _format_langsearch_items(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "ok")

    def test_camel_case_keys(self):
        items = [
            {
                "title": "T",
                "snippet": "Snippet text",
                "url": "https://example.com",
                "source": "Src",
                "publishedAt": "2026-01-01",
            }
        ]
        result = _format_langsearch_items(items)
        self.assertEqual(result[0]["title"], "T")
        self.assertEqual(result[0]["summary"], "Snippet text")
        self.assertEqual(result[0]["url"], "https://example.com")
        self.assertEqual(result[0]["date"], "2026-01-01")

    def test_snake_case_keys(self):
        items = [
            {
                "name": "N",
                "summary": "Summary text",
                "link": "https://example.com/link",
                "siteName": "Site",
                "published_at": "2026-01-02",
            }
        ]
        result = _format_langsearch_items(items)
        self.assertEqual(result[0]["title"], "N")
        self.assertEqual(result[0]["summary"], "Summary text")
        self.assertEqual(result[0]["url"], "https://example.com/link")
        self.assertEqual(result[0]["source"], "Site")
        self.assertEqual(result[0]["date"], "2026-01-02")

    def test_fallback_keys(self):
        items = [
            {
                "description": "Desc",
                "body": "Body",
                "href": "https://example.com/href",
                "site": "Site2",
                "displayUrl": "display",
                "date": "2026-01-03",
            }
        ]
        result = _format_langsearch_items(items)
        self.assertEqual(result[0]["summary"], "Desc")
        self.assertEqual(result[0]["url"], "https://example.com/href")
        # source uses "site" before "displayUrl"
        self.assertEqual(result[0]["source"], "Site2")
        self.assertEqual(result[0]["date"], "2026-01-03")

    def test_missing_fields_get_empty_strings(self):
        items = [{}]
        result = _format_langsearch_items(items)
        self.assertEqual(result[0]["title"], "")
        self.assertEqual(result[0]["summary"], "")
        self.assertEqual(result[0]["url"], "")
        self.assertEqual(result[0]["source"], "langsearch")
        self.assertEqual(result[0]["date"], "")


class SummarizeHttpErrorTestCase(unittest.TestCase):
    def test_no_response_returns_str(self):
        exc = ValueError("something failed")
        result = _summarize_http_error(exc)
        self.assertEqual(result, "something failed")

    def test_with_response_status(self):
        import requests
        response = MagicMock()
        response.status_code = 429
        response.text = "Too Many Requests"
        exc = requests.HTTPError("rate limit", response=response)
        result = _summarize_http_error(exc)
        self.assertIn("429", result)
        self.assertIn("Too Many Requests", result)

    def test_with_empty_body(self):
        import requests
        response = MagicMock()
        response.status_code = 500
        response.text = ""
        exc = requests.HTTPError("server error", response=response)
        result = _summarize_http_error(exc)
        self.assertIn("500", result)
        # Use io mock to handle unexpected empty text
        self.assertIn("<empty>", result)

    def test_truncates_long_body(self):
        import requests
        response = MagicMock()
        response.status_code = 400
        response.text = "x" * 500
        exc = requests.HTTPError("bad request", response=response)
        result = _summarize_http_error(exc)
        self.assertLess(len(result), 320)  # status prefix + 300 + ellipsis

    def test_request_exception_no_response(self):
        import requests
        exc = requests.ConnectionError("connection refused")
        result = _summarize_http_error(exc)
        self.assertEqual(result, "connection refused")



if __name__ == "__main__":
    unittest.main()
