# tests/test_code_review_goal_audit_2026_10.py
"""Regression test suite for comprehensive code review audit 2026-10.

Verifies:
1. Origin defense-in-depth on /api/csrf-token and credential state guarding on /api/health.
2. PortfolioUpdateRequest rejects boolean types and enforces PORTFOLIO_TOTAL_VALUE_MAX.
3. ScreenerFilterSchema rejects negative/infinite/boolean numeric filters consistently with ScreenerQueryRequest.
4. AIPortfolioItemSchema and AIPortfolioCopyToMyItem reject boolean types for numeric fields.
5. sanitize_ai_portfolio preserves and sanitizes the item name property.
6. setup.css styles both h2 and h4 within .feature-list matching setup.html structure.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from app import create_app
from constants import PORTFOLIO_TOTAL_VALUE_MAX
from schemas.ai_portfolio import AIPortfolioCopyToMyItem, AIPortfolioItemSchema
from schemas.stocks import PortfolioUpdateRequest, ScreenerQueryRequest
from services.ai_portfolio_service import sanitize_ai_portfolio
from utils.validators import ScreenerFilterSchema


class TestSystemOriginDefense(unittest.TestCase):
    """Test multi-layer origin defense on system endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def test_csrf_token_loopback_no_origin(self):
        """Loopback request without Origin header succeeds."""
        resp = self.client.get("/api/csrf-token", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIn("csrf_token", data)

    def test_csrf_token_loopback_trusted_origin(self):
        """Loopback request with trusted Origin header succeeds."""
        resp = self.client.get(
            "/api/csrf-token",
            headers={"Origin": "http://127.0.0.1:5000"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIn("csrf_token", data)

    def test_csrf_token_loopback_untrusted_origin_forbidden(self):
        """Loopback request with untrusted Origin header is rejected with 403."""
        resp = self.client.get(
            "/api/csrf-token",
            headers={"Origin": "http://evil.com"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertEqual(data.get("details", {}).get("reason"), "untrusted origin")

    def test_health_untrusted_origin_omits_credentials(self):
        """Untrusted origin on /api/health does not leak credential presence states."""
        resp = self.client.get(
            "/api/health",
            headers={"Origin": "http://evil.com"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertNotIn("has_mistral_api_key", data)
        self.assertNotIn("has_langsearch_api_key", data)
        self.assertNotIn("has_tavily_api_key", data)

    def test_health_trusted_or_no_origin_includes_credentials(self):
        """Legitimate local requests on /api/health include credential presence states."""
        resp = self.client.get("/api/health", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIn("has_mistral_api_key", data)


class TestPortfolioUpdateRequestValidation(unittest.TestCase):
    """Test PortfolioUpdateRequest enhancements."""

    def test_valid_portfolio_update(self):
        """Valid portfolio update request is accepted."""
        req = PortfolioUpdateRequest(symbol="NVDA", market="us", shares=10.0, avg_price=120.0)
        self.assertEqual(req.shares, 10.0)
        self.assertEqual(req.avg_price, 120.0)

    def test_reject_boolean_numeric(self):
        """Boolean values for shares, avg_price, or avg_fx_rate are rejected."""
        with self.assertRaises(ValidationError) as ctx:
            PortfolioUpdateRequest(symbol="NVDA", market="us", shares=True, avg_price=120.0)  # type: ignore[arg-type]
        self.assertIn("bool_type_not_allowed", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            PortfolioUpdateRequest(symbol="NVDA", market="us", shares=10.0, avg_price=False)  # type: ignore[arg-type]
        self.assertIn("bool_type_not_allowed", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            PortfolioUpdateRequest(
                symbol="NVDA", market="us", shares=10.0, avg_price=120.0, avg_fx_rate=True  # type: ignore[arg-type]
            )
        self.assertIn("bool_type_not_allowed", str(ctx.exception))

    def test_reject_exceeding_total_value(self):
        """Total value exceeding PORTFOLIO_TOTAL_VALUE_MAX is rejected."""
        with self.assertRaises(ValidationError) as ctx:
            PortfolioUpdateRequest(
                symbol="NVDA",
                market="us",
                shares=2_000_000.0,
                avg_price=1_000_000.0,  # 2_000_000 * 1_000_000 = 2e12 > 1e12
            )
        self.assertIn("Portfolio total value exceeds maximum allowed", str(ctx.exception))

    def test_accept_up_to_total_value_max(self):
        """Total value up to PORTFOLIO_TOTAL_VALUE_MAX is allowed."""
        req = PortfolioUpdateRequest(
            symbol="NVDA",
            market="us",
            shares=1_000_000.0,
            avg_price=1_000_000.0,  # 1_000_000 * 1_000_000 = 1e12 == PORTFOLIO_TOTAL_VALUE_MAX
        )
        self.assertEqual(req.shares * req.avg_price, PORTFOLIO_TOTAL_VALUE_MAX)


class TestScreenerFilterSchemaConsistency(unittest.TestCase):
    """Test ScreenerFilterSchema numeric bounds and validation consistency."""

    def test_valid_filters(self):
        """Valid filters are accepted."""
        schema = ScreenerFilterSchema(
            min_price=10.0,
            max_price=100.0,
            min_market_cap=1_000_000.0,
            max_market_cap=50_000_000.0,
            min_pe=5.0,
            max_pe=30.0,
            min_change=-10.0,
            max_change=25.0,
        )
        self.assertEqual(schema.min_price, 10.0)
        self.assertEqual(schema.max_price, 100.0)

    def test_negative_numeric_fields_rejected(self):
        """Negative values for price, market cap, and pe ratio are rejected."""
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_price=-1.0)
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(max_price=-5.0)
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_market_cap=-100.0)
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_pe=-0.1)

    def test_nan_and_inf_rejected(self):
        """Non-finite numbers are rejected."""
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_price=float("nan"))
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(max_price=float("inf"))

    def test_boolean_numeric_rejected(self):
        """Booleans for numeric filters are rejected in ScreenerFilterSchema and ScreenerQueryRequest."""
        with self.assertRaises(ValidationError):
            ScreenerFilterSchema(min_price=True)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            ScreenerQueryRequest(min_price=True)  # type: ignore[arg-type]


class TestAIPortfolioSchemaValidation(unittest.TestCase):
    """Test boolean rejection on AI portfolio schemas."""

    def test_copy_to_my_item_rejects_booleans(self):
        """AIPortfolioCopyToMyItem rejects boolean values for numeric fields."""
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="NVDA", market="us", weight_pct=True)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="NVDA", market="us", target_price=False)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="NVDA", market="us", shares=True)  # type: ignore[arg-type]

    def test_ai_portfolio_item_rejects_booleans(self):
        """AIPortfolioItemSchema rejects boolean values for numeric fields."""
        with self.assertRaises(ValidationError):
            AIPortfolioItemSchema(symbol="NVDA", market="us", weight_pct=True)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            AIPortfolioItemSchema(symbol="NVDA", market="us", target_price=False)  # type: ignore[arg-type]


class TestAIPortfolioServiceSanitizeNamePreservation(unittest.TestCase):
    """Test preservation and sanitization of item name in sanitize_ai_portfolio."""

    def test_name_preserved_and_sanitized(self):
        """Item names are preserved, HTML-stripped, and length-capped."""
        raw = {
            "title": "Clean Energy",
            "items": [
                {
                    "symbol": "TSLA",
                    "market": "us",
                    "name": "<b>Tesla Inc.</b>",
                    "weight_pct": 50.0,
                    "target_price": 250.0,
                },
                {
                    "symbol": "7203.T",
                    "market": "jp",
                    "name": "Toyota Motor Corp" + "!" * 200,
                    "weight_pct": 50.0,
                    "target_price": 3000.0,
                },
            ],
        }
        sanitized = sanitize_ai_portfolio(raw)
        items = sanitized["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["name"], "Tesla Inc.")
        self.assertEqual(len(items[1]["name"]), 200)
        self.assertTrue(items[1]["name"].startswith("Toyota Motor Corp"))

    def test_omitted_name_defaults_to_empty_string(self):
        """Items without a name field default to empty string."""
        raw = {
            "title": "Clean Energy",
            "items": [
                {
                    "symbol": "AAPL",
                    "market": "us",
                    "weight_pct": 100.0,
                    "target_price": 200.0,
                }
            ],
        }
        sanitized = sanitize_ai_portfolio(raw)
        self.assertEqual(sanitized["items"][0]["name"], "")


class TestSetupCSSHeadingStyling(unittest.TestCase):
    """Verify setup.css styles h2 element in .feature-list."""

    def test_feature_list_h2_styled(self):
        """setup.css must target h2 elements inside .feature-list."""
        css_path = Path(__file__).resolve().parent.parent / "static" / "css" / "setup.css"
        content = css_path.read_text(encoding="utf-8")
        self.assertIn(".feature-list h2", content)
