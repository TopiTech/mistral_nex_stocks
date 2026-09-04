"""Regression tests for review findings CORE-1 / CORE-2 (app.py).

Scope:
  * CORE-1 [High]: /api/shutdown is CSRF-exempt, but the endpoint itself
    still enforces Origin / Sec-Fetch-Site verification. These tests pin that
    the defense-in-depth contract holds so the CSRF exemption can never be
    widened into an unprotected shutdown path.
  * CORE-2 [Medium]: When FLASK_SECRET_KEY is unset, the auto-generated key
    is persisted to the config file. A persistence failure must NOT make the
    app unable to start: _configure_secret_key falls back to an in-memory key
    and startup continues.
"""

import json
import os
from unittest.mock import patch

import pytest


def _without_production_env():
    """Return a context where the app is NOT in production mode.

    _is_production_env() returns True when MNS_PROD or remote-API mode is
    active. Tests for the auto-generated key path must clear both so the
    dev fallback branch is exercised.
    """
    return patch.dict(
        os.environ,
        {
            "MNS_PROD": "0",
            "MNS_ALLOW_REMOTE_API": "0",
            "MNS_PROXY_FIX": "0",
        },
        clear=False,
    )


class TestCORE1ShutdownOriginVerification:
    """CORE-1: CSRF exemption must not disable Origin/Sec-Fetch-Site checks.

    /api/shutdown is exempted from Flask-WTF CSRF (see app.py:227) and is
    listed in _csrf_exempt_post_paths. The verification gap only exists if a
    request can reach shutdown work without (a) an allowed Origin and (b) a
    valid one-time token. These tests pin that both gates fire.
    """

    def _post_shutdown(self, client, headers, environ=None):
        return client.post(
            "/api/shutdown",
            data=json.dumps({"confirm": True}),
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "127.0.0.1", **(environ or {})},
            headers=headers,
        )

    def test_shutdown_cross_site_untrusted_origin_blocked(self, client, monkeypatch):
        """cross-site + untrusted Origin is rejected before token validation.

        _enforce_sec_fetch_site_check runs for every request (app.py:152).
        Even though /api/shutdown is CSRF-exempt, a cross-site request from an
        origin outside the allow-list must be blocked by the Sec-Fetch-Site
        gate regardless of MNS_STRICT_SEC_FETCH_SITE.
        """
        monkeypatch.delenv("MNS_STRICT_SEC_FETCH_SITE", raising=False)
        with client.application.test_request_context(
            "/api/shutdown",
            method="POST",
            json={"confirm": True},
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Origin": "http://evil.example",
            },
        ):
            from app import _enforce_sec_fetch_site_check

            result = _enforce_sec_fetch_site_check()
            assert result is not None
            _, status = result
            assert status == 403

    def test_shutdown_missing_token_rejected(self, client):
        """Even with a trusted Origin, a missing one-time token is rejected.

        This is the fail-closed backstop when no token has been generated
        (shutdown_token is None) or when the caller simply omits it.
        """
        response = self._post_shutdown(
            client,
            headers={"Origin": "http://localhost:5000"},
        )
        assert response.status_code == 403

    def test_shutdown_invalid_token_rejected(self, client):
        """A trusted Origin + wrong token must not reach shutdown work."""
        response = self._post_shutdown(
            client,
            headers={"Origin": "http://localhost:5000", "X-MNS-Shutdown-Token": "invalid"},
        )
        assert response.status_code == 403

    def test_shutdown_untrusted_origin_always_blocked(self, client, monkeypatch):
        """The endpoint-level origin gate is independent of the strict flag.

        _is_allowed_shutdown_origin() is called unconditionally in
        api_shutdown() (routes/api_system.py:759), so even with
        MNS_STRICT_SEC_FETCH_SITE=0 (default) a non-allowed Origin is blocked.
        """
        monkeypatch.setenv("MNS_STRICT_SEC_FETCH_SITE", "0")
        response = self._post_shutdown(
            client,
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403

    def test_shutdown_strict_flag_none_passes_sec_fetch_gate(self, client, monkeypatch):
        """Strict mode: /api/shutdown is CSRF-exempt, so R5 lets 'none' through.

        /api/shutdown is in _csrf_exempt_post_paths (app.py:554). Under
        MNS_STRICT_SEC_FETCH_SITE=1, a Sec-Fetch-Site:none request with no
        Origin is deliberately NOT blocked by the R5 hook — the endpoint
        itself enforces Origin + one-time token (routes/api_system.py:759).
        This pins the existing defense contract so the hook is not later made
        to double-block the CSRF-exempt path.
        """
        monkeypatch.setenv("MNS_STRICT_SEC_FETCH_SITE", "1")
        with client.application.test_request_context(
            "/api/shutdown",
            method="POST",
            json={"confirm": True},
            headers={"Sec-Fetch-Site": "none"},
        ):
            from app import _enforce_sec_fetch_site_check

            result = _enforce_sec_fetch_site_check()
            assert result is None

        # Defense-in-depth still holds end-to-end: the endpoint's own origin
        # gate rejects an untrusted Origin even when the hook passes it.
        response = self._post_shutdown(
            client,
            headers={"Sec-Fetch-Site": "none", "Origin": "http://evil.example"},
        )
        assert response.status_code == 403


class TestCORE2SecretKeyPersistenceFailure:
    """CORE-2: A config persistence failure must not prevent startup.

    get_or_create_flask_secret_key() raises when save_config fails (e.g.
    read-only APP_DATA_DIR, disk full, Windows lock contention). Previously
    _configure_secret_key propagated the exception and the app could not
    start. The fix falls back to an in-memory key with a warning.
    """

    def test_persistence_failure_still_starts_app(self, monkeypatch):
        """A RuntimeError from the secret store must not abort create_app."""
        from app import _configure_secret_key

        with _without_production_env():
            monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
            import credential_manager as cm_mod

            # _configure_secret_key does `from credential_manager import
            # get_or_create_flask_secret_key` at call time, so patching the
            # module attribute is enough to simulate a persistence failure.
            def _boom():
                raise RuntimeError("Failed to persist generated master key")

            monkeypatch.setattr(cm_mod, "get_or_create_flask_secret_key", _boom)

            class _FakeApp:
                def __init__(self):
                    self.secret_key = None

            fake_app = _FakeApp()
            _configure_secret_key(fake_app)

            # Startup continued: an in-memory key is assigned.
            assert fake_app.secret_key is not None
            assert len(fake_app.secret_key) >= 32

    def test_persistence_failure_logs_warning(self, monkeypatch):
        """The persistence failure must surface as a warning, not an abort."""

        from app import _configure_secret_key

        with _without_production_env():
            monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
            import credential_manager as cm_mod

            def _boom():
                raise OSError("disk full")

            monkeypatch.setattr(cm_mod, "get_or_create_flask_secret_key", _boom)

            class _FakeApp:
                def __init__(self):
                    self.secret_key = None

            fake_app = _FakeApp()
            with patch("app.logger.warning") as mock_warning:
                _configure_secret_key(fake_app)

            assert fake_app.secret_key is not None
            assert len(fake_app.secret_key) >= 32
            # A warning mentioning the fallback was emitted.
            assert any("in-memory key" in str(call) for call in mock_warning.call_args_list)

    def test_production_fail_closed_preserved(self, monkeypatch):
        """Production without FLASK_SECRET_KEY still fails closed.

        The CORE-2 fix must NOT relax the production guard: a production
        environment without FLASK_SECRET_KEY still raises ValueError.
        """
        from app import _configure_secret_key

        with patch.dict(
            os.environ,
            {"MNS_PROD": "1", "MNS_ALLOW_REMOTE_API": "0", "MNS_PROXY_FIX": "0"},
            clear=False,
        ):
            monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)

            class _FakeApp:
                def __init__(self):
                    self.secret_key = None

            with pytest.raises(ValueError):
                _configure_secret_key(_FakeApp())

    def test_short_env_secret_key_rejected(self, monkeypatch):
        """A short FLASK_SECRET_KEY is still rejected before any fallback."""
        from app import _configure_secret_key

        with _without_production_env():
            monkeypatch.setenv("FLASK_SECRET_KEY", "short")

            class _FakeApp:
                def __init__(self):
                    self.secret_key = None

            with pytest.raises(ValueError):
                _configure_secret_key(_FakeApp())
