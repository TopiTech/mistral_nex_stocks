import logging
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

import config_utils
import error_handlers
import logging_config
import route_helpers
import utils.normalization as normalization
import utils.threading as threading_utils
import utils.validators as validators


class RouteHelpersCoverageTestCase(unittest.TestCase):
    def test_rate_limit_env_name_sanitizes_endpoint(self):
        self.assertEqual(
            route_helpers._rate_limit_env_name("/api/hello-world", "MAX"),
            "MNS_RATE_LIMIT__API_HELLO_WORLD_MAX",
        )

    def test_resolve_rate_limit_env_overrides(self):
        with patch.dict(
            "os.environ",
            {
                "MNS_RATE_LIMIT_DEFAULT_MAX": "11",
                "MNS_RATE_LIMIT_DEFAULT_WINDOW": "22",
                "MNS_RATE_LIMIT_API_TEST_MAX": "33",
                "MNS_RATE_LIMIT_API_TEST_WINDOW": "44",
            },
            clear=False,
        ):
            self.assertEqual(route_helpers._resolve_rate_limit("api-test", 5, 6), (33, 44))

    def test_cleanup_rate_limit_store_prunes_expired_and_excess(self):
        with (
            patch.object(route_helpers, "_RATE_LIMIT_MAX_ENTRIES", 1),
            patch.object(route_helpers, "_rate_limit_window_by_key", {"old": 1, "keep": 9999}),
            patch.object(route_helpers, "_rate_limit_store", {"old": [1.0], "keep": [1000.0]}),
            patch.object(route_helpers, "time") as mock_time,
        ):
            mock_time.time.return_value = 2000.0
            route_helpers._cleanup_rate_limit_store()
            self.assertEqual(list(route_helpers._rate_limit_store.keys()), ["keep"])

    def test_extract_text_from_mistral_content_variants(self):
        self.assertEqual(route_helpers._extract_text_from_mistral_content(" hello "), "hello")
        self.assertEqual(
            route_helpers._extract_text_from_mistral_content(
                [{"type": "text", "text": "A"}, {"type": "citation"}, "B"]
            ),
            "A",
        )

    def test_seconds_until_clamps_zero(self):
        with patch("route_helpers.time.time", return_value=100.0):
            self.assertEqual(route_helpers._seconds_until(90.0), 0.0)
            self.assertEqual(route_helpers._seconds_until(102.345), 2.34)


class ValidatorsCoverageTestCase(unittest.TestCase):
    def test_portfolio_schema_rejects_negative_values(self):
        with self.assertRaises(Exception):
            validators.PortfolioInputSchema(symbol="AAPL", market="us", shares=-1, avg_price=1)

    def test_validate_analysis_result(self):
        valid, reason = validators.validate_analysis_result({"recommendation": "買い"})
        self.assertTrue(valid)
        self.assertEqual(reason, "")

        valid, reason = validators.validate_analysis_result({"target_price_3m": "bad"})
        self.assertFalse(valid)
        self.assertIn("numeric", reason)

    def test_normalize_analysis_result_fills_defaults(self):
        result = validators.normalize_analysis_result({"analysis_summary": "ok"})
        self.assertEqual(result["recommendation"], "中立")
        self.assertEqual(result["analysis_summary"], "ok")

    def test_extract_chat_content_base_model_and_list(self):
        class DummyModel(BaseModel):
            x: int = 1

        class DummyMessage:
            def __init__(self, content):
                self.content = content

        class DummyChoice:
            def __init__(self, message):
                self.message = message

        response = SimpleNamespace(choices=[DummyChoice(DummyMessage(DummyModel()))])
        self.assertEqual(validators.extract_chat_content(response), '{"x":1}')

        self.assertEqual(
            validators.extract_chat_content(
                {"choices": [{"message": {"content": [{"type": "text", "text": "Hi"}]}}]}
            ),
            "Hi",
        )

    def test_safe_parse_analysis_result(self):
        parsed = validators.safe_parse_analysis_result(
            {"choices": [{"message": {"content": {"analysis_summary": "A"}}}]},
            api_key="dummy",
        )
        self.assertEqual(parsed["analysis_summary"], "A")


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

    def test_normalize_history_frame(self):
        df = pd.DataFrame({"Close": [1, 2]}, index=["2026-01-01", "2026-01-02"])
        out = normalization.normalize_history_frame(df)
        self.assertFalse(out.empty)
        self.assertIsInstance(out.index, pd.DatetimeIndex)


class ConfigUtilsCoverageTestCase(unittest.TestCase):
    def test_resolve_model_target_and_alias(self):
        self.assertEqual(
            config_utils.resolve_model_target("1")["name"], config_utils.MISTRAL_MODELS["1"]["name"]
        )
        self.assertEqual(
            config_utils.resolve_model_target("mistral-small-latest")["name"], "mistral-small-2603"
        )
        self.assertIsNone(config_utils.resolve_model_target("unknown-model"))

    def test_get_all_models(self):
        self.assertIn("1", config_utils.get_all_models())


class ErrorHandlersCoverageTestCase(unittest.TestCase):
    def test_register_error_handlers(self):
        app = MagicMock()
        handlers = {}

        def errorhandler(code):
            def decorator(fn):
                handlers[code] = fn
                return fn

            return decorator

        app.errorhandler.side_effect = errorhandler
        error_handlers.register_error_handlers(app)
        self.assertIn(400, handlers)
        self.assertIn(500, handlers)


class LoggingConfigCoverageTestCase(unittest.TestCase):
    def test_init_logging_suppresses_yfinance(self):
        app = MagicMock()
        root_logger = logging.getLogger()
        old_handlers = list(root_logger.handlers)
        new_handlers = []
        tmp = tempfile.TemporaryDirectory()
        try:
            with patch.object(logging_config, "BASE_DIR", Path(tmp.name)):
                logging_config.init_logging(app)
                new_handlers = [h for h in root_logger.handlers if h not in old_handlers]
                self.assertGreaterEqual(logging.getLogger("yfinance").level, logging.WARNING)
        finally:
            for h in new_handlers:
                try:
                    root_logger.removeHandler(h)
                    h.close()
                except Exception:
                    pass
            root_logger.handlers = old_handlers
            tmp.cleanup()


class ThreadingCoverageTestCase(unittest.TestCase):
    def test_executor_submit_and_done_callback(self):
        ex = threading_utils.DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="mns-test")
        fut = ex.submit(lambda: 42)
        self.assertEqual(fut.result(timeout=2), 42)
        ex.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
