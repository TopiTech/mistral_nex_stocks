# tests/test_code_review_goal_audit_2026_09_v10.py
"""Regression test suite for comprehensive code review audit 2026-09 v10.

Verifies:
1. is_valid_symbol handles lowercase, mixed case, and normalized forms correctly.
2. sanitize_ai_portfolio deduplicates items by (symbol, market) preventing duplicate stock entries.
3. AIPortfolioCopyToMyItem and AIPortfolioCopyToMyRequest validate bounds (gt=0.0)
   and enforce symbol uniqueness after market normalization (e.g. '7203' vs '7203.T' for 'jp').
4. settings.html contains proper initial tabindex attributes for ARIA tablist navigation.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from schemas.ai_portfolio import AIPortfolioCopyToMyItem, AIPortfolioCopyToMyRequest
from services.ai_portfolio_service import sanitize_ai_portfolio
from utils.normalization import is_valid_symbol


class TestNormalizationSymbolValidity(unittest.TestCase):
    """Test enhanced is_valid_symbol case-insensitivity and boundary validation."""

    def test_lowercase_and_mixed_case_symbols(self):
        """Lowercase and mixed-case symbols should be recognized as valid."""
        self.assertTrue(is_valid_symbol("aapl"))
        self.assertTrue(is_valid_symbol("nvda"))
        self.assertTrue(is_valid_symbol("msft"))
        self.assertTrue(is_valid_symbol("AaPl"))
        self.assertTrue(is_valid_symbol("brk.b"))
        self.assertTrue(is_valid_symbol("7203.t"))
        self.assertTrue(is_valid_symbol("9984.t"))

    def test_uppercase_symbols_remain_valid(self):
        """Standard uppercase symbols remain valid."""
        self.assertTrue(is_valid_symbol("AAPL"))
        self.assertTrue(is_valid_symbol("NVDA"))
        self.assertTrue(is_valid_symbol("BRK.B"))
        self.assertTrue(is_valid_symbol("7203.T"))
        self.assertTrue(is_valid_symbol("^GSPC"))

    def test_invalid_symbols_rejected(self):
        """Invalid characters, path traversal, and malicious strings are rejected."""
        self.assertFalse(is_valid_symbol(""))
        self.assertFalse(is_valid_symbol(None))
        self.assertFalse(is_valid_symbol("../bad"))
        self.assertFalse(is_valid_symbol("a/b"))
        self.assertFalse(is_valid_symbol("a\\b"))
        self.assertFalse(is_valid_symbol("a%20b"))
        self.assertFalse(is_valid_symbol("a\x00b"))
        self.assertFalse(is_valid_symbol("a\nb"))
        self.assertFalse(is_valid_symbol("TOOLONGSYMBOLNAME12345"))  # >15 chars


class TestAIPortfolioSanitizeDeduplication(unittest.TestCase):
    """Test duplicate symbol elimination in sanitize_ai_portfolio."""

    def test_duplicate_symbols_filtered(self):
        """sanitize_ai_portfolio should drop duplicate items for the same symbol and market."""
        raw_portfolio = {
            "id": "test-pf-1",
            "theme": "Tech Growth",
            "items": [
                {
                    "symbol": "AAPL",
                    "market": "us",
                    "weight_pct": 30.0,
                    "target_price": 200.0,
                    "rationale": "Strong iPhone sales",
                    "risk_level": "mid",
                },
                {
                    "symbol": "aapl",  # Duplicate US symbol in lowercase
                    "market": "us",
                    "weight_pct": 20.0,
                    "target_price": 210.0,
                    "rationale": "Secondary entry",
                    "risk_level": "low",
                },
                {
                    "symbol": "7203",  # JP market normalized to 7203.T
                    "market": "jp",
                    "weight_pct": 25.0,
                    "target_price": 3000.0,
                },
                {
                    "symbol": "7203.T",  # Duplicate JP symbol
                    "market": "jp",
                    "weight_pct": 25.0,
                    "target_price": 3100.0,
                },
                {
                    "symbol": "MSFT",
                    "market": "us",
                    "weight_pct": 25.0,
                    "target_price": 400.0,
                },
            ],
        }

        sanitized = sanitize_ai_portfolio(raw_portfolio)
        clean_items = sanitized["items"]

        # Duplicate AAPL and duplicate 7203.T should be omitted
        self.assertEqual(len(clean_items), 3)
        symbols = [(it["symbol"], it["market"]) for it in clean_items]
        self.assertIn(("AAPL", "us"), symbols)
        self.assertIn(("7203.T", "jp"), symbols)
        self.assertIn(("MSFT", "us"), symbols)
        # Verify first occurrence values retained and weights rebalanced to 100%
        aapl_item = next(it for it in clean_items if it["symbol"] == "AAPL")
        self.assertEqual(aapl_item["weight_pct"], 37.6)
        self.assertEqual(aapl_item["target_price"], 200.0)
        self.assertAlmostEqual(sum(it["weight_pct"] for it in clean_items), 100.0, places=1)


class TestAIPortfolioCopyToMyValidation(unittest.TestCase):
    """Test AIPortfolioCopyToMyItem and Request boundary constraints and normalization."""

    def test_weight_pct_boundary_constraints(self):
        """weight_pct must be strictly > 0 and <= 100."""
        # Valid positive weight
        item_valid = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=10.0)
        self.assertEqual(item_valid.weight_pct, 10.0)

        # None is allowed (defaults to unassigned / server resolved)
        item_none = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=None)
        self.assertIsNone(item_none.weight_pct)

        # 0.0 must be rejected
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=0.0)

        # Negative must be rejected
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=-5.0)

        # > 100 must be rejected
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="AAPL", market="us", weight_pct=100.1)

    def test_target_price_boundary_constraints(self):
        """target_price must be strictly > 0 and <= PORTFOLIO_AVG_PRICE_MAX."""
        # Valid positive target price
        item_valid = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", target_price=150.0)
        self.assertEqual(item_valid.target_price, 150.0)

        # None is allowed
        item_none = AIPortfolioCopyToMyItem(symbol="AAPL", market="us", target_price=None)
        self.assertIsNone(item_none.target_price)

        # 0.0 must be rejected
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="AAPL", market="us", target_price=0.0)

        # Negative must be rejected
        with self.assertRaises(ValidationError):
            AIPortfolioCopyToMyItem(symbol="AAPL", market="us", target_price=-10.0)

    def test_duplicate_symbol_normalization_in_request(self):
        """AIPortfolioCopyToMyRequest rejects duplicates after market symbol normalization."""
        # '7203' and '7203.T' in jp market both normalize to '7203.T'
        item1 = AIPortfolioCopyToMyItem(symbol="7203", market="jp", weight_pct=30.0)
        item2 = AIPortfolioCopyToMyItem(symbol="7203.T", market="jp", weight_pct=20.0)

        with self.assertRaises(ValidationError) as ctx:
            AIPortfolioCopyToMyRequest(items=[item1, item2])
        self.assertIn("Duplicate stock in items: 7203.T (jp)", str(ctx.exception))

        # Different markets with same symbol base should succeed
        item_us = AIPortfolioCopyToMyItem(symbol="NVDA", market="us", weight_pct=20.0)
        item_jp = AIPortfolioCopyToMyItem(symbol="7203", market="jp", weight_pct=30.0)
        req = AIPortfolioCopyToMyRequest(items=[item_us, item_jp])
        self.assertEqual(len(req.items), 2)


class TestSettingsAccessibilityHtml(unittest.TestCase):
    """Test accessibility tablist tabindex attributes in settings.html."""

    def test_settings_tabs_tabindex(self):
        """Active tab must have tabindex='0' and inactive tabs must have tabindex='-1'."""
        html_path = Path(__file__).resolve().parent.parent / "templates" / "settings.html"
        content = html_path.read_text(encoding="utf-8")

        # Active tab
        self.assertIn('id="tab-btn-stocks"', content)
        self.assertIn('tabindex="0"', content)

        # Inactive tabs
        self.assertIn('id="tab-btn-appearance"', content)
        self.assertIn('id="tab-btn-ai"', content)
        self.assertIn('id="tab-btn-system"', content)
        self.assertIn('tabindex="-1"', content)


if __name__ == "__main__":
    unittest.main()
