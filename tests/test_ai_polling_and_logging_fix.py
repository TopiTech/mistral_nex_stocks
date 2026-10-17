"""tests/test_ai_polling_and_logging_fix.py
Unit tests for AI polling progress metadata, logging filter expansion, and error handling.
"""

import logging
import unittest
from unittest.mock import patch

import httpx
from flask import Flask, g

from app_state import BackendLogFilter
from routes.api_analysis import _analyze_v2_error_response, _chat_error_response
from routes.stocks.ai_portfolio import _ai_portfolio_error_response
from services.ai_service import ProviderAPIError
from services.news_service import NewsService
from utils.validators import safe_parse_analysis_result


class TestBackendLogFilter(unittest.TestCase):
    """Verify BackendLogFilter allows AI-related INFO logs through."""

    def setUp(self):
        self.filter = BackendLogFilter()

    def test_ai_patterns_pass_filter(self):
        test_patterns = [
            "api_analyze_v2 job start id=123 symbol=AAPL market=us",
            "Analyze-v2 success id=123 symbol=AAPL recommendation=BUY sentiment=BULLISH",
            "api_chat job start id=456 symbol=MSFT",
            "OpenAI-compat call start id=789 model=test-model",
            "Mistral SDK call start id=abc model=mistral-small",
            "AI technical lines call rejected due to model restriction",
            "safe_parse_analysis_result received provider error: quota exceeded",
        ]
        for msg in test_patterns:
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg=msg,
                args=(),
                exc_info=None,
            )
            self.assertTrue(
                self.filter.filter(record),
                f"Pattern {msg} should be allowed by BackendLogFilter",
            )

    def test_warning_and_error_always_pass(self):
        for lvl in (logging.WARNING, logging.ERROR, logging.CRITICAL):
            record = logging.LogRecord(
                name="test",
                level=lvl,
                pathname="test.py",
                lineno=1,
                msg="unrelated warning or error",
                args=(),
                exc_info=None,
            )
            self.assertTrue(self.filter.filter(record))


class TestSafeParseAnalysisResultErrors(unittest.TestCase):
    """Verify safe_parse_analysis_result explicitly flags provider errors."""

    def test_provider_error_handling(self):
        error_response = {
            "error": {
                "message": "Model is overloaded. Please try again later.",
                "status_code": 503,
                "provider": "openai_compatible",
            }
        }
        res = safe_parse_analysis_result(error_response, "dummy_key")
        self.assertFalse(res.get("parsed", True))
        self.assertIn("provider_error", res)
        self.assertEqual(res["provider_error"]["status_code"], 503)
        self.assertIn("AI解析APIエラー", res.get("analysis_summary", ""))

    def test_empty_response_handling(self):
        res = safe_parse_analysis_result({}, "dummy_key")
        self.assertFalse(res.get("parsed", True))
        self.assertTrue(res.get("fallback_used", False))


class TestErrorResponses(unittest.TestCase):
    """Verify error responses for analyze-v2 and chat."""

    def test_analyze_v2_timeout_returns_504(self):
        app = Flask(__name__)
        with app.test_request_context():
            g.request_id = "test-req"
            exc = httpx.TimeoutException("Read timed out")
            resp, status = _analyze_v2_error_response(exc, g)
            self.assertEqual(status, 504)
            data = resp.get_json()
            self.assertIn("details", data)
            self.assertIn("タイムアウト", data["details"].get("reason", ""))

    def test_analyze_v2_network_returns_503(self):
        app = Flask(__name__)
        with app.test_request_context():
            g.request_id = "test-req"
            exc = httpx.NetworkError("Connection refused")
            _resp, status = _analyze_v2_error_response(exc, g)
            self.assertEqual(status, 503)

    def test_chat_timeout_returns_504(self):
        app = Flask(__name__)
        with app.test_request_context():
            g.request_id = "test-req"
            exc = httpx.TimeoutException("Read timed out")
            resp, status = _chat_error_response(exc, g, "token123")
            self.assertEqual(status, 504)
            data = resp.get_json()
            self.assertIn("タイムアウト", data.get("reply", ""))


class TestNewsAndPortfolioFixes(unittest.TestCase):
    """Verify error handling, progress metadata, and filter additions for news and portfolio."""

    def test_news_filter_patterns(self):
        flt = BackendLogFilter()
        for pattern in (
            "Starting background news job id=123 strategy=tavily",
            "News bundle LLM call starting context_hash=abc",
            "Starting background AI portfolio generation job id=456 theme=tech",
            "News job failed id=789 error_type=RuntimeError",
            "AI portfolio job failed id=999 error_type=ValueError",
        ):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg=pattern,
                args=(),
                exc_info=None,
            )
            self.assertTrue(flt.filter(record), f"{pattern} should pass filter")

    def test_news_service_raises_provider_api_error_on_failure(self):
        svc = NewsService()
        progress_calls = []

        def mock_progress(step, msg):
            progress_calls.append((step, msg))

        with patch("services.news_service.collect_market_news_context", return_value="some news"), \
             patch("services.news_service.collect_market_trending_titles", return_value=["Trend1"]), \
             patch("services.news_service.call_mistral_chat", return_value={
                 "error": {"message": "Rate limit exceeded (429)", "status_code": 429}
             }):
            with self.assertRaises(ProviderAPIError) as cm:
                svc.get_synchronized_market_news(
                    api_key="test_key",
                    langsearch_api_key="",
                    tavily_api_key="",
                    force_refresh=True,
                    progress_callback=mock_progress,
                )
            self.assertEqual(cm.exception.status_code, 429)
            self.assertIn("Rate limit exceeded", str(cm.exception))
            # Verify progress callback was triggered
            steps = [call[0] for call in progress_calls]
            self.assertIn("collecting_news", steps)
            self.assertIn("generating_summary", steps)

    def test_ai_portfolio_error_response_mapping(self):
        app = Flask(__name__)
        with app.test_request_context():
            g.request_id = "test-portfolio"

            # 1. Provider error
            prov_exc = ProviderAPIError("Quota exceeded", status_code=429)
            resp, status = _ai_portfolio_error_response(prov_exc, g)
            self.assertEqual(status, 429)
            self.assertIn("Quota exceeded", resp.get_json()["details"]["reason"])

            # 2. Timeout error
            timeout_exc = httpx.TimeoutException("Read timed out")
            _resp, status = _ai_portfolio_error_response(timeout_exc, g)
            self.assertEqual(status, 504)

            # 3. Network error
            net_exc = httpx.ConnectError("Connection refused")
            _resp, status = _ai_portfolio_error_response(net_exc, g)
            self.assertEqual(status, 503)

            # 4. Unexpected internal error
            val_exc = ValueError("Data corruption")
            _resp, status = _ai_portfolio_error_response(val_exc, g)
            self.assertEqual(status, 500)


if __name__ == "__main__":
    unittest.main()
