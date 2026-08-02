"""Regression tests for release-hardening fixes (persist/AI validation/bootstrap)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from app import app, bootstrap
from app_state import app_state
from error_codes import ErrorCode
from utils import storage


class StockPersistRollbackTestCase(unittest.TestCase):
    """Persisted stock mutations must roll back memory on any save failure."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        with app_state.market.user_stocks_lock:
            self._orig_us = dict(app_state.market.user_us)
            self._orig_jp = dict(app_state.market.user_jp)
            self._orig_idx = dict(app_state.market.user_idx)
            app_state.market.user_us.clear()
            app_state.market.user_jp.clear()
            app_state.market.user_idx.clear()

    def tearDown(self):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.clear()
            app_state.market.user_us.update(self._orig_us)
            app_state.market.user_jp.clear()
            app_state.market.user_jp.update(self._orig_jp)
            app_state.market.user_idx.clear()
            app_state.market.user_idx.update(self._orig_idx)

    def test_add_stock_rolls_back_when_master_key_raises(self):
        with (
            patch(
                "utils.storage.config_store.get_or_create_master_key",
                side_effect=RuntimeError("master key unavailable"),
            ),
            patch("routes.api_stocks.schedule_sync_all_stocks_now"),
            patch("app_bg.announce_current_market_state"),
        ):
            response = self.client.post(
                "/api/stocks/add",
                json={"symbol": "ROLLBAK1", "market": "us", "name": "Rollback Test"},
                headers={"Origin": "http://localhost:5000"},
            )
        self.assertEqual(response.status_code, 503)
        with app_state.market.user_stocks_lock:
            self.assertNotIn("ROLLBAK1", app_state.market.user_us)


class AIInputValidationTestCase(unittest.TestCase):
    """AI endpoints must reject non-string/non-list payloads with 400."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_chat_rejects_non_string_message(self):
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars-long!!"):
            response = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": "AAPL",
                    "message": {"evil": True},
                    "request_token": "validate-message-01",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertEqual(data.get("error_code"), int(ErrorCode.INVALID_INPUT))

    def test_analyze_v2_rejects_non_list_chart_data(self):
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars-long!!"):
            response = self.client.post(
                "/api/analyze-v2",
                json={
                    "market": "us",
                    "symbol": "AAPL",
                    "chart_data": 1,
                    "request_token": "validate-chart-0001",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertEqual(data.get("error_code"), int(ErrorCode.INVALID_INPUT))

    def test_analyze_v2_rejects_non_finite_price(self):
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars-long!!"):
            response = self.client.post(
                "/api/analyze-v2",
                json={
                    "market": "us",
                    "symbol": "AAPL",
                    "price": "not-a-number",
                    "request_token": "validate-price-0001",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(response.status_code, 400)


class BootstrapFailClosedTestCase(unittest.TestCase):
    """Core bootstrap failure must remain retryable and not mark ready."""

    def test_bootstrap_init_failure_keeps_done_false(self):
        import app as app_module

        with patch.dict(
            os.environ,
            {
                "MNS_ALLOW_REMOTE_API": "0",
                "MNS_SKIP_BOOTSTRAP": "",
                "MNS_ADMIN_TOKEN": "",
            },
            clear=False,
        ):
            os.environ.pop("MNS_ADMIN_TOKEN", None)
            with app_module._app_bootstrap_lock:
                was_done = app_module._app_bootstrap_done
                app_module._app_bootstrap_done = False
            app_state.bootstrap_ready.clear()
            try:
                with (
                    patch.object(
                        app_state,
                        "get_or_create_shutdown_token",
                        side_effect=RuntimeError("token store failed"),
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    bootstrap(app)
                self.assertFalse(app_module._app_bootstrap_done)
                self.assertFalse(app_state.bootstrap_ready.is_set())
            finally:
                with app_module._app_bootstrap_lock:
                    app_module._app_bootstrap_done = was_done


class StorageExceptionNormalizationTestCase(unittest.TestCase):
    def test_runtime_error_normalized_to_persist_error(self):
        with patch(
            "utils.storage.config_store.get_or_create_master_key",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(storage.UserStocksPersistError):
                storage.save_user_stocks()


if __name__ == "__main__":
    unittest.main()
