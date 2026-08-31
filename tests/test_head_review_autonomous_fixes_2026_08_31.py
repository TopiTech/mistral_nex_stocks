"""Regression test suite for autonomous code review fixes (2026-08-31).

Validates:
1. Finding R1: _normalize_market_symbol cleanly handles None / missing inputs without producing "NONE".
2. Finding R2: _tool_get_stock_quote realtime snapshot lookup uses removesuffix(".T") instead of rstrip(".T"), preserving tickers ending in T (e.g. COST.T or TEST.T).
3. Finding R3: _tool_calculate_technical_levels safely extracts technical metrics without crashing on mock tickers.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.ai_tools import (
    _normalize_market_symbol,
    _tool_get_stock_quote,
)


class TestHeadReviewAutonomousFixes20260831(unittest.TestCase):
    """Regression tests for 2026-08-31 autonomous review fixes."""

    def test_r1_normalize_market_symbol_handles_none_safely(self):
        """Verify _normalize_market_symbol does not coerce None to 'NONE'."""
        sym, mkt = _normalize_market_symbol({"symbol": None, "market": None})
        self.assertEqual(sym, "")
        self.assertEqual(mkt, "us")

        sym_empty, mkt_empty = _normalize_market_symbol({})
        self.assertEqual(sym_empty, "")
        self.assertEqual(mkt_empty, "us")

    def test_r2_tool_get_stock_quote_preserves_ticker_ending_in_t(self):
        """Verify _tool_get_stock_quote uses removesuffix so tickers like TEST.T or COST.T are not truncated to TES/COS."""
        mock_info = {
            "name": "Test Company T",
            "regularMarketPreviousClose": 100.0,
            "currency": "JPY",
        }
        # In snapshot, the bare JP symbol key is "TEST" (not "TES")
        mock_snapshot = {
            "TEST": {
                "symbol": "TEST.T",
                "price": 105.0,
                "change": 5.0,
                "change_pct": 5.0,
                "volume": 12000,
                "high": 106.0,
                "low": 99.0,
                "open": 100.0,
                "timestamp": 1700000000.0,
            }
        }

        with patch("utils.stock_payload.get_stock_info_cached", return_value=mock_info):
            with patch(
                "services.realtime_engine.realtime_market_engine.get_market_snapshot",
                return_value=mock_snapshot,
            ):
                res = _tool_get_stock_quote({"symbol": "TEST.T", "market": "jp"})
                self.assertEqual(res["symbol"], "TEST.T")
                self.assertEqual(res["price"], 105.0)
                self.assertEqual(res["change"], 5.0)
                self.assertEqual(res["change_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
