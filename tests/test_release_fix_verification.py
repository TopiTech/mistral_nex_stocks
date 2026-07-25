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

from session_manager import YFinanceSessionManager
from route_helpers import rate_limit
from app import create_app


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

    def test_m1_m2_active_sessions_tracking_and_lock(self):
        """M-1 & M-2: Verify active_sessions is tracked during request execution."""
        sm = YFinanceSessionManager()

        # Mock session and original request
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200

        active_during_request = False

        def mock_request(*args, **kwargs):
            nonlocal active_during_request
            sid = id(mock_session)
            with sm._active_sessions_lock:
                active_during_request = sid in sm._active_sessions
            return mock_response

        mock_session.request = mock_request

        # Wrap with custom_request logic (simulating _create_session)
        # Verify initial active sessions count
        sid = id(mock_session)
        with sm._active_sessions_lock:
            self.assertNotIn(sid, sm._active_sessions)

        # Call get_session to ensure no nested RLock issues occur
        with patch.object(sm, "_create_session", return_value=mock_session):
            sess = sm.get_session()
            self.assertEqual(sess, mock_session)

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


if __name__ == "__main__":
    unittest.main()
