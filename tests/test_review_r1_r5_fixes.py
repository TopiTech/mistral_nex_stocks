"""Unit tests for R1-R5 code review fixes."""

import unittest
from unittest.mock import patch

from app_bg import sync_all_stocks_now
from app_state import _InMemoryYfCache
from services.realtime_engine import (
    RealtimeMarketEngine,
    TradingViewWSClient,
    YahooJPRealtimeScraper,
    _BaseFallbackScraper,
)


class TestReviewFixesR1ToR5(unittest.TestCase):
    def test_r1_sync_all_stocks_now_prevents_concurrent_execution(self):
        """Test R1: sync_all_stocks_now prevents concurrent execution using _sync_execution_lock."""
        import threading

        import app_bg
        from app_state import app_state

        executed_count = 0
        t1_in_fetch = threading.Event()
        proceed_t1 = threading.Event()

        def mock_fetch(items, snapshot_ts_ms=None):
            nonlocal executed_count
            executed_count += 1
            t1_in_fetch.set()
            proceed_t1.wait(timeout=5.0)
            return []

        # Ensure any in-flight sync from previous tests finishes cleanly
        if app_bg._sync_execution_lock.locked():
            acquired = app_bg._sync_execution_lock.acquire(timeout=5.0)
            if acquired:
                app_bg._sync_execution_lock.release()

        test_lock = threading.Lock()
        app_bg._sync_start_time = 0.0
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False

        with (
            patch.object(app_bg, "_sync_execution_lock", test_lock),
            patch("app_bg.fetch_stocks_batch", side_effect=mock_fetch),
            patch("app_bg._is_sync_leader", True),
        ):
            t1 = threading.Thread(target=sync_all_stocks_now)
            t2 = threading.Thread(target=sync_all_stocks_now)

            t1.start()
            # Deterministically wait until t1 has acquired the lock and entered mock_fetch
            self.assertTrue(t1_in_fetch.wait(timeout=5.0), "t1 did not reach fetch_stocks_batch")

            # Now t2 runs while t1 is actively holding the lock
            t2.start()
            t2.join(timeout=5.0)
            self.assertFalse(t2.is_alive(), "t2 did not finish in time")

            # Release t1 to finish
            proceed_t1.set()
            t1.join(timeout=5.0)
            self.assertFalse(t1.is_alive(), "t1 did not finish in time")

            # executed_count should be 1 because t2 skipped concurrent execution
            self.assertEqual(executed_count, 1)

    def test_r2_event_not_cleared_prematurely_in_wait_for_updates(self):
        """Test R2: wait_for_updates does not call clear() on the event handle."""
        engine = RealtimeMarketEngine()
        cid = engine.register_client()

        # Engine produces update -> sets client event
        engine._notify_all_clients()

        # wait_for_updates should return True without calling clear()
        signaled = engine.wait_for_updates(cid, timeout=0.1)
        self.assertTrue(signaled)

        # get_market_deltas clears the event while holding store_lock
        deltas = engine.get_market_deltas(cid)
        self.assertIsInstance(deltas, dict)

        # Now event is cleared
        evt = engine._client_events.get(cid)
        self.assertIsNotNone(evt)
        self.assertFalse(evt.is_set())

        engine.unregister_client(cid)

    def test_r3_tv_client_remove_symbol_clears_last_quotes(self):
        """Test R3: remove_symbol removes symbol from _last_quotes dict."""
        client = TradingViewWSClient()
        client.add_symbol("AAPL")
        client._last_quotes["AAPL"] = {
            "symbol": "AAPL",
            "price": 150.0,
            "change": 1.0,
            "change_percent": 0.5,
        }

        self.assertIn("AAPL", client._last_quotes)
        client.remove_symbol("AAPL")

        self.assertNotIn("AAPL", client.symbols)
        self.assertNotIn("AAPL", client._last_quotes)

    def test_r4_scrapers_remove_symbol_purges_tracking_state(self):
        """Test R4: remove_symbol purges tracking state from fallback scrapers."""
        fallback = _BaseFallbackScraper()
        fallback._consecutive_failures["7203"] = 5
        fallback._structure_change_reported.add("7203")
        fallback._structure_change_reported_time["7203"] = 100.0
        fallback._last_failure_time["7203"] = 100.0

        fallback.remove_symbol("7203")
        self.assertNotIn("7203", fallback._consecutive_failures)
        self.assertNotIn("7203", fallback._structure_change_reported)
        self.assertNotIn("7203", fallback._structure_change_reported_time)
        self.assertNotIn("7203", fallback._last_failure_time)

        yahoo_scraper = YahooJPRealtimeScraper(symbols=["7203"])
        yahoo_scraper._last_dispatch_price["7203"] = 2500.0
        yahoo_scraper._consecutive_failures[("7203", "regular")] = 3

        yahoo_scraper.remove_symbol("7203")
        self.assertNotIn("7203", yahoo_scraper.symbols)
        self.assertNotIn("7203", yahoo_scraper._last_dispatch_price)
        self.assertNotIn(("7203", "regular"), yahoo_scraper._consecutive_failures)

    def test_r5_in_memory_yf_cache_evicts_oldest_in_o1(self):
        """Test R5: _InMemoryYfCache evicts oldest entry when MAX_ENTRIES is exceeded."""
        cache = _InMemoryYfCache()
        cache._MAX_ENTRIES = 3

        cache.store("key1", "val1")
        cache.store("key2", "val2")
        cache.store("key3", "val3")
        self.assertEqual(len(cache._store), 3)

        # Storing key4 should evict key1
        cache.store("key4", "val4")
        self.assertEqual(len(cache._store), 3)
        self.assertIsNone(cache.lookup("key1"))
        self.assertEqual(cache.lookup("key2"), "val2")
        self.assertEqual(cache.lookup("key3"), "val3")
        self.assertEqual(cache.lookup("key4"), "val4")


if __name__ == "__main__":
    unittest.main()
