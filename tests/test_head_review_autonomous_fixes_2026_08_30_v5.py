"""Regression test suite for autonomous code review fixes (2026-08-30 v5).

Validates:
1. Finding R1: _tool_get_stock_quote resolves live price, change, volume via snapshot/cache fallbacks, and _tool_get_company_fundamentals extracts earningsPerShare.
2. Finding R2: _tool_calculate_technical_levels computes valid RSI 14 on short histories (length 14) without NaN fallback.
3. Finding R3: ScreenerQueryRequest and ScreenerFilterSchema accept extended query filters and sort options.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from schemas.stocks import ScreenerQueryRequest
from services.ai_tools import (
    _tool_calculate_technical_levels,
    _tool_get_company_fundamentals,
    _tool_get_stock_quote,
)
from utils.validators import ScreenerFilterSchema


class TestHeadReviewAutonomousFixesV5(unittest.TestCase):
    """Regression tests for R1, R2, R3 fixes."""

    def test_r1_tool_get_stock_quote_resolves_realtime_snapshot(self):
        """Verify _tool_get_stock_quote retrieves live price and change from realtime snapshot."""
        mock_info = {
            "name": "NVIDIA Corporation",
            "regularMarketPreviousClose": 120.0,
            "currency": "USD",
        }
        mock_snapshot = {
            "NVDA": {
                "symbol": "NVDA",
                "price": 128.5,
                "change": 8.5,
                "change_pct": 7.08,
                "volume": 50000000,
                "high": 130.0,
                "low": 125.0,
                "open": 126.0,
                "timestamp": 1700000000.0,
            }
        }

        with patch("utils.stock_payload.get_stock_info_cached", return_value=mock_info):
            with patch(
                "services.realtime_engine.realtime_market_engine.get_market_snapshot",
                return_value=mock_snapshot,
            ):
                res = _tool_get_stock_quote({"symbol": "NVDA", "market": "us"})
                self.assertEqual(res["symbol"], "NVDA")
                self.assertEqual(res["price"], 128.5)
                self.assertEqual(res["change"], 8.5)
                self.assertEqual(res["change_pct"], 7.08)
                self.assertEqual(res["volume"], 50000000)
                self.assertEqual(res["open"], 126.0)

    def test_r1_tool_get_stock_quote_resolves_disk_payload_cache(self):
        """Verify _tool_get_stock_quote falls back to disk payload cache when snapshot is empty."""
        mock_info = {
            "name": "Sony Group",
            "currency": "JPY",
        }
        mock_cached_payload = {
            "symbol": "6758.T",
            "name": "ソニーグループ",
            "price": 13500.0,
            "change": 150.0,
            "change_percent": 1.12,
            "volume": 3000000,
            "high": 13600.0,
            "low": 13400.0,
            "open": 13450.0,
            "updated_at": "2026-08-30 15:00:00",
        }

        with patch("utils.stock_payload.get_stock_info_cached", return_value=mock_info):
            with patch(
                "services.realtime_engine.realtime_market_engine.get_market_snapshot",
                return_value={},
            ):
                with patch(
                    "app_state.app_state.payload_disk_cache.get", return_value=mock_cached_payload
                ):
                    res = _tool_get_stock_quote({"symbol": "6758.T", "market": "jp"})
                    self.assertEqual(res["symbol"], "6758.T")
                    self.assertEqual(res["price"], 13500.0)
                    self.assertEqual(res["change"], 150.0)
                    self.assertEqual(res["change_pct"], 1.12)
                    self.assertEqual(res["high"], 13600.0)

    def test_r1_tool_get_company_fundamentals_extracts_earnings_per_share(self):
        """Verify _tool_get_company_fundamentals extracts EPS from earningsPerShare key."""
        mock_info = {
            "name": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "marketCap": 3000000000000,
            "trailingPE": 30.5,
            "forwardPE": 28.0,
            "priceToBook": 45.0,
            "dividendYield": 0.005,
            "earningsPerShare": 6.42,
            "fiftyTwoWeekHigh": 235.0,
            "fiftyTwoWeekLow": 165.0,
        }

        with patch("utils.stock_payload.get_stock_info_cached", return_value=mock_info):
            res = _tool_get_company_fundamentals({"symbol": "AAPL"})
            self.assertEqual(res["symbol"], "AAPL")
            self.assertEqual(res["eps"], 6.42)
            self.assertEqual(res["sector"], "Technology")
            self.assertEqual(res["market_cap"], 3000000000000)

    def test_r2_tool_calculate_technical_levels_rsi14_exact_length(self):
        """Verify RSI calculation computes accurately for exactly 14 records without NaN."""
        prices = [100.0 + i * 2.0 for i in range(14)]
        dates = pd.date_range("2026-08-01", periods=14, freq="D")
        df = pd.DataFrame(
            {"Close": prices, "Open": prices, "High": prices, "Low": prices}, index=dates
        )

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df

        with patch("utils.market_utils.safe_get_ticker", return_value=mock_ticker):
            res = _tool_calculate_technical_levels({"symbol": "AAPL", "period": "1mo"})
            self.assertNotIn("error", res)
            self.assertEqual(res["symbol"], "AAPL")
            self.assertEqual(res["current_price"], 126.0)
            self.assertEqual(res["rsi_14"], 100.0)

    def test_r3_screener_schemas_accept_extended_parameters(self):
        """Verify ScreenerQueryRequest and ScreenerFilterSchema accept extended filters."""
        req = ScreenerQueryRequest(
            market="us",
            sector="Technology",
            q="AI",
            sort_by="pe_ratio",
            sort_order="asc",
            min_price=10.0,
            max_price=500.0,
            min_market_cap=1_000_000_000.0,
            max_market_cap=100_000_000_000.0,
            min_pe=5.0,
            max_pe=30.0,
            limit=25,
        )
        self.assertEqual(req.sort_by, "pe_ratio")
        self.assertEqual(req.min_market_cap, 1_000_000_000.0)
        self.assertEqual(req.max_pe, 30.0)
        self.assertEqual(req.limit, 25)

        validator_req = ScreenerFilterSchema(
            market="jp",
            sector="Finance",
            q="Bank",
            sort_by="change_pct",
            sort_order="desc",
            min_price=500.0,
            max_price=10000.0,
            min_market_cap=50_000_000.0,
            max_market_cap=5_000_000_000.0,
            min_pe=10.0,
            max_pe=50.0,
            limit=100,
        )
        self.assertEqual(validator_req.sort_by, "change_pct")
        self.assertEqual(validator_req.q, "Bank")
        self.assertEqual(validator_req.limit, 100)


if __name__ == "__main__":
    unittest.main()
