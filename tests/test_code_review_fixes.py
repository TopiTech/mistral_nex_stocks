import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from app_state import app_state
from services.realtime_engine import YahooJPRealtimeScraper


class TestCodeReviewFixes(unittest.TestCase):
    """Regression test suite for verified code review fixes R1, R2, R3."""

    def setUp(self):
        self.app = create_app(skip_bootstrap=True)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        if hasattr(app_state, "ai") and hasattr(app_state.ai, "chat_history"):
            app_state.ai.chat_history.close_all()

    def test_r1_sec_fetch_site_blocks_cross_site_ai_portfolio_get(self):
        """[R1] Ensure Sec-Fetch-Site: cross-site blocks GET /api/ai-portfolio with 403."""
        # Cross-site GET request should be rejected by _enforce_sec_fetch_site_check
        resp = self.client.get(
            "/api/ai-portfolio",
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.com"},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json() or {}
        self.assertIn("error", data)

        # Same-origin GET request with local origin should pass Sec-Fetch-Site check
        resp_ok = self.client.get(
            "/api/ai-portfolio",
            headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:5000"},
        )
        # Should succeed and return valid JSON structure (200)
        self.assertEqual(resp_ok.status_code, 200)

    def test_r2_yahoojp_scraper_rapid_stop_start_lifecycle(self):
        """[R2] Ensure YahooJPRealtimeScraper handles rapid stop() without unhandled worker errors."""
        scraper = YahooJPRealtimeScraper()
        scraper._fetch_regular_with_fallback = MagicMock(
            return_value={"symbol": "7203.T", "price": 2500.0}
        )

        with app_state.market.user_stocks_lock:
            app_state.market.user_jp["7203.T"] = {"shares": 100}
            app_state.market.user_jp["9984.T"] = {"shares": 100}

        try:
            # Rapid start and stop cycles
            for _ in range(5):
                scraper.start()
                scraper.stop()
            self.assertFalse(scraper.running)
            self.assertIsNone(scraper._executor)
        finally:
            scraper.stop()

    def test_r3_screener_sorting_with_none_and_non_numeric_fields(self):
        """[R3] Ensure /api/screener sorts safely when items have None or invalid numbers."""
        # Mock screener base rows containing None and invalid types
        mock_stocks = [
            {
                "symbol": "NULL1",
                "name": "Null Stock 1",
                "price": None,
                "change_percent": None,
                "volume": None,
                "market_cap": None,
                "pe_ratio": None,
                "dividend_yield": None,
                "market": "us",
                "sector": "Technology",
                "is_active": True,
            },
            {
                "symbol": "VALID1",
                "name": "Valid Stock 1",
                "price": 150.0,
                "change_percent": 2.5,
                "volume": 1000000,
                "market_cap": 5000000000,
                "pe_ratio": 25.0,
                "dividend_yield": 1.5,
                "market": "us",
                "sector": "Technology",
                "is_active": True,
            },
            {
                "symbol": "NULL2",
                "name": "Null Stock 2",
                "price": "invalid_number",
                "change_percent": float("nan"),
                "volume": None,
                "market_cap": None,
                "pe_ratio": None,
                "dividend_yield": None,
                "market": "us",
                "sector": "Technology",
                "is_active": True,
            },
        ]

        with (
            patch("routes.api_stocks.build_screener_base_rows", return_value=mock_stocks),
            patch("routes.api_stocks.POPULAR_US", []),
            patch("routes.api_stocks.POPULAR_JP", []),
        ):
            for sort_field in ("price", "change_percent", "volume", "market_cap", "symbol"):
                for sort_order in ("asc", "desc"):
                    resp = self.client.get(
                        f"/api/screener?sort_by={sort_field}&sort_order={sort_order}",
                        headers={"Origin": "http://127.0.0.1:5000"},
                    )
                    self.assertEqual(resp.status_code, 200, f"Failed for {sort_field} {sort_order}")
                    data = resp.get_json()
                    self.assertTrue(data.get("ok"))
                    self.assertEqual(data.get("total"), 3)
                    self.assertEqual(len(data.get("stocks")), 3)


if __name__ == "__main__":
    unittest.main()
