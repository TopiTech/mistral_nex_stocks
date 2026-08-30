"""
tests/test_head_review_autonomous_fixes_2026_08_30_v3.py
Regression tests for autonomous HEAD review findings (R1 - R4).
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from app_state import app_state


class TestHeadReviewAutonomousFixes20260830V3(unittest.TestCase):
    """Test cases for R1 through R4."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.app.config["WTF_CSRF_ENABLED"] = False

    def test_r1_stream_mistral_chat_semaphore_no_self_deadlock_on_fallback(self):
        """R1: Test stream_mistral_chat does not deadlock on fallback when semaphore slot is 1.

        Previously, stream_mistral_chat held app_state.ai.mistral_stream_semaphore
        while calling yield from stream_mistral_chat(...) on 400 reasoning_effort
        or Tier restriction, causing self-deadlock on non-reentrant semaphore.
        """
        import httpx

        from services.ai_service import stream_mistral_chat

        mock_client = MagicMock()
        call_count = [0]

        def mock_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: 400 error indicating reasoning_effort rejection
                mock_resp = MagicMock()
                mock_resp.status_code = 400
                mock_resp.json.return_value = {
                    "object": "error",
                    "message": "Invalid parameter: reasoning_effort is not supported on this endpoint",
                }
                mock_resp.text = '{"message": "reasoning_effort parameter rejected"}'
                raise httpx.HTTPStatusError("400 Bad Request", request=MagicMock(), response=mock_resp)

            # Second call (fallback): returns stream chunks
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = "Stream succeeded without reasoning"
            chunk.usage = None
            return [chunk]

        mock_client.chat.stream.side_effect = mock_stream

        # Restrict semaphore to exactly 1 permit to immediately expose self-deadlock
        test_semaphore = threading.Semaphore(1)

        with patch.object(app_state.ai, "mistral_stream_semaphore", test_semaphore), \
             patch("services.ai_service._get_mistral_client", return_value=mock_client), \
             patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603"):

            # Execute stream with a thread timeout to guard against infinite hang
            chunks = []
            def run_stream():
                gen = stream_mistral_chat(
                    api_key="test-key",
                    messages=[{"role": "user", "content": "hello"}],
                    reasoning_effort="high",
                )
                chunks.extend(gen)

            t = threading.Thread(target=run_stream)
            t.start()
            t.join(timeout=3.0)

            self.assertFalse(t.is_alive(), "stream_mistral_chat deadlocked waiting for its own semaphore!")
            self.assertEqual(call_count[0], 2)
            delta_texts = [c.get("text") for c in chunks if c.get("type") == "delta"]
            self.assertIn("Stream succeeded without reasoning", delta_texts)

    def test_r2_news_api_does_not_cache_error_bundle_for_24h(self):
        """R2: Test /api/news does not cache invalid or error bundles into 24-hour cache.

        Previously, any dict with retrieve_status was cached for 86400s even if
        all market texts were '解析エラー' or '解析中...'.
        """
        client = self.app.test_client()

        error_bundle = {
            "us": {"content": "解析エラー", "timestamp": "2026-08-30T00:00:00Z", "status": "failed"},
            "jp": {"content": "解析エラー", "timestamp": "2026-08-30T00:00:00Z", "status": "failed"},
            "trends": {"content": "解析エラー", "timestamp": "2026-08-30T00:00:00Z", "status": "failed"},
            "trending_raw": [],
            "retrieve_status": {"us": "failed", "jp": "failed", "trends": "failed"},
        }

        mock_set_cached = MagicMock()
        with patch("services.news_service.news_service.get_synchronized_market_news", return_value=error_bundle), \
             patch("utils.caching._set_cached_value", mock_set_cached), \
             patch("utils.caching._get_cached_value", return_value=None), \
             patch("routes.api_analysis.extract_api_key", return_value="test_key"):

            resp = client.post("/api/news", json={"market": "all", "force": True})
            self.assertEqual(resp.status_code, 200)

            # Verify that _set_cached_value was NOT called with latest_news_bundle and 86400s duration
            for call in mock_set_cached.call_args_list:
                args = call[0]
                cache_key = args[0]
                duration = call[1].get("duration") if len(args) < 3 else args[2]
                if cache_key == "latest_news_bundle":
                    self.assertNotEqual(duration, 86400, "Error bundle must not be cached for 86400 seconds!")

    def test_r3_chat_retry_checks_for_mistral_error_and_does_not_save_error_to_history(self):
        """R3: Test _call_mistral_chat_with_retry checks is_mistral_error on retry response.

        Previously, if the initial call returned empty string and retry returned an
        error payload (e.g. rate limit), extract_chat_content extracted the error message
        which was then returned as normal AI response and persisted to chat_history.
        """
        import routes.api_analysis as raa

        # 1. Initial call returns empty content
        empty_response = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
        # 2. Retry call returns Mistral error dict
        error_response = {
            "error": {
                "message": "Rate limit exceeded. Please wait 60s.",
                "type": "rate_limit_error",
                "code": "429",
            }
        }

        with patch("services.ai_service.call_mistral_chat_with_tools", return_value=empty_response), \
             patch("services.ai_service.call_mistral_chat", return_value=error_response):

            # Calling _call_mistral_chat_with_retry should raise RuntimeError on retry error,
            # rather than returning "Rate limit exceeded. Please wait 60s." as valid AI content.
            with self.assertRaises(RuntimeError) as ctx:
                raa._call_mistral_chat_with_retry(
                    api_key="test-key",
                    messages_snapshot=[{"role": "user", "content": "Hi"}],
                    market="us",
                    symbol="AAPL",
                )
            self.assertIn("Rate limit exceeded", str(ctx.exception))

    def test_r4_realtime_engine_stop_closes_yahoojp_scraper(self):
        """R4: Test RealtimeMarketEngine.stop calls yahoojp_scraper.close() to release sessions.

        Previously, stop() called yahoojp_scraper.stop(), sbi_scraper.close(), etc.
        but omitted yahoojp_scraper.close(), leaking HTTP sessions.
        """
        from services.realtime.engine import RealtimeMarketEngine

        engine = RealtimeMarketEngine()
        mock_close = MagicMock()
        engine.yahoojp_scraper.close = mock_close

        engine.stop()
        mock_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
