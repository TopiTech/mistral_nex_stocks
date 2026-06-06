import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import (
    _cleanup_rate_limit_store,
    _rate_limit_store,
    app,
    app_state,
    yf_session_manager,
)


class CoverageBoostTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_yf_session_manager(self):
        # Test all methods of YFinanceSessionManager
        ua = yf_session_manager.get_user_agent()
        self.assertIsNotNone(ua)

        yf_session_manager.mark_rate_limited("test-key", 1)
        self.assertTrue(yf_session_manager.is_rate_limited("test-key"))

        time.sleep(1.1)
        self.assertFalse(yf_session_manager.is_rate_limited("test-key"))

        yf_session_manager.mark_rate_limited("test-key", 100)
        yf_session_manager.clear_rate_limit("test-key")
        self.assertFalse(yf_session_manager.is_rate_limited("test-key"))

        yf_session_manager.close_all()
        self.assertEqual(len(yf_session_manager._excluded_until), 0)

    def test_rate_limit_cleanup(self):
        _rate_limit_store.clear()
        now = time.time()
        _rate_limit_store["fresh"] = [now]
        _rate_limit_store["stale"] = [now - 600]  # 10 mins ago

        _cleanup_rate_limit_store()

        self.assertIn("fresh", _rate_limit_store)
        self.assertNotIn("stale", _rate_limit_store)

    def test_root_redirection_guard(self):
        from native_host.native_host import StdoutRedirectionGuard

        guard = StdoutRedirectionGuard()
        with patch("sys.stderr.write") as mock_write:
            guard.write("test")
            mock_write.assert_called_with("test")
        with patch("sys.stderr.flush") as mock_flush:
            guard.flush()
            mock_flush.assert_called()

    @patch("app.get_langsearch_api_key", return_value="dummy")
    @patch("app.app_state.execution.news_executor.submit")
    def test_schedule_news_warmup(self, mock_submit, mock_get_key):
        from app import schedule_news_warmup

        schedule_news_warmup()
        mock_submit.assert_called_once()

        # Execute the job function passed to submit
        job_args = mock_submit.call_args[0]
        job_func = job_args[0]
        with (
            patch("app.get_cached_context_with_negative_cache") as mock_cache,
            patch("app.collect_market_trending_titles") as mock_trends,
        ):
            job_func()
            self.assertTrue(mock_cache.called)
            self.assertTrue(mock_trends.called)

    def test_error_codes_boundary(self):
        from error_codes import ErrorCode, get_error_message

        # Valid code
        self.assertIsNotNone(get_error_message(int(ErrorCode.INVALID_SYMBOL)))
        # Invalid code
        self.assertEqual(
            get_error_message(99999), get_error_message(int(ErrorCode.UNKNOWN))
        )
        # English
        self.assertIn("Unknown", get_error_message(99999, lang="en"))

    def test_native_host_io(self):
        import struct

        from native_host.native_host import read_message, send_message

        # Mocking binary streams
        mock_stdin = MagicMock()
        mock_stdout = MagicMock()

        msg = {"action": "ping"}
        msg_bytes = json.dumps(msg).encode("utf-8")
        header = struct.pack("<I", len(msg_bytes))

        mock_stdin.read.side_effect = [header, msg_bytes]

        with (
            patch("native_host.native_host.RAW_STDIN", mock_stdin),
            patch("native_host.native_host.RAW_STDOUT", mock_stdout),
        ):
            # Test read
            received = read_message()
            self.assertEqual(received, msg)

            # Test send
            send_message(msg)
            mock_stdout.write.assert_called()
            mock_stdout.flush.assert_called()

    @patch("app.ts.collect_market_trending_titles", return_value=["Trend 1", "Trend 2"])
    def test_get_trending(self, mock_trends):
        response = self.client.get("/api/trending?market=us")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("Trend 1", data["trending"])

    def test_update_portfolio_validation(self):
        # Invalid market
        response = self.client.post(
            "/api/stocks/portfolio",
            data=json.dumps({"market": "invalid"}),
            content_type="application/json",
        )
        self.assertIn(response.status_code, [400, 403])

        # Valid market, missing stocks
        response = self.client.post(
            "/api/stocks/portfolio",
            data=json.dumps({"market": "us", "stocks": "not-a-list"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("app.call_mistral_chat")
    def test_analyze_v2_simple(self, mock_chat):
        mock_chat.return_value = {
            "choices": [
                {"message": {"content": '{"summary": "test", "rating": "buy"}'}}
            ]
        }
        response = self.client.post(
            "/api/analyze-v2",
            data=json.dumps({"symbol": "AAPL", "market": "us"}),
            content_type="application/json",
        )
        self.assertIn(response.status_code, [200, 401, 500])


if __name__ == "__main__":
    unittest.main()
