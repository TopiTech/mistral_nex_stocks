# tests/test_code_review_goal_audit_2026_09_v5.py
"""
Regression test suite for Code Review Findings R27, R28, and R29.

R27: Cross-field bounds validation for ScreenerQueryRequest and ScreenerFilterSchema,
     and symbol trimming/casing normalization in StockHistoryQueryRequest and StockDetailsQueryRequest.
R28: In-memory cache FX rate boundary isolation in api_copy_ai_portfolio_to_my() for non-US markets.
R29: Roving keyboard navigation (ArrowLeft/Right/Home/End) for Screener and SSE mode button groups.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import app
from routes.stocks.common import app_state
from schemas.stocks import (
    ScreenerQueryRequest,
    StockDetailsQueryRequest,
    StockHistoryQueryRequest,
)
from utils.validators import ScreenerFilterSchema


class TestCodeReviewAuditV5(unittest.TestCase):
    """Test suite verifying fixes for R27, R28, and R29."""

    def setUp(self) -> None:
        self._orig_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["WTF_CSRF_ENABLED"] = False

    def tearDown(self) -> None:
        app.config["WTF_CSRF_ENABLED"] = self._orig_csrf

    def test_r27_screener_query_request_bounds(self) -> None:
        """Verify ScreenerQueryRequest validates min <= max across all metric ranges."""
        # Valid cases
        req = ScreenerQueryRequest(
            min_price=10.0,
            max_price=100.0,
            min_change=-2.5,
            max_change=5.0,
            min_market_cap=1_000_000.0,
            max_market_cap=10_000_000.0,
            min_pe=10.0,
            max_pe=25.0,
        )
        self.assertEqual(req.min_price, 10.0)
        self.assertEqual(req.max_price, 100.0)

        # Equal bounds are allowed
        req_eq = ScreenerQueryRequest(min_price=50.0, max_price=50.0)
        self.assertEqual(req_eq.min_price, req_eq.max_price)

        # Inverted bounds raise ValidationError
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_price=50.0, max_price=49.9)

        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_change=1.0, max_change=0.5)

        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_market_cap=100.0, max_market_cap=99.0)

        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_pe=30.0, max_pe=20.0)

    def test_r27_screener_filter_schema_bounds(self) -> None:
        """Verify ScreenerFilterSchema validates min <= max across all metric ranges."""
        schema = ScreenerFilterSchema(min_price=10.0, max_price=20.0)
        self.assertEqual(schema.min_price, 10.0)

        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_price=25.0, max_price=20.0)

        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_change=10.0, max_change=-10.0)

        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_market_cap=1000.0, max_market_cap=500.0)

        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_pe=50.0, max_pe=10.0)

    def test_r27_stock_query_requests_symbol_normalization(self) -> None:
        """Verify StockHistoryQueryRequest and StockDetailsQueryRequest normalize symbol."""
        hist = StockHistoryQueryRequest(symbol="  nvda  ", market="us")
        self.assertEqual(hist.symbol, "NVDA")

        details = StockDetailsQueryRequest(symbol="  7203.t  ", market="jp")
        self.assertEqual(details.symbol, "7203.T")

        with self.assertRaises(ValidationError):
            StockHistoryQueryRequest(symbol="   ", market="us")

        with self.assertRaises(ValidationError):
            StockDetailsQueryRequest(symbol="   ", market="jp")

    def test_r28_copy_ai_portfolio_cache_fx_isolation(self) -> None:
        """Verify api_copy_ai_portfolio_to_my purges avg_fx_rate from caches for non-US markets."""
        client = app.test_client()

        # Seed in-memory caches with a non-US item that has a stale avg_fx_rate
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache["jp"] = [
                {"symbol": "9984.T", "name": "SoftBank", "shares": 10, "avg_fx_rate": 155.0}
            ]
            app_state.market.target_stocks_cache["jp"] = [
                {"symbol": "9984.T", "name": "SoftBank", "shares": 10, "avg_fx_rate": 155.0}
            ]

        # Ensure 9984.T is not in user_stocks so copy succeeds
        with app_state.market.user_stocks_lock:
            app_state.market.user_jp.pop("9984.T", None)

        test_payload = {
            "items": [
                {
                    "symbol": "9984.T",
                    "market": "jp",
                    "target_price": 8500.0,
                    "weight_pct": 20.0,
                }
            ]
        }

        with (
            patch("routes.stocks.ai_portfolio.save_user_stocks", return_value=None),
            patch("routes.stocks.ai_portfolio._announce_watchlist_state"),
            patch("routes.stocks.ai_portfolio.schedule_sync_all_stocks_now"),
        ):
            resp = client.post(
                "/api/ai-portfolio/copy-to-my",
                json=test_payload,
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            data = resp.get_json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("added_count"), 1)

            # Verify that avg_fx_rate was purged from current and target caches
            with app_state.cache.sse_data_lock:
                for cache in (
                    app_state.market.current_stocks_cache["jp"],
                    app_state.market.target_stocks_cache["jp"],
                ):
                    matched = [s for s in cache if s.get("symbol") == "9984.T"]
                    self.assertEqual(len(matched), 1)
                    item = matched[0]
                    self.assertNotIn("avg_fx_rate", item)
                    self.assertGreater(item.get("shares"), 0)
                    self.assertEqual(item.get("avg_price"), 8500.0)

            # Clean up user_stocks
            with app_state.market.user_stocks_lock:
                app_state.market.user_jp.pop("9984.T", None)

    def test_r29_screener_and_dashboard_keyboard_nav_attributes(self) -> None:
        """Verify HTML markup and script references for roving keyboard navigation."""
        client = app.test_client()

        # Screener page markup
        resp_screener = client.get("/screener")
        self.assertEqual(resp_screener.status_code, 200)
        html_screener = resp_screener.get_data(as_text=True)
        self.assertIn('id="screenerMarketToggle"', html_screener)
        self.assertIn('id="screenerChangePreset"', html_screener)
        self.assertIn('role="group"', html_screener)

        # Dashboard page markup
        resp_index = client.get("/main")
        self.assertEqual(resp_index.status_code, 200)
        html_index = resp_index.get_data(as_text=True)
        self.assertIn('id="sseModeSelector"', html_index)
        self.assertIn('role="group"', html_index)

        # Verify static JS files contain roving keyboard navigation handlers
        with open("static/js/screener.js", encoding="utf-8") as f:
            screener_js = f.read()
            self.assertIn("setupButtonGroupKeyboardNav", screener_js)
            self.assertIn("ArrowLeft", screener_js)
            self.assertIn("ArrowRight", screener_js)
            self.assertIn("Home", screener_js)
            self.assertIn("End", screener_js)

        with open("static/js/index_main.js", encoding="utf-8") as f:
            index_js = f.read()
            self.assertIn(".sse-mode-btn", index_js)
            self.assertIn("ArrowLeft", index_js)
            self.assertIn("ArrowRight", index_js)
            self.assertIn("Home", index_js)
            self.assertIn("End", index_js)


if __name__ == "__main__":
    unittest.main()
