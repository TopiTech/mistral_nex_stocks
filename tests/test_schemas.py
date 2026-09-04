# tests/test_schemas.py
"""Unit tests for Pydantic request/response and configuration schemas."""

import unittest

from pydantic import ValidationError

from schemas.ai_portfolio import (
    AIPortfolioGenerateRequest,
    AIPortfolioSaveRequest,
)
from schemas.config import AppConfigSchema
from schemas.stocks import (
    PortfolioUpdateRequest,
    ScreenerQueryRequest,
    StockAddExtRequest,
    StockAddRequest,
    StockDeleteRequest,
    StockHistoryQueryRequest,
)


class TestSchemas(unittest.TestCase):
    def test_stock_add_request_valid(self):
        req = StockAddRequest(symbol="aapl", name="Apple Inc.", market="us")
        self.assertEqual(req.symbol, "AAPL")
        self.assertEqual(req.name, "Apple Inc.")
        self.assertEqual(req.market, "us")

    def test_stock_add_request_invalid_market(self):
        with self.assertRaises(ValidationError):
            StockAddRequest(symbol="AAPL", name="Apple", market="invalid")

    def test_stock_add_request_blank_symbol(self):
        with self.assertRaises(ValidationError):
            StockAddRequest(symbol="   ", name="Apple", market="us")

    def test_stock_add_ext_request_defaults(self):
        req = StockAddExtRequest(symbol="7203.t")
        self.assertEqual(req.symbol, "7203.T")
        self.assertEqual(req.market, "us")
        self.assertIsNone(req.name)

    def test_stock_delete_request(self):
        req = StockDeleteRequest(symbol="MSFT", market="us")
        self.assertEqual(req.symbol, "MSFT")
        self.assertEqual(req.market, "us")

    def test_portfolio_update_request_validation(self):
        req = PortfolioUpdateRequest(
            symbol="NVDA", market="us", shares=10.5, avg_price=120.0, avg_fx_rate=155.0
        )
        self.assertEqual(req.shares, 10.5)
        self.assertEqual(req.avg_price, 120.0)

    def test_portfolio_update_negative_shares_rejected(self):
        with self.assertRaises(ValidationError):
            PortfolioUpdateRequest(symbol="NVDA", market="us", shares=-5.0, avg_price=100.0)

    def test_screener_query_defaults(self):
        req = ScreenerQueryRequest()
        self.assertEqual(req.market, "all")
        self.assertEqual(req.sort_by, "market_cap")
        self.assertEqual(req.sort_order, "desc")

    def test_stock_history_query(self):
        req = StockHistoryQueryRequest(symbol="TSLA", market="us", period="1mo")
        self.assertEqual(req.period, "1mo")
        with self.assertRaises(ValidationError):
            StockHistoryQueryRequest(symbol="TSLA", period="invalid_period")

    def test_ai_portfolio_generate_request(self):
        req = AIPortfolioGenerateRequest(theme="  Renewable Energy  ")
        self.assertEqual(req.theme, "Renewable Energy")
        with self.assertRaises(ValidationError):
            AIPortfolioGenerateRequest(theme="   ")

    def test_ai_portfolio_save_request(self):
        req = AIPortfolioSaveRequest(
            theme="quantum", name="Quantum Computing", portfolio={"items": []}
        )
        self.assertEqual(req.theme, "quantum")

    def test_app_config_schema_defaults(self):
        cfg = AppConfigSchema()
        self.assertEqual(cfg.port, 5000)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertTrue(cfg.simulate_fluctuation)
        self.assertTrue(cfg.security.csrf_enabled)


if __name__ == "__main__":
    unittest.main()
