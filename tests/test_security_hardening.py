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
            "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
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
                    "X-MNS-Admin-Token": "test-admin-token-0123456789abcdef",
                },
            )
            self.assertEqual(allowed.status_code, 200)

    def test_api_stocks_requires_admin_token_in_remote_mode(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
        }
        with patch.dict(os.environ, env, clear=False):
            denied = client.get("/api/stocks")
            self.assertEqual(denied.status_code, 403)

            allowed = client.get(
                "/api/stocks",
                headers={"X-MNS-Admin-Token": "test-admin-token-0123456789abcdef"},
            )
            self.assertEqual(allowed.status_code, 200)

            # Query-param admin token must NOT be accepted on non-SSE endpoints:
            # it would leak the secret into access logs / proxies / history.
            denied_qp = client.get("/api/stocks?token=test-admin-token-0123456789abcdef")
            self.assertEqual(denied_qp.status_code, 403)

            denied_qp2 = client.get("/api/stocks?admin_token=test-admin-token-0123456789abcdef")
            self.assertEqual(denied_qp2.status_code, 403)

    def test_api_stocks_stream_requires_admin_token_in_remote_mode(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
        }
        with patch.dict(os.environ, env, clear=False):
            denied = client.get("/api/stocks/stream")
            self.assertEqual(denied.status_code, 403)

            allowed = client.get(
                "/api/stocks/stream",
                headers={"X-MNS-Admin-Token": "test-admin-token-0123456789abcdef"},
            )
            self.assertEqual(allowed.status_code, 200)

            # Check query param authentication
            allowed_qp = client.get("/api/stocks/stream?token=test-admin-token-0123456789abcdef")
            self.assertEqual(allowed_qp.status_code, 200)

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

    def test_remote_mode_with_weak_admin_token_returns_503(self):
        with patch.dict(
            os.environ,
            {"MNS_ALLOW_REMOTE_API": "1", "MNS_ADMIN_TOKEN": "too-short"},
            clear=False,
        ):
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
            "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
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
                # MNS-003: portfolio updates require the symbol to already be in
                # the watch list. Seed it so this test still exercises the
                # admin-token gate (which is the point of this test) rather than
                # the unregistered-symbol 404.
                if path == "/api/stocks/portfolio":
                    with app_state.market.user_stocks_lock:
                        app_state.market.user_us[payload["symbol"]] = payload["symbol"]
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
                        "X-MNS-Admin-Token": "test-admin-token-0123456789abcdef",
                    },
                )
                self.assertEqual(
                    allowed.status_code,
                    200,
                    f"{path} did not accept correct admin token: {allowed.get_data(as_text=True)}",
                )


class AdminTokenQueryParamRestrictionTestCase(unittest.TestCase):
    """MNS-2026-01: admin token in the URL must be SSE-only.

    EventSource cannot send headers, so /api/stocks/stream legitimately accepts
    the token as a query param. Every other gated endpoint must reject it so
    the secret is never exposed in access logs, reverse proxies, or browser
    history.
    """

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self.env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
        }

    def test_sse_stream_accepts_query_token(self):
        with patch.dict(os.environ, self.env, clear=False):
            resp = self.client.get("/api/stocks/stream?token=test-admin-token-0123456789abcdef")
            self.assertNotEqual(
                resp.status_code,
                403,
                "SSE stream must accept the admin token via query param",
            )

    def test_non_sse_endpoint_rejects_query_token(self):
        with patch.dict(os.environ, self.env, clear=False):
            # GET endpoints: /api/stocks rejects query-param token (header required).
            resp = self.client.get(
                "/api/stocks?token=test-admin-token-0123456789abcdef",
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(
                resp.status_code,
                403,
                "/api/stocks must NOT accept the admin token via query param",
            )
            resp2 = self.client.get(
                "/api/stocks?admin_token=test-admin-token-0123456789abcdef",
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(
                resp2.status_code,
                403,
                "/api/stocks must NOT accept admin_token via query param",
            )

            # POST endpoint: /api/stocks/portfolio/snapshot rejects query-param token.
            snap = self.client.post(
                "/api/stocks/portfolio/snapshot?token=test-admin-token-0123456789abcdef",
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(
                snap.status_code,
                403,
                "/api/stocks/portfolio/snapshot must NOT accept token via query param",
            )

    def test_non_sse_endpoint_accepts_header_token(self):
        with patch.dict(os.environ, self.env, clear=False):
            resp = self.client.get(
                "/api/stocks",
                headers={
                    "Origin": "http://localhost:5000",
                    "X-MNS-Admin-Token": "test-admin-token-0123456789abcdef",
                },
            )
            self.assertEqual(resp.status_code, 200)


class MaskSensitiveUrlTestCase(unittest.TestCase):
    """MNS-2026-01: secret-bearing query params must be redacted in logs."""

    def test_masks_admin_token(self):
        from utils.networking import mask_sensitive_url

        self.assertEqual(
            mask_sensitive_url("/api/stocks/stream?token=supersecret"),
            "/api/stocks/stream?token=[REDACTED]",
        )
        self.assertEqual(
            mask_sensitive_url("/api/stocks/stream?admin_token=supersecret"),
            "/api/stocks/stream?admin_token=[REDACTED]",
        )

    def test_preserves_non_sensitive_params(self):
        from utils.networking import mask_sensitive_url

        self.assertEqual(
            mask_sensitive_url("/api/stocks?force=true&market=us"),
            "/api/stocks?force=true&market=us",
        )

    def test_no_query_unchanged(self):
        from utils.networking import mask_sensitive_url

        self.assertEqual(mask_sensitive_url("/api/stocks"), "/api/stocks")


class RemoteMarketDataAuthorizationTestCase(unittest.TestCase):
    """Remote mode must gate every endpoint that can trigger market work."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_market_data_routes_reject_missing_admin_token(self):
        env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
        }
        with patch.dict(os.environ, env, clear=False):
            requests = [
                ("/api/indices?force=true", "get"),
                ("/api/stock-details?symbol=AAPL&market=us", "get"),
                ("/api/stock-history?symbol=AAPL&market=us&period=1d", "get"),
                ("/api/search?q=apple", "get"),
                ("/api/heatmap?market=us", "get"),
                ("/api/trending?market=us", "get"),
            ]
            for path, method in requests:
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 403, path)

    def test_market_data_routes_accept_header_admin_token(self):
        env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self.client.get(
                "/api/indices",
                headers={"X-MNS-Admin-Token": "test-admin-token-0123456789abcdef"},
            )
            self.assertNotEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
