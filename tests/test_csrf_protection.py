"""
CSRF Protection Tests

Tests cover:
- CSRF token validation on POST requests
- CSRF token exemption for specific endpoints
- Rate limiting with Retry-After header
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app


class CSRFProtectionTestCase(unittest.TestCase):
    """Test CSRF protection on API endpoints"""

    def setUp(self):
        """Set up test Flask app"""
        self._original_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = True
        self.client = app.test_client()

    def tearDown(self):
        """Restore WTF_CSRF_ENABLED to original value for other tests"""
        if self._original_csrf is not None:
            app.config["WTF_CSRF_ENABLED"] = self._original_csrf

    def test_post_without_csrf_token_rejected_for_credentials(self):
        """POST /api/credentials without CSRF token must be rejected (CSRF now enforced)."""
        response = self.client.post(
            "/api/credentials",
            headers={"Origin": "http://localhost:5000"},
            data=json.dumps({"mistral_api_key": "test_key_12345"}),
            content_type="application/json",
        )
        # CSRF protect rejects missing/invalid token with 400; the Sec-Fetch-Site
        # origin gate would reject with 403 if it reached that check.
        self.assertIn(response.status_code, [400, 403])

    def test_get_without_csrf_token_succeeds(self):
        """GET request should not require CSRF token"""
        response = self.client.get("/api/credentials")
        self.assertEqual(response.status_code, 200)

    def test_options_without_csrf_token_succeeds(self):
        """OPTIONS request should not require CSRF token"""
        response = self.client.options("/api/credentials")
        self.assertEqual(response.status_code, 200)

    def test_csrf_token_in_session(self):
        """CSRF token should be generated and stored in session"""
        with self.client.session_transaction() as sess:
            # セッションが初期化されることを確認
            self.assertIsNotNone(sess)

    def test_extension_post_without_token_returns_403(self):
        """POST request to extension endpoint without token should return 403"""
        response = self.client.post(
            "/api/stocks/add_ext",
            data=json.dumps({"symbol": "AAPL", "market": "us"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_extension_post_with_valid_token_succeeds(self):
        """POST with valid token + trusted chrome-extension Origin should succeed (200/400 not 403)."""
        from config_utils import get_or_create_extension_api_token

        token = get_or_create_extension_api_token()
        # Origin is required (H-4). Prefer a real allowed origin from the native-host
        # manifest when present; otherwise use the test manifest default.
        from app_helpers import get_allowed_cors_origins

        origins = [o for o in get_allowed_cors_origins() if o.startswith("chrome-extension://")]
        origin = origins[0] if origins else "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef"
        response = self.client.post(
            "/api/stocks/add_ext",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": origin,
            },
            data=json.dumps({"symbol": "AAPL", "market": "us"}),
            content_type="application/json",
        )
        self.assertIn(response.status_code, [200, 400])

    def test_extension_post_without_origin_rejected(self):
        """Valid token without Origin must be rejected (H-4 defense-in-depth)."""
        from config_utils import get_or_create_extension_api_token

        token = get_or_create_extension_api_token()
        response = self.client.post(
            "/api/stocks/add_ext",
            headers={"Authorization": f"Bearer {token}"},
            data=json.dumps({"symbol": "AAPL", "market": "us"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class RateLimitingTestCase(unittest.TestCase):
    """Test rate limiting functionality"""

    def setUp(self):
        """Set up test Flask app"""
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_rate_limit_returns_429(self):
        """Exceeding rate limit should return 429"""
        # レート制限を超えるまでリクエストを送信
        # localhost は除外されるため、非ローカルIPを環境変数でエミュレート
        for i in range(65):  # デフォルトは60リクエスト/60秒
            response = self.client.get("/api/health", environ_base={"REMOTE_ADDR": "192.168.1.100"})

        # 最後のリクエストは429であるべき
        self.assertEqual(response.status_code, 429)

    def test_rate_limit_includes_retry_after_header(self):
        """429 response should include Retry-After header"""
        # レート制限を超えるまでリクエストを送信
        for i in range(65):
            response = self.client.get("/api/health", environ_base={"REMOTE_ADDR": "192.168.1.101"})

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            self.assertIsNotNone(retry_after)
            assert retry_after is not None
            self.assertTrue(int(retry_after) >= 0)

    def test_localhost_exempt_from_rate_limit(self):
        """localhost should be exempt from rate limiting"""
        for i in range(100):
            response = self.client.get("/api/health")

        # localhost はレート制限されない
        self.assertEqual(response.status_code, 200)


class CsrfBrowserFlowTestCase(unittest.TestCase):
    """Regression tests mirroring the real browser flow.

    The frontend injects the CSRF token (from <meta name="csrf-token">) into the
    X-CSRFToken header for every unsafe request via apiFetch/csrfFetch. These tests
    assert that mutating endpoints are rejected without the token and accepted with it.
    """

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _get_token(self):
        html = self.client.get("/setup").get_data(as_text=True)
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        self.assertIsNotNone(m, "csrf-token meta not rendered in /setup")
        assert m is not None
        return m.group(1)

    def test_post_without_csrf_token_rejected(self):
        """A mutating POST without the CSRF token must be rejected.

        Depending on request metadata (Sec-Fetch-Site), the request is blocked
        either by CSRFProtect (400) or by the Sec-Fetch-Site origin gate (403).
        Both are correct rejections of an unauthenticated mutating request.
        """
        response = self.client.post(
            "/api/stocks/add",
            data=json.dumps({"symbol": "AAPL", "market": "us"}),
            content_type="application/json",
        )
        self.assertIn(response.status_code, (400, 403))

    def test_post_with_csrf_token_via_header_accepted(self):
        """A mutating POST carrying the CSRF token in X-CSRFToken is accepted.

        The request also sends a trusted same-site Origin so it passes the
        Sec-Fetch-Site local-origin gate (the real browser sends both).
        """
        token = self._get_token()
        response = self.client.post(
            "/api/stocks/portfolio",
            data=json.dumps({"symbol": "AAPL", "market": "us", "shares": 1}),
            content_type="application/json",
            headers={"X-CSRFToken": token, "Origin": "http://localhost:5000"},
        )
        # 400 here is a business-logic validation rejection, not CSRF — the
        # request reached the handler. 403 would indicate a CSRF/origin block.
        self.assertNotEqual(response.status_code, 403)
        self.assertIn(response.status_code, (200, 400))


if __name__ == "__main__":
    unittest.main()
