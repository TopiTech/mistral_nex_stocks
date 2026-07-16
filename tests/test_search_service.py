"""
search_service.py のユニットテスト

外部 HTTP / SDK を多用する search_service の主要分岐をモック化して検証する。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import search_service


class DdgsTimeoutTestCase(unittest.TestCase):
    """_get_ddgs_timeout の環境変数パース"""

    def test_default_value(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(search_service._get_ddgs_timeout(), 5)

    def test_custom_value(self):
        with patch.dict("os.environ", {"DDGS_TIMEOUT": "12"}, clear=False):
            self.assertEqual(search_service._get_ddgs_timeout(), 12)

    def test_invalid_value_falls_back_to_default(self):
        with patch.dict("os.environ", {"DDGS_TIMEOUT": "not-a-number"}, clear=False):
            self.assertEqual(search_service._get_ddgs_timeout(), 5)

    def test_value_clamped_to_min(self):
        with patch.dict("os.environ", {"DDGS_TIMEOUT": "0"}, clear=False):
            self.assertEqual(search_service._get_ddgs_timeout(), 1)

    def test_value_clamped_to_max(self):
        with patch.dict("os.environ", {"DDGS_TIMEOUT": "999"}, clear=False):
            self.assertEqual(search_service._get_ddgs_timeout(), 60)


class DdgsNewsSearchTestCase(unittest.TestCase):
    """ddgs_news_search のフォールバックとクエリ長制限"""

    def test_empty_query_returns_empty(self):
        # Mock ddgs_session to avoid real network access
        session = MagicMock()
        result = search_service.ddgs_news_search("", ddgs_session=session)
        self.assertEqual(result, [])
        session.news.assert_not_called()

    def test_query_truncated_when_too_long(self):
        long_query = "x" * 1000
        captured_kwargs = {}

        def fake_news(**kwargs):
            captured_kwargs.update(kwargs)
            return [{"title": "ok", "body": "ok", "url": "u", "source": "s", "date": "d"}]

        session = MagicMock()
        session.news.side_effect = fake_news
        result = search_service.ddgs_news_search(long_query, ddgs_session=session)
        self.assertEqual(len(result), 1)
        self.assertLessEqual(len(captured_kwargs["query"]), search_service.MAX_DDGS_QUERY_LEN)

    def test_news_results_normalized(self):
        session = MagicMock()
        session.news.return_value = [
            {"title": "T", "body": "B", "url": "U", "source": "S", "date": "D"}
        ]
        result = search_service.ddgs_news_search("apple", ddgs_session=session)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "T")
        self.assertEqual(result[0]["body"], "B")
        self.assertEqual(result[0]["source"], "S")
        self.assertEqual(result[0]["date"], "D")


class DdgsTextSearchTestCase(unittest.TestCase):
    """ddgs_text_search のフォールバック"""

    def test_empty_query_returns_empty(self):
        session = MagicMock()
        session.text.return_value = []
        result = search_service.ddgs_text_search("", ddgs_session=session)
        self.assertEqual(result, [])

    def test_text_results_normalized_to_ddgs_text_source(self):
        session = MagicMock()
        session.text.return_value = [{"title": "T", "body": "B", "href": "U"}]
        result = search_service.ddgs_text_search("apple", ddgs_session=session)
        self.assertEqual(len(result), 1)
        # ddgs_text_search returns raw DDGS items; downstream _format_ddgs_text_items
        # normalizes them. Here we verify the raw "href" is preserved.
        self.assertEqual(result[0]["href"], "U")
        self.assertEqual(result[0]["title"], "T")


class FormatDdgsItemsTestCase(unittest.TestCase):
    def test_format_ddgs_news_items(self):
        items = [{"title": "t", "body": "b", "url": "u", "source": "s", "date": "d"}]
        rows = search_service._format_ddgs_news_items(items)
        self.assertEqual(rows[0]["summary"], "b")
        self.assertEqual(rows[0]["date"], "d")

    def test_format_ddgs_text_items_uses_href_as_url(self):
        items = [{"title": "t", "body": "b", "href": "u"}]
        rows = search_service._format_ddgs_text_items(items)
        self.assertEqual(rows[0]["url"], "u")
        self.assertEqual(rows[0]["source"], "ddgs_text")
        self.assertEqual(rows[0]["date"], "")


class FormatLangSearchItemsTestCase(unittest.TestCase):
    def test_normalizes_camel_and_snake_keys(self):
        items = [
            {
                "title": "T",
                "snippet": "S",
                "url": "U",
                "source": "X",
                "publishedAt": "2025-01-01",
            },
            {
                "name": "N",
                "summary": "S2",
                "link": "U2",
                "siteName": "Y",
                "datePublished": "2025-01-02",
            },
        ]
        rows = search_service._format_langsearch_items(items)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["url"], "U")
        self.assertEqual(rows[0]["date"], "2025-01-01")
        self.assertEqual(rows[1]["url"], "U2")
        self.assertEqual(rows[1]["date"], "2025-01-02")

    def test_skips_non_dict_entries(self):
        items = ["bad", 42, {"title": "ok", "snippet": "x", "url": "u", "source": "s"}]
        rows = search_service._format_langsearch_items(items)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "ok")


class ExtractLangSearchEntriesTestCase(unittest.TestCase):
    def test_webpages_value_path(self):
        payload = {"data": {"webPages": {"value": [{"url": "u"}]}}}
        entries = search_service._extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u"}])

    def test_results_array_path(self):
        payload = {"results": [{"url": "u"}]}
        entries = search_service._extract_langsearch_entries(payload)
        self.assertEqual(entries, [{"url": "u"}])

    def test_empty_payload(self):
        entries = search_service._extract_langsearch_entries({})
        self.assertEqual(entries, [])

    def test_non_dict_payload(self):
        entries = search_service._extract_langsearch_entries("not a dict")
        self.assertEqual(entries, [])


class MapLangSearchFreshnessTestCase(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(search_service._map_langsearch_freshness("d"), "oneDay")
        self.assertEqual(search_service._map_langsearch_freshness("w"), "oneWeek")
        self.assertEqual(search_service._map_langsearch_freshness("m"), "oneMonth")
        self.assertEqual(search_service._map_langsearch_freshness("y"), "oneYear")

    def test_unknown_defaults_to_no_limit(self):
        self.assertEqual(search_service._map_langsearch_freshness("x"), "noLimit")
        self.assertEqual(search_service._map_langsearch_freshness(""), "noLimit")
        self.assertEqual(search_service._map_langsearch_freshness(None), "noLimit")


class LangSearchRetryableTestCase(unittest.TestCase):
    def test_timeout_is_retryable(self):
        import requests

        self.assertTrue(search_service._langsearch_request_retryable(requests.Timeout("x")))
        self.assertTrue(search_service._langsearch_request_retryable(requests.ConnectionError("x")))

    def test_quota_errors_not_retryable(self):
        import requests

        response = MagicMock()
        response.status_code = 429
        exc = requests.HTTPError("insufficient balance", response=response)
        self.assertFalse(search_service._langsearch_request_retryable(exc))

    def test_429_retryable_when_not_quota(self):
        import requests

        response = MagicMock()
        response.status_code = 429
        exc = requests.HTTPError("rate limited", response=response)
        self.assertTrue(search_service._langsearch_request_retryable(exc))

    def test_503_retryable(self):
        import requests

        response = MagicMock()
        response.status_code = 503
        exc = requests.HTTPError("unavailable", response=response)
        self.assertTrue(search_service._langsearch_request_retryable(exc))


class LangSearchSearchTestCase(unittest.TestCase):
    def test_empty_query_returns_empty(self):
        result = search_service.langsearch_search("", api_key="dummy")
        self.assertEqual(result, [])

    def test_missing_api_key_raises(self):
        with self.assertRaises(ValueError):
            search_service.langsearch_search("apple", api_key="")


class LangSearchRerankTestCase(unittest.TestCase):
    def test_returns_documents_unchanged_for_empty(self):
        docs = [{"summary": "x", "title": "t"}]
        result = search_service.langsearch_rerank("query", docs, api_key="")
        self.assertEqual(result, docs)

    def test_returns_documents_unchanged_when_too_few(self):
        docs = [{"summary": "x", "title": "t"}]
        result = search_service.langsearch_rerank("query", docs, api_key="key")
        self.assertEqual(result, docs)

    def test_empty_query_returns_documents_unchanged(self):
        docs = [{"summary": f"d{i}", "title": f"t{i}"} for i in range(3)]
        result = search_service.langsearch_rerank("", docs, api_key="key")
        self.assertEqual(result, docs)


class LangSearchRequestRetryableWith503TestCase(unittest.TestCase):
    """503のリトライ可能性を確認"""

    def test_503_with_no_response_is_retryable(self):
        import requests

        exc = requests.HTTPError("server error", response=None)
        # Without a response object, status is None, so returns False.
        self.assertFalse(search_service._langsearch_request_retryable(exc))


class MarketTrendsCacheKeyTestCase(unittest.TestCase):
    def test_cache_key_format(self):
        self.assertEqual(
            search_service._market_trends_cache_key("us", "ls"),
            "market_trends_us_ls",
        )
        self.assertEqual(
            search_service._market_trends_cache_key("jp", "ddgs"),
            "market_trends_jp_ddgs",
        )


class CollectMarketNewsContextTestCase(unittest.TestCase):
    """collect_market_news_context: LangSearch成功時は呼び出さない"""

    def test_uses_langsearch_when_api_key_present(self):
        with (
            patch(
                "services.search_service._collect_langsearch_items",
                return_value=[
                    {"title": "t", "summary": "s", "url": "u", "source": "ls", "date": "d"}
                ],
            ) as mock_ls,
            patch(
                "services.search_service._collect_ddgs_items",
                return_value=[],
            ) as mock_ddgs,
            patch(
                "trend_sources.collect_market_news_items_fast",
                return_value=[],
            ),
            patch(
                "trend_sources.dedupe_items",
                side_effect=lambda items: list(items),
            ),
            patch(
                "trend_sources.compact_context",
                return_value="compacted",
            ),
        ):
            result = search_service.collect_market_news_context("us", langsearch_api_key="key")
            self.assertEqual(result, "compacted")
            mock_ls.assert_called_once()
            mock_ddgs.assert_not_called()

    def test_falls_back_to_ddgs_when_langsearch_empty(self):
        with (
            patch(
                "services.search_service._collect_langsearch_items",
                return_value=[],
            ),
            patch(
                "services.search_service._collect_ddgs_items",
                return_value=[
                    {"title": "t", "summary": "s", "url": "u", "source": "ddgs", "date": ""}
                ],
            ) as mock_ddgs,
            patch(
                "trend_sources.collect_market_news_items_fast",
                return_value=[],
            ),
            patch(
                "trend_sources.dedupe_items",
                side_effect=lambda items: list(items),
            ),
            patch(
                "trend_sources.compact_context",
                return_value="ddgs-text",
            ),
        ):
            result = search_service.collect_market_news_context("us", langsearch_api_key="key")
            self.assertEqual(result, "ddgs-text")
            mock_ddgs.assert_called_once()

    def test_uses_ddgs_when_no_api_key(self):
        with (
            patch(
                "services.search_service._collect_ddgs_items",
                return_value=[],
            ) as mock_ddgs,
            patch(
                "trend_sources.collect_market_news_items_fast",
                return_value=[],
            ),
            patch(
                "trend_sources.dedupe_items",
                side_effect=lambda items: list(items),
            ),
            patch(
                "trend_sources.compact_context",
                return_value="only-ddgs",
            ),
        ):
            result = search_service.collect_market_news_context("us", langsearch_api_key="")
            self.assertEqual(result, "only-ddgs")
            # With no api keys, the strategy is "ddgs_only", so only _collect_ddgs_items is called.
            mock_ddgs.assert_called_once()


class YahooNewsExtractUrlTestCase(unittest.TestCase):
    """YahooNewsのextract_urlモンキーパッチのテスト"""

    def test_extract_url_with_redirect_format(self):
        # /RU= と /RK= を含むYahooリダイレクト形式のURL
        u = "https://r.search.yahoo.com/_ylt=A2RTG2ktgTZqjAIATk7QtDMD;_ylu=Y29sbwNhcC1zb3V0aGVhc3QtMQRwb3MDMTAEdnRpZAMEc2VjA3Ny/RV=2/RE=1783166509/RO=10/RU=https://www.aljazeera.com/economy/2026/6/15/stock-markets-soar-oil-falls-as-us-iran-confirm-deal-to-end-war/RK=2/RS=6HdJvgXtggmyzIYZphqufg867L8-"
        import ddgs.engines.yahoo_news

        extracted = ddgs.engines.yahoo_news.extract_url(u)
        self.assertEqual(
            extracted,
            "https://www.aljazeera.com/economy/2026/6/15/stock-markets-soar-oil-falls-as-us-iran-confirm-deal-to-end-war",
        )

    def test_extract_url_with_direct_format(self):
        # /RU= を含まないYahoo直接記事形式のURL
        u = "https://finance.yahoo.com/markets/stocks/articles/first-time-over-155-years-185000390.html"
        import ddgs.engines.yahoo_news

        extracted = ddgs.engines.yahoo_news.extract_url(u)
        self.assertEqual(extracted, u)


if __name__ == "__main__":
    unittest.main()
