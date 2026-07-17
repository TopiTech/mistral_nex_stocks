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

    def test_portfolio_snapshot_requires_trusted_request(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()

        denied = client.post("/api/stocks/portfolio/snapshot")
        self.assertEqual(denied.status_code, 403)

        allowed = client.post(
            "/api/stocks/portfolio/snapshot",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(allowed.status_code, 200)
        row = allowed.get_json()["stocks"]["us"][0]
        self.assertEqual(row["shares"], 12)
        self.assertEqual(row["avg_price"], 150.5)

    def test_portfolio_snapshot_requires_admin_token_in_remote_mode(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_ADMIN_TOKEN": "test-admin-token",
        }
        with patch.dict(os.environ, env, clear=False):
            denied = client.post(
                "/api/stocks/portfolio/snapshot",
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(denied.status_code, 403)

            allowed = client.post(
                "/api/stocks/portfolio/snapshot",
                headers={
                    "Origin": "http://localhost:5000",
                    "X-MNS-Admin-Token": "test-admin-token",
                },
            )
            self.assertEqual(allowed.status_code, 200)

    def test_api_stocks_stream_strips_portfolio(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        response = client.get("/api/stocks/stream", headers={"Origin": "http://localhost:5000"})
        self.assertEqual(response.status_code, 200)

        chunks = []
        for chunk in response.response:
            chunks.append(chunk.decode("utf-8"))
            if len(chunks) >= 2:
                break

        full_text = "".join(chunks)
        self.assertIn("initial_snapshot", full_text)

        data_line = None
        for line in full_text.split("\n"):
            if line.startswith("data: "):
                data_line = line[len("data: ") :].strip()
                break

        self.assertIsNotNone(data_line)
        payload = json.loads(data_line)
        stocks_data = payload.get("stocks") or {}
        rows = stocks_data.get("us") or []
        self.assertTrue(rows)
        for key in ("shares", "avg_price", "avg_fx_rate", "portfolio_value", "portfolio_pl"):
            self.assertNotIn(key, rows[0])

    def test_api_stocks_stream_limit_exceeded(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()

        from constants import MAX_SSE_LISTENERS
        from app_state import app_state
        from error_codes import ErrorCode

        with patch.object(app_state.sse_announcer, "listener_count", return_value=MAX_SSE_LISTENERS):
            response = client.get("/api/stocks/stream", headers={"Origin": "http://localhost:5000"})
            self.assertEqual(response.status_code, 429)
            data = json.loads(response.data.decode("utf-8"))
            self.assertEqual(data["error_code"], int(ErrorCode.TOO_MANY_REQUESTS))

    def test_api_stocks_stream_keepalive(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()

        response = client.get("/api/stocks/stream", headers={"Origin": "http://localhost:5000"})
        self.assertEqual(response.status_code, 200)

        iterator = iter(response.response)

        # First chunk is initial snapshot
        first_chunk = next(iterator).decode("utf-8")
        self.assertIn("initial_snapshot", first_chunk)

        # Wait for keepalive chunk (times out after 2.0s)
        import time
        t0 = time.time()
        second_chunk = next(iterator).decode("utf-8")
        duration = time.time() - t0

        self.assertGreaterEqual(duration, 1.8)
        self.assertEqual(second_chunk, ": keepalive\n\n")


class UserStocksRouteRollbackTestCase(unittest.TestCase):
    """Persistence failures must not leave memory ahead of disk state."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        with app_state.market.user_stocks_lock:
            self._saved_us = app_state.market.user_us.copy()
            self._saved_jp = app_state.market.user_jp.copy()
            self._saved_idx = app_state.market.user_idx.copy()
            app_state.market.user_us = {"AAPL": "Apple"}
            app_state.market.user_jp = {}
            app_state.market.user_idx = {}

    def tearDown(self):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = self._saved_us
            app_state.market.user_jp = self._saved_jp
            app_state.market.user_idx = self._saved_idx

    @patch(
        "routes.api_stocks.save_user_stocks",
        side_effect=storage.UserStocksPersistError("disk full"),
    )
    def test_delete_restores_memory_when_persist_fails(self, _mock_save):
        response = self.client.post(
            "/api/stocks/delete",
            json={"symbol": "AAPL", "market": "us"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(app_state.market.user_us, {"AAPL": "Apple"})

    @patch(
        "routes.api_stocks.save_user_stocks",
        side_effect=storage.UserStocksPersistError("disk full"),
    )
    def test_add_does_not_mutate_memory_when_persist_fails(self, _mock_save):
        response = self.client.post(
            "/api/stocks/add",
            json={"symbol": "ZZTEST", "name": "Test Corporation", "market": "us"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("ZZTEST", app_state.market.user_us)


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


class StockMutationAdminTokenRemoteGuardTestCase(unittest.TestCase):
    """Mutating stock/portfolio endpoints must require the admin token in remote mode."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    @patch("routes.api_stocks.save_user_stocks", return_value=None)
    def test_mutations_require_admin_token_in_remote_mode(self, _mock_save):
        env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_ADMIN_TOKEN": "test-admin-token",
        }
        endpoints = [
            ("/api/stocks/add", {"symbol": "TESTSEC", "name": "Test Secure", "market": "us"}),
            ("/api/stocks/delete", {"symbol": "TESTSEC", "market": "us"}),
            (
                "/api/stocks/portfolio",
                {"symbol": "TESTSEC", "market": "us", "shares": 10, "avg_price": 150.0},
            ),
            ("/api/stocks/reset", {}),
        ]
        with patch.dict(os.environ, env, clear=False):
            for path, payload in endpoints:
                # 1. Reject without token
                denied = self.client.post(
                    path,
                    json=payload,
                    headers={"Origin": "http://localhost:5000"},
                )
                self.assertEqual(
                    denied.status_code, 403, f"{path} did not reject missing admin token"
                )

                # 2. Reject with invalid token
                denied_bad = self.client.post(
                    path,
                    json=payload,
                    headers={
                        "Origin": "http://localhost:5000",
                        "X-MNS-Admin-Token": "wrong-token",
                    },
                )
                self.assertEqual(
                    denied_bad.status_code, 403, f"{path} did not reject wrong admin token"
                )

                # 3. Accept with correct token
                allowed = self.client.post(
                    path,
                    json=payload,
                    headers={
                        "Origin": "http://localhost:5000",
                        "X-MNS-Admin-Token": "test-admin-token",
                    },
                )
                self.assertEqual(
                    allowed.status_code,
                    200,
                    f"{path} did not accept correct admin token: {allowed.get_data(as_text=True)}",
                )


if __name__ == "__main__":
    unittest.main()
