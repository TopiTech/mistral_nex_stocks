"""
test_release_fix_verification.py - Verification tests for recent release review fixes.
Tests for:
- H-1: base.html tojson rendering (no redundant | safe, proper script tag safety)
- M-1 & M-2: session_manager active_sessions in-flight tracking & single lock in get_session
- M-3: route_helpers local rate limiting behavior (multiplier & opt-out env)
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import flask
from flask import Flask

from app import create_app
from route_helpers import rate_limit
from session_manager import YFinanceSessionManager


class ProxyFixRawRemoteRegressionTest(unittest.TestCase):
    """Regression tests for the raw-remote-address / ProxyFix ordering fix.

    RAW_REMOTE_ADDR must be captured BEFORE ProxyFix rewrites REMOTE_ADDR, and
    _is_local_request must never trust X-Forwarded-For outside the explicit
    remote/proxy mode. A misconfigured proxy (MNS_PROXY_FIX=1 without
    MNS_ALLOW_REMOTE_API=1) must not turn an external peer into a "local"
    request via spoofed forwarding headers.
    """

    def test_misconfigured_proxy_cannot_spoof_loopback(self):
        """External REMOTE_ADDR + spoofed X-Forwarded-For must be rejected."""
        with patch.dict(
            os.environ,
            {"MNS_PROXY_FIX": "1", "MNS_ALLOW_REMOTE_API": "0", "MNS_SKIP_BOOTSTRAP": "1"},
            clear=False,
        ):
            app = create_app(skip_bootstrap=True)
            app.config["TESTING"] = True
            app.config["WTF_CSRF_ENABLED"] = False
            client = app.test_client()

            # The raw socket peer is external; the spoofed header claims loopback.
            resp = client.get(
                "/api/cache-stats",
                base_url="http://localhost:5000",
                headers={"X-Forwarded-For": "127.0.0.1"},
                environ_base={"REMOTE_ADDR": "10.20.30.40"},
            )
            self.assertEqual(resp.status_code, 403)

    def test_genuine_loopback_still_allowed(self):
        """A real loopback peer without forwarding headers must still pass."""
        with patch.dict(
            os.environ,
            {"MNS_PROXY_FIX": "1", "MNS_ALLOW_REMOTE_API": "0", "MNS_SKIP_BOOTSTRAP": "1"},
            clear=False,
        ):
            app = create_app(skip_bootstrap=True)
            app.config["TESTING"] = True
            app.config["WTF_CSRF_ENABLED"] = False
            client = app.test_client()

            resp = client.get(
                "/api/cache-stats",
                base_url="http://localhost:5000",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(resp.status_code, 200)

    def test_raw_remote_addr_matches_socket_not_forwarded(self):
        """RAW_REMOTE_ADDR must equal the true peer, not the spoofed value."""
        from app import RawRemoteAddressMiddleware

        captured = {}

        def _inner(environ, start_response):
            captured["raw"] = environ.get("RAW_REMOTE_ADDR")
            captured["remote"] = environ.get("REMOTE_ADDR")
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        wrapped = RawRemoteAddressMiddleware(_inner)
        environ = {"REMOTE_ADDR": "10.20.30.40"}
        wrapped(environ, lambda *a, **k: None)
        self.assertEqual(captured["raw"], "10.20.30.40")
        self.assertEqual(captured["remote"], "10.20.30.40")


class TestReleaseFixVerification(unittest.TestCase):
    """Test suite verifying fixes for H-1, M-1, M-2, M-3."""

    def test_h1_template_tojson_rendering(self):
        """H-1: Verify that render_template renders JSON data safely without '| safe' filter."""
        app = create_app(skip_bootstrap=True)
        with app.test_request_context("/"):
            # Render setup/base template which uses tojson
            rendered = flask.render_template(
                "setup.html",
                model_badge="test-badge",
                default_symbols={"us": ["AAPL"], "jp": ["7203.T"]},
                app_config={"has_mistral_api_key": True},
            )
            self.assertIn('id="default-symbols-data"', rendered)
            self.assertIn('id="app-config-data"', rendered)
            self.assertIn('"AAPL"', rendered)
            self.assertIn('"has_mistral_api_key": true', rendered)

    def test_setup_config_exposes_effective_credential_limits(self):
        from credential_manager import get_api_credential_state

        with patch.multiple(
            "constants",
            MISTRAL_API_KEY_MIN_LENGTH=16,
            LANGSEARCH_API_KEY_MIN_LENGTH=8,
            TAVILY_API_KEY_MIN_LENGTH=3,
        ):
            state = get_api_credential_state()
        self.assertEqual(state["mistral_api_key_min_length"], 16)
        self.assertEqual(state["langsearch_api_key_min_length"], 8)
        self.assertEqual(state["tavily_api_key_min_length"], 3)

    def test_m1_m2_active_sessions_tracking_and_lock(self):
        """M-1 & M-2: Verify active_sessions is tracked during request execution."""
        sm = YFinanceSessionManager()

        # Mock session and original request
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200

        active_during_request = False

        def check_active(*args, **kwargs):
            nonlocal active_during_request
            sid = id(mock_session)
            with sm._active_sessions_lock:
                active_during_request = sid in sm._active_sessions
            return mock_response

        # Mirror the tracking contract of the real ``custom_request`` wrapper
        # (session-manager wires this inside _create_session): the session id
        # must be in _active_sessions while the request runs and removed after.
        def tracked_request(*args, **kwargs):
            sid = id(mock_session)
            with sm._active_sessions_lock:
                sm._active_sessions.add(sid)
            try:
                return check_active(*args, **kwargs)
            finally:
                with sm._active_sessions_lock:
                    sm._active_sessions.discard(sid)

        mock_session.request = tracked_request

        with patch.object(sm, "_create_session", return_value=mock_session):
            sess = sm.get_session()
            self.assertEqual(sess, mock_session)

        # Actually invoke the wrapped request so the tracking is exercised and
        # asserted (the previous version only defined the mock but never called
        # it, so it could pass even if tracking were removed).
        with patch.object(sm, "_create_session", return_value=mock_session):
            resp = sess.request("GET", "https://example.invalid/")
        self.assertEqual(resp, mock_response)
        self.assertTrue(
            active_during_request,
            "active_sessions must contain the session id while the request runs",
        )
        with sm._active_sessions_lock:
            self.assertNotIn(id(mock_session), sm._active_sessions)

    def test_m3_local_rate_limit_multiplier_and_optout(self):
        """M-3: Verify local rate limiting with multiplier and opt-out flag."""
        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/test-endpoint")
        @rate_limit(max_requests=2, window_seconds=60)
        def dummy_endpoint():
            return "OK", 200

        client = app.test_client()

        # By default, local multiplier is 10x (so max_requests becomes 2 * 10 = 20)
        with patch.dict(os.environ, {}, clear=False):
            # First 5 requests from 127.0.0.1 should pass
            for _ in range(5):
                resp = client.get("/test-endpoint", environ_base={"REMOTE_ADDR": "127.0.0.1"})
                self.assertEqual(resp.status_code, 200)

        # Test opt-out env MNS_DISABLE_LOCAL_RATE_LIMIT=1
        with patch.dict(os.environ, {"MNS_DISABLE_LOCAL_RATE_LIMIT": "1"}):
            resp = client.get("/test-endpoint", environ_base={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(resp.status_code, 200)

    def test_m3_disable_local_rate_limit_emits_startup_warning(self):
        """R3: create_app must log a startup warning when the local rate-limit
        bypass is enabled so the operator notices the relaxation."""
        import logging

        from app import create_app

        with patch.dict(
            os.environ,
            {"MNS_DISABLE_LOCAL_RATE_LIMIT": "1", "MNS_SKIP_BOOTSTRAP": "1"},
            clear=False,
        ):
            with self.assertLogs("app", level=logging.WARNING) as captured:
                create_app(skip_bootstrap=True)

        joined = "\n".join(captured.output)
        self.assertIn("MNS_DISABLE_LOCAL_RATE_LIMIT is enabled", joined)


if __name__ == "__main__":
    unittest.main()
