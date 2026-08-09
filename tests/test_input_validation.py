import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_store
import config_utils as cu
import crypto_utils
from utils.normalization import (
    normalize_market,
    normalize_symbol_for_market,
)
from utils.text_utils import parse_non_negative_float


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

    def test_stock_history_rejects_invalid_symbol_before_backend_work(self):
        from app import app

        with (
            patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.api_stocks.app_state.market.is_circuit_open") as circuit_open,
            patch("routes.api_stocks._submit_async_history_fetch") as submit_fetch,
        ):
            response = app.test_client().get(
                "/api/stock-history?symbol=../../etc/passwd&market=us&period=3mo"
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error_code"], 1001)
            circuit_open.assert_not_called()
            submit_fetch.assert_not_called()

    def test_save_api_credentials_preserves_protected_langsearch_when_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            with (
                patch.object(config_store, "CONFIG_FILE", cfg_path),
                patch.object(
                    crypto_utils,
                    "_encode_secret",
                    side_effect=lambda value, key_name="default": {
                        "scheme": "test",
                        "value": value,
                    },
                ),
            ):
                cu.save_config(
                    {
                        "mistral_model": "mistral-small-latest",
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
