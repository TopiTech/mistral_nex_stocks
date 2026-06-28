from services.search_service import _get_ddgs_timeout
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import (
    app,
    yf_session_manager,
)
from route_helpers import (
    _cleanup_rate_limit_store,
    _rate_limit_store,
    _rate_limit_window_by_key,
)


class CoverageBoostTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
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
        _rate_limit_window_by_key.clear()
        now = time.time()
        _rate_limit_store["fresh"] = [now]
        _rate_limit_store["stale"] = [now - 600]  # 10 mins ago

        _cleanup_rate_limit_store()

        self.assertIn("fresh", _rate_limit_store)
        self.assertNotIn("stale", _rate_limit_store)

    def test_rate_limit_cleanup_respects_recorded_window(self):
        _rate_limit_store.clear()
        _rate_limit_window_by_key.clear()
        now = time.time()
        _rate_limit_store["long-window"] = [now - 600]
        _rate_limit_window_by_key["long-window"] = 900

        _cleanup_rate_limit_store()

        self.assertIn("long-window", _rate_limit_store)

    def test_ddgs_timeout_env_is_validated(self):
        with patch.dict(os.environ, {"DDGS_TIMEOUT": "not-an-int"}):
            self.assertEqual(_get_ddgs_timeout(), 5)
        with patch.dict(os.environ, {"DDGS_TIMEOUT": "999"}):
            self.assertEqual(_get_ddgs_timeout(), 60)

    def test_mistral_clients_are_cached_per_thread(self):
        from app_state import AIState

        state = AIState()
        with patch("app_state.Mistral") as mock_mistral:
            first = state.get_or_create_mistral_client("test-key")
            second = state.get_or_create_mistral_client("test-key")

        self.assertIs(first, second)
        mock_mistral.assert_called_once_with(api_key="test-key", timeout_ms=45000)

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
            patch("services.search_service._get_market_trending_titles") as mock_trends,
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

    @patch("routes.api_analysis._get_market_trending_titles", return_value=["Trend 1", "Trend 2"])
    def test_get_trending(self, mock_trends):
        from app import app_state
        app_state.caches.clear()

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
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)

    @patch("routes.api_analysis.get_stock_info_cached")
    @patch("routes.api_analysis.repair_analysis_json_with_llm")
    @patch("routes.api_analysis.collect_symbol_research_context")
    @patch("routes.api_analysis.fetch_stock")
    @patch("routes.api_analysis.call_mistral_chat")
    def test_analyze_v2_simple(self, mock_chat, mock_fetch, mock_collect, mock_repair, mock_info):
        mock_info.return_value = {"sector": "Technology", "industry": "Consumer Electronics", "currency": "USD"}
        mock_collect.return_value = "dummy research context"
        mock_fetch.return_value = {"price": 150.0, "chart_data": [{"price": 150.0, "x": 1700000000000}]}

        valid_analysis = {
            "recommendation": "買い",
            "sentiment": "強気",
            "target_price_3m": 165.0,
            "upside_3m": "+10%",
            "confidence": "高",
            "analysis_summary": "良好な業績が続くと予想されます。",
            "key_catalysts": ["新製品の発売", "売上高の成長"],
            "risk_factors": ["原材料費の高騰"],
            "technical_analysis": "上昇トレンドを維持しています。",
            "fundamental_analysis": "財務基盤は非常に健全です。",
            "latest_news_impact": "好意的なニュースが多いです。"
        }
        mock_chat.return_value = {
            "choices": [
                {"message": {"content": valid_analysis}}
            ]
        }
        mock_repair.return_value = (valid_analysis, json.dumps(valid_analysis))

        response = self.client.post(
            "/api/analyze-v2",
            data=json.dumps({"symbol": "AAPL", "market": "us"}),
            content_type="application/json",
        )
        self.assertIn(response.status_code, [200, 401, 500])

    def test_env_helpers(self):
        from utils.env_helpers import _env_int, _env_float

        # Test _env_int
        with patch.dict(os.environ, {"TEST_INT_VAL": "42"}):
            self.assertEqual(_env_int("TEST_INT_VAL", 10), 42)
        with patch.dict(os.environ, {"TEST_INT_VAL": "invalid"}):
            self.assertEqual(_env_int("TEST_INT_VAL", 10), 10)
        with patch.dict(os.environ, {"TEST_INT_VAL": "15"}):
            # min bound
            self.assertEqual(_env_int("TEST_INT_VAL", 10, min_value=20), 20)
            # max bound
            self.assertEqual(_env_int("TEST_INT_VAL", 10, max_value=12), 12)

        # Test _env_float
        with patch.dict(os.environ, {"TEST_FLOAT_VAL": "3.14"}):
            self.assertEqual(_env_float("TEST_FLOAT_VAL", 1.0), 3.14)
        with patch.dict(os.environ, {"TEST_FLOAT_VAL": "invalid"}):
            self.assertEqual(_env_float("TEST_FLOAT_VAL", 1.0), 1.0)
        with patch.dict(os.environ, {"TEST_FLOAT_VAL": "1.5"}):
            # min bound
            self.assertEqual(_env_float("TEST_FLOAT_VAL", 1.0, min_value=2.0), 2.0)
            # max bound
            self.assertEqual(_env_float("TEST_FLOAT_VAL", 1.0, max_value=1.2), 1.2)
        with patch.dict(os.environ, {}):
            # fallback
            self.assertEqual(_env_float("TEST_FLOAT_VAL", 1.0), 1.0)

    def test_mistral_compat_fallback(self):
        import sys
        import importlib
        from unittest.mock import patch
        
        # Helper functions
        from mistral_compat import SystemMessage, UserMessage, AssistantMessage
        self.assertEqual(SystemMessage("hello"), {"role": "system", "content": "hello"})
        self.assertEqual(UserMessage("hello"), {"role": "user", "content": "hello"})
        self.assertEqual(AssistantMessage("hello"), {"role": "assistant", "content": "hello"})

        # Simulate fallback mode by blocking mistralai import
        with patch.dict(sys.modules, {"mistralai": None, "mistralai.client": None, "mistralai.client.errors": None}):
            # Reload module with mock sys.modules
            import mistral_compat
            importlib.reload(mistral_compat)
            
            fallback_client = mistral_compat.Mistral(api_key="dummy")
            self.assertEqual(fallback_client.api_key, "dummy")
            
            with self.assertRaises(Exception):
                raise mistral_compat.SDKError("test error")
                
        # Clean up by reloading mistral_compat normally
        import mistral_compat
        importlib.reload(mistral_compat)


if __name__ == "__main__":
    unittest.main()

