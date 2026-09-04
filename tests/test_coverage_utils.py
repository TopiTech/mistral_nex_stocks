"""Coverage tests for utilities: validators, normalization, route_helpers, storage, http_utils, text_utils, native_host."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_state import app_state
from native_host import native_host
from utils import env_helpers, http_utils, normalization, storage, text_utils
from utils.market_utils import _market_state_from_metadata
from utils.stock_payload import (
    _resolve_indices_for_response,
    _resolve_stocks_for_response,
    get_stock_info_cached,
)


class RouteHelpersCoverageTestCase(unittest.TestCase):
    def test_rate_limit_env_name_sanitizes_endpoint(self):
        import route_helpers

        self.assertEqual(
            route_helpers._rate_limit_env_name("/api/hello-world", "MAX"),
            "MNS_RATE_LIMIT__API_HELLO_WORLD_MAX",
        )

    def test_resolve_rate_limit_env_overrides(self):
        import route_helpers

        with patch.dict(
            "os.environ",
            {
                "MNS_RATE_LIMIT_DEFAULT_MAX": "11",
                "MNS_RATE_LIMIT_DEFAULT_WINDOW": "22",
            },
            clear=False,
        ):
            self.assertEqual(route_helpers._resolve_rate_limit("default", 5, 6), (11, 22))

    def test_extract_text_from_mistral_content_variants(self):
        import route_helpers

        self.assertEqual(route_helpers._extract_text_from_mistral_content(" hello "), "hello")
        self.assertEqual(
            route_helpers._extract_text_from_mistral_content(
                [{"type": "text", "text": "A"}, {"type": "citation"}, "B"]
            ),
            "A",
        )

    def test_seconds_until_clamps_zero(self):
        import route_helpers

        with patch("route_helpers.time.time", return_value=100.0):
            self.assertEqual(route_helpers._seconds_until(90.0), 0.0)
            self.assertEqual(route_helpers._seconds_until(102.345), 2.34)


class ValidatorsCoverageTestCase(unittest.TestCase):
    def test_portfolio_schema_rejects_negative_values(self):
        from utils.validators import PortfolioInputSchema

        with self.assertRaises(ValueError):
            PortfolioInputSchema(symbol="AAPL", market="us", shares=-1, avg_price=1)

    def test_validate_analysis_result(self):
        from utils.validators import validate_analysis_result

        valid, reason = validate_analysis_result({"recommendation": "買い"})
        self.assertTrue(valid)
        self.assertEqual(reason, "")

        valid, reason = validate_analysis_result({"target_price_3m": "bad"})
        self.assertFalse(valid)
        self.assertIn("numeric", reason)

    def test_normalize_analysis_result_fills_defaults(self):
        from utils.validators import normalize_analysis_result

        result = normalize_analysis_result({"analysis_summary": "ok"})
        self.assertEqual(result["recommendation"], "中立")
        self.assertEqual(result["analysis_summary"], "ok")

    def test_extract_json_payload_variants(self):
        from utils.validators import extract_json_payload

        self.assertEqual(extract_json_payload({"a": 1}), '{"a": 1}')
        self.assertIn('"a": 1', extract_json_payload('```json\n{"a": 1}\n```'))

    def test_extract_chat_content_variants(self):
        from utils.validators import extract_chat_content

        self.assertEqual(
            extract_chat_content({"choices": [{"message": {"content": None}}]}),
            "(応答が返されませんでした)",
        )
        self.assertIn(
            '"x": 1',
            extract_chat_content({"choices": [{"message": {"content": {"x": 1}}}]}),
        )

    def test_safe_parse_analysis_result_fallback(self):
        from utils.validators import safe_parse_analysis_result

        result = safe_parse_analysis_result(
            {}, api_key="dummy", repair_func=lambda *args, **kwargs: ({}, None)
        )
        self.assertIn("analysis_summary", result)


class NormalizationCoverageTestCase(unittest.TestCase):
    def test_normalize_text_and_symbol(self):
        self.assertEqual(normalization.normalize_text(None, default="x"), "x")
        self.assertEqual(normalization.normalize_text(" a "), "a")
        self.assertEqual(normalization.normalize_symbol(123), "123")
        self.assertEqual(normalization.normalize_symbol_for_market("7203", "jp"), "7203.T")

    def test_market_and_symbol_validation(self):
        self.assertEqual(normalization.normalize_market("JP"), "jp")
        self.assertIsNone(normalization.normalize_market("invalid"))
        self.assertTrue(normalization.is_valid_symbol("AAPL"))
        self.assertFalse(normalization.is_valid_symbol("../bad"))

    def test_optional_number_and_formatters(self):
        self.assertEqual(normalization.normalize_optional_number("10.5"), 10.5)
        self.assertIsNone(normalization.normalize_optional_number("bad"))
        self.assertEqual(normalization._fmt(1.234), 1.23)
        self.assertEqual(normalization._fmt_vol(10.9), 10)


class ConfigUtilsCoverageTestCase(unittest.TestCase):
    def test_resolve_model_target_and_alias(self):
        import config_utils

        self.assertEqual(
            config_utils.resolve_model_target("1")["name"], config_utils.MISTRAL_MODELS["1"]["name"]
        )
        self.assertEqual(
            config_utils.resolve_model_target("mistral-small-latest")["name"], "mistral-small-2603"
        )
        self.assertIsNone(config_utils.resolve_model_target("unknown-model"))


class EnvHelpersCoverageTestCase(unittest.TestCase):
    def test_env_bool_recognizes_explicit_false_and_true_values(self):
        for value in ("0", "false", "False", "no", "off"):
            with patch.dict("os.environ", {"MNS_TEST_BOOL": value}, clear=False):
                self.assertFalse(env_helpers._env_bool("MNS_TEST_BOOL", True))
        for value in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict("os.environ", {"MNS_TEST_BOOL": value}, clear=False):
                self.assertTrue(env_helpers._env_bool("MNS_TEST_BOOL"))

    def test_env_int_default_and_bounds(self):
        self.assertEqual(env_helpers._env_int("NOT_SET_X", 7), 7)
        with patch.dict("os.environ", {"MNS_TEST_INT": "abc"}, clear=False):
            self.assertEqual(env_helpers._env_int("MNS_TEST_INT", 5, 1, 10), 5)
        with patch.dict("os.environ", {"MNS_TEST_INT": "50"}, clear=False):
            self.assertEqual(env_helpers._env_int("MNS_TEST_INT", 5, 1, 10), 10)

    def test_is_production_env(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(env_helpers._is_production_env())
        with patch.dict("os.environ", {"MNS_PROD": "1"}, clear=True):
            self.assertTrue(env_helpers._is_production_env())


class HttpUtilsCoverageTestCase(unittest.TestCase):
    def test_none_response(self):
        self.assertIsNone(http_utils.parse_retry_after(None))

    def test_dict_headers(self):
        self.assertEqual(
            http_utils.parse_retry_after(SimpleNamespace(headers={"Retry-After": "30"})), 30.0
        )
        self.assertIsNone(http_utils.parse_retry_after({"headers": {}}))

    def test_case_insensitive_headers(self):
        class CI:
            def get(self, k, default=None):
                return {"retry-after": "5"}.get(k.lower(), default)

        self.assertEqual(http_utils.parse_retry_after(SimpleNamespace(headers=CI())), 5.0)

    def test_invalid_http_date(self):
        self.assertIsNone(
            http_utils.parse_retry_after(SimpleNamespace(headers={"Retry-After": "garbage"}))
        )


class TextUtilsCoverageTestCase(unittest.TestCase):
    def test_short_text_strips_control(self):
        self.assertEqual(text_utils._short_text("  hello  "), "hello")
        self.assertEqual(text_utils._short_text("a\tb\nc"), "abc")
        long = "x" * 200
        self.assertTrue(text_utils._short_text(long).endswith("..."))

    def test_token_fingerprint_and_mask(self):
        self.assertEqual(text_utils._token_fingerprint(""), "none")
        fp = text_utils._token_fingerprint("secret")
        self.assertTrue(fp.startswith("sha256="))
        self.assertEqual(text_utils._token_mask(""), "none")
        self.assertEqual(text_utils._token_mask("ab"), "**")
        self.assertEqual(text_utils._token_mask("abcdef"), "ab...ef")

    def test_is_valid_api_key(self):
        self.assertFalse(text_utils._is_valid_api_key(None))
        self.assertFalse(text_utils._is_valid_api_key("short"))
        self.assertFalse(text_utils._is_valid_api_key("has space"))
        self.assertTrue(text_utils._is_valid_api_key("validkey12"))

    def test_sanitize_error_message(self):
        self.assertEqual(text_utils._sanitize_error_message(""), "")
        dirty = "api_key=abc12345 token=xyz secret=pass"
        sanitized = text_utils._sanitize_error_message(dirty)
        self.assertNotIn("abc12345", sanitized)
        self.assertIn("[REDACTED]", sanitized)


class StorageCoverageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.test_stocks_file = self.tmp_path / "user_stocks.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_mark_user_stocks_load_failure_and_backup(self):
        with (
            patch.object(storage, "USER_STOCKS_FILE", self.test_stocks_file),
            patch("config_store.USER_STOCKS_FILE", self.test_stocks_file),
            patch("config_store.APP_DATA_DIR", self.tmp_path),
        ):
            self.test_stocks_file.write_text("corrupt data!", encoding="utf-8")
            storage._mark_user_stocks_load_failure("Corrupted file test")
            self.assertTrue(app_state.market.user_stocks_load_error)
            baks = list(self.tmp_path.glob("user_stocks.bak.*"))
            self.assertTrue(len(baks) >= 1)

    def test_save_user_stocks_refuses_when_load_error_set(self):
        app_state.market.user_stocks_load_error = True
        with self.assertRaises(storage.UserStocksPersistError):
            storage.save_user_stocks()
        app_state.market.user_stocks_load_error = False

    def test_migrate_legacy_user_stocks(self):
        legacy_path = self.tmp_path / "legacy_user_stocks.json"
        legacy_path.write_text(json.dumps({"us": {"AAPL": "Apple"}}), encoding="utf-8")
        with (
            patch("utils.storage.LEGACY_USER_STOCKS_FILE", legacy_path),
            patch.object(storage, "USER_STOCKS_FILE", self.test_stocks_file),
            patch("config_store.USER_STOCKS_FILE", self.test_stocks_file),
        ):
            storage._migrate_legacy_user_stocks()
            self.assertTrue(self.test_stocks_file.exists())

    def test_load_and_save_user_stocks_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            stock_file = Path(td) / "user_stocks.json"
            with (
                patch.object(storage, "USER_STOCKS_FILE", str(stock_file)),
                patch.object(app_state.market, "user_stocks_lock", MagicMock()),
                patch.object(app_state.market, "user_us", {"AAPL": "Apple"}),
                patch.object(app_state.market, "user_jp", {"7203.T": "Toyota"}),
                patch.object(app_state.market, "user_idx", {"^N225": "Nikkei"}),
                patch.object(app_state.market, "last_usdjpy_rate", 150.0),
            ):
                storage.save_user_stocks()
                self.assertTrue(stock_file.exists())


class NativeHostCoverageTestCase(unittest.TestCase):
    def test_sanitize_log_message(self):
        msg = native_host._sanitize_log_message(
            "Request with api_key='sk-1234567890' and token=secret"
        )
        self.assertNotIn("sk-1234567890", msg)
        self.assertNotIn("secret", msg)
        self.assertIn("[REDACTED]", msg)

    def test_safe_env_helpers(self):
        with patch.dict(os.environ, {"TEST_INT": "100", "TEST_FLOAT": "2.5", "BAD_INT": "xyz"}):
            self.assertEqual(native_host._safe_int_env("TEST_INT", 10), 100)
            self.assertEqual(native_host._safe_float_env("TEST_FLOAT", 1.0), 2.5)
            self.assertEqual(native_host._safe_int_env("BAD_INT", 10), 10)
            self.assertEqual(native_host._safe_int_env("TEST_INT", 10, min_value=200), 200)

    def test_token_action_rate_limiter(self):
        with native_host._rate_limit_lock:
            native_host._token_action_timestamps.clear()
        for _ in range(3):
            self.assertTrue(native_host._token_action_allowed())
        self.assertFalse(native_host._token_action_allowed())

    def test_read_and_send_message_io(self):
        import struct

        msg = {"action": "ping"}
        raw_msg = json.dumps(msg).encode("utf-8")
        header = struct.pack("@I", len(raw_msg))

        input_stream = io.BytesIO(header + raw_msg)
        output_stream = io.BytesIO()

        with (
            patch.object(native_host, "RAW_STDIN", input_stream),
            patch.object(native_host, "RAW_STDOUT", output_stream),
        ):
            received = native_host.read_message()
            self.assertEqual(received, msg)

            native_host.send_message({"ok": True, "pong": True})
            out_bytes = output_stream.getvalue()
            self.assertTrue(len(out_bytes) > 4)
            resp_len = struct.unpack("@I", out_bytes[:4])[0]
            resp_data = json.loads(out_bytes[4 : 4 + resp_len].decode("utf-8"))
            self.assertTrue(resp_data.get("ok"))


class MarketUtilsCoverageTestCase(unittest.TestCase):
    def test_market_state_from_metadata_variants(self):
        self.assertEqual(_market_state_from_metadata({"marketState": "REGULAR"}), "REGULAR")
        self.assertEqual(_market_state_from_metadata({"marketState": "PRE"}), "CLOSED")
        self.assertEqual(
            _market_state_from_metadata(
                {"currentTradingPeriod": {"regular": {"start": 100.0, "end": 200.0}}}
            ),
            "CLOSED",
        )

    @patch("utils.market_utils.time.time", return_value=150.0)
    def test_market_state_from_metadata_regular_period(self, _mock_time):
        self.assertEqual(
            _market_state_from_metadata(
                {"currentTradingPeriod": {"regular": {"start": 100.0, "end": 200.0}}}
            ),
            "REGULAR",
        )


class StockPayloadCoverageTestCase(unittest.TestCase):
    def test_resolve_stocks_and_indices_response(self):
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {"us": [{"symbol": "AAPL"}], "jp": [], "idx": []}
        stocks = _resolve_stocks_for_response()
        self.assertEqual(stocks["us"][0]["symbol"], "AAPL")

        app_state.market.current_indices_cache = {}
        app_state.market.target_indices_cache = {"SP500": {"price": 1}}
        indices = _resolve_indices_for_response()
        self.assertIn("SP500", indices)

    @patch("utils.stock_payload._has_cached_key", return_value=True)
    def test_get_stock_info_cached_negative_cache(self, _mock_cached):
        self.assertEqual(get_stock_info_cached("AAPL"), {})


if __name__ == "__main__":
    unittest.main()
