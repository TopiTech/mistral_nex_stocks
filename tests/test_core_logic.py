"""
Core Logic Unit Tests
"""

import unittest
import os
import json
import time
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import (
    app,
    app_state,
    sync_all_stocks_now,
    bg_interpolate_loop,
    call_mistral_chat,
)


class CoreLogicTestCase(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    @patch('app.fetch_index_data')
    @patch('app.fetch_stocks_batch')
    def test_sync_all_stocks_now_success(self, mock_fetch_batch, mock_fetch_index):
        # Setup mocked data returned by batch fetcher
        mock_fetch_index.return_value = ("DJI", {"price": "30000.0", "change": "100.0", "percent": "0.33"})
        mock_fetch_batch.return_value = [
            {"symbol": "AAPL", "market": "us", "price": 150.0, "change": 1.5, "change_percent": 1.0, "market_state": "REGULAR"},
            {"symbol": "9984.T", "market": "jp", "price": 6000.0, "change": -50.0, "change_percent": -0.83, "market_state": "REGULAR"},
            {"symbol": "^N225", "market": "idx", "price": 38000.0, "change": 100.0, "change_percent": 0.26, "market_state": "REGULAR"}
        ]
        
        # Reset caching variables in app_state
        app_state.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.is_syncing = False
        
        result = sync_all_stocks_now()
        # Assertions
        self.assertFalse(app_state.is_syncing)
        self.assertTrue(len(app_state.target_stocks_cache["us"]) > 0)
        self.assertTrue(len(app_state.target_stocks_cache["jp"]) > 0)
        self.assertTrue(len(app_state.target_stocks_cache["idx"]) > 0)

    @patch('time.sleep', side_effect=KeyboardInterrupt("stop loop"))
    def test_bg_interpolate_loop_exits(self, mock_sleep):
        # Mock sse_announcer to return some listener count
        mock_announcer = MagicMock()
        mock_announcer.listener_count.return_value = 1
        
        # Temporarily mock app_state.sse_announcer
        old_announcer = app_state.sse_announcer
        app_state.sse_announcer = mock_announcer
        try:
            # Running this should raise KeyboardInterrupt instantly because time.sleep is called inside.
            with self.assertRaises(KeyboardInterrupt):
                bg_interpolate_loop()
        finally:
            app_state.sse_announcer = old_announcer

    @patch('app.requests.post')
    def test_call_mistral_chat_live(self, mock_post):
        # Setup mock response
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hello response"}}],
            "model": "mistral-large-latest"
        }
        mock_post.return_value = mock_response

        # Ensure no rate limiting or cache hits interference
        app_state.mistral_response_cache.clear()
        
        with app.app_context():
            res = call_mistral_chat(
                api_key="test-api-key",
                messages=[{"role": "user", "content": "hello"}],
                use_cache=False
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["choices"][0]["message"]["content"], "Hello response")
            mock_post.assert_called_once()

    def test_call_mistral_chat_cached(self):
        # Insert a value into the cache directly
        cache_key = ("mistral-large-latest", "some-unique-messages-hash")
        app_state.mistral_response_cache[cache_key] = {
            "choices": [{"message": {"content": "Cached response"}}],
            "model": "mistral-large-latest"
        }
        
        with patch('app._build_mistral_cache_key', return_value=cache_key):
            with app.app_context():
                res = call_mistral_chat(
                    api_key="test-api-key",
                    messages=[{"role": "user", "content": "hello"}],
                    use_cache=True
                )
                self.assertEqual(res["choices"][0]["message"]["content"], "Cached response")

    @patch('app.call_mistral_chat')
    @patch('app.collect_market_trending_titles')
    @patch('app.collect_market_news_context')
    def test_api_news_bundle(self, mock_collect_context, mock_collect_trends, mock_call_mistral):
        # Mocking news context gather and mistral responses
        mock_collect_context.return_value = "Mocked news context content"
        mock_collect_trends.return_value = ["US market rises", "Nikkei 225 slides"]
        
        mock_call_mistral.return_value = {
            "choices": [{
                "message": {
                    "content": '{"us": "US summary line 1", "jp": "JP summary line 1", "trends": "Trends summary line 1"}'
                }
            }]
        }
        
        # Test endpoint
        response = self.client.post(
            '/api/news',
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key"
            }
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("us", data)
        self.assertIn("jp", data)
        self.assertIn("trends", data)

    @patch('app.call_mistral_chat')
    def test_api_analyze_v2(self, mock_call_mistral):
        # Mock LLM analysis response with expected function call format
        mock_call_mistral.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "generate_analysis_json",
                            "arguments": '{"analysis_summary": "Strong growth", "sentiment": "Bullish", "score": 8, "reasoning": "Strong growth", "risks": ["Competition"]}'
                        }
                    }]
                }
            }]
        }
        
        response = self.client.post(
            '/api/analyze-v2',
            json={
                "symbol": "AAPL",
                "market": "us",
                "history": [{"date": "2026-05-20", "close": 150.0}],
                "news": "some news context",
                "indices_summary": "indices are up"
            },
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key"
            }
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data.get("version"), "v2-function-calling")
        self.assertEqual(data.get("analysis_summary"), "Strong growth")

    def test_api_update_portfolio_forbidden(self):
        # Set remote IP to non-loopback to test forbidden case
        response = self.client.post(
            '/api/stocks/portfolio',
            environ_base={'REMOTE_ADDR': '192.168.1.1'},
            json={
                "symbol": "AAPL",
                "market": "us",
                "shares": 10,
                "avg_price": 150.0
            }
        )
        self.assertEqual(response.status_code, 403)

    def test_api_update_portfolio_success(self):
        # Ensure AAPL is in user_us config mapping
        with app_state.user_stocks_lock:
            app_state.user_us["AAPL"] = "Apple Inc."
            
        response = self.client.post(
            '/api/stocks/portfolio',
            environ_base={'REMOTE_ADDR': '127.0.0.1'},
            json={
                "symbol": "AAPL",
                "market": "us",
                "shares": 10.5,
                "avg_price": 150.25,
                "avg_fx_rate": 150.0
            },
            headers={"Origin": "http://localhost:5000"}
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data.get("success"))

    @patch('app._langsearch_post_json')
    def test_langsearch_rerank_with_empty_or_whitespace_query(self, mock_post):
        from app import langsearch_rerank
        docs = [{"title": "Doc 1"}, {"title": "Doc 2"}]
        # Query is empty
        res = langsearch_rerank("", docs, "dummy-key")
        self.assertEqual(res, docs)
        mock_post.assert_not_called()

        # Query is whitespace
        res2 = langsearch_rerank("   ", docs, "dummy-key")
        self.assertEqual(res2, docs)
        mock_post.assert_not_called()

    @patch('app._langsearch_post_json')
    def test_langsearch_rerank_sends_placeholder_for_empty_fields(self, mock_post):
        from app import langsearch_rerank
        mock_post.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.1}
            ]
        }
        docs = [
            {"title": ""}, # empty title, no summary
            {"summary": "   "} # blank summary, no title
        ]
        res = langsearch_rerank("query", docs, "dummy-key")
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1] # payload is the 2nd arg
        self.assertEqual(payload["documents"], ["[no content]", "[no content]"])

    @patch('app._langsearch_post_json')
    def test_langsearch_rerank_reordering(self, mock_post):
        from app import langsearch_rerank
        mock_post.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0.8}
            ]
        }
        docs = [
            {"title": "Low relevance"},
            {"title": "High relevance"}
        ]
        res = langsearch_rerank("query", docs, "dummy-key")
        # High relevance should be first because of score 0.8 > 0.2
        self.assertEqual(res[0]["title"], "High relevance")
        self.assertEqual(res[0]["relevance_score"], 0.8)
        self.assertEqual(res[1]["title"], "Low relevance")
        self.assertEqual(res[1]["relevance_score"], 0.2)


if __name__ == '__main__':
    unittest.main()
