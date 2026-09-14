# tests/test_code_review_goal_audit_2026_09_v7.py
"""Regression tests for Code Review & Hardening Audit (R33-R35).

Covers:
- R33: Schema and contract alignment for MAX_STOCK_NAME_LENGTH, PORTFOLIO_AVG_PRICE_MAX,
  and AIPortfolioCopyToMyRequest validation.
- R34: Analysis endpoint request validation schemas (schemas/analysis.py).
- R35: Accessibility and keyboard navigation in frontend scripts (roving tabindex and
  4-way arrow keys).
"""

from __future__ import annotations

import pathlib
import unittest

from pydantic import ValidationError

import schemas
from constants import (
    CHART_IMAGE_MAX_IMAGE_DATA_CHARS,
    CHAT_MAX_MSG_LENGTH,
    MAX_STOCK_NAME_LENGTH,
    PORTFOLIO_AVG_PRICE_MAX,
)
from schemas.ai_portfolio import (
    AIPortfolioCopyToMyItem,
    AIPortfolioCopyToMyRequest,
    AIPortfolioItemSchema,
)
from schemas.analysis import (
    AIAnalyzeChartImageRequest,
    AIAnalyzeV2Request,
    AIChatRequest,
    AINewsRequest,
    AITechnicalLinesRequest,
)
from schemas.stocks import StockAddExtRequest, StockAddRequest


class TestCodeReviewAuditV7(unittest.TestCase):
    """Test suite for findings R33, R34, and R35."""

    # -------------------------------------------------------------------------
    # R33: Schema & Contract Alignment
    # -------------------------------------------------------------------------
    def test_r33_max_stock_name_length_consistency(self):
        """MAX_STOCK_NAME_LENGTH is imported and used in schemas."""
        self.assertEqual(MAX_STOCK_NAME_LENGTH, 200)

        # Exact 200-char name accepted
        name_200 = "あ" * 200
        req_add = StockAddRequest(symbol="7203.T", name=name_200, market="jp")
        self.assertEqual(req_add.name, name_200)

        req_add_ext = StockAddExtRequest(symbol="7203.T", name=name_200, market="jp")
        self.assertEqual(req_add_ext.name, name_200)

        item_schema = AIPortfolioItemSchema(symbol="7203.T", name=name_200)
        self.assertEqual(item_schema.name, name_200)

        copy_item = AIPortfolioCopyToMyItem(symbol="7203.T", name=name_200)
        self.assertEqual(copy_item.name, name_200)

        # 201-char name rejected
        name_201 = "あ" * 201
        with self.assertRaises(ValidationError):
            StockAddRequest(symbol="7203.T", name=name_201, market="jp")

        with self.assertRaises(ValidationError):
            StockAddExtRequest(symbol="7203.T", name=name_201, market="jp")

        with self.assertRaises(ValidationError):
            AIPortfolioItemSchema(symbol="7203.T", name=name_201)

        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="7203.T", name=name_201)

    def test_r33_portfolio_avg_price_max_bounds(self):
        """target_price upper bound is enforced via PORTFOLIO_AVG_PRICE_MAX."""
        valid_item = AIPortfolioCopyToMyItem(
            symbol="NVDA", market="us", target_price=PORTFOLIO_AVG_PRICE_MAX
        )
        self.assertEqual(valid_item.target_price, PORTFOLIO_AVG_PRICE_MAX)

        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(
                symbol="NVDA", market="us", target_price=PORTFOLIO_AVG_PRICE_MAX + 1.0
            )

        with self.assertRaises(ValidationError):
            AIPortfolioItemSchema(
                symbol="NVDA", market="us", target_price=PORTFOLIO_AVG_PRICE_MAX + 1.0
            )

    def test_r33_copy_to_my_request_validations(self):
        """AIPortfolioCopyToMyRequest validates duplicate symbols and weight sums."""
        item_a = AIPortfolioCopyToMyItem(symbol="NVDA", market="us", weight_pct=60.0)
        item_b = AIPortfolioCopyToMyItem(symbol="nvda", market="us", weight_pct=20.0)
        with self.assertRaises(ValidationError) as ctx:
            AIPortfolioCopyToMyRequest(items=[item_a, item_b])
        self.assertIn("Duplicate stock", str(ctx.exception))

        # Different markets with same symbol allowed
        item_c = AIPortfolioCopyToMyItem(symbol="NVDA", market="jp", weight_pct=30.0)
        req_diff_markets = AIPortfolioCopyToMyRequest(items=[item_a, item_c])
        self.assertEqual(len(req_diff_markets.items), 2)

        # Over 100.5% total weight rejected
        item_d = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=50.0)
        item_e = AIPortfolioCopyToMyItem(symbol="MSFT", market="us", weight_pct=50.6)
        with self.assertRaises(ValidationError) as ctx2:
            AIPortfolioCopyToMyRequest(items=[item_d, item_e])
        self.assertIn("Total weight_pct exceeds 100%", str(ctx2.exception))

        # Within 100.5% accepted (allows slight rounding tolerance)
        item_f = AIPortfolioCopyToMyItem(symbol="MSFT", market="us", weight_pct=50.5)
        req_tolerance = AIPortfolioCopyToMyRequest(items=[item_d, item_f])
        self.assertEqual(len(req_tolerance.items), 2)

    # -------------------------------------------------------------------------
    # R34: Analysis Schemas & Export
    # -------------------------------------------------------------------------
    def test_r34_analysis_schemas_reexported(self):
        """schemas package exports all analysis request schemas."""
        self.assertTrue(hasattr(schemas, "AIChatRequest"))
        self.assertTrue(hasattr(schemas, "AIAnalyzeV2Request"))
        self.assertTrue(hasattr(schemas, "AINewsRequest"))
        self.assertTrue(hasattr(schemas, "AITechnicalLinesRequest"))
        self.assertTrue(hasattr(schemas, "AIAnalyzeChartImageRequest"))

    def test_r34_ai_chat_request(self):
        """AIChatRequest validates message length and idempotency token."""
        valid_token = "tok_" + "x" * 20
        req = AIChatRequest(
            symbol="msft",
            market="us",
            message="What is the forecast?",
            request_token=valid_token,
        )
        self.assertEqual(req.symbol, "MSFT")
        self.assertEqual(req.market, "us")
        self.assertEqual(req.message, "What is the forecast?")

        # Invalid token formats rejected
        with self.assertRaises(ValidationError):
            AIChatRequest(
                symbol="MSFT",
                message="Hi",
                request_token="too-short",
            )
        with self.assertRaises(ValidationError):
            AIChatRequest(
                symbol="MSFT",
                message="Hi",
                request_token="bad token with spaces" * 2,
            )

        # Max message length
        long_msg = "A" * (CHAT_MAX_MSG_LENGTH + 1)
        with self.assertRaises(ValidationError):
            AIChatRequest(
                symbol="MSFT",
                message=long_msg,
                request_token=valid_token,
            )

    def test_r34_ai_analyze_v2_request(self):
        """AIAnalyzeV2Request handles chart_data, name length, and price bounds."""
        valid_token = "tok_" + "y" * 20
        req = AIAnalyzeV2Request(
            symbol="aapl",
            market="us",
            name="Apple Inc.",
            price=220.5,
            chart_data=[{"date": "2026-09-01", "close": 220.0}],
            request_token=valid_token,
        )
        self.assertEqual(req.symbol, "AAPL")
        self.assertEqual(req.price, 220.5)
        self.assertEqual(len(req.chart_data), 1)

        # Negative price rejected
        with self.assertRaises(ValidationError):
            AIAnalyzeV2Request(
                symbol="AAPL",
                price=-10.0,
                request_token=valid_token,
            )

    def test_r34_ai_news_request(self):
        """AINewsRequest handles force boolean flag."""
        req = AINewsRequest(force=True)
        self.assertTrue(req.force)
        req_def = AINewsRequest()
        self.assertFalse(req_def.force)

    def test_r34_ai_technical_lines_request(self):
        """AITechnicalLinesRequest enforces valid period literal."""
        req = AITechnicalLinesRequest(symbol="7203.T", market="jp", period="6mo")
        self.assertEqual(req.period, "6mo")

        with self.assertRaises(ValidationError):
            AITechnicalLinesRequest(symbol="7203.T", period="10y")

    def test_r34_ai_analyze_chart_image_request(self):
        """AIAnalyzeChartImageRequest requires image_data or image alias."""
        req1 = AIAnalyzeChartImageRequest(image_data="data:image/png;base64,iVBORw0KGgo=")
        self.assertEqual(req1.image_data, "data:image/png;base64,iVBORw0KGgo=")

        req2 = AIAnalyzeChartImageRequest(image="iVBORw0KGgo=")
        self.assertEqual(req2.image, "iVBORw0KGgo=")

        with self.assertRaises(ValidationError):
            AIAnalyzeChartImageRequest()

        with self.assertRaises(ValidationError):
            AIAnalyzeChartImageRequest(image_data="   ")

        # Exceeding max char limit rejected
        oversized = "a" * (CHART_IMAGE_MAX_IMAGE_DATA_CHARS + 1)
        with self.assertRaises(ValidationError):
            AIAnalyzeChartImageRequest(image_data=oversized)

    # -------------------------------------------------------------------------
    # R35: Frontend Roving Tabindex & 4-Way Arrow Navigation
    # -------------------------------------------------------------------------
    def test_r35_screener_js_keyboard_nav_and_tabindex(self):
        """screener.js implements 4-way arrow keys and roving tabindex."""
        js_path = pathlib.Path(__file__).parent.parent / "static" / "js" / "screener.js"
        content = js_path.read_text(encoding="utf-8")

        self.assertIn('"ArrowUp"', content)
        self.assertIn('"ArrowDown"', content)
        self.assertIn('b.setAttribute("tabindex", i === nextIndex ? "0" : "-1")', content)
        self.assertIn('b.setAttribute("tabindex", active ? "0" : "-1")', content)
        self.assertIn('b.setAttribute("tabindex", isActive ? "0" : "-1")', content)

    def test_r35_heatmap_js_keyboard_nav_and_tabindex(self):
        """heatmap.js implements 4-way arrow keys and roving tabindex."""
        js_path = pathlib.Path(__file__).parent.parent / "static" / "js" / "heatmap.js"
        content = js_path.read_text(encoding="utf-8")

        self.assertIn('"ArrowUp"', content)
        self.assertIn('"ArrowDown"', content)
        self.assertIn('b.setAttribute("tabindex", i === nextIndex ? "0" : "-1")', content)
        self.assertIn('els.toggleUs?.setAttribute("tabindex", market === "us" ? "0" : "-1")', content)
        self.assertIn('els.view2d?.setAttribute("tabindex", "-1")', content)
        self.assertIn('els.view3d?.setAttribute("tabindex", "0")', content)

    def test_r35_api_and_index_main_js_tabindex(self):
        """api.js and index_main.js implement 4-way arrow keys and roving tabindex."""
        api_path = pathlib.Path(__file__).parent.parent / "static" / "js" / "api.js"
        api_content = api_path.read_text(encoding="utf-8")
        self.assertIn('btn.setAttribute("tabindex", isActive ? "0" : "-1")', api_content)

        index_path = pathlib.Path(__file__).parent.parent / "static" / "js" / "index_main.js"
        index_content = index_path.read_text(encoding="utf-8")
        self.assertIn('"ArrowUp"', index_content)
        self.assertIn('"ArrowDown"', index_content)
        self.assertIn('b.setAttribute("tabindex", i === nextIndex ? "0" : "-1")', index_content)


if __name__ == "__main__":
    unittest.main()
