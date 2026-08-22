import math
import unittest
from unittest.mock import patch

from app import app, app_state
from app_bg import _update_indices_data, _warm_payload_cache_from_disk


class TestIndicesCachePersistence(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        with app_state.cache.sse_data_lock:
            app_state.market.current_indices_cache.clear()
            app_state.market.target_indices_cache.clear()
        app_state.payload_disk_cache.clear()

    def tearDown(self):
        with app_state.cache.sse_data_lock:
            app_state.market.current_indices_cache.clear()
            app_state.market.target_indices_cache.clear()
        app_state.payload_disk_cache.clear()

    def test_indices_cache_saving_and_loading(self):
        with app.app_context():
            # Clean caches first
            with app_state.cache.sse_data_lock:
                app_state.market.current_indices_cache.clear()
            app_state.payload_disk_cache.delete("indices_cache")

            # 1. Test update_indices_data saves to disk
            idx_res = [
                {
                    "symbol": "^N225",
                    "price": "38000.00",
                    "change": "100.00",
                    "change_percent": "0.26",
                    "open": "37900.00",
                    "high": "38100.00",
                    "low": "37800.00",
                    "volume": "1000000",
                    "market_state": "REGULAR",
                    "market": "idx",
                }
            ]
            _update_indices_data(idx_res, [], [])

            # Check memory cache is updated
            with app_state.cache.sse_data_lock:
                self.assertIn("N225", app_state.market.current_indices_cache)
                self.assertEqual(
                    app_state.market.current_indices_cache["N225"]["price"], "38000.00"
                )

            # Check disk cache is updated
            disk_data = app_state.payload_disk_cache.get("indices_cache")
            self.assertIsNotNone(disk_data)
            self.assertIn("N225", disk_data)
            self.assertEqual(disk_data["N225"]["price"], "38000.00")

            # Clear memory cache to test warming
            with app_state.cache.sse_data_lock:
                app_state.market.current_indices_cache.clear()

            # 2. Test warm_payload_cache_from_disk restores memory cache
            _warm_payload_cache_from_disk()

            with app_state.cache.sse_data_lock:
                self.assertIn("N225", app_state.market.current_indices_cache)
                self.assertEqual(
                    app_state.market.current_indices_cache["N225"]["price"], "38000.00"
                )

    def test_valid_usdjpy_update_persists_rate_and_freshness_together(self):
        item = {
            "symbol": "USDJPY=X",
            "price": "147.25",
            "change": "0.10",
            "change_percent": "0.07",
            "market_state": "REGULAR",
            "market": "idx",
        }
        with (
            patch("app_bg.fetch_index_data", return_value=None),
            patch("app_bg.save_user_stocks") as save,
            patch("app_bg.time.time", return_value=1_765_000_123.5),
        ):
            _update_indices_data([item], [], [])

        self.assertEqual(app_state.market.last_usdjpy_rate, 147.25)
        self.assertEqual(app_state.market.last_usdjpy_rate_ts, 1_765_000_123.5)
        save.assert_called_once_with()

    def test_nonfinite_usdjpy_update_is_not_marked_fresh_or_persisted(self):
        item = {
            "symbol": "USDJPY=X",
            "price": math.inf,
            "market_state": "REGULAR",
            "market": "idx",
        }
        app_state.market.last_usdjpy_rate = 150.0
        app_state.market.last_usdjpy_rate_ts = 123.0
        with (
            patch("app_bg.fetch_index_data", return_value=None),
            patch("app_bg.save_user_stocks") as save,
        ):
            _update_indices_data([item], [], [])

        self.assertEqual(app_state.market.last_usdjpy_rate, 150.0)
        self.assertEqual(app_state.market.last_usdjpy_rate_ts, 123.0)
        save.assert_not_called()

    def test_reset_clears_disk_indices_cache(self):
        with app.app_context():
            # Setup indices cache in disk
            app_state.payload_disk_cache.set("indices_cache", {"SP500": {"price": "5000.00"}})

            with patch("routes.api_stocks.schedule_sync_all_stocks_now", return_value=True):
                response = self.client.post(
                    "/api/stocks/reset",
                    headers={"Origin": "http://localhost:5000"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["success"])

            # Verify that indices_cache key is deleted from disk cache
            disk_data = app_state.payload_disk_cache.get("indices_cache")
            self.assertIsNone(disk_data)

    def test_sync_does_not_clear_existing_indices_cache(self):
        from app_bg import sync_all_stocks_now

        with app.app_context():
            # Set up initial indices cache
            with app_state.cache.sse_data_lock:
                app_state.market.current_indices_cache = {"N225": {"price": "38000.00"}}

            # Mock fetch_stocks_batch and other methods to return early
            with (
                patch("app_bg._is_sync_leader", True),
                patch("app_bg._prepare_sync_items", return_value=[("AAPL", "Apple", "us")]),
                patch("app_bg.fetch_stocks_batch", return_value=[None]),
            ):
                # Run sync
                sync_all_stocks_now()

            # Verify that indices cache was not cleared
            with app_state.cache.sse_data_lock:
                self.assertIn("N225", app_state.market.current_indices_cache)
                self.assertEqual(
                    app_state.market.current_indices_cache["N225"]["price"], "38000.00"
                )


if __name__ == "__main__":
    unittest.main()
