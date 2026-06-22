"""
Core Logic Unit Tests
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app
from app_state import app_state
from app_bg import bg_interpolate_loop, clone_structure_for_current, sync_all_stocks_now
from app_helpers import _fetch_live_market_state, is_market_open
from services.ai_service import call_mistral_chat


class CoreLogicTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    @patch("app_bg.fetch_index_data")
    @patch("app_bg.fetch_stocks_batch")
    def test_sync_all_stocks_now_success(self, mock_fetch_batch, mock_fetch_index):
        # Setup mocked data returned by batch fetcher
        mock_fetch_index.return_value = (
            "DJI",
            {"price": "30000.0", "change": "100.0", "percent": "0.33"},
        )
        mock_fetch_batch.return_value = [
            {
                "symbol": "AAPL",
                "market": "us",
                "price": 150.0,
                "change": 1.5,
                "change_percent": 1.0,
                "market_state": "REGULAR",
            },
            {
                "symbol": "9984.T",
                "market": "jp",
                "price": 6000.0,
                "change": -50.0,
                "change_percent": -0.83,
                "market_state": "REGULAR",
            },
            {
                "symbol": "^N225",
                "market": "idx",
                "price": 38000.0,
                "change": 100.0,
                "change_percent": 0.26,
                "market_state": "REGULAR",
            },
        ]

        # Reset caching variables in app_state
        app_state.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.is_syncing = False

        sync_all_stocks_now()
        # Assertions
        self.assertFalse(app_state.is_syncing)
        self.assertTrue(len(app_state.target_stocks_cache["us"]) > 0)
        self.assertTrue(len(app_state.target_stocks_cache["jp"]) > 0)
        self.assertTrue(len(app_state.target_stocks_cache["idx"]) > 0)

    @patch("app_state.app_state.execution.shutdown_event.wait", side_effect=KeyboardInterrupt("stop loop"))
    def test_bg_interpolate_loop_exits(self, mock_wait):
        # Mock sse_announcer to return some listener count
        mock_announcer = MagicMock()
        mock_announcer.listener_count.return_value = 1

        # Temporarily mock app_state.sse_announcer
        old_announcer = app_state.sse_announcer
        app_state.sse_announcer = mock_announcer
        try:
            # Running this should raise KeyboardInterrupt instantly because wait is called inside.
            with self.assertRaises(KeyboardInterrupt):
                bg_interpolate_loop()
        finally:
            app_state.sse_announcer = old_announcer

    @patch("app_helpers.safe_get_ticker")
    @patch("app_helpers.time.time", return_value=150.0)
    def test_fetch_live_market_state_uses_history_metadata_when_open(
        self, mock_time, mock_get_ticker
    ):
        ticker = MagicMock()
        ticker.get_history_metadata.return_value = {
            "currentTradingPeriod": {"regular": {"start": 100.0, "end": 200.0}}
        }
        mock_get_ticker.return_value = ticker

        self.assertEqual(_fetch_live_market_state("us"), "REGULAR")
        mock_get_ticker.assert_called_once_with("^GSPC")

    @patch("app_helpers.safe_get_ticker")
    @patch("app_helpers.time.time", return_value=250.0)
    def test_fetch_live_market_state_uses_history_metadata_when_closed(
        self, mock_time, mock_get_ticker
    ):
        ticker = MagicMock()
        ticker.get_history_metadata.return_value = {
            "currentTradingPeriod": {"regular": {"start": 100.0, "end": 200.0}}
        }
        mock_get_ticker.return_value = ticker

        self.assertEqual(_fetch_live_market_state("jp"), "CLOSED")
        mock_get_ticker.assert_called_once_with("^N225")

    @patch("app_helpers._fetch_live_market_state", return_value="REGULAR")
    def test_is_market_open_uses_live_state_when_open(self, mock_fetch_live_market_state):
        self.assertTrue(is_market_open("us", bypass_cache=True))
        mock_fetch_live_market_state.assert_called_once_with("us")

    @patch("app_helpers._fetch_live_market_state", return_value="CLOSED")
    def test_is_market_open_uses_live_state_when_closed(self, mock_fetch_live_market_state):
        self.assertFalse(is_market_open("jp", bypass_cache=True))
        mock_fetch_live_market_state.assert_called_once_with("jp")

    def test_clone_structure_for_current_respects_closed_market(self):
        target_list = [
            {
                "symbol": "AAPL",
                "price": 100.0,
                "change": 2.0,
                "change_percent": 1.0,
                "market_state": "REGULAR",
            }
        ]
        current_list = [
            {
                "symbol": "AAPL",
                "price": 90.0,
                "change": 1.0,
                "change_percent": 0.5,
                "market_state": "REGULAR",
            }
        ]

        result = clone_structure_for_current(
            target_list, current_list, market="us", is_open=False
        )

        self.assertEqual(result[0]["price"], 100.0)
        self.assertEqual(result[0]["change"], 2.0)
        self.assertEqual(result[0]["change_percent"], 1.0)
        self.assertEqual(result[0]["market_state"], "CLOSED")

    @patch("services.ai_service._get_mistral_client")
    def test_call_mistral_chat_live(self, mock_get_client):
        # Setup mock client
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Mock chat response
        mock_response = MagicMock()
        # Mock choice
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.content = "Hello response"
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]

        # Mock model_dump to return a dict
        mock_response.model_dump.return_value = {
            "choices": [{"message": {"content": "Hello response"}}]
        }

        mock_client.chat.complete.return_value = mock_response

        # Ensure no rate limiting or cache hits interference
        app_state.mistral_response_cache.clear()

        with app.app_context():
            res = call_mistral_chat(
                api_key="test-api-key",
                messages=[{"role": "user", "content": "hello"}],
                use_cache=False,
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["choices"][0]["message"]["content"], "Hello response")
            mock_client.chat.complete.assert_called_once()

    def test_call_mistral_chat_cached(self):
        # Insert a value into the cache directly
        cache_key = ("mistral-large-latest", "some-unique-messages-hash")
        app_state.mistral_response_cache[cache_key] = {
            "choices": [{"message": {"content": "Cached response"}}],
            "model": "mistral-large-latest",
        }

        with patch("services.ai_service._build_mistral_cache_key", return_value=cache_key):
            with app.app_context():
                res = call_mistral_chat(
                    api_key="test-api-key",
                    messages=[{"role": "user", "content": "hello"}],
                    use_cache=True,
                )
                self.assertEqual(
                    res["choices"][0]["message"]["content"], "Cached response"
                )

    @patch("routes.api_analysis.call_mistral_chat")
    @patch("routes.api_analysis.collect_market_trending_titles")
    @patch("routes.api_analysis.collect_market_news_context")
    def test_api_news_bundle(
        self, mock_collect_context, mock_collect_trends, mock_call_mistral
    ):
        # Mocking news context gather and mistral responses
        mock_collect_context.return_value = "Mocked news context content"
        mock_collect_trends.return_value = ["US market rises", "Nikkei 225 slides"]

        mock_call_mistral.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"us": "US summary line 1", "jp": "JP summary line 1", "trends": "Trends summary line 1"}'
                    }
                }
            ]
        }

        # Test endpoint
        response = self.client.post(
            "/api/news",
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("us", data)
        self.assertIn("jp", data)
        self.assertIn("trends", data)

    @patch("routes.api_analysis.call_mistral_chat")
    @patch("routes.api_analysis.collect_market_trending_titles")
    @patch("routes.api_analysis.collect_market_news_context")
    def test_api_news_bundle_caching(
        self, mock_collect_context, mock_collect_trends, mock_call_mistral
    ):
        # Reset cache and stats
        app_state.cache.reset_stats()

        # Mocking news context gather (2 calls per request: US and JP)
        mock_collect_context.side_effect = [
            "News A US", "News A JP", # Request 1
            "News A US", "News A JP", # Request 2
            "News B US", "News B JP"  # Request 3
        ]
        mock_collect_trends.return_value = ["US market rises"]

        mock_call_mistral.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"us": "US summary A", "jp": "JP summary A", "trends": "Trends summary A"}'
                    }
                }
            ]
        }

        # 1st call: fresh call, should call mistral
        response = self.client.post(
            "/api/news",
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        mock_call_mistral.assert_called_once()
        mock_call_mistral.reset_mock()

        # 2nd call: same context, should use cache and NOT call mistral
        response = self.client.post(
            "/api/news",
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        mock_call_mistral.assert_not_called()

        # Pop context keys from caches so the 3rd request fetches the new mock context
        for cache in list(app_state.caches.values()):
            cache.pop("market_news_context_us_ddgs", None)
            cache.pop("market_news_context_jp_ddgs", None)

        # 3rd call: different context, should call mistral again
        mock_call_mistral.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"us": "US summary B", "jp": "JP summary B", "trends": "Trends summary B"}'
                    }
                }
            ]
        }
        response = self.client.post(
            "/api/news",
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        mock_call_mistral.assert_called_once()



    @patch("routes.api_analysis.get_stock_info_cached")
    @patch("routes.api_analysis.collect_symbol_research_context")
    @patch("routes.api_analysis.fetch_stock")
    @patch("routes.api_analysis.call_mistral_chat")
    def test_api_analyze_v2(self, mock_call_mistral, mock_fetch, mock_collect, mock_info):
        mock_info.return_value = {"sector": "Technology", "industry": "Consumer Electronics", "currency": "USD"}
        mock_collect.return_value = "dummy research context"
        mock_fetch.return_value = {"price": 150.0, "chart_data": [{"price": 150.0, "x": 1700000000000}]}

        # Mock LLM analysis response matching StockAnalysis Pydantic model
        mock_call_mistral.return_value = {
            "choices": [
                {
                    "message": {
                        "content": {
                            "recommendation": "買い",
                            "sentiment": "強気",
                            "target_price_3m": 180.0,
                            "upside_3m": "+20%",
                            "confidence": "高",
                            "analysis_summary": "Strong growth",
                            "key_catalysts": ["AI demand surge", "Services growth"],
                            "risk_factors": ["Competition"],
                            "technical_analysis": "Bullish trend",
                            "fundamental_analysis": "Solid fundamentals",
                            "latest_news_impact": "Positive earnings report",
                        }
                    }
                }
            ]
        }

        response = self.client.post(
            "/api/analyze-v2",
            json={
                "symbol": "AAPL",
                "market": "us",
                "history": [{"date": "2026-05-20", "close": 150.0}],
                "news": "some news context",
                "indices_summary": "indices are up",
            },
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data.get("version"), "v2-structured-pydantic-2026")
        self.assertEqual(data.get("analysis_summary"), "Strong growth")

    def test_api_update_portfolio_forbidden(self):
        # Set remote IP to non-loopback to test forbidden case
        response = self.client.post(
            "/api/stocks/portfolio",
            environ_base={"REMOTE_ADDR": "192.168.1.1"},
            json={"symbol": "AAPL", "market": "us", "shares": 10, "avg_price": 150.0},
        )
        self.assertEqual(response.status_code, 403)

    def test_api_update_portfolio_success(self):
        # Ensure AAPL is in user_us config mapping
        with app_state.user_stocks_lock:
            app_state.user_us["AAPL"] = "Apple Inc."

        response = self.client.post(
            "/api/stocks/portfolio",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            json={
                "symbol": "AAPL",
                "market": "us",
                "shares": 10.5,
                "avg_price": 150.25,
                "avg_fx_rate": 150.0,
            },
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data.get("success"))

    @patch("services.search_service._langsearch_post_json")
    def test_langsearch_rerank_with_empty_or_whitespace_query(self, mock_post):
        from services.search_service import langsearch_rerank

        docs = [{"title": "Doc 1"}, {"title": "Doc 2"}]
        # Query is empty
        res = langsearch_rerank("", docs, "dummy-key")
        self.assertEqual(res, docs)
        mock_post.assert_not_called()

        # Query is whitespace
        res2 = langsearch_rerank("   ", docs, "dummy-key")
        self.assertEqual(res2, docs)
        mock_post.assert_not_called()

    @patch("services.search_service._langsearch_post_json")
    def test_langsearch_rerank_sends_placeholder_for_empty_fields(self, mock_post):
        from services.search_service import langsearch_rerank

        mock_post.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.1},
            ]
        }
        docs = [
            {"title": ""},  # empty title, no summary
            {"summary": "   "},  # blank summary, no title
        ]
        langsearch_rerank("query", docs, "dummy-key")
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1]  # payload is the 2nd arg
        self.assertEqual(payload["documents"], ["[no content]", "[no content]"])

    @patch("services.search_service._langsearch_post_json")
    def test_langsearch_rerank_reordering(self, mock_post):
        from services.search_service import langsearch_rerank

        mock_post.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0.8},
            ]
        }
        docs = [{"title": "Low relevance"}, {"title": "High relevance"}]
        res = langsearch_rerank("query", docs, "dummy-key")
        # High relevance should be first because of score 0.8 > 0.2
        self.assertEqual(res[0]["title"], "High relevance")
        self.assertEqual(res[0]["relevance_score"], 0.8)
        self.assertEqual(res[1]["title"], "Low relevance")
        self.assertEqual(res[1]["relevance_score"], 0.2)


if __name__ == "__main__":
    unittest.main()
