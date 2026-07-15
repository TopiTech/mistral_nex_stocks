"""Regression tests for high-priority security / data-integrity fixes (H-1..H-6)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_store
import utils.storage as storage
from app import app, bootstrap
from utils.stock_payload import _resolve_stocks_for_response
from app_state import app_state


class LoadConfigDeepCopyTestCase(unittest.TestCase):
    """H-1: load_config must never return a mutable reference to the cache."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_file = Path(self.temp_dir.name) / "config.json"
        self.patcher = patch.object(config_store, "CONFIG_FILE", self.config_file)
        self.addCleanup(self.patcher.stop)
        self.patcher.start()
        # Force cache reset so each test starts clean
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None

    def test_load_config_returns_isolated_copy(self):
        config_store.save_config({"mistral_model": "mistral-small-latest", "api_credentials": {}})
        cfg_a = config_store.load_config()
        cfg_a["mistral_model"] = "MUTATED"
        cfg_a.setdefault("api_credentials", {})["injected"] = "bad"
        cfg_b = config_store.load_config()
        self.assertEqual(cfg_b["mistral_model"], "mistral-small-latest")
        self.assertNotIn("injected", cfg_b.get("api_credentials", {}))
        self.assertIsNot(cfg_a, cfg_b)


class PortfolioStripTestCase(unittest.TestCase):
    """H-3: portfolio fields must not appear on unauthenticated stock responses."""

    def setUp(self):
        with app_state.cache.sse_data_lock:
            self._saved_current = app_state.market.current_stocks_cache
            self._saved_target = app_state.market.target_stocks_cache
            app_state.market.current_stocks_cache = {
                "us": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple",
                        "price": "100",
                        "shares": 12,
                        "avg_price": 150.5,
                        "avg_fx_rate": 150.0,
                        "portfolio_value": "1800",
                        "portfolio_pl": "100",
                    }
                ],
                "jp": [],
                "idx": [],
            }
            app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}

    def tearDown(self):
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = self._saved_current
            app_state.market.target_stocks_cache = self._saved_target

    def test_resolve_strips_portfolio_by_default(self):
        stocks = _resolve_stocks_for_response()
        row = stocks["us"][0]
        self.assertEqual(row["symbol"], "AAPL")
        for key in ("shares", "avg_price", "avg_fx_rate", "portfolio_value", "portfolio_pl"):
            self.assertNotIn(key, row)

    def test_resolve_can_include_portfolio_when_requested(self):
        stocks = _resolve_stocks_for_response(include_portfolio=True)
        row = stocks["us"][0]
        self.assertEqual(row["shares"], 12)
        self.assertEqual(row["avg_price"], 150.5)

    def test_api_stocks_strips_portfolio(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        response = client.get("/api/stocks")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        rows = (data.get("stocks") or {}).get("us") or []
        self.assertTrue(rows)
        for key in ("shares", "avg_price", "avg_fx_rate", "portfolio_value", "portfolio_pl"):
            self.assertNotIn(key, rows[0])


class UserStocksPersistTestCase(unittest.TestCase):
    """H-5: Windows lock busy / write failure must raise, not silent-skip."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.stocks_file = Path(self.temp_dir.name) / "user_stocks.json"
        self.patcher = patch.object(storage, "USER_STOCKS_FILE", str(self.stocks_file))
        self.addCleanup(self.patcher.stop)
        self.patcher.start()
        app_state.market.user_us = {"AAPL": "Apple"}
        app_state.market.user_jp = {}
        app_state.market.user_idx = {}

    def test_missing_tmp_after_lock_write_raises(self):
        def fake_write(_data, tmp_file, _lock_file):
            # Simulate "skipped" write that leaves no tmp file
            if Path(tmp_file).exists():
                Path(tmp_file).unlink()

        with patch.object(storage, "_write_user_stocks_with_lock", side_effect=fake_write):
            with self.assertRaises(storage.UserStocksPersistError):
                storage.save_user_stocks()


class CredentialsAdminTokenTestCase(unittest.TestCase):
    """H-2 / H-6: admin token + remote API fail-closed behaviour."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_local_get_without_admin_token_still_works(self):
        env = {k: v for k, v in os.environ.items() if k != "MNS_ADMIN_TOKEN"}
        env["MNS_ALLOW_REMOTE_API"] = "0"
        with patch.dict(os.environ, env, clear=True):
            response = self.client.get("/api/credentials")
            self.assertEqual(response.status_code, 200)

    def test_admin_token_required_when_configured(self):
        with patch.dict(
            os.environ,
            {"MNS_ADMIN_TOKEN": "super-secret-admin-token-32chars!!", "MNS_ALLOW_REMOTE_API": "0"},
            clear=False,
        ):
            denied = self.client.get("/api/credentials")
            self.assertEqual(denied.status_code, 403)
            allowed = self.client.get(
                "/api/credentials",
                headers={"X-MNS-Admin-Token": "super-secret-admin-token-32chars!!"},
            )
            self.assertEqual(allowed.status_code, 200)

    def test_remote_mode_without_admin_token_returns_503(self):
        env = {k: v for k, v in os.environ.items() if k != "MNS_ADMIN_TOKEN"}
        env["MNS_ALLOW_REMOTE_API"] = "1"
        with patch.dict(os.environ, env, clear=True):
            response = self.client.get("/api/credentials")
            self.assertEqual(response.status_code, 503)


class BootstrapRemoteGuardTestCase(unittest.TestCase):
    """H-6: bootstrap must refuse remote API without admin token."""

    def test_bootstrap_raises_without_admin_token(self):
        import app as app_module

        with patch.dict(
            os.environ,
            {"MNS_ALLOW_REMOTE_API": "1", "MNS_ADMIN_TOKEN": "", "MNS_SKIP_BOOTSTRAP": ""},
            clear=False,
        ):
            os.environ.pop("MNS_ADMIN_TOKEN", None)
            # Reset bootstrap flag so the guard re-runs
            with app_module._app_bootstrap_lock:
                was_done = app_module._app_bootstrap_done
                app_module._app_bootstrap_done = False
            try:
                with self.assertRaises(RuntimeError):
                    bootstrap(app)
                # Flag must remain False so a corrected env can retry
                self.assertFalse(app_module._app_bootstrap_done)
            finally:
                with app_module._app_bootstrap_lock:
                    app_module._app_bootstrap_done = was_done


class ExtensionOriginRequiredTestCase(unittest.TestCase):
    """H-4: add_ext must reject missing Origin even with a valid token."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_missing_origin_forbidden(self):
        from credential_manager import get_or_create_extension_api_token

        token = get_or_create_extension_api_token()
        response = self.client.post(
            "/api/stocks/add_ext",
            headers={"Authorization": f"Bearer {token}"},
            data=json.dumps({"symbol": "AAPL", "market": "us"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
