import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app
from app_helpers import (
    _market_state_from_metadata,
    _resolve_indices_for_response,
    _resolve_stocks_for_response,
    get_stock_info_cached,
)
from app_state import app_state
import route_helpers
import utils.validators as validators
from utils import storage


class AppHelpersBranchCoverageTestCase(unittest.TestCase):
    def test_market_state_from_metadata_variants(self):
        self.assertEqual(_market_state_from_metadata({"marketState": "REGULAR"}), "REGULAR")
        self.assertEqual(_market_state_from_metadata({"marketState": "PRE"}), "CLOSED")
        self.assertEqual(
            _market_state_from_metadata({"currentTradingPeriod": {"regular": {"start": 100.0, "end": 200.0}}}),
            "CLOSED",
        )

    @patch("utils.market_utils.time.time", return_value=150.0)
    def test_market_state_from_metadata_regular_period(self, _mock_time):
        self.assertEqual(
            _market_state_from_metadata({"currentTradingPeriod": {"regular": {"start": 100.0, "end": 200.0}}}),
            "REGULAR",
        )

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

    @patch("utils.stock_payload._has_cached_key", return_value=False)
    @patch("utils.stock_payload.get_cached")
    @patch("utils.stock_payload.app_state.stock_provider")
    def test_get_stock_info_cached_fetches_currency_for_index(self, mock_provider, mock_get_cached, _mock_cached):
        mock_provider.get_fast_info.return_value = {"currency": "JPY", "marketCap": 123}
        mock_provider.get_info.return_value = {}
        mock_get_cached.side_effect = lambda key, fetch_func, duration=86400, valid_func=None: fetch_func()

        info = get_stock_info_cached("^N225")

        self.assertEqual(info.get("currency"), "JPY")
        mock_provider.get_fast_info.assert_called_once_with("^N225")
        mock_provider.get_info.assert_not_called()

    def test_get_stock_info_cached_uses_real_short_cache(self):
        """Verify get_stock_info_cached reads from the REAL yfinance_short_cache.

        Pre-populate the short cache, call the function, and confirm it
        returns the cached data without hitting yfinance.
        """
        from app_state import app_state
        from tests import reset_app_state_internals

        reset_app_state_internals()

        # Pre-populate the real short cache with test data
        cache_key = "info_short_AAPL"
        expected = {"currency": "USD", "marketCap": 999_999_999_999}
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache[cache_key] = dict(expected)

        info = get_stock_info_cached("AAPL")

        self.assertEqual(info.get("currency"), "USD")
        self.assertEqual(info.get("marketCap"), 999_999_999_999)

        # Ensure the negative cache is NOT set (the function never called
        # the real fetch path because short cache returned immediately)
        from app_helpers import _has_cached_key
        self.assertFalse(_has_cached_key("info_AAPL__failed", 600))


class ValidatorsBranchCoverageTestCase(unittest.TestCase):
    def test_extract_json_payload_variants(self):
        self.assertEqual(validators.extract_json_payload({"a": 1}), '{"a": 1}')
        self.assertIn('"a": 1', validators.extract_json_payload('```json\n{"a": 1}\n```'))
        self.assertIn('"a": 1', validators.extract_json_payload('xx {"a": 1,} yy'))

    def test_extract_chat_content_variants(self):
        self.assertEqual(validators.extract_chat_content({"choices": [{"message": {"content": None}}]}), "(応答が返されませんでした)")
        self.assertIn("不予期", validators.extract_chat_content({"choices": [{"message": {"content": 123}}]}))
        self.assertIn('"x": 1', validators.extract_chat_content({"choices": [{"message": {"content": {"x": 1}}}]}))

    def test_safe_parse_analysis_result_fallback(self):
        result = validators.safe_parse_analysis_result({}, api_key="dummy", repair_func=lambda *args, **kwargs: ({}, None))
        self.assertIn("analysis_summary", result)

    def test_validate_analysis_result_more(self):
        self.assertEqual(validators.validate_analysis_result("bad"), (False, "result is not an object"))
        self.assertEqual(validators.validate_analysis_result({}), (False, "missing core analysis fields"))
        self.assertEqual(validators.validate_analysis_result({"key_catalysts": "x", "analysis_summary": "ok"}), (False, "key_catalysts must be an array"))


class RouteHelpersBranchCoverageTestCase(unittest.TestCase):
    def test_parse_stock_request_errors(self):
        with app.app_context():
            payload, err = route_helpers._parse_stock_request({}, require_name=True)
            self.assertIsNone(payload)
            self.assertIsNotNone(err)

            payload, err = route_helpers._parse_stock_request({"symbol": "AAPL", "market": "bad"})
            self.assertIsNone(payload)
            self.assertIsNotNone(err)

            payload, err = route_helpers._parse_stock_request({"symbol": "../bad", "market": "us"})
            self.assertIsNone(payload)
            self.assertIsNotNone(err)

    def test_cache_helper_branches(self):
        app_state.market.current_stocks_cache = {"us": []}
        app_state.market.target_stocks_cache = {"us": []}
        route_helpers.ensure_stock_placeholder_in_caches("AAPL", "Apple", "us")
        self.assertEqual(app_state.market.current_stocks_cache["us"][0]["symbol"], "AAPL")
        route_helpers.remove_stock_from_caches("AAPL", "us")
        self.assertEqual(app_state.market.current_stocks_cache["us"], [])


class StorageBranchCoverageTestCase(unittest.TestCase):
    def test_load_user_stocks_decrypt_failure_deletes_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_stocks.json"
            path.write_text(json.dumps({"scheme": "x", "value": "y"}), encoding="utf-8")
            with patch.object(storage, "USER_STOCKS_FILE", str(path)), \
                patch("utils.storage.unprotect_data", return_value=""):
                storage.load_user_stocks(force=True)
                self.assertFalse(path.exists())


class ErrorHandlersBranchCoverageTestCase(unittest.TestCase):
    def test_registered_handlers_return_json(self):
        flask_app = app
        client = flask_app.test_client()
        response = client.get("/definitely-not-found")
        self.assertEqual(response.status_code, 404)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "Not Found")


if __name__ == "__main__":
    unittest.main()