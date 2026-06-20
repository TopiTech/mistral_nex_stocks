"""
Tests for utils/validators.py — input validation, JSON extraction, content extraction.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.validators import (
    validate_portfolio_input,
    extract_chat_content,
    extract_json_payload,
    normalize_analysis_result,
    validate_analysis_result,
)


class ValidatePortfolioInputTestCase(unittest.TestCase):
    """validate_portfolio_input のテスト"""

    def test_valid_input(self):
        errors = validate_portfolio_input(10.5, 150.25)
        self.assertEqual(errors, [])

    def test_valid_input_with_fx_rate(self):
        errors = validate_portfolio_input(10.5, 150.25, avg_fx_rate=145.0)
        self.assertEqual(errors, [])

    def test_negative_shares(self):
        errors = validate_portfolio_input(-1, 150.25)
        self.assertTrue(len(errors) > 0)

    def test_negative_avg_price(self):
        errors = validate_portfolio_input(10, -100)
        self.assertTrue(len(errors) > 0)

    def test_zero_valid(self):
        errors = validate_portfolio_input(0, 0)
        self.assertEqual(errors, [])

    def test_shares_exceeds_max(self):
        from constants import PORTFOLIO_SHARES_MAX

        errors = validate_portfolio_input(PORTFOLIO_SHARES_MAX + 1, 100)
        self.assertTrue(len(errors) > 0)

    def test_avg_price_exceeds_max(self):
        from constants import PORTFOLIO_AVG_PRICE_MAX

        errors = validate_portfolio_input(1, PORTFOLIO_AVG_PRICE_MAX + 1)
        self.assertTrue(len(errors) > 0)

    def test_total_value_exceeds_max(self):
        from constants import PORTFOLIO_TOTAL_VALUE_MAX

        # shares * avg_price > PORTFOLIO_TOTAL_VALUE_MAX (確実に超過させる)
        huge_shares = PORTFOLIO_TOTAL_VALUE_MAX // 1000 + 1
        errors = validate_portfolio_input(huge_shares, 1000)
        self.assertTrue(len(errors) > 0)

    def test_negative_fx_rate(self):
        errors = validate_portfolio_input(1, 100, avg_fx_rate=-5)
        self.assertTrue(len(errors) > 0)


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
        result = normalize_analysis_result(
            {"recommendation": "買い", "sentiment": "強気"}
        )
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
        valid, reason = validate_analysis_result({"recommendation": "買い"})
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
        self.assertIn("Rate limit", result)

    def test_error_dict_response(self):
        response = {"error": {"message": "Invalid API key"}}
        result = extract_chat_content(response)
        self.assertIn("Invalid API key", result)

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
        self.assertIn("Unexpected", result)


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
        result = extract_json_payload(
            text, required_fields=["recommendation", "sentiment"]
        )
        parsed = json.loads(result)
        self.assertIn("recommendation", parsed)
        self.assertEqual(parsed["recommendation"], "Buy")


if __name__ == "__main__":
    unittest.main()
