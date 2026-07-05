"""Unit tests for services/ai_service.py utility functions.

Tests the utility/comparison functions that don't require live API calls.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.ai_service import (
    _get_mistral_model_name,
    _build_mistral_cache_key,
    _is_mistral_capacity_error,
    _extract_mistral_wait_seconds,
)


class GetMistralModelNameTestCase(unittest.TestCase):
    """_get_mistral_model_name tests"""

    @patch("services.ai_service.get_model_name")
    def test_empty_returns_default(self, mock_get):
        mock_get.return_value = ""
        result = _get_mistral_model_name()
        self.assertEqual(result, "mistral-small-2603")

    @patch("services.ai_service.get_model_name")
    def test_none_returns_default(self, mock_get):
        mock_get.return_value = None
        result = _get_mistral_model_name()
        self.assertEqual(result, "mistral-small-2603")

    @patch("services.ai_service.get_model_name")
    def test_known_model_passed_through(self, mock_get):
        mock_get.return_value = "mistral-large-2512"
        result = _get_mistral_model_name()
        self.assertEqual(result, "mistral-large-2512")

    @patch("services.ai_service.get_model_name")
    @patch("config_utils.MISTRAL_LEGACY_ALIASES", {"mistral-medium": "mistral-medium-2604"})
    def test_legacy_alias_resolved(self, mock_get):
        mock_get.return_value = "mistral-medium"
        result = _get_mistral_model_name()
        self.assertEqual(result, "mistral-medium-2604")

    @patch("services.ai_service.get_model_name")
    @patch("config_utils.MISTRAL_SUPPORTED_MODELS", {"mistral-small-2603", "mistral-large-2512"})
    def test_unknown_model_falls_back(self, mock_get):
        mock_get.return_value = "mistral-unknown-v9"
        result = _get_mistral_model_name()
        self.assertEqual(result, "mistral-small-2603")


class BuildMistralCacheKeyTestCase(unittest.TestCase):
    """_build_mistral_cache_key tests"""

    def test_produces_deterministic_key(self):
        key1 = _build_mistral_cache_key(
            "mistral-small-4",
            [{"role": "user", "content": "hello"}],
            600,
            None,
        )
        key2 = _build_mistral_cache_key(
            "mistral-small-4",
            [{"role": "user", "content": "hello"}],
            600,
            None,
        )
        self.assertEqual(key1, key2)
        self.assertTrue(key1.startswith("mistral_chat_"))

    def test_different_inputs_produce_different_keys(self):
        key1 = _build_mistral_cache_key(
            "mistral-small-4", [{"role": "user", "content": "hello"}], 600, None
        )
        key2 = _build_mistral_cache_key(
            "mistral-large-3", [{"role": "user", "content": "hello"}], 600, None
        )
        self.assertNotEqual(key1, key2)

    def test_different_max_tokens_produce_different_keys(self):
        key1 = _build_mistral_cache_key(
            "mistral-small-4", [{"role": "user", "content": "hello"}], 600, None
        )
        key2 = _build_mistral_cache_key(
            "mistral-small-4", [{"role": "user", "content": "hello"}], 1200, None
        )
        self.assertNotEqual(key1, key2)

    def test_handles_cache_key_override(self):
        key_with = _build_mistral_cache_key(
            "mistral-small-4",
            [{"role": "user", "content": "hello"}],
            600,
            None,
            cache_key_override="override_abc",
        )
        key_without = _build_mistral_cache_key(
            "mistral-small-4",
            [{"role": "user", "content": "hello"}],
            600,
            None,
        )
        # Different override values should produce different keys
        self.assertNotEqual(key_with, key_without)
        self.assertTrue(key_with.startswith("mistral_chat_"))

    def test_handles_response_format(self):
        key = _build_mistral_cache_key(
            "mistral-small-4",
            [{"role": "user", "content": "hello"}],
            600,
            {"type": "json_object"},
        )
        self.assertTrue(key.startswith("mistral_chat_"))

    def test_handles_tools_and_tool_choice(self):
        key = _build_mistral_cache_key(
            "mistral-small-4",
            [{"role": "user", "content": "hello"}],
            600,
            None,
            tools=[{"type": "function", "function": {"name": "test"}}],
            tool_choice="auto",
        )
        self.assertTrue(key.startswith("mistral_chat_"))

    def test_handles_reasoning_effort(self):
        key = _build_mistral_cache_key(
            "mistral-small-4",
            [{"role": "user", "content": "hello"}],
            600,
            None,
            reasoning_effort="medium",
        )
        self.assertTrue(key.startswith("mistral_chat_"))

    def test_handles_messages_with_model_dump(self):
        class FakeMessage:
            def model_dump(self):
                return {"role": "user", "content": "hello"}

        key = _build_mistral_cache_key(
            "mistral-small-4",
            [FakeMessage()],
            600,
            None,
        )
        self.assertTrue(key.startswith("mistral_chat_"))




class IsMistralCapacityErrorTestCase(unittest.TestCase):
    """_is_mistral_capacity_error tests"""

    def test_capacity_exceeded_type(self):
        self.assertTrue(
            _is_mistral_capacity_error(
                {"error": {"type": "service_tier_capacity_exceeded"}}
            )
        )

    def test_code_3505(self):
        self.assertTrue(
            _is_mistral_capacity_error({"error": {"code": "3505"}})
        )

    def test_status_429(self):
        self.assertTrue(
            _is_mistral_capacity_error({"error": {"status_code": 429}})
        )

    def test_none_input(self):
        self.assertFalse(_is_mistral_capacity_error(None))

    def test_empty_payload(self):
        self.assertFalse(_is_mistral_capacity_error({}))

    def test_missing_error(self):
        self.assertFalse(_is_mistral_capacity_error({"ok": True}))

    def test_non_dict_error(self):
        self.assertFalse(_is_mistral_capacity_error({"error": "string"}))

    def test_no_match_returns_false(self):
        self.assertFalse(
            _is_mistral_capacity_error(
                {"error": {"type": "invalid_request_error", "code": "400", "status_code": 400}}
            )
        )


class ExtractMistralWaitSecondsTestCase(unittest.TestCase):
    """_extract_mistral_wait_seconds tests"""

    def test_retry_after_seconds(self):
        response = MagicMock()
        response.headers = {"Retry-After": "30"}
        result = _extract_mistral_wait_seconds(response)
        self.assertAlmostEqual(result, 30.0)

    def test_retry_after_http_date(self):
        response = MagicMock()
        # Set Retry-After to a future date (1 hour from now)
        future_ts = time.time() + 3600
        from email.utils import formatdate
        response.headers = {"Retry-After": formatdate(future_ts, usegmt=True)}
        result = _extract_mistral_wait_seconds(response)
        self.assertGreater(result, 3500)
        self.assertLess(result, 3700)

    def test_retry_after_ms(self):
        response = MagicMock()
        response.headers = {"Retry-After": "500ms"}
        result = _extract_mistral_wait_seconds(response)
        self.assertAlmostEqual(result, 0.5, places=1)

    def test_x_ratelimit_reset_epoch(self):
        response = MagicMock()
        future_ts = time.time() + 120
        response.headers = {"X-RateLimit-Reset": str(future_ts)}
        result = _extract_mistral_wait_seconds(response)
        self.assertGreater(result, 115)
        self.assertLess(result, 125)

    def test_invalid_retry_after_returns_zero(self):
        response = MagicMock()
        response.headers = {"Retry-After": "not-a-number"}
        result = _extract_mistral_wait_seconds(response)
        self.assertEqual(result, 0.0)

    def test_no_headers_returns_zero(self):
        response = MagicMock()
        response.headers = {}
        result = _extract_mistral_wait_seconds(response)
        self.assertEqual(result, 0.0)

    def test_no_response_returns_zero(self):
        result = _extract_mistral_wait_seconds(None)
        self.assertEqual(result, 0.0)

    def test_case_insensitive_retry_after(self):
        response = MagicMock()
        response.headers = {"retry-after": "15"}
        result = _extract_mistral_wait_seconds(response)
        self.assertAlmostEqual(result, 15.0)

    def test_x_ratelimit_reset_requests(self):
        response = MagicMock()
        future_ts = time.time() + 45
        response.headers = {"x-ratelimit-reset-requests": str(future_ts)}
        result = _extract_mistral_wait_seconds(response)
        self.assertGreater(result, 40)
        self.assertLess(result, 50)

    def test_retry_after_invalid_ms(self):
        response = MagicMock()
        response.headers = {"Retry-After": "abcms"}
        result = _extract_mistral_wait_seconds(response)
        self.assertEqual(result, 0.0)


if __name__ == "__main__":
    unittest.main()
