# tests/test_review_followup_20260814.py
"""Regression tests for review follow-up fixes (2026-08-14).

Covers findings R1-R8 from the read-only review:
 - R1 bootstrap safe_submit retry
 - R2 portfolio fields not carried into current_stocks_cache
 - R3 rate-limit by admin token fingerprint
 - R5 shutdown token restricted tmp perms
 - R7 backup umask typo
 - credentials envelope normalization (R4)
"""

import hashlib
import os

from app import bootstrap, create_app
from app_bg import _interpolate_and_fluctuate_market
from execution_state import ExecutionState
from routes.api_system import api_credentials
from utils.stock_payload import _PORTFOLIO_RESPONSE_FIELDS


class TestBootstrapHandlesQueueFull:
    def test_bootstrap_uses_safe_submit_and_retries(self, monkeypatch):
        """Bootstrap must use safe_submit and retry on queue full (R1)."""
        import app as app_mod

        calls = []

        def fake_safe_submit(name, fn, *a, **kw):
            calls.append(name)
            # First attempt for sync_refresh_executor fails, second succeeds
            if name == "sync_refresh_executor" and calls.count(name) == 1:
                return False
            return True

        monkeypatch.setattr(app_mod.app_state.execution, "safe_submit", fake_safe_submit)
        app2 = create_app(skip_bootstrap=True)
        # Manually clear done flag to force bootstrap path
        app_mod._app_bootstrap_done = False
        monkeypatch.setenv("MNS_SKIP_BOOTSTRAP", "0")
        try:
            # bootstrap will attempt safe_submit; second attempt should succeed
            # and not raise
            # Patch expensive side effects
            monkeypatch.setattr(app_mod.app_state, "get_or_create_shutdown_token", lambda: "tok")
            monkeypatch.setattr(app_mod.app_state, "initialize_yfinance_cache", lambda: None)
            import utils.storage as storage_mod

            monkeypatch.setattr(storage_mod, "load_user_stocks", lambda force=False: None)
            monkeypatch.setattr(app_mod, "_start_background_threads", lambda: None)
            bootstrap(app2)
            assert "sync_refresh_executor" in calls
            assert "news_executor" in calls
        finally:
            app_mod._app_bootstrap_done = False


class TestInterpolateDoesNotCarryHoldings:
    def test_holdings_stripped_on_existing_entry(self):
        target = [
            {
                "symbol": "AAPL",
                "market": "us",
                "currency": "USD",
                "shares": 10,
                "avg_price": 100.0,
                "portfolio_value": 1000.0,
                "portfolio_pl": 50.0,
                "price": 150.0,
                "change": 5.0,
            }
        ]
        current = [{"symbol": "AAPL", "price": 145.0, "shares": 99, "avg_price": 1.0}]
        out = _interpolate_and_fluctuate_market(target, current, True, "us")
        assert len(out) == 1
        for pk in ("shares", "avg_price", "portfolio_value", "portfolio_pl", "avg_fx_rate"):
            assert pk not in out[0], f"leaked {pk}"

    def test_holdings_stripped_on_new_entry(self):
        target = [
            {
                "symbol": "AAPL",
                "market": "us",
                "currency": "USD",
                "shares": 10,
                "avg_price": 100.0,
                "portfolio_value": 1000.0,
                "price": 150.0,
                "change": 5.0,
            }
        ]
        out = _interpolate_and_fluctuate_market(target, [], True, "us")
        for pk in ("shares", "avg_price", "portfolio_value", "portfolio_pl", "avg_fx_rate"):
            assert pk not in out[0]

    def test_normal_flow_preserves_market_fields(self):
        target = [
            {"symbol": "AAPL", "market": "us", "currency": "USD", "price": 150.0, "change": 5.0}
        ]
        out = _interpolate_and_fluctuate_market(target, [], True, "us")
        assert out[0]["market"] == "us"
        assert "price" in out[0]


class TestRateLimitAdminTokenIsolation:
    def test_admin_token_bucket_independent_from_anonymous(self, monkeypatch):
        """Authed token bucket must not be starved by anonymous flood (R3)."""
        from route_helpers import _rate_limit_lock, _rate_limit_store

        # Isolate rate-limit state for this test; global store is shared across
        # tests and prior failures may have left stale buckets.
        with _rate_limit_lock:
            _rate_limit_store.clear()
        app2 = create_app(skip_bootstrap=True)
        app2.config["TESTING"] = True
        client = app2.test_client()
        monkeypatch.setenv("MNS_ADMIN_TOKEN", "x" * 32)
        # First, an authed request must succeed even after anon flood,
        # because the authed bucket (adm:<hash>) is separate from IP bucket.
        # Do anonymous flood on a dedicated IP + endpoint.
        for _ in range(70):
            client.get("/api/health", environ_base={"REMOTE_ADDR": "192.168.1.210"})
        resp_anon = client.get("/api/health", environ_base={"REMOTE_ADDR": "192.168.1.210"})
        assert resp_anon.status_code == 429
        # Same IP but with valid admin header should hit a DIFFERENT bucket -> 200
        resp_authed = client.get(
            "/api/health",
            environ_base={"REMOTE_ADDR": "192.168.1.210"},
            headers={"X-MNS-Admin-Token": "x" * 32},
        )
        assert resp_authed.status_code == 200, resp_authed.get_data(as_text=True)[:500]
        # Clear admin token for other tests
        monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
        with _rate_limit_lock:
            _rate_limit_store.clear()


class TestShutdownTokenRestrictedTmp:
    def test_persist_used_marker_uses_restricted_open(self, monkeypatch, tmp_path):
        """_persist_used_marker must use os.open 0o600 path instead of write_text (R5)."""
        from shutdown_manager import ShutdownTokenManager

        mgr = ShutdownTokenManager()
        mgr.runtime_state_dir = tmp_path
        mgr.token_file = tmp_path / ".mns_shutdown_token"
        mgr.used_marker = tmp_path / ".mns_shutdown_token.used"
        mgr.shutdown_token = "tok"
        # Introspect that _persist_used_marker delegates to _write_atomic_restricted
        import inspect

        src = inspect.getsource(mgr._persist_used_marker)
        assert "_write_atomic_restricted" in src
        ok = mgr._persist_used_marker()
        assert ok is True
        assert mgr.used_marker.exists()

    def test_rotate_uses_restricted_open(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        from shutdown_manager import ShutdownTokenManager

        mgr = ShutdownTokenManager()
        mgr.runtime_state_dir = tmp_path
        mgr.token_file = tmp_path / ".mns_shutdown_token"
        mgr.used_marker = tmp_path / ".mns_shutdown_token.used"
        # need master key for protect_data; set env
        monkeypatch.setenv("MNS_MASTER_KEY", "Ij2VbZwpP-Du-IHWL5VUPKL8BHUXUbddJY7JNj4xJ6g=")
        seen_modes = []

        orig_open = os.open

        def tracking_open(path, flags, mode=0o777):
            if ".tmp" in str(path):
                seen_modes.append(mode)
            return orig_open(path, flags, mode)

        monkeypatch.setattr(os, "open", tracking_open)
        mgr.rotate_shutdown_token()
        # At least one restricted mode seen
        assert any(m == 0o600 for m in seen_modes)


class TestCredentialsEnvelope:
    def test_remote_without_token_returns_typed_403(self, monkeypatch):
        """Remote probe without token must return typed ErrorCode 403, not plaintext 503 (R4)."""
        app2 = create_app(skip_bootstrap=True)
        app2.config["TESTING"] = True
        app2.config["WTF_CSRF_ENABLED"] = False
        monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
        monkeypatch.setenv("MNS_ADMIN_TOKEN", "")
        client = app2.test_client()
        resp = client.get("/api/credentials")
        assert resp.status_code == 403
        data = resp.get_json()
        assert data.get("error_code") == 1403
        assert data.get("ok") is False

    def test_invalid_token_returns_typed_403(self, monkeypatch):
        app2 = create_app(skip_bootstrap=True)
        app2.config["TESTING"] = True
        app2.config["WTF_CSRF_ENABLED"] = False
        monkeypatch.setenv("MNS_ADMIN_TOKEN", "z" * 32)
        monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)
        client = app2.test_client()
        resp = client.get("/api/credentials", headers={"X-MNS-Admin-Token": "wrong"})
        assert resp.status_code == 403
        assert resp.get_json().get("error_code") == 1403


class TestBackupUmaskTypoFixed:
    def test_backup_uses_0o077(self):
        """config_store backup must use 0o077, not 0o177 (R7)."""
        import pathlib

        import config_store

        src = pathlib.Path(config_store.__file__).read_text(encoding="utf-8")
        assert "os.umask(0o177)" not in src
        assert "os.umask(0o077)" in src
