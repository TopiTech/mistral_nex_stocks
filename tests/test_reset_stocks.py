import unittest
from unittest.mock import patch

from app import app, app_state


class ResetStocksTests(unittest.TestCase):
    def setUp(self):
        self._orig_testing = app.config.get("TESTING")
        self._orig_wtf_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        with app_state.market.user_stocks_lock:
            self._orig_us = dict(app_state.market.user_us)
            self._orig_jp = dict(app_state.market.user_jp)
            self._orig_idx = dict(app_state.market.user_idx)
        with app_state.cache.sse_data_lock:
            self._orig_cur_stocks = dict(app_state.market.current_stocks_cache)
            self._orig_tgt_stocks = dict(app_state.market.target_stocks_cache)
            self._orig_cur_indices = dict(app_state.market.current_indices_cache)
            self._orig_tgt_indices = dict(app_state.market.target_indices_cache)

    def tearDown(self):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = self._orig_us
            app_state.market.user_jp = self._orig_jp
            app_state.market.user_idx = self._orig_idx
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = self._orig_cur_stocks
            app_state.market.target_stocks_cache = self._orig_tgt_stocks
            app_state.market.current_indices_cache = self._orig_cur_indices
            app_state.market.target_indices_cache = self._orig_tgt_indices
        if self._orig_testing is None:
            app.config.pop("TESTING", None)
        else:
            app.config["TESTING"] = self._orig_testing
        if self._orig_wtf_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = self._orig_wtf_csrf

    def test_reset_clears_runtime_caches(self):
        with app.app_context():
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {"AAA": {"name": "AAA"}}
                app_state.market.user_jp = {"1111.T": {"name": "JP"}}
                app_state.market.user_idx = {"^TEST": {"name": "IDX"}}
            with app_state.cache.sse_data_lock:
                app_state.market.current_stocks_cache = {
                    "us": [{"symbol": "AAA"}],
                    "jp": [],
                    "idx": [],
                }
                app_state.market.target_stocks_cache = {
                    "us": [{"symbol": "AAA"}],
                    "jp": [],
                    "idx": [],
                }
                app_state.market.current_indices_cache = {"SP500": {"price": 1}}
                app_state.market.target_indices_cache = {"SP500": {"price": 1}}

            with (
                patch("routes.stocks.views.save_user_stocks") as mocked_save,
                patch(
                    "routes.stocks.views.schedule_sync_all_stocks_now", return_value=True
                ) as mocked_schedule,
            ):
                response = self.client.post(
                    "/api/stocks/reset",
                    headers={"Origin": "http://localhost:5000"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["success"])
            mocked_save.assert_called_once()
            mocked_schedule.assert_called_once()

            with app_state.cache.sse_data_lock:
                self.assertEqual(
                    app_state.market.current_stocks_cache, {"us": [], "jp": [], "idx": []}
                )
                self.assertEqual(
                    app_state.market.target_stocks_cache, {"us": [], "jp": [], "idx": []}
                )
                self.assertEqual(app_state.market.current_indices_cache, {})
                self.assertEqual(app_state.market.target_indices_cache, {})


if __name__ == "__main__":
    unittest.main()
