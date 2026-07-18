"""Additional coverage tests for utils/stock_payload.py.

Tests edge cases in helper functions not already covered by test_build_stock_payload.py.
"""

import unittest

import pandas as pd
from flask import Flask

from app_state import app_state

# Module to test
from utils.stock_payload import (
    DEFAULT_US,
    DEFAULT_JP,
    DEFAULT_IDX,
    _build_chart_ohlc_data,
    _build_portfolio_metrics,
    _compute_price_metrics,
    _extract_portfolio_fields,
    _get_stock_container,
    _default_stock_names,
    _has_ready_indices_snapshot,
    _has_ready_stocks_snapshot,
    _resolve_indices_for_response,
    _resolve_stocks_for_response,
    _stock_is_default_or_user,
    _strip_portfolio_fields,
    choose_display_name,
    clear_yfinance_short_cache_prefix,
    error_response,
    get_default_symbols,
)

# Create a minimal Flask app for jsonify
_flask_app = Flask(__name__)
_flask_app.config["TESTING"] = True


class TestGetDefaultSymbols(unittest.TestCase):
    def test_get_default_symbols_has_all_markets(self):
        result = get_default_symbols()
        self.assertIn("us", result)
        self.assertIn("jp", result)
        self.assertIn("idx", result)
        self.assertEqual(len(result["us"]), len(DEFAULT_US))
        self.assertEqual(len(result["jp"]), len(DEFAULT_JP))
        self.assertEqual(len(result["idx"]), len(DEFAULT_IDX))


class TestClearYfinanceShortCachePrefix(unittest.TestCase):
    def setUp(self):
        with app_state.yfinance_short_cache_lock:
            self._saved = dict(app_state.yfinance_short_cache)
            app_state.yfinance_short_cache.clear()

    def tearDown(self):
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.clear()
            app_state.yfinance_short_cache.update(self._saved)

    def test_clear_prefix_removes_matching(self):
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache["info_short_AAPL"] = {"a": 1}
            app_state.yfinance_short_cache["info_short_MSFT"] = {"b": 2}
            app_state.yfinance_short_cache["other_key"] = {"c": 3}
        clear_yfinance_short_cache_prefix("info_short_")
        with app_state.yfinance_short_cache_lock:
            self.assertNotIn("info_short_AAPL", app_state.yfinance_short_cache)
            self.assertNotIn("info_short_MSFT", app_state.yfinance_short_cache)
            self.assertIn("other_key", app_state.yfinance_short_cache)

    def test_clear_prefix_empty(self):
        """Empty prefix does nothing."""
        clear_yfinance_short_cache_prefix("")


class TestGetStockContainer(unittest.TestCase):
    def test_get_stock_container_us(self):
        self.assertIs(_get_stock_container("us"), app_state.market.user_us)

    def test_get_stock_container_jp(self):
        self.assertIs(_get_stock_container("jp"), app_state.market.user_jp)

    def test_get_stock_container_idx(self):
        self.assertIs(_get_stock_container("idx"), app_state.market.user_idx)

    def test_get_stock_container_none(self):
        self.assertIsNone(_get_stock_container(None))

    def test_get_stock_container_unknown(self):
        self.assertIsNone(_get_stock_container("unknown"))


class TestDefaultStockNames(unittest.TestCase):
    def test_default_stock_names_us(self):
        result = _default_stock_names("us")
        self.assertEqual(result, DEFAULT_US)

    def test_default_stock_names_jp(self):
        result = _default_stock_names("jp")
        self.assertEqual(result, DEFAULT_JP)

    def test_default_stock_names_idx(self):
        result = _default_stock_names("idx")
        self.assertEqual(result, DEFAULT_IDX)

    def test_default_stock_names_empty(self):
        self.assertEqual(_default_stock_names("unknown"), {})


class TestStockIsDefaultOrUser(unittest.TestCase):
    def setUp(self):
        with app_state.market.user_stocks_lock:
            self._saved_us = dict(app_state.market.user_us)
            self._saved_jp = dict(app_state.market.user_jp)
            self._saved_idx = dict(app_state.market.user_idx)
            app_state.market.user_us = {"AAPL": "Apple"}
            app_state.market.user_jp = {"7203.T": "Toyota"}

    def tearDown(self):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = self._saved_us
            app_state.market.user_jp = self._saved_jp
            app_state.market.user_idx = self._saved_idx

    def test_default_symbol(self):
        self.assertTrue(_stock_is_default_or_user("NVDA", "us"))

    def test_user_symbol(self):
        self.assertTrue(_stock_is_default_or_user("AAPL", "us"))

    def test_unknown_symbol(self):
        self.assertFalse(_stock_is_default_or_user("ZZZZ", "us"))

    def test_unknown_market(self):
        self.assertFalse(_stock_is_default_or_user("TEST", "unknown"))


class TestChooseDisplayName(unittest.TestCase):
    def test_prefers_short_name(self):
        result = choose_display_name("AAPL", "Apple Inc.", {"shortName": "Apple"})
        self.assertEqual(result, "Apple")

    def test_falls_back_to_long_name(self):
        result = choose_display_name(
            "AAPL", "Apple Inc.", {"longName": "Apple Inc."}
        )
        self.assertEqual(result, "Apple Inc.")

    def test_falls_back_to_display_name(self):
        result = choose_display_name("AAPL", "Apple Inc.", {"displayName": "Apple"})
        self.assertEqual(result, "Apple")

    def test_falls_back_to_fallback_name(self):
        result = choose_display_name("ZZZZ", "Custom Name", {})
        self.assertEqual(result, "Custom Name")

    def test_fallback_name_is_dict(self):
        result = choose_display_name("AAPL", {"name": "Apple"}, {})
        self.assertEqual(result, "Apple")

    def test_fallback_to_symbol(self):
        result = choose_display_name("UNKNOWN", "", {})
        self.assertEqual(result, "UNKNOWN")

    def test_info_is_none(self):
        result = choose_display_name("AAPL", "Apple", None)
        self.assertEqual(result, "Apple")


class TestExtractPortfolioFields(unittest.TestCase):
    def test_string_input(self):
        name, shares, avg_price, avg_fx = _extract_portfolio_fields("Apple")
        self.assertEqual(name, "Apple")
        self.assertEqual(shares, 0.0)
        self.assertEqual(avg_price, 0.0)
        self.assertIsNone(avg_fx)

    def test_dict_input_with_all_fields(self):
        result = _extract_portfolio_fields({
            "name": "Apple",
            "shares": "10",
            "avg_price": "150.5",
            "avg_fx_rate": "145.0"
        })
        self.assertEqual(result, ("Apple", 10.0, 150.5, 145.0))

    def test_dict_input_with_invalid_shares(self):
        name, shares, avg_price, avg_fx = _extract_portfolio_fields(
            {"name": "Test", "shares": "invalid", "avg_price": "bad"}
        )
        self.assertEqual(shares, 0.0)
        self.assertEqual(avg_price, 0.0)

    def test_dict_input_with_invalid_fx(self):
        name, shares, avg_price, avg_fx = _extract_portfolio_fields(
            {"name": "Test", "avg_fx_rate": "not_a_number"}
        )
        self.assertIsNone(avg_fx)

    def test_dict_input_no_shares(self):
        name, shares, avg_price, avg_fx = _extract_portfolio_fields(
            {"name": "Test"}
        )
        self.assertEqual(shares, 0.0)
        self.assertEqual(avg_price, 0.0)

    def test_empty_dict(self):
        name, shares, avg_price, avg_fx = _extract_portfolio_fields({})
        self.assertEqual(name, "")
        self.assertEqual(shares, 0.0)

    def test_empty_string(self):
        name, shares, avg_price, avg_fx = _extract_portfolio_fields("")
        self.assertEqual(name, "")


class TestComputePriceMetrics(unittest.TestCase):
    def _make_hist(self, closes, dates=None):
        if dates is None:
            dates = pd.date_range("2026-01-01", periods=len(closes))
        return pd.DataFrame(
            {"Close": closes, "Open": closes, "High": [x + 1 for x in closes],
             "Low": [x - 1 for x in closes], "Volume": [1000] * len(closes)},
            index=dates,
        )

    def test_normal_two_days(self):
        hist = self._make_hist([100.0, 110.0])
        price, change, pct = _compute_price_metrics(hist, "TEST")
        self.assertEqual(float(price), 110.0)
        self.assertEqual(float(change), 10.0)
        self.assertEqual(float(pct), 10.0)

    def test_single_day(self):
        hist = self._make_hist([100.0])
        price, change, pct = _compute_price_metrics(hist, "TEST")
        self.assertEqual(float(price), 100.0)
        self.assertEqual(float(change), 0.0)
        self.assertEqual(float(pct), 0.0)

    def test_prev_is_zero(self):
        hist = self._make_hist([0.0, 100.0])
        result = _compute_price_metrics(hist, "TEST")
        self.assertIsNone(result[0])  # price is 0 => None

    def test_price_is_zero(self):
        hist = self._make_hist([100.0, 0.0])
        result = _compute_price_metrics(hist, "TEST")
        self.assertIsNone(result[0])

    def test_price_is_nan(self):
        hist = self._make_hist([100.0, float("nan")])
        result = _compute_price_metrics(hist, "TEST")
        self.assertIsNone(result[0])


class TestBuildChartOhlcData(unittest.TestCase):
    def _make_hist(self, n_days=5):
        dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
        return pd.DataFrame(
            {
                "Open": [100.0] * n_days,
                "High": [105.0] * n_days,
                "Low": [95.0] * n_days,
                "Close": [100.0 + i * 2 for i in range(n_days)],
                "Volume": [1000] * n_days,
            },
            index=dates,
        )

    def test_basic_chart_ohlc(self):
        df = self._make_hist(10)
        df["MA5"] = df["Close"].rolling(5).mean()
        df["MA25"] = df["Close"].rolling(25).mean()
        chart, ohlc = _build_chart_ohlc_data(df)
        self.assertGreater(len(chart), 0)
        self.assertGreater(len(ohlc), 0)

    def test_with_date_column_candidates(self):
        """Test date column detection with various candidate names."""
        dates = pd.to_datetime(["2026-01-01", "2026-01-02"])
        df = pd.DataFrame(
            {
                "Date": dates,
                "Open": [100.0, 110.0],
                "High": [105.0, 115.0],
                "Low": [95.0, 105.0],
                "Close": [100.0, 110.0],
                "Volume": [1000, 1500],
            }
        )
        df["MA5"] = df["Close"].rolling(5).mean()
        chart, ohlc = _build_chart_ohlc_data(df)
        self.assertGreater(len(ohlc), 0)

    def test_with_timestamp_index(self):
        """Test with a DatetimeIndex that has timestamp method."""
        df = self._make_hist(3)
        df["MA5"] = df["Close"].rolling(5).mean()
        chart, ohlc = _build_chart_ohlc_data(df)
        self.assertGreater(len(ohlc), 0)

    def test_volume_exception_handling(self):
        """Volume parsing exception should be caught."""
        dates = pd.date_range("2026-01-01", periods=3)
        df = pd.DataFrame(
            {
                "Open": [100.0] * 3, "High": [105.0] * 3, "Low": [95.0] * 3,
                "Close": [100.0, 102.0, 104.0],
                "Volume": [1000, 1500, "bad"],
            },
            index=dates,
        )
        df["MA5"] = df["Close"].rolling(5).mean()
        chart, ohlc = _build_chart_ohlc_data(df)
        self.assertGreater(len(ohlc), 0)


class TestBuildPortfolioMetrics(unittest.TestCase):
    def setUp(self):
        self._saved_indices = dict(app_state.market.current_indices_cache)

    def tearDown(self):
        app_state.market.current_indices_cache = self._saved_indices

    def test_non_usd_currency(self):
        value, pl = _build_portfolio_metrics(10, 100, None, "JPY", 110)
        self.assertIsNotNone(value)
        self.assertIsNotNone(pl)

    def test_usd_with_avg_fx(self):
        app_state.market.current_indices_cache["USDJPY"] = {"price": 150}
        value, pl = _build_portfolio_metrics(10, 100, 145, "USD", 110)
        self.assertIsNotNone(value)
        self.assertIsNotNone(pl)

    def test_usd_without_fx_cache(self):
        app_state.market.current_indices_cache.pop("USDJPY", None)
        value, pl = _build_portfolio_metrics(10, 100, None, "USD", 110)
        # Falls back to 150.0: 10*110*150 = 165000, pl = (10*110*150)-(10*100*150) = 165000-150000 = 15000
        self.assertIsNotNone(value)
        self.assertIsNotNone(pl)

    def test_usd_with_zero_shares(self):
        value, pl = _build_portfolio_metrics(0, 0, None, "JPY", 110)
        self.assertIsNotNone(value)
        self.assertIsNotNone(pl)


class TestStripPortfolioFields(unittest.TestCase):
    def test_strip_non_dict(self):
        self.assertEqual(_strip_portfolio_fields("string"), "string")

    def test_strip_none(self):
        self.assertIsNone(_strip_portfolio_fields(None))

    def test_strip_list(self):
        self.assertEqual(_strip_portfolio_fields([1, 2, 3]), [1, 2, 3])

    def test_strip_removes_sensitive_fields(self):
        row = {
            "symbol": "AAPL",
            "price": 150.0,
            "shares": 10,
            "avg_price": 140.0,
            "avg_fx_rate": 150.0,
            "portfolio_value": 15000,
            "portfolio_pl": 1000,
            "sector": "Tech",
        }
        stripped = _strip_portfolio_fields(row)
        self.assertNotIn("shares", stripped)
        self.assertNotIn("avg_price", stripped)
        self.assertNotIn("avg_fx_rate", stripped)
        self.assertNotIn("portfolio_value", stripped)
        self.assertNotIn("portfolio_pl", stripped)
        self.assertIn("symbol", stripped)
        self.assertIn("price", stripped)
        self.assertIn("sector", stripped)

    def test_strip_preserves_empty_row(self):
        self.assertEqual(_strip_portfolio_fields({}), {})


class TestResolveStocksForResponse(unittest.TestCase):
    def setUp(self):
        self._saved_current = app_state.market.current_stocks_cache
        self._saved_target = app_state.market.target_stocks_cache

    def tearDown(self):
        app_state.market.current_stocks_cache = self._saved_current
        app_state.market.target_stocks_cache = self._saved_target

    def test_uses_current_when_available(self):
        app_state.market.current_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 150}],
            "jp": [],
            "idx": [],
        }
        result = _resolve_stocks_for_response()
        self.assertEqual(len(result["us"]), 1)

    def test_uses_target_when_current_empty(self):
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 150}],
            "jp": [],
            "idx": [],
        }
        result = _resolve_stocks_for_response()
        self.assertEqual(len(result["us"]), 1)

    def test_strips_portfolio_by_default(self):
        app_state.market.current_stocks_cache = {
            "us": [{"symbol": "AAPL", "shares": 10, "price": 150}],
            "jp": [],
            "idx": [],
        }
        result = _resolve_stocks_for_response()
        self.assertNotIn("shares", result["us"][0])

    def test_include_portfolio_preserves_fields(self):
        app_state.market.current_stocks_cache = {
            "us": [{"symbol": "AAPL", "shares": 10, "price": 150}],
            "jp": [],
            "idx": [],
        }
        result = _resolve_stocks_for_response(include_portfolio=True)
        self.assertIn("shares", result["us"][0])

    def test_empty_cache_returns_empty(self):
        app_state.market.current_stocks_cache = None
        app_state.market.target_stocks_cache = None
        result = _resolve_stocks_for_response()
        self.assertEqual(result["us"], [])
        self.assertEqual(result["jp"], [])
        self.assertEqual(result["idx"], [])

    def test_malformed_market_data(self):
        app_state.market.current_stocks_cache = {
            "us": "not_a_list",
            "jp": None,
            "idx": [{"symbol": "AAPL"}],
        }
        result = _resolve_stocks_for_response()
        self.assertEqual(result["us"], [])
        self.assertEqual(result["idx"], [{"symbol": "AAPL"}])


class TestResolveIndicesForResponse(unittest.TestCase):
    def setUp(self):
        self._saved_current = app_state.market.current_indices_cache
        self._saved_target = app_state.market.target_indices_cache

    def tearDown(self):
        app_state.market.current_indices_cache = self._saved_current
        app_state.market.target_indices_cache = self._saved_target

    def test_uses_current_when_available(self):
        app_state.market.current_indices_cache = {"^N225": {"price": 38000}}
        app_state.market.target_indices_cache = {}
        result = _resolve_indices_for_response()
        self.assertIn("^N225", result)

    def test_falls_back_to_target(self):
        app_state.market.current_indices_cache = {}
        app_state.market.target_indices_cache = {"^N225": {"price": 38000}}
        result = _resolve_indices_for_response()
        self.assertIn("^N225", result)


class TestHasReadySnapshots(unittest.TestCase):
    def setUp(self):
        self._saved_current_stocks = app_state.market.current_stocks_cache
        self._saved_target_stocks = app_state.market.target_stocks_cache
        self._saved_current_idx = app_state.market.current_indices_cache
        self._saved_target_idx = app_state.market.target_indices_cache

    def tearDown(self):
        app_state.market.current_stocks_cache = self._saved_current_stocks
        app_state.market.target_stocks_cache = self._saved_target_stocks
        app_state.market.current_indices_cache = self._saved_current_idx
        app_state.market.target_indices_cache = self._saved_target_idx

    def test_no_ready_indices(self):
        app_state.market.current_indices_cache = {}
        app_state.market.target_indices_cache = {}
        self.assertFalse(_has_ready_indices_snapshot())

    def test_has_ready_indices_in_current(self):
        app_state.market.current_indices_cache = {"^N225": {"price": 38000}}
        self.assertTrue(_has_ready_indices_snapshot())

    def test_has_ready_indices_in_target(self):
        app_state.market.current_indices_cache = {}
        app_state.market.target_indices_cache = {"^N225": {"price": 38000}}
        self.assertTrue(_has_ready_indices_snapshot())

    def test_no_ready_stocks(self):
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        self.assertFalse(_has_ready_stocks_snapshot())

    def test_has_ready_stocks_in_target(self):
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "AAPL"}], "jp": [], "idx": []
        }
        self.assertTrue(_has_ready_stocks_snapshot())

    def test_empty_cache_dict_returns_false(self):
        app_state.market.current_stocks_cache = None
        app_state.market.target_stocks_cache = {"us": None, "jp": [], "idx": []}
        self.assertFalse(_has_ready_stocks_snapshot())


class TestErrorResponse(unittest.TestCase):
    def test_error_response_with_details(self):
        with _flask_app.app_context():
            resp, status = error_response(1001, status_code=404, details={"reason": "Not found"})
            self.assertEqual(status, 404)
            data = resp.get_json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error_code"], 1001)
            self.assertIn("details", data)
            self.assertEqual(data["details"]["reason"], "Not found")

    def test_error_response_without_details(self):
        with _flask_app.app_context():
            resp, status = error_response(1002, status_code=400)
            self.assertEqual(status, 400)
            data = resp.get_json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error_code"], 1002)

    def test_error_response_sanitizes_sensitive_values(self):
        """_sanitize_error_message redacts API keys, tokens, etc."""
        with _flask_app.app_context():
            resp, status = error_response(
                1003, details={"msg": "api_key=sk-1234567890abcdef"}
            )
            data = resp.get_json()
            # The sensitive pattern (api_key=...) should be redacted
            self.assertNotIn("sk-1234567890abcdef", data.get("details", {}).get("msg", ""))


if __name__ == "__main__":
    unittest.main()
