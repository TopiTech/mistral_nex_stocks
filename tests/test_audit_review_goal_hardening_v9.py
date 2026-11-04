"""
tests/test_audit_review_goal_hardening_v9.py - Comprehensive tests for Goal Hardening v9 audit.

Covers:
- NewsService news collection, partial failure handling, cache validation, and fanout pool lifecycle.
- RSS collectors resilience against malformed feeds, RuntimeError, AttributeError.
- Search service string-cache parsing with multiple delimiter types.
- API validation boundaries and edge cases.
"""

from __future__ import annotations

import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import trend_sources as ts
from schemas.ai_portfolio import AIPortfolioGenerateRequest
from schemas.analysis import AIChatRequest
from schemas.stocks import PortfolioUpdateRequest, ScreenerQueryRequest
from services.ai_service import ProviderAPIError
from services.news_service import (
    NewsService,
    _get_news_fanout_pool,
    shutdown_news_fanout_pool,
)
from services.search_service import _get_market_trending_titles


class TestNewsServiceHardening(unittest.TestCase):
    """Unit tests for NewsService synchronization, caching, and error resilience."""

    def setUp(self) -> None:
        self.service = NewsService()

    def test_news_fanout_pool_lifecycle(self) -> None:
        """Verify the module-level fan-out pool shuts down cleanly and recreates on demand."""
        pool1 = _get_news_fanout_pool()
        self.assertIsNotNone(pool1)
        shutdown_news_fanout_pool(wait=False)
        pool2 = _get_news_fanout_pool()
        self.assertIsNotNone(pool2)
        self.assertIsNot(pool1, pool2)

    @patch("services.news_service.call_mistral_chat")
    @patch("services.news_service.wait")
    @patch("services.news_service._get_news_fanout_pool")
    def test_get_synchronized_market_news_success(
        self, mock_get_pool: MagicMock, mock_wait: MagicMock, mock_chat: MagicMock
    ) -> None:
        """Test full successful synchronization pipeline."""
        pool_mock = MagicMock()
        mock_get_pool.return_value = pool_mock

        fut_us = Future()
        fut_us.set_result("US GDP grew by 2.8% in Q3 exceeding expectations.")
        fut_jp = Future()
        fut_jp.set_result("Nikkei rises 200 points as semiconductor stocks rally.")
        fut_us_t = Future()
        fut_us_t.set_result(["Tech Rally", "Rate Cut Prospects"])
        fut_jp_t = Future()
        fut_jp_t.set_result(["日経平均上昇", "円安進行"])

        pool_mock.submit.side_effect = [fut_us, fut_jp, fut_us_t, fut_jp_t]
        mock_wait.return_value = ({fut_us, fut_jp, fut_us_t, fut_jp_t}, set())

        mock_chat.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"us":"米GDPが好調に推移し市場を後押し","jp":"半導体関連株が牽引し日経平均続伸","trends":"ハイテク株と金融政策への関心が継続"}'
                    }
                }
            ]
        }

        progress_calls: list[tuple[str, str]] = []
        result = self.service.get_synchronized_market_news(
            api_key="test-key-12345",
            langsearch_api_key="lang-key-12345",
            tavily_api_key="tvly-key-12345",
            force_refresh=True,
            progress_callback=lambda stage, msg: progress_calls.append((stage, msg)),
        )

        self.assertIn("us", result)
        self.assertIn("jp", result)
        self.assertIn("trends", result)
        self.assertEqual(result["retrieve_status"]["us"], "success")
        self.assertEqual(result["retrieve_status"]["jp"], "success")
        self.assertEqual(result["retrieve_status"]["trends"], "success")
        self.assertTrue(len(progress_calls) >= 2)
        self.assertIn("米GDPが好調", result["us"]["content"])

    @patch("services.news_service.wait")
    @patch("services.news_service._get_news_fanout_pool")
    def test_get_synchronized_market_news_empty_context_skips_llm(
        self, mock_get_pool: MagicMock, mock_wait: MagicMock
    ) -> None:
        """When all context collectors return empty, LLM must not be called."""
        pool_mock = MagicMock()
        mock_get_pool.return_value = pool_mock

        fut_us = Future()
        fut_us.set_result("")
        fut_jp = Future()
        fut_jp.set_result("")
        fut_us_t = Future()
        fut_us_t.set_result([])
        fut_jp_t = Future()
        fut_jp_t.set_result([])

        pool_mock.submit.side_effect = [fut_us, fut_jp, fut_us_t, fut_jp_t]
        mock_wait.return_value = ({fut_us, fut_jp, fut_us_t, fut_jp_t}, set())

        with patch("services.news_service.call_mistral_chat") as mock_chat:
            result = self.service.get_synchronized_market_news(
                api_key="test-key-12345",
                langsearch_api_key="lang-key-12345",
                tavily_api_key="tvly-key-12345",
                force_refresh=True,
            )
            mock_chat.assert_not_called()

        self.assertEqual(result["retrieve_status"]["us"], "empty")
        self.assertEqual(result["retrieve_status"]["jp"], "empty")
        self.assertEqual(result["retrieve_status"]["trends"], "empty")
        self.assertEqual(result["us"]["content"], "")
        self.assertEqual(result["jp"]["content"], "")

    @patch("services.news_service.call_mistral_chat")
    @patch("services.news_service.wait")
    @patch("services.news_service._get_news_fanout_pool")
    def test_get_synchronized_market_news_partial_timeout_and_error(
        self, mock_get_pool: MagicMock, mock_wait: MagicMock, mock_chat: MagicMock
    ) -> None:
        """Verify status mapping when some futures timeout and others fail."""
        pool_mock = MagicMock()
        mock_get_pool.return_value = pool_mock

        fut_us = Future()
        fut_us.set_result("US inflation cool down")
        fut_jp = Future()
        fut_jp.set_exception(RuntimeError("Network error"))
        fut_us_t = Future()  # not done -> timeout
        fut_jp_t = Future()
        fut_jp_t.set_result(["円高進行"])

        pool_mock.submit.side_effect = [fut_us, fut_jp, fut_us_t, fut_jp_t]
        mock_wait.return_value = ({fut_us, fut_jp, fut_jp_t}, {fut_us_t})

        mock_chat.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"us":"米インフレ鈍化","jp":"","trends":"為替相場に注目"}'
                    }
                }
            ]
        }

        result = self.service.get_synchronized_market_news(
            api_key="test-key-12345",
            langsearch_api_key="lang-key-12345",
            tavily_api_key="tvly-key-12345",
            force_refresh=True,
        )

        self.assertEqual(result["retrieve_status"]["us"], "success")
        self.assertEqual(result["retrieve_status"]["jp"], "error")
        self.assertEqual(result["trend_source_status"]["us"], "timeout")
        self.assertEqual(result["trend_source_status"]["jp"], "success")
        # Since merged_trends has "円高進行", trends aggregate is success
        self.assertEqual(result["retrieve_status"]["trends"], "success")

    @patch("services.news_service.call_mistral_chat")
    @patch("services.news_service.wait")
    @patch("services.news_service._get_news_fanout_pool")
    def test_get_synchronized_market_news_provider_error_raises(
        self, mock_get_pool: MagicMock, mock_wait: MagicMock, mock_chat: MagicMock
    ) -> None:
        """When Mistral API returns an error response, ProviderAPIError must be raised."""
        pool_mock = MagicMock()
        mock_get_pool.return_value = pool_mock

        fut_us = Future()
        fut_us.set_result("US news context")
        fut_jp = Future()
        fut_jp.set_result("JP news context")
        fut_us_t = Future()
        fut_us_t.set_result(["US Trend"])
        fut_jp_t = Future()
        fut_jp_t.set_result(["JP Trend"])

        pool_mock.submit.side_effect = [fut_us, fut_jp, fut_us_t, fut_jp_t]
        mock_wait.return_value = ({fut_us, fut_jp, fut_us_t, fut_jp_t}, set())

        mock_chat.return_value = {
            "error": {"message": "Rate limit exceeded", "status_code": 429}
        }

        with self.assertRaises(ProviderAPIError) as cm:
            self.service.get_synchronized_market_news(
                api_key="test-key-12345",
                langsearch_api_key="lang-key-12345",
                tavily_api_key="tvly-key-12345",
                force_refresh=True,
            )
        self.assertEqual(cm.exception.status_code, 429)

    @patch("services.news_service.get_cached")
    @patch("services.news_service.wait")
    @patch("services.news_service._get_news_fanout_pool")
    def test_news_bundle_caching_valid_func_logic(
        self, mock_get_pool: MagicMock, mock_wait: MagicMock, mock_get_cached: MagicMock
    ) -> None:
        """Verify the valid_func passed to get_cached accepts partial content and rejects error markers."""
        pool_mock = MagicMock()
        mock_get_pool.return_value = pool_mock

        fut_us = Future()
        fut_us.set_result("US GDP news")
        fut_jp = Future()
        fut_jp.set_result("")
        fut_us_t = Future()
        fut_us_t.set_result([])
        fut_jp_t = Future()
        fut_jp_t.set_result([])

        pool_mock.submit.side_effect = [fut_us, fut_jp, fut_us_t, fut_jp_t]
        mock_wait.return_value = ({fut_us, fut_jp, fut_us_t, fut_jp_t}, set())

        # Run with force_refresh=False so get_cached is called
        mock_get_cached.return_value = {"us": "米GDP増加", "jp": "", "trends": ""}
        self.service.get_synchronized_market_news(
            api_key="test-key-12345",
            langsearch_api_key="lang-key-12345",
            tavily_api_key="tvly-key-12345",
            force_refresh=False,
        )

        self.assertTrue(mock_get_cached.called)
        _, kwargs = mock_get_cached.call_args
        valid_func = kwargs.get("valid_func")
        self.assertIsNotNone(valid_func)

        # 1. Partial valid bundle (US has content, JP/trends empty) -> Valid (True)
        self.assertTrue(valid_func({"us": "米GDP増加", "jp": "", "trends": ""}))
        # 2. Complete valid bundle -> Valid (True)
        self.assertTrue(valid_func({"us": "US news", "jp": "JP news", "trends": "Trend"}))
        # 3. Completely empty bundle -> Invalid (False)
        self.assertFalse(valid_func({"us": "", "jp": "", "trends": ""}))
        # 4. Error marker in one section -> Invalid (False)
        self.assertFalse(valid_func({"us": "米GDP増加", "jp": "解析エラー", "trends": ""}))
        # 5. Pending marker in one section -> Invalid (False)
        self.assertFalse(valid_func({"us": "米GDP増加", "jp": "解析中...", "trends": ""}))
        # 6. Non-dict -> Invalid (False)
        self.assertFalse(valid_func(None))
        self.assertFalse(valid_func("invalid string"))


class TestRssCollectionHardening(unittest.TestCase):
    """Unit tests for RSS and trend source error resilience."""

    @patch("trend_sources._fetch_rss_feed")
    def test_collect_rss_items_resilience(self, mock_fetch: MagicMock) -> None:
        """Verify collect_rss_items skips failing feeds without aborting others."""
        feed_good = MagicMock()
        feed_good.feed.title = "Good Feed"
        feed_good.entries = [
            {
                "title": "Good News Item",
                "summary": "Everything is proceeding normally.",
                "link": "https://news.example.com/item1",
                "published": "2026-11-04T12:00:00Z",
            }
        ]

        # Feed 1 raises RuntimeError (feedparser missing/failed),
        # Feed 2 raises AttributeError (corrupt feed structure),
        # Feed 3 succeeds
        mock_fetch.side_effect = [
            RuntimeError("Feedparser error"),
            AttributeError("'NoneType' object has no attribute 'feed'"),
            feed_good,
        ]

        items = ts.collect_rss_items(
            ["https://bad1.com/rss", "https://bad2.com/rss", "https://good.com/rss"],
            feed_source="test_source",
            max_per_feed=5,
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Good News Item")
        self.assertEqual(items[0]["source"], "Good Feed")

    @patch("trend_sources._fetch_rss_feed")
    def test_collect_google_trends_rss_resilience(self, mock_fetch: MagicMock) -> None:
        """Verify collect_google_trends_rss_items catches RuntimeError and returns empty."""
        mock_fetch.side_effect = RuntimeError("Service unavailable")
        items = ts.collect_google_trends_rss_items("us", count=5)
        self.assertEqual(items, [])


class TestSearchServiceTrendingHardening(unittest.TestCase):
    """Unit tests for search service trending titles parsing."""

    @patch("services.search_service._get_cached_value")
    def test_get_market_trending_titles_multi_delimiter_string(
        self, mock_cache: MagicMock
    ) -> None:
        """Verify cached string is parsed with newlines, commas, and Japanese punctuation."""
        mock_cache.return_value = "Title One\nTitle Two, Title Three、Title Four"
        titles = _get_market_trending_titles("us", "tavily", "lang-key", "tavily-key")
        self.assertEqual(titles, ["Title One", "Title Two", "Title Three", "Title Four"])


class TestValidationBoundariesHardening(unittest.TestCase):
    """Unit tests for API boundary validation constraints."""

    def test_portfolio_update_rejects_boolean_values(self) -> None:
        """Boolean values must be rejected for numeric shares and price fields."""
        with self.assertRaises(ValueError):
            PortfolioUpdateRequest(
                symbol="AAPL",
                market="us",
                shares=True,  # type: ignore[arg-type]
                avg_price=150.0,
            )

        with self.assertRaises(ValueError):
            PortfolioUpdateRequest(
                symbol="AAPL",
                market="us",
                shares=10.0,
                avg_price=False,  # type: ignore[arg-type]
            )

    def test_screener_query_validates_bounds(self) -> None:
        """Min price greater than max price must fail validation."""
        with self.assertRaises(ValueError):
            ScreenerQueryRequest(min_price=200.0, max_price=100.0)

        with self.assertRaises(ValueError):
            ScreenerQueryRequest(min_change=10.0, max_change=-5.0)

    def test_chat_request_validates_token_pattern(self) -> None:
        """Idempotency token must conform to allowed character set and length."""
        with self.assertRaises(ValueError):
            AIChatRequest(
                symbol="AAPL",
                market="us",
                message="Explain recent earnings",
                request_token="invalid!token#123",
            )

    def test_ai_portfolio_generate_validates_theme(self) -> None:
        """Blank or empty theme must fail validation."""
        with self.assertRaises(ValueError):
            AIPortfolioGenerateRequest(theme="   ")


if __name__ == "__main__":
    unittest.main()
