"""
Tests for utils/validators.py — input validation, JSON extraction, content extraction.
"""

import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.validators import (
    extract_chat_content,
    extract_json_payload,
    normalize_analysis_result,
    safe_parse_analysis_result,
    validate_analysis_result,
)


class NormalizeAnalysisResultTestCase(unittest.TestCase):
    """normalize_analysis_result のテスト"""

    def test_empty_dict_gets_defaults(self):
        result = normalize_analysis_result({})
        self.assertEqual(result["recommendation"], "中立")
        self.assertEqual(result["sentiment"], "中立")
        self.assertEqual(result["target_price_3m"], 0)
        self.assertIsInstance(result["key_catalysts"], list)
        self.assertIsInstance(result["risk_factors"], list)

    def test_partial_result_preserves_values(self):
        result = normalize_analysis_result({"recommendation": "買い", "sentiment": "強気"})
        self.assertEqual(result["recommendation"], "買い")
        self.assertEqual(result["sentiment"], "強気")
        # Missing keys get defaults
        self.assertEqual(result["target_price_3m"], 0)

    def test_converts_non_list_catalysts_to_list(self):
        result = normalize_analysis_result({"key_catalysts": "single catalyst"})
        self.assertIsInstance(result["key_catalysts"], list)

    def test_converts_non_list_risks_to_list(self):
        result = normalize_analysis_result({"risk_factors": "single risk"})
        self.assertIsInstance(result["risk_factors"], list)

    def test_none_values_are_overwritten(self):
        result = normalize_analysis_result({"recommendation": None, "sentiment": None})
        self.assertEqual(result["recommendation"], "中立")
        self.assertEqual(result["sentiment"], "中立")


class ValidateAnalysisResultTestCase(unittest.TestCase):
    """validate_analysis_result のテスト"""

    def test_valid_result(self):
        valid, reason = validate_analysis_result({"analysis_summary": "Test"})
        self.assertTrue(valid)
        self.assertEqual(reason, "")

    def test_valid_with_recommendation(self):
        valid, _reason = validate_analysis_result({"recommendation": "買い"})
        self.assertTrue(valid)

    def test_none_returns_false(self):
        valid, reason = validate_analysis_result(None)
        self.assertFalse(valid)
        self.assertIn("not an object", reason)

    def test_empty_dict_returns_false(self):
        valid, reason = validate_analysis_result({})
        self.assertFalse(valid)
        self.assertIn("missing core", reason)

    def test_non_numeric_target_price(self):
        valid, reason = validate_analysis_result(
            {
                "analysis_summary": "test",
                "target_price_3m": "not-a-number",
            }
        )
        self.assertFalse(valid)
        self.assertIn("numeric", reason)

    def test_non_list_catalysts(self):
        valid, reason = validate_analysis_result(
            {
                "analysis_summary": "test",
                "key_catalysts": "not-a-list",
            }
        )
        self.assertFalse(valid)
        self.assertIn("array", reason)

    def test_non_finite_target_price_is_rejected_and_falls_back(self):
        invalid_result = {"analysis_summary": "test", "target_price_3m": math.nan}
        valid, reason = validate_analysis_result(invalid_result)
        self.assertFalse(valid)
        self.assertIn("finite", reason)

        parsed = safe_parse_analysis_result(
            {"choices": [{"message": {"content": invalid_result}}]},
            api_key="test",
            repair_func=lambda *_args: (invalid_result, False),
        )
        self.assertTrue(parsed["fallback_used"])
        self.assertTrue(math.isfinite(parsed["target_price_3m"]))


class ExtractChatContentTestCase(unittest.TestCase):
    """extract_chat_content のテスト"""

    def test_simple_string_content(self):
        response = {"choices": [{"message": {"content": "Hello world"}}]}
        result = extract_chat_content(response)
        self.assertEqual(result, "Hello world")

    def test_empty_response(self):
        result = extract_chat_content(None)
        self.assertIn("空", result)

    def test_error_object_response(self):
        response = {"object": "error", "message": "Rate limit exceeded"}
        result = extract_chat_content(response)
        self.assertEqual(result, "(AIサービスから有効な応答を取得できませんでした)")

    def test_error_dict_response(self):
        response = {"error": {"message": "Invalid API key"}}
        result = extract_chat_content(response)
        self.assertEqual(result, "(AIサービスから有効な応答を取得できませんでした)")

    def test_list_content_chunks(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "Hello "},
                            {"type": "text", "text": "world!"},
                        ]
                    }
                }
            ]
        }
        result = extract_chat_content(response)
        self.assertEqual(result, "Hello world!")

    def test_thinking_chunks_are_skipped(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "I think..."},
                            {"type": "text", "text": "Final answer"},
                        ]
                    }
                }
            ]
        }
        result = extract_chat_content(response)
        self.assertEqual(result, "Final answer")

    def test_no_choices(self):
        response = {"foo": "bar"}
        result = extract_chat_content(response)
        self.assertEqual(result, "(AIサービスから有効な応答を取得できませんでした)")


class ExtractJsonPayloadTestCase(unittest.TestCase):
    """extract_json_payload のテスト"""

    def test_simple_json_object(self):
        result = extract_json_payload('{"key": "value"}')
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_markdown_fence(self):
        result = extract_json_payload('```json\n{"key": "value"}\n```')
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_markdown_fence_without_json_label(self):
        result = extract_json_payload('```\n{"key": "value"}\n```')
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_dict_input_passed_through(self):
        result = extract_json_payload({"key": "value"})
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_trailing_comma_removed(self):
        result = extract_json_payload('{"key": "value",}')
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_truncated_closing_braces_salvaged(self):
        result = extract_json_payload('{"key": {"nested": "value"')
        parsed = json.loads(result)
        self.assertEqual(parsed["key"]["nested"], "value")

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            extract_json_payload("")

    def test_no_json_block_raises(self):
        with self.assertRaises(ValueError):
            extract_json_payload("just some text without json")

    def test_field_extraction_fallback(self):
        # depth tracking が失敗するが、required_fields の正規表現マッチで修復
        text = 'Some text with "recommendation": "Buy" and "sentiment": "Bullish" but no valid JSON'
        result = extract_json_payload(text, required_fields=["recommendation", "sentiment"])
        parsed = json.loads(result)
        self.assertIn("recommendation", parsed)
        self.assertEqual(parsed["recommendation"], "Buy")

class TemplateConfigSchemaTestCase(unittest.TestCase):
    """Test TemplateConfigSchema template configuration validation."""

    def test_app_config_schema_openai_fields_preservation(self):
        from schemas.config import TemplateConfigSchema

        data = {
            "has_mistral_api_key": False,
            "has_openai_api_key": True,
            "has_langsearch_api_key": False,
            "has_tavily_api_key": False,
            "has_alphavantage_api_key": False,
            "has_ai_access": True,
            "ai_provider": "openai_compatible",
            "is_beta_provider": True,
            "provider_notice": "Beta notice",
            "openai_base_url": "https://api.openai.com/v1",
            "openai_model": "gpt-4o",
            "openai_embeddings_model": "text-embedding-3-large",
            "is_local_openai_base_url": False,
            "api_mode": "chat",
            "agent_id": "",
            "preview_mode": False,
            "model_selectable": True,
            "mistral_model": "mistral-medium-3.5",
            "is_ai_technical_lines_eligible": True,
        }
        schema = TemplateConfigSchema.model_validate(data)
        dumped = schema.model_dump()
        self.assertEqual(dumped["ai_provider"], "openai_compatible")
        self.assertTrue(dumped["has_openai_api_key"])
        self.assertEqual(dumped["openai_model"], "gpt-4o")
        self.assertEqual(dumped["openai_embeddings_model"], "text-embedding-3-large")
        self.assertTrue(dumped["is_beta_provider"])


if __name__ == "__main__":
    unittest.main()

