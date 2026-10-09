# tests/test_code_review_goal_audit_2026_10_v3.py
"""Regression tests verifying defects identified during full codebase review (October 2026).

Covers:
1. Japanese alphanumeric securities code handling in watchlist symbol aliases.
2. RealtimeMarketEngine JPY currency formatting for bare/alphanumeric Japanese symbols.
3. Realtime purge key variants for alphanumeric Japanese tickers.
4. Yahoo JP realtime scraper early return guards for non-Japanese symbols.
5. P/E ratio fallback support (forwardPE, pe_ratio, pe) in /api/stock-details.
6. Accessibility attributes (dialog, aria-modal, aria-labelledby) on fullscreen chart modal.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import create_app
from app_state import app_state
from routes.stocks.common import _stored_symbol_aliases
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.scrapers import YahooJPRealtimeScraper
from services.realtime.utils import _tv_purge_key_variants


class TestCodeReviewDefectRegression(unittest.TestCase):
    """Verify bug fixes and enhancements from full code review."""

    def test_stored_symbol_aliases_alphanumeric_jp(self) -> None:
        """TSE alphanumeric symbols (e.g. 130A) should produce both .T and bare aliases."""
        # Alphanumeric JP code
        aliases_alpha = _stored_symbol_aliases("130A", "jp")
        self.assertEqual(aliases_alpha, ("130A.T", "130A"))

        aliases_alpha_dot = _stored_symbol_aliases("130A.T", "jp")
        self.assertEqual(aliases_alpha_dot, ("130A.T", "130A"))

        # Numeric JP code
        aliases_num = _stored_symbol_aliases("7203", "jp")
        self.assertEqual(aliases_num, ("7203.T", "7203"))

        aliases_num_dot = _stored_symbol_aliases("7203.T", "jp")
        self.assertEqual(aliases_num_dot, ("7203.T", "7203"))

        # Non-JP code
        aliases_us = _stored_symbol_aliases("AAPL", "us")
        self.assertEqual(aliases_us, ("AAPL",))

        # Index
        aliases_idx = _stored_symbol_aliases("^N225", "jp")
        self.assertEqual(aliases_idx, ("^N225",))

    def test_realtime_engine_alphanumeric_jpy_decimals(self) -> None:
        """RealtimeMarketEngine should format change with 2 decimals for alphanumeric JP symbols."""
        engine = RealtimeMarketEngine()

        with patch("services.realtime.engine._get_yfinance_previous_close", return_value=123.456):
            # Test bare alphanumeric JP symbol "130A"
            payload_alpha_bare = {"symbol": "130A", "price": 130.1234}
            engine._handle_producer_update(payload_alpha_bare)
            self.assertIn("130A", engine.market_store)
            # Decimals should be 2 for JPY (130.1234 - 123.456 = 6.6674 -> 6.67)
            self.assertEqual(engine.market_store["130A"]["change"], 6.67)

            # Test bare numeric JP symbol "7203"
            payload_num_bare = {"symbol": "7203", "price": 130.1234}
            engine._handle_producer_update(payload_num_bare)
            self.assertEqual(engine.market_store["7203"]["change"], 6.67)

            # Test US symbol "AAPL" (decimals should be 4)
            payload_us = {"symbol": "AAPL", "price": 130.1234}
            engine._handle_producer_update(payload_us)
            self.assertEqual(engine.market_store["AAPL"]["change"], 6.6674)

    def test_tv_purge_key_variants_alphanumeric_jp(self) -> None:
        """_tv_purge_key_variants should map alphanumeric JP symbols to their .T counterparts."""
        variants_bare = _tv_purge_key_variants("130A")
        self.assertIn("130A.T", variants_bare)
        self.assertIn("130A", variants_bare)

        variants_dot = _tv_purge_key_variants("130A.T")
        self.assertIn("130A", variants_dot)
        self.assertIn("130A.T", variants_dot)

        variants_us = _tv_purge_key_variants("AAPL")
        self.assertNotIn("AAPL.T", variants_us)

    def test_scrapers_non_jp_early_return(self) -> None:
        """YahooJPRealtimeScraper should immediately return None for non-JP symbols without requests."""
        scraper = YahooJPRealtimeScraper()
        with patch.object(scraper, "_get_session") as mock_session:
            res_jp = scraper.fetch_jp_symbol("AAPL")
            self.assertIsNone(res_jp)
            mock_session.assert_not_called()

            res_pts = scraper.fetch_pts_symbol("NVDA")
            self.assertIsNone(res_pts)
            mock_session.assert_not_called()

            res_invalid = scraper.fetch_jp_symbol("NOT_A_VALID_CODE")
            self.assertIsNone(res_invalid)
            mock_session.assert_not_called()

    def test_api_stock_details_pe_ratio_fallback(self) -> None:
        """api_stock_details should fall back to forwardPE or pe_ratio if trailingPE is missing."""
        app = create_app()
        app.config["TESTING"] = True

        client = app.test_client()

        # Mock app_state.yfinance_short_cache with forwardPE only
        mock_info = {
            "symbol": "TEST",
            "trailingPE": None,
            "forwardPE": 18.75,
            "marketCap": 50000000,
            "sector": "Technology",
            "industry": "Software",
        }

        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache["info_short_TEST"] = mock_info

        with patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, "")):
            resp = client.get("/api/stock-details?symbol=TEST&market=us")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["pe_ratio"], 18.75)
            self.assertEqual(data["market_cap"], 50000000)

            # Now test fallback to pe_ratio
            mock_info_pe_ratio = {
                "symbol": "TEST2",
                "trailingPE": None,
                "forwardPE": None,
                "pe_ratio": 22.3,
            }
            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache["info_short_TEST2"] = mock_info_pe_ratio

            resp = client.get("/api/stock-details?symbol=TEST2&market=us")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["pe_ratio"], 22.3)

            # When all PE fields are None
            mock_info_none = {
                "symbol": "TEST3",
                "trailingPE": None,
                "forwardPE": None,
                "pe_ratio": None,
                "pe": None,
            }
            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache["info_short_TEST3"] = mock_info_none

            resp = client.get("/api/stock-details?symbol=TEST3&market=us")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIsNone(data["pe_ratio"])

    def test_fullscreen_chart_modal_a11y_markup(self) -> None:
        """The fullscreen chart modal should have explicit accessible dialog labelling."""
        with open("templates/index.html", encoding="utf-8") as f:
            html = f.read()

        self.assertIn('id="chart-fullscreen-modal"', html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn('aria-labelledby="fs-chart-title"', html)
        self.assertIn('id="fs-chart-title"', html)


if __name__ == "__main__":
    unittest.main()
