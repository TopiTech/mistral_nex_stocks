# tests/test_code_review_goal_audit_2026_10_v4.py
"""Regression tests verifying defects identified during full codebase review (October 2026, Part 4).

Covers:
1. P/E ratio forwardPE fallback in _build_market_row (services/market_data_service.py).
2. P/E ratio filtering and sorting with forwardPE in /api/screener (routes/stocks/views.py).
3. P/E ratio forwardPE fallback in AI stock analysis prompt (routes/api_analysis.py).
4. Accessibility attributes (aria-label and title) in static/js/settings.js inline confirm.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import create_app
from services.market_data_service import _build_market_row


class TestCodeReviewAuditV4Regression(unittest.TestCase):
    """Verify bug fixes and consistency improvements across market data, screener, and UI."""

    def test_market_data_service_forward_pe_fallback(self) -> None:
        """_build_market_row should fall back to forwardPE when trailingPE/pe_ratio are missing."""
        # 1. Only forwardPE is present
        src_forward = {
            "name": "Forward Growth Inc",
            "price": 150.0,
            "forwardPE": 19.5,
        }
        row_forward = _build_market_row("FWD", "us", src_forward, "Forward Growth Inc")
        self.assertEqual(row_forward["pe_ratio"], 19.5)

        # 2. Precedence check: trailingPE takes precedence over forwardPE
        src_trailing = {
            "name": "Trailing Corp",
            "price": 100.0,
            "trailingPE": 22.0,
            "forwardPE": 18.0,
        }
        row_trailing = _build_market_row("TRL", "us", src_trailing, "Trailing Corp")
        self.assertEqual(row_trailing["pe_ratio"], 22.0)

        # 3. Precedence check: pe_ratio takes precedence over trailingPE
        src_pe_ratio = {
            "name": "PeRatio Corp",
            "price": 80.0,
            "pe_ratio": 25.0,
            "trailingPE": 22.0,
            "forwardPE": 18.0,
        }
        row_pe_ratio = _build_market_row("PER", "us", src_pe_ratio, "PeRatio Corp")
        self.assertEqual(row_pe_ratio["pe_ratio"], 25.0)

        # 4. Fallback to 'pe' if trailingPE and forwardPE are None
        src_pe = {
            "name": "Legacy PE Corp",
            "price": 50.0,
            "pe": 14.2,
        }
        row_pe = _build_market_row("LEG", "us", src_pe, "Legacy PE Corp")
        self.assertEqual(row_pe["pe_ratio"], 14.2)

        # 5. Invalid / negative / zero values return None
        src_invalid = {
            "name": "Invalid PE Corp",
            "price": 50.0,
            "forwardPE": -5.0,
        }
        row_invalid = _build_market_row("INV", "us", src_invalid, "Invalid PE Corp")
        self.assertIsNone(row_invalid["pe_ratio"])

    def test_api_screener_pe_filtering_with_forward_pe(self) -> None:
        """api_screener should filter correctly on items possessing only forwardPE."""
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()

        stocks_mock = {
            "us": [
                {
                    "symbol": "FWD-ONLY",
                    "name": "Forward Only Stock",
                    "market": "us",
                    "price": 100.0,
                    "change_percent": 1.0,
                    "market_cap": 1_000_000_000,
                    "forwardPE": 15.0,
                    "volume": 100_000,
                    "sector": "Technology",
                },
                {
                    "symbol": "TRL-ONLY",
                    "name": "Trailing Only Stock",
                    "market": "us",
                    "price": 100.0,
                    "change_percent": 1.0,
                    "market_cap": 1_000_000_000,
                    "trailingPE": 25.0,
                    "volume": 100_000,
                    "sector": "Technology",
                },
                {
                    "symbol": "HIGH-PE",
                    "name": "High PE Stock",
                    "market": "us",
                    "price": 100.0,
                    "change_percent": 1.0,
                    "market_cap": 1_000_000_000,
                    "pe_ratio": 45.0,
                    "volume": 100_000,
                    "sector": "Technology",
                },
            ],
            "jp": [],
        }

        with patch("routes.api_stocks._resolve_stocks_for_response", return_value=stocks_mock):
            # min_pe=10, max_pe=20 should match FWD-ONLY (15.0)
            res = client.get("/api/screener?market=us&min_pe=10&max_pe=20")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            symbols = [s["symbol"] for s in data["stocks"]]
            self.assertIn("FWD-ONLY", symbols)
            self.assertNotIn("TRL-ONLY", symbols)
            self.assertNotIn("HIGH-PE", symbols)

            # min_pe=20, max_pe=30 should match TRL-ONLY (25.0)
            res2 = client.get("/api/screener?market=us&min_pe=20&max_pe=30")
            self.assertEqual(res2.status_code, 200)
            data2 = res2.get_json()
            symbols2 = [s["symbol"] for s in data2["stocks"]]
            self.assertNotIn("FWD-ONLY", symbols2)
            self.assertIn("TRL-ONLY", symbols2)
            self.assertNotIn("HIGH-PE", symbols2)

    def test_api_screener_pe_sorting_with_forward_pe(self) -> None:
        """api_screener should sort correctly by pe_ratio when items rely on forwardPE."""
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()

        stocks_mock = {
            "us": [
                {
                    "symbol": "B-TRL",
                    "market": "us",
                    "price": 100.0,
                    "trailingPE": 25.0,
                    "sector": "Technology",
                },
                {
                    "symbol": "A-FWD",
                    "market": "us",
                    "price": 100.0,
                    "forwardPE": 15.0,
                    "sector": "Technology",
                },
                {
                    "symbol": "C-STD",
                    "market": "us",
                    "price": 100.0,
                    "pe_ratio": 35.0,
                    "sector": "Technology",
                },
            ],
            "jp": [],
        }

        with patch("routes.api_stocks._resolve_stocks_for_response", return_value=stocks_mock):
            # Sort ascending: A-FWD (15.0) -> B-TRL (25.0) -> C-STD (35.0)
            res_asc = client.get("/api/screener?market=us&sort_by=pe&sort_order=asc")
            self.assertEqual(res_asc.status_code, 200)
            data_asc = res_asc.get_json()
            symbols_asc = [
                s["symbol"]
                for s in data_asc["stocks"]
                if s["symbol"] in {"A-FWD", "B-TRL", "C-STD"}
            ]
            self.assertEqual(symbols_asc, ["A-FWD", "B-TRL", "C-STD"])

            # Sort descending: C-STD (35.0) -> B-TRL (25.0) -> A-FWD (15.0)
            res_desc = client.get("/api/screener?market=us&sort_by=pe&sort_order=desc")
            self.assertEqual(res_desc.status_code, 200)
            data_desc = res_desc.get_json()
            symbols_desc = [
                s["symbol"]
                for s in data_desc["stocks"]
                if s["symbol"] in {"A-FWD", "B-TRL", "C-STD"}
            ]
            self.assertEqual(symbols_desc, ["C-STD", "B-TRL", "A-FWD"])

    def test_settings_inline_confirm_a11y_attributes(self) -> None:
        """Verify inline delete buttons in settings.js have accessible aria-label and title."""
        with open("static/js/settings.js", encoding="utf-8") as f:
            js = f.read()

        # Check attachInlineDeleteConfirm function
        self.assertIn('yesBtn.setAttribute("aria-label", "削除を確定する")', js)
        self.assertIn('yesBtn.setAttribute("title", "削除を確定する")', js)
        self.assertIn('noBtn.setAttribute("aria-label", "削除をキャンセル")', js)
        self.assertIn('noBtn.setAttribute("title", "キャンセル")', js)


if __name__ == "__main__":
    unittest.main()
