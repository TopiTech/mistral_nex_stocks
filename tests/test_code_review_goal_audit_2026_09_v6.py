# tests/test_code_review_goal_audit_2026_09_v6.py
"""
Regression test suite for Code Review Findings R30, R31, and R32.

R30: API & Schema contract alignment:
     - Screener limit up to 500 (default 150) in ScreenerQueryRequest and ScreenerFilterSchema.
     - Heatmap market restricted to 'us' and 'jp' (default 'us') in HeatmapFilterSchema and HeatmapQueryRequest.
     - AIPortfolioDeleteRequest and AIPortfolioCopyToMyRequest validation schemas.
R31: Chart image analysis input normalization and validation:
     - api_analyze_chart_image normalizes Japanese symbols to .T suffix.
     - Rejects malformed symbols with INVALID_SYMBOL.
R32: WAI-ARIA roving keyboard navigation completeness:
     - Screener table rows support Home/End keys.
     - AI Portfolio mode tabs support Home/End keys.
     - Detail drawer tab bar supports Home/End keys.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import app
from error_codes import ErrorCode
from schemas import (
    AIPortfolioCopyToMyItem,
    AIPortfolioCopyToMyRequest,
    AIPortfolioDeleteRequest,
    HeatmapQueryRequest,
    ScreenerQueryRequest,
)
from utils.validators import HeatmapFilterSchema, ScreenerFilterSchema


class TestCodeReviewAuditV6(unittest.TestCase):
    """Test suite verifying fixes for R30, R31, and R32."""

    def setUp(self) -> None:
        self._orig_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["WTF_CSRF_ENABLED"] = False

    def tearDown(self) -> None:
        app.config["WTF_CSRF_ENABLED"] = self._orig_csrf

    # -------------------------------------------------------------------------
    # R30: Schema & Contract Alignment
    # -------------------------------------------------------------------------

    def test_r30_screener_query_and_validator_limit_bounds(self) -> None:
        """ScreenerQueryRequest and ScreenerFilterSchema accept limit up to 500, default 150."""
        # Defaults
        q_req = ScreenerQueryRequest()
        self.assertEqual(q_req.limit, 150)
        v_req = ScreenerFilterSchema()
        self.assertEqual(v_req.limit, 150)

        # 300 and 500 are valid
        q_300 = ScreenerQueryRequest(limit=300)
        self.assertEqual(q_300.limit, 300)
        v_300 = ScreenerFilterSchema(limit=300)
        self.assertEqual(v_300.limit, 300)

        q_500 = ScreenerQueryRequest(limit=500)
        self.assertEqual(q_500.limit, 500)
        v_500 = ScreenerFilterSchema(limit=500)
        self.assertEqual(v_500.limit, 500)

        # Over 500 is rejected
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(limit=501)
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(limit=501)

        # 0 or negative is rejected
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(limit=0)
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(limit=0)

    def test_r30_heatmap_schemas_market_alignment(self) -> None:
        """HeatmapQueryRequest and HeatmapFilterSchema restrict market to 'us'/'jp'."""
        # Defaults
        q_req = HeatmapQueryRequest()
        self.assertEqual(q_req.market, "us")
        v_req = HeatmapFilterSchema()
        self.assertEqual(v_req.market, "us")

        # JP market
        q_jp = HeatmapQueryRequest(market="jp")
        self.assertEqual(q_jp.market, "jp")
        v_jp = HeatmapFilterSchema(market="jp")
        self.assertEqual(v_jp.market, "jp")

        # 'all' is invalid for heatmap endpoint
        with self.assertRaises(ValidationError):
            HeatmapQueryRequest(market="all")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            HeatmapFilterSchema(market="all")  # type: ignore[arg-type]

    def test_r30_ai_portfolio_mutation_schemas(self) -> None:
        """AIPortfolioDeleteRequest and AIPortfolioCopyToMyRequest schema validation."""
        # Delete request
        del_req = AIPortfolioDeleteRequest(id="  portfolio_abc_123  ")
        self.assertEqual(del_req.id, "portfolio_abc_123")

        with self.assertRaises(ValidationError):
            AIPortfolioDeleteRequest(id="   ")
        with self.assertRaises(ValidationError):
            AIPortfolioDeleteRequest(id="x" * 257)

        # Copy to my request
        item1 = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=50.0)
        item2 = AIPortfolioCopyToMyItem(symbol="7203.T", market="jp", weight_pct=50.0)
        copy_req = AIPortfolioCopyToMyRequest(items=[item1, item2])
        self.assertEqual(len(copy_req.items), 2)
        self.assertEqual(copy_req.items[0].symbol, "AAPL")
        self.assertEqual(copy_req.items[1].symbol, "7203.T")

        # Empty items list rejected
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyRequest(items=[])

        # Over 20 items rejected
        excessive = [AIPortfolioCopyToMyItem(symbol=f"SYM{i}", market="us") for i in range(21)]
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyRequest(items=excessive)

    # -------------------------------------------------------------------------
    # R31: Chart Image Analysis Input Normalization & Validation
    # -------------------------------------------------------------------------

    def test_r31_chart_image_analysis_symbol_normalization(self) -> None:
        """api_analyze_chart_image normalizes JP ticker 7203 -> 7203.T."""
        client = app.test_client()
        with (
            patch("routes.api_analysis.analyze_chart_image_with_mistral") as mock_analyze,
            patch("routes.api_analysis.extract_api_key", return_value="test_key"),
        ):
            mock_analyze.return_value = {
                "ok": True,
                "analysis": "Test analysis result",
            }
            # Send JP symbol '7203' without .T suffix
            resp = client.post(
                "/api/analyze-chart-image",
                json={
                    "image_data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
                    "symbol": "7203",
                    "market": "jp",
                    "prompt": "Analyze pattern",
                },
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            mock_analyze.assert_called_once()
            _, kwargs = mock_analyze.call_args
            self.assertEqual(kwargs.get("symbol"), "7203.T")
            self.assertEqual(kwargs.get("market"), "jp")

    def test_r31_chart_image_analysis_rejects_malformed_symbol(self) -> None:
        """api_analyze_chart_image rejects invalid symbol inputs."""
        client = app.test_client()
        with patch("routes.api_analysis.extract_api_key", return_value="test_key"):
            # Invalid symbol with special characters
            resp = client.post(
                "/api/analyze-chart-image",
                json={
                    "image_data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
                    "symbol": "<script>alert(1)</script>",
                    "market": "us",
                },
            )
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), str(ErrorCode.INVALID_SYMBOL.value))
            self.assertEqual(data.get("error_code"), ErrorCode.INVALID_SYMBOL.value)

    def test_r31_chart_image_analysis_optional_symbol_allowed(self) -> None:
        """api_analyze_chart_image allows omitted or blank symbol."""
        client = app.test_client()
        with (
            patch("routes.api_analysis.analyze_chart_image_with_mistral") as mock_analyze,
            patch("routes.api_analysis.extract_api_key", return_value="test_key"),
        ):
            mock_analyze.return_value = {
                "ok": True,
                "analysis": "Generic chart pattern",
            }
            resp = client.post(
                "/api/analyze-chart-image",
                json={
                    "image_data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
                    "prompt": "Identify support and resistance",
                },
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            mock_analyze.assert_called_once()
            _, kwargs = mock_analyze.call_args
            self.assertEqual(kwargs.get("symbol"), "")

    # -------------------------------------------------------------------------
    # R32: WAI-ARIA Roving Keyboard Navigation Completeness
    # -------------------------------------------------------------------------

    def test_r32_screener_table_row_home_end_navigation(self) -> None:
        """Verify screener.js row keydown listener implements Home and End navigation."""
        with open("static/js/screener.js", encoding="utf-8") as f:
            screener_js = f.read()
            self.assertIn('e.key === "Home"', screener_js)
            self.assertIn('e.key === "End"', screener_js)
            self.assertIn('parent.querySelector(\'tr[role="row"]\')', screener_js)
            self.assertIn('parent.querySelectorAll(\'tr[role="row"]\')', screener_js)

    def test_r32_ai_portfolio_tabs_home_end_navigation(self) -> None:
        """Verify ai_portfolio.js mode tabs implement Home and End navigation."""
        with open("static/js/ai_portfolio.js", encoding="utf-8") as f:
            ai_pf_js = f.read()
            self.assertIn('e.key === "Home"', ai_pf_js)
            self.assertIn('e.key === "End"', ai_pf_js)
            self.assertIn("switchToMy()", ai_pf_js)
            self.assertIn("switchToAi()", ai_pf_js)

    def test_r32_drawer_tab_bar_home_end_navigation(self) -> None:
        """Verify ui.js drawer tab bar implements Home and End navigation."""
        with open("static/js/ui.js", encoding="utf-8") as f:
            ui_js = f.read()
            self.assertIn(".drawer-tab-bar", ui_js)
            self.assertIn('e.key === "Home"', ui_js)
            self.assertIn('e.key === "End"', ui_js)
            self.assertIn("selectChartTab()", ui_js)
            self.assertIn("selectAiTab()", ui_js)


if __name__ == "__main__":
    unittest.main()
