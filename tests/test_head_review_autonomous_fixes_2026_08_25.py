"""Regression tests for autonomous HEAD review fixes (R1 - R11)."""

import time
import unittest
from unittest.mock import MagicMock, patch

from schemas.ai_portfolio import AIPortfolioItemSchema
from schemas.stocks import (
    PortfolioUpdateRequest,
    StockDetailsQueryRequest,
    StockHistoryQueryRequest,
)
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.scrapers import Nikkei225JPScraper, YahooJPRealtimeScraper
from services.realtime.tv_client import TradingViewWSClient
from services.search_service import _execute_search_strategy
from services.stock_provider import YFinanceProvider


class TestHeadReviewAutonomousFixes20260825(unittest.TestCase):
    def test_r1_search_service_tavily_hybrid_fallback_on_langsearch_error(self):
        with patch("services.search_service._collect_langsearch_items", return_value=[]):
            with patch("services.search_service._collect_hybrid_items", return_value=[{"title": "Hybrid Test"}]) as mock_hybrid:
                items = _execute_search_strategy(
                    strategy="langsearch",
                    queries=["AI stocks"],
                    region="us-en",
                    timelimit="d",
                    news_n=3,
                    text_n=2,
                    langsearch_api_key="ls-key",
                    tavily_api_key="tv-key",
                    context_label="test",
                )
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["title"], "Hybrid Test")
                mock_hybrid.assert_called_once()

    def test_r2_ai_portfolio_copy_to_my_uses_display_name(self):
        from routes.stocks.ai_portfolio import _stock_display_name

        jp_name = _stock_display_name("7203.T", "jp")
        self.assertTrue(isinstance(jp_name, str) and len(jp_name) > 0)
        self.assertNotEqual(jp_name, "7203.T")

    def test_r3_tradingview_ws_client_callback_exception_protection(self):
        def failing_callback(payload):
            raise ValueError("Simulated callback consumer failure")

        client = TradingViewWSClient(on_update_callback=failing_callback)
        sample_frame = '~m~85~m~{"m":"qsd","p":["s_1",{"n":"NASDAQ:AAPL","v":{"lp":150.0,"ch":1.5,"chp":1.0,"volume":1000}}]}'
        client._on_message(None, sample_frame)

    def test_r4_scraper_sessions_closed_on_close(self):
        scraper = Nikkei225JPScraper()
        sess1 = scraper._get_session()
        self.assertIn(sess1, scraper._all_sessions)

        scraper.close()
        self.assertEqual(len(scraper._all_sessions), 0)
        self.assertIsNone(getattr(scraper._thread_local, "session", None))

        yp_scraper = YahooJPRealtimeScraper()
        sess2 = yp_scraper._get_session()
        self.assertIn(sess2, yp_scraper._all_sessions)
        yp_scraper.close()
        self.assertEqual(len(yp_scraper._all_sessions), 0)

    def test_r5_realtime_market_engine_lifecycle_lock_exists(self):
        engine = RealtimeMarketEngine()
        self.assertTrue(hasattr(engine, "_lifecycle_lock"))
        with engine._lifecycle_lock:
            with engine._lifecycle_lock:
                pass

    def test_r6_ticker_cache_updates_lru_ordering_on_hit(self):
        provider = YFinanceProvider()
        mock_sess = MagicMock()
        with patch("services.stock_provider.yf_session_manager.get_session", return_value=mock_sess):
            with patch("services.stock_provider.yf_session_manager.is_session_alive", return_value=True):
                now = time.monotonic()
                provider._ticker_cache["AAPL"] = (MagicMock(), mock_sess, now)
                provider._ticker_cache["MSFT"] = (MagicMock(), mock_sess, now)

                keys_before = list(provider._ticker_cache.keys())
                self.assertEqual(keys_before, ["AAPL", "MSFT"])

                t = provider.get_ticker("AAPL")
                self.assertIsNotNone(t)

                keys_after = list(provider._ticker_cache.keys())
                self.assertEqual(keys_after, ["MSFT", "AAPL"])

    def test_r7_portfolio_update_rejects_jp_symbol_in_us_market(self):
        from app import app
        from app_state import app_state

        app.config["WTF_CSRF_ENABLED"] = False
        with app_state.market.user_stocks_lock:
            app_state.market.user_us["7203.T"] = "Toyota"

        with app.test_client() as client:
            res = client.post(
                "/api/stocks/portfolio",
                json={
                    "symbol": "7203.T",
                    "market": "us",
                    "shares": 10,
                    "avg_price": 2000,
                },
                headers={
                    "Origin": "http://127.0.0.1:5000",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            self.assertEqual(res.status_code, 400)
            data = res.get_json()
            self.assertIn("mismatch", data.get("details", {}).get("reason", "").lower())

    def test_r8_schema_contract_consistency(self):
        req = PortfolioUpdateRequest(
            symbol="AAPL",
            market="us",
            shares=50_000_000.0,
            avg_price=500_000.0,
            avg_fx_rate=150.0,
        )
        self.assertEqual(req.shares, 50_000_000.0)

        hist_req = StockHistoryQueryRequest(symbol="^N225", market="idx", period="1mo")
        self.assertEqual(hist_req.market, "idx")

        det_req = StockDetailsQueryRequest(symbol="^GSPC", market="idx")
        self.assertEqual(det_req.market, "idx")

        ai_item = AIPortfolioItemSchema(
            symbol="NVDA",
            name="NVIDIA Corp",
            market="us",
            weight_pct=25.0,
            target_price=135.0,
            rationale="Leading AI semiconductor designer with strong data center momentum.",
            risk_level="mid",
        )
        self.assertEqual(ai_item.weight_pct, 25.0)
        self.assertEqual(ai_item.risk_level, "mid")


if __name__ == "__main__":
    unittest.main()
