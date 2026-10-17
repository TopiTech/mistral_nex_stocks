"""tests/test_ai_polling_and_logging_fix.py
Unit tests for AI polling progress metadata, logging filter expansion, and error handling.
"""

import logging
import unittest

import httpx
from flask import Flask, g

from app_state import BackendLogFilter
from routes.api_analysis import _analyze_v2_error_response, _chat_error_response
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


if __name__ == "__main__":
    unittest.main()
