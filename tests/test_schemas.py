# tests/test_schemas.py
"""Unit tests for Pydantic request/response and configuration schemas."""

import unittest

from pydantic import ValidationError

from schemas.ai_portfolio import (
    AIPortfolioCopyToMyItem,
    AIPortfolioCopyToMyRequest,
    AIPortfolioDeleteRequest,
    AIPortfolioGenerateRequest,
    AIPortfolioItemSchema,
    AIPortfolioSaveRequest,
)
from schemas.analysis import (
    AIAnalyzeChartImageRequest,
    AIAnalyzeV2Request,
    AIChatRequest,
    AINewsRequest,
    AITechnicalLinesRequest,
)
from schemas.config import AppConfigSchema
from schemas.stocks import (
    HeatmapQueryRequest,
    PortfolioUpdateRequest,
    ScreenerQueryRequest,
    StockAddExtRequest,
    StockAddRequest,
    StockDeleteRequest,
    StockDetailsQueryRequest,
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

    def test_stock_mutation_requests_accept_idx_market(self):
        self.assertEqual(
            StockAddRequest(symbol="^N225", name="Nikkei 225", market="idx").market,
            "idx",
        )
        self.assertEqual(StockAddExtRequest(symbol="^N225", market="idx").market, "idx")
        self.assertEqual(StockDeleteRequest(symbol="^N225", market="idx").market, "idx")
        self.assertEqual(
            PortfolioUpdateRequest(
                symbol="^N225", market="idx", shares=1.0, avg_price=38_000.0
            ).market,
            "idx",
        )

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

    def test_portfolio_update_rejects_zero_or_non_finite_fx_rate(self):
        with self.assertRaises(ValidationError):
            PortfolioUpdateRequest(
                symbol="NVDA", market="us", shares=1.0, avg_price=100.0, avg_fx_rate=0.0
            )
        with self.assertRaises(ValidationError):
            PortfolioUpdateRequest(symbol="NVDA", market="us", shares=float("nan"), avg_price=100.0)

    def test_portfolio_update_rejects_fx_rate_for_non_us_market(self):
        with self.assertRaises(ValidationError):
            PortfolioUpdateRequest(
                symbol="^N225", market="idx", shares=1.0, avg_price=38000.0, avg_fx_rate=150.0
            )
        with self.assertRaises(ValidationError):
            PortfolioUpdateRequest(
                symbol="7203.T", market="jp", shares=100.0, avg_price=2000.0, avg_fx_rate=150.0
            )

    def test_screener_query_defaults(self):
        req = ScreenerQueryRequest()
        self.assertEqual(req.market, "all")
        self.assertEqual(req.sort_by, "market_cap")
        self.assertEqual(req.sort_order, "desc")

    def test_screener_query_validates_bounds(self):
        # Valid bounds
        req = ScreenerQueryRequest(
            min_price=10.0,
            max_price=100.0,
            min_change=-5.0,
            max_change=5.0,
            min_market_cap=1000.0,
            max_market_cap=5000.0,
            min_pe=10.0,
            max_pe=30.0,
        )
        self.assertEqual(req.min_price, 10.0)

        # Zero bounds should be accepted
        req_zero = ScreenerQueryRequest(
            min_price=0.0,
            max_price=0.0,
            min_market_cap=0.0,
            max_market_cap=0.0,
            min_pe=0.0,
            max_pe=0.0,
        )
        self.assertEqual(req_zero.min_price, 0.0)
        self.assertEqual(req_zero.max_price, 0.0)
        self.assertEqual(req_zero.min_market_cap, 0.0)
        self.assertEqual(req_zero.max_market_cap, 0.0)
        self.assertEqual(req_zero.min_pe, 0.0)
        self.assertEqual(req_zero.max_pe, 0.0)

        # Inverted bounds
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_price=100.0, max_price=10.0)
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_change=10.0, max_change=-5.0)
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_market_cap=5000.0, max_market_cap=1000.0)
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_pe=30.0, max_pe=10.0)

    def test_stock_history_query(self):
        req = StockHistoryQueryRequest(symbol="  tsla  ", market="us", period="1mo")
        self.assertEqual(req.symbol, "TSLA")
        self.assertEqual(req.period, "1mo")
        with self.assertRaises(ValidationError):
            StockHistoryQueryRequest(symbol="TSLA", period="invalid_period")
        with self.assertRaises(ValidationError):
            StockHistoryQueryRequest(symbol="   ", market="us")

    def test_stock_details_query(self):
        req = StockDetailsQueryRequest(symbol="  aapl  ", market="us")
        self.assertEqual(req.symbol, "AAPL")
        with self.assertRaises(ValidationError):
            StockDetailsQueryRequest(symbol="   ", market="us")

    def test_ai_portfolio_generate_request(self):
        req = AIPortfolioGenerateRequest(theme="  Renewable Energy  ")
        self.assertEqual(req.theme, "Renewable Energy")
        with self.assertRaises(ValidationError):
            AIPortfolioGenerateRequest(theme="   ")

    def test_ai_portfolio_item_rejects_non_finite_numbers(self):
        with self.assertRaises(ValidationError):
            AIPortfolioItemSchema(symbol="AAPL", target_price=float("inf"))

    def test_ai_portfolio_save_request(self):
        req = AIPortfolioSaveRequest(
            theme="quantum", name="Quantum Computing", portfolio={"items": []}
        )
        self.assertEqual(req.theme, "quantum")

    def test_screener_query_limit_bounds(self):
        req_default = ScreenerQueryRequest()
        self.assertEqual(req_default.limit, 150)
        req_500 = ScreenerQueryRequest(limit=500)
        self.assertEqual(req_500.limit, 500)
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(limit=0)
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(limit=501)

    def test_heatmap_query_request(self):
        req_default = HeatmapQueryRequest()
        self.assertEqual(req_default.market, "us")
        req_jp = HeatmapQueryRequest(market="jp")
        self.assertEqual(req_jp.market, "jp")
        with self.assertRaises(ValidationError):
            HeatmapQueryRequest(market="all")
        with self.assertRaises(ValidationError):
            HeatmapQueryRequest(market="invalid")

    def test_ai_portfolio_delete_request(self):
        req = AIPortfolioDeleteRequest(id="  custom_123  ")
        self.assertEqual(req.id, "custom_123")
        with self.assertRaises(ValidationError):
            AIPortfolioDeleteRequest(id="   ")
        with self.assertRaises(ValidationError):
            AIPortfolioDeleteRequest(id="")
        with self.assertRaises(ValidationError):
            AIPortfolioDeleteRequest(id="a" * 257)

    def test_ai_portfolio_copy_to_my_request(self):
        item = AIPortfolioCopyToMyItem(symbol="  nvda  ", market="us", weight_pct=25.0, target_price=130.0)
        self.assertEqual(item.symbol, "NVDA")
        self.assertEqual(item.market, "us")
        req = AIPortfolioCopyToMyRequest(items=[item])
        self.assertEqual(len(req.items), 1)

        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyRequest(items=[])

        too_many = [AIPortfolioCopyToMyItem(symbol=f"SYM{i}", market="us") for i in range(21)]
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyRequest(items=too_many)

    def test_stock_name_max_length_boundary(self):
        valid_name = "N" * 200
        invalid_name = "N" * 201
        req_add = StockAddRequest(symbol="AAPL", name=valid_name, market="us")
        self.assertEqual(req_add.name, valid_name)
        with self.assertRaises(ValidationError):
            StockAddRequest(symbol="AAPL", name=invalid_name, market="us")

        req_ext = StockAddExtRequest(symbol="AAPL", name=valid_name, market="us")
        self.assertEqual(req_ext.name, valid_name)
        with self.assertRaises(ValidationError):
            StockAddExtRequest(symbol="AAPL", name=invalid_name, market="us")

        ai_item = AIPortfolioItemSchema(symbol="AAPL", name=valid_name)
        self.assertEqual(ai_item.name, valid_name)
        with self.assertRaises(ValidationError):
            AIPortfolioItemSchema(symbol="AAPL", name=invalid_name)

        copy_item = AIPortfolioCopyToMyItem(symbol="AAPL", name=valid_name)
        self.assertEqual(copy_item.name, valid_name)
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="AAPL", name=invalid_name)

    def test_ai_portfolio_target_price_upper_bound(self):
        ai_item = AIPortfolioItemSchema(symbol="AAPL", target_price=1_000_000_000.0)
        self.assertEqual(ai_item.target_price, 1_000_000_000.0)
        with self.assertRaises(ValidationError):
            AIPortfolioItemSchema(symbol="AAPL", target_price=1_000_000_000.1)

        copy_item = AIPortfolioCopyToMyItem(symbol="AAPL", target_price=1_000_000_000.0)
        self.assertEqual(copy_item.target_price, 1_000_000_000.0)
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="AAPL", target_price=1_000_000_000.1)

    def test_ai_portfolio_copy_to_my_duplicates_and_weight_limit(self):
        item1 = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=50.0)
        item2 = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=30.0)
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyRequest(items=[item1, item2])

        item_jp = AIPortfolioCopyToMyItem(symbol="7203.T", market="jp", weight_pct=50.0)
        req_valid = AIPortfolioCopyToMyRequest(items=[item1, item_jp])
        self.assertEqual(len(req_valid.items), 2)

        item_heavy = AIPortfolioCopyToMyItem(symbol="MSFT", market="us", weight_pct=50.0)
        item_heavy2 = AIPortfolioCopyToMyItem(symbol="GOOG", market="us", weight_pct=51.0)
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyRequest(items=[item_heavy, item_heavy2])

    def test_ai_chat_request_schema(self):
        token = "a" * 24
        req = AIChatRequest(symbol="aapl", market="us", message="Hello", request_token=token)
        self.assertEqual(req.symbol, "AAPL")
        self.assertEqual(req.message, "Hello")

        with self.assertRaises(ValidationError):
            AIChatRequest(symbol="AAPL", message="   ", request_token=token)
        with self.assertRaises(ValidationError):
            AIChatRequest(symbol="AAPL", message="Hi", request_token="short")

    def test_ai_analyze_v2_request_schema(self):
        token = "b" * 32
        req = AIAnalyzeV2Request(
            symbol="NVDA",
            market="us",
            name="Nvidia",
            price=125.5,
            chart_data=[{"close": 125}],
            request_token=token,
        )
        self.assertEqual(req.symbol, "NVDA")
        self.assertEqual(req.price, 125.5)

        with self.assertRaises(ValidationError):
            AIAnalyzeV2Request(symbol="NVDA", request_token="invalid token with spaces")

    def test_ai_news_request_schema(self):
        req_default = AINewsRequest()
        self.assertFalse(req_default.force)
        req_force = AINewsRequest(force=True)
        self.assertTrue(req_force.force)

    def test_ai_technical_lines_request_schema(self):
        req = AITechnicalLinesRequest(
            symbol="msft",
            market="us",
            period="1mo",
            history_data=[{"close": 400}],
        )
        self.assertEqual(req.symbol, "MSFT")
        self.assertEqual(req.period, "1mo")

        with self.assertRaises(ValidationError):
            AITechnicalLinesRequest(symbol="MSFT", period="invalid_period")

    def test_ai_analyze_chart_image_request_schema(self):
        req = AIAnalyzeChartImageRequest(image_data="data:image/png;base64,ABCDEF")
        self.assertEqual(req.image_data, "data:image/png;base64,ABCDEF")

        req_alias = AIAnalyzeChartImageRequest(image="ABCDEF")
        self.assertEqual(req_alias.image, "ABCDEF")

        with self.assertRaises(ValidationError):
            AIAnalyzeChartImageRequest(image_data="", image="")

    def test_app_config_schema_defaults(self):
        cfg = AppConfigSchema()
        self.assertEqual(cfg.port, 5000)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertTrue(cfg.simulate_fluctuation)
        self.assertTrue(cfg.security.csrf_enabled)


if __name__ == "__main__":
    unittest.main()

