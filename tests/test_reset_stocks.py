import unittest
from unittest.mock import patch

from app import app, app_state


class ResetStocksTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_reset_clears_runtime_caches(self):
        with app.app_context():
            with app_state.user_stocks_lock:
                app_state.user_us = {"AAA": {"name": "AAA"}}
                app_state.user_jp = {"1111.T": {"name": "JP"}}
                app_state.user_idx = {"^TEST": {"name": "IDX"}}
            with app_state.sse_data_lock:
                app_state.current_stocks_cache = {"us": [{"symbol": "AAA"}], "jp": [], "idx": []}
                app_state.target_stocks_cache = {"us": [{"symbol": "AAA"}], "jp": [], "idx": []}
                app_state.current_indices_cache = {"SP500": {"price": 1}}
                app_state.target_indices_cache = {"SP500": {"price": 1}}

            with patch("app.schedule_sync_all_stocks_now", return_value=True) as mocked_schedule:
                response = self.client.post("/api/stocks/reset")

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["success"])
            mocked_schedule.assert_called_once()

            with app_state.sse_data_lock:
                self.assertEqual(app_state.current_stocks_cache, {"us": [], "jp": [], "idx": []})
                self.assertEqual(app_state.target_stocks_cache, {"us": [], "jp": [], "idx": []})
                self.assertEqual(app_state.current_indices_cache, {})
                self.assertEqual(app_state.target_indices_cache, {})


if __name__ == "__main__":
    unittest.main()