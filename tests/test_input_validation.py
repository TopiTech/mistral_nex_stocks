import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app_helpers import (
    normalize_market,
    normalize_symbol_for_market,
    parse_non_negative_float,
)
from app_bg import interpolate_value
import config_utils as cu


class InputValidationTests(unittest.TestCase):
    def test_normalize_market_accepts_known_values(self):
        self.assertEqual(normalize_market("US"), "us")
        self.assertEqual(normalize_market("jp"), "jp")
        self.assertEqual(normalize_market("idx"), "idx")

    def test_normalize_market_rejects_unknown_values(self):
        self.assertIsNone(normalize_market("crypto"))

    def test_normalize_symbol_for_market_jp_adds_suffix_for_digits(self):
        self.assertEqual(normalize_symbol_for_market("7203", "jp"), "7203.T")

    def test_normalize_symbol_for_market_keeps_non_digit_symbol(self):
        self.assertEqual(normalize_symbol_for_market("AAPL", "us"), "AAPL")
        self.assertEqual(normalize_symbol_for_market("7203.T", "jp"), "7203.T")

    def test_parse_non_negative_float_accepts_valid_number(self):
        self.assertEqual(parse_non_negative_float("10.5", "shares"), 10.5)

    def test_parse_non_negative_float_rejects_negative(self):
        with self.assertRaises(ValueError):
            parse_non_negative_float("-1", "shares")

    def test_parse_non_negative_float_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            parse_non_negative_float("abc", "shares")

    def test_parse_non_negative_float_rejects_over_max(self):
        with self.assertRaises(ValueError):
            parse_non_negative_float("101", "shares", max_value=100)

    def test_interpolate_value_handles_numeric_strings(self):
        value = interpolate_value("100.0", "104.0")

        self.assertIsInstance(value, float)
        self.assertGreater(value, 100.0)
        self.assertLess(value, 104.0)

    def test_interpolate_value_preserves_non_numeric_placeholder(self):
        self.assertEqual(interpolate_value("--", "--"), "--")

    def test_save_api_credentials_preserves_protected_langsearch_when_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            with patch.object(cu, "CONFIG_FILE", cfg_path), patch.object(
                cu,
                "_encode_secret",
                side_effect=lambda value, key_name="default": {
                    "scheme": "test",
                    "value": value,
                },
            ):
                cu.save_config(
                    {
                        "mistral_model": "mistral-small-latest",
                        "model_badge": "mistral-small",
                        "api_credentials": {
                            "mistral_api_key": "keep-mistral",
                            "langsearch_api_key": {
                                "scheme": "test",
                                "value": "keep-langsearch",
                            },
                        },
                    },
                    create_backup=False,
                )

                cu.save_api_credentials(
                    mistral_api_key="new-mistral",
                    langsearch_api_key="",
                )

                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertIn("langsearch_api_key", saved["api_credentials"])
                self.assertEqual(
                    saved["api_credentials"]["langsearch_api_key"],
                    {"scheme": "test", "value": "keep-langsearch"},
                )
                self.assertEqual(
                    saved["api_credentials"]["mistral_api_key"],
                    {"scheme": "test", "value": "new-mistral"},
                )


if __name__ == "__main__":
    unittest.main()
