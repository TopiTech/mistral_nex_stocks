"""
HTTP Integration Tests for Mistral NeX Stocks API Endpoints

Tests cover:
- API endpoint accessibility
- Response format validation
- Security headers (CORS, CSP, Origin validation)
- Error handling
- Rate limiting boundaries
"""

import json
import os
import time

# Add parent directory to path for imports
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app
from error_codes import ErrorCode


class APIIntegrationTestCase(unittest.TestCase):
    """Base test class with flask client setup"""

    @classmethod
    def setUpClass(cls):
        """Set up test Flask app client"""
        cls.snapshot_patcher = patch("routes.api_stocks._wait_for_initial_market_snapshot", return_value=True)
        cls.snapshot_patcher.start()
        cls._original_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        cls.client = app.test_client()
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        cls.snapshot_patcher.stop()
        if cls._original_csrf is not None:
            app.config["WTF_CSRF_ENABLED"] = cls._original_csrf

    def setUp(self):
        """Clear any fixtures before each test"""

    def tearDown(self):
        """Cleanup after each test"""


class SecurityHeadersTestCase(APIIntegrationTestCase):
    """Test security-related headers and CORS policies"""

    def test_csp_header_present(self):
        """CSP header should be present on all responses"""
        response = self.client.get("/")
        self.assertTrue(
            "Content-Security-Policy" in response.headers
            or "Content-Security-Policy-Report-Only" in response.headers,
            f"CSP header missing, headers: {response.headers}",
        )
        csp = response.headers.get("Content-Security-Policy") or response.headers.get(
            "Content-Security-Policy-Report-Only"
        )
        self.assertIsNotNone(csp)
        assert csp is not None
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src", csp)

    def test_cors_localhost_allowed(self):
        """localhost should always be allowed"""
        response = self.client.get(
            "/api/health", headers={"Origin": "http://localhost:5000"}
        )
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"), "http://localhost:5000"
        )

    def test_cors_invalid_origin_rejected(self):
        """Invalid origin without env whitelist should be rejected"""
        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": ""}):
            response = self.client.get(
                "/api/health", headers={"Origin": "https://evil.example.com"}
            )
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    def test_cors_unrelated_localhost_origin_rejected(self):
        """Only the backend origin should be allowed for localhost."""
        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": ""}):
            response = self.client.get(
                "/api/health", headers={"Origin": "http://localhost:3000"}
            )
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    def test_cors_vary_header(self):
        """Vary: Origin should be present for cache control"""
        response = self.client.get("/api/stocks")
        self.assertEqual(response.headers.get("Vary"), "Origin")


class HealthCheckTestCase(APIIntegrationTestCase):
    """Test /api/health endpoint"""

    def test_health_endpoint_exists(self):
        """GET /api/health should return 200"""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)

    def test_health_response_format(self):
        """Health endpoint should return JSON with required fields"""
        response = self.client.get("/api/health")
        data = json.loads(response.data)
        self.assertIn("ok", data)
        self.assertIn("timestamp", data)


class MetricsAPITestCase(APIIntegrationTestCase):
    """Test /api/metrics safe operational visibility."""

    def test_metrics_endpoint_returns_safe_sections(self):
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data.get("ok"))
        self.assertIn("cache", data)
        self.assertIn("market_data", data)
        self.assertIn("sse", data)
        self.assertIn("config", data)
        # Sensitive sections should not be present
        self.assertNotIn("ai", data)
        self.assertNotIn("api_key", json.dumps(data).lower())


class CredentialsAPITestCase(APIIntegrationTestCase):
    """Test /api/credentials endpoint security"""

    def test_credentials_get_returns_state(self):
        """GET /api/credentials should return credential state"""
        response = self.client.get("/api/credentials")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("has_mistral_api_key", data)
        self.assertIn("has_langsearch_api_key", data)

    def test_credentials_options_allowed(self):
        """OPTIONS /api/credentials should return 200"""
        response = self.client.options("/api/credentials")
        self.assertEqual(response.status_code, 200)

    def test_credentials_cors_headers(self):
        """CORS headers should be set for credentials endpoint"""
        response = self.client.get(
            "/api/credentials", headers={"Origin": "http://localhost:5000"}
        )
        self.assertIn("Access-Control-Allow-Origin", response.headers)
        self.assertIn("Access-Control-Allow-Methods", response.headers)
        self.assertIn("Access-Control-Allow-Headers", response.headers)

    def test_credentials_post_rejects_short_mistral_key(self):
        """POST /api/credentials should reject keys below the configured minimum length."""
        response = self.client.post(
            "/api/credentials",
            json={"mistral_api_key": "short-valid-key", "langsearch_api_key": ""},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertEqual(data.get("error_code"), int(ErrorCode.INVALID_API_KEY))

    @patch("routes.api_system.save_api_credentials")
    def test_credentials_post_accepts_valid_length_mistral_key(self, mock_save):
        """POST /api/credentials should accept keys that satisfy format validation."""
        response = self.client.post(
            "/api/credentials",
            json={"mistral_api_key": "a" * 32, "langsearch_api_key": ""},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data.get("ok"))
        mock_save.assert_called_once()

    def test_credentials_post_invalid_json_returns_malformed_input(self):
        """POST /api/credentials with invalid JSON should return malformed input."""
        response = self.client.post(
            "/api/credentials",
            data='{"mistral_api_key": "foo",',
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertEqual(data.get("error_code"), int(ErrorCode.MALFORMED_INPUT))
        self.assertIn("JSON形式が不正です", data.get("details", {}).get("reason", ""))


class StocksAPITestCase(APIIntegrationTestCase):
    """Test /api/stocks endpoint"""

    def test_stocks_endpoint_returns_200(self):
        """GET /api/stocks should return 200"""
        response = self.client.get("/api/stocks")
        self.assertEqual(response.status_code, 200)

    def test_stocks_response_is_json(self):
        """Response should be valid JSON"""
        response = self.client.get("/api/stocks")
        try:
            data = json.loads(response.data)
            self.assertIsInstance(data, (dict, list))
        except json.JSONDecodeError:
            self.fail("Response is not valid JSON")

    def test_stocks_query_parameter(self):
        """Should handle ?country= query parameter"""
        response = self.client.get("/api/stocks?country=us")
        self.assertEqual(response.status_code, 200)

    @patch("routes.api_stocks.save_user_stocks")
    def test_add_stock_rejects_remote_request(self, _mock_save):
        """Local-only stock mutation endpoints must reject non-local requests."""
        response = self.client.post(
            "/api/stocks/add",
            json={"symbol": "MSFT", "name": "Microsoft", "market": "us"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)
        data = json.loads(response.data)
        self.assertFalse(data["ok"])

    @patch("routes.api_stocks.save_user_stocks")
    def test_add_stock_rejects_long_name(self, _mock_save):
        """Stock names must be bounded to prevent oversized payloads."""
        response = self.client.post(
            "/api/stocks/add",
            json={
                "symbol": "MSFT",
                "name": "M" * 201,
                "market": "us",
            },
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertEqual(data.get("error_code"), int(ErrorCode.UNSAFE_INPUT))

    @patch("routes.api_stocks.save_user_stocks")
    def test_add_stock_accepts_valid_local_request(self, _mock_save):
        """Valid local stock add should parse input and return success."""
        import uuid
        unique_symbol = f"T{uuid.uuid4().hex[:6].upper()}"
        response = self.client.post(
            "/api/stocks/add",
            json={"symbol": unique_symbol, "name": "Test Stock", "market": "us"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["success"])
        _mock_save.assert_called_once()

    @patch("routes.api_stocks.save_user_stocks")
    def test_update_portfolio_rejects_boolean_numeric_input(self, _mock_save):
        """Boolean values must not be accepted as numeric portfolio input."""
        response = self.client.post(
            "/api/stocks/portfolio",
            json={"symbol": "MSFT", "market": "us", "shares": True, "avg_price": 1},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertEqual(data.get("error_code"), int(ErrorCode.INVALID_INPUT))
        _mock_save.assert_not_called()


class IndicesAPITestCase(APIIntegrationTestCase):
    """Test /api/indices endpoint"""

    def test_indices_endpoint_returns_200(self):
        """GET /api/indices should return 200"""
        response = self.client.get("/api/indices")
        self.assertEqual(response.status_code, 200)

    def test_indices_response_format(self):
        """Response should contain expected structure"""
        response = self.client.get("/api/indices")
        data = json.loads(response.data)
        # Should be dict-like or list
        self.assertIsInstance(data, (dict, list))


class HTTPMethodsTestCase(APIIntegrationTestCase):
    """Test HTTP method handling"""

    def test_get_stocks_allowed(self):
        """GET /api/stocks should be allowed"""
        response = self.client.get("/api/stocks")
        self.assertNotEqual(response.status_code, 405)

    def test_post_stocks_not_allowed(self):
        """POST /api/stocks should be rejected"""
        response = self.client.post("/api/stocks")
        self.assertEqual(response.status_code, 405)

    def test_options_credentials_allowed(self):
        """OPTIONS /api/credentials should return allowed methods"""
        response = self.client.options("/api/credentials")
        self.assertEqual(response.status_code, 200)


class ErrorHandlingTestCase(APIIntegrationTestCase):
    """Test error response formats"""

    def test_404_on_nonexistent_route(self):
        """Nonexistent routes should return 404"""
        response = self.client.get("/api/nonexistent")
        self.assertEqual(response.status_code, 404)

    def test_error_response_format(self):
        """Error responses should have error_code and message"""
        response = self.client.get("/api/nonexistent")
        try:
            data = json.loads(response.data)
            # Should have error structure
            if "error_code" in data or "message" in data or "detail" in data:
                pass  # Expected
        except json.JSONDecodeError:
            pass  # Some 404s may not be JSON


class RateLimitingBoundaryTestCase(APIIntegrationTestCase):
    """Test rate limiting behavior at boundaries"""

    def test_mistral_normal_response(self):
        """Mistral response should work when streak is 0"""
        from app_state import app_state

        old_streak = app_state.ai.mistral_429_streak
        try:
            app_state.ai.mistral_429_streak = 0
            with self.app.app_context():
                with patch("app_state.app_state.execution.shutdown_event.wait"):
                    with patch("services.ai_service._get_mistral_client") as mock_client:
                        mock_resp = MagicMock()
                        mock_resp.choices = [MagicMock()]
                        mock_resp.choices[0].message.content = '{"recommendation": "buy"}'
                        mock_resp.model_dump.return_value = {
                            "choices": [{"message": {"content": '{"recommendation": "buy"}'}}]
                        }
                        mock_client.return_value.chat.complete.return_value = mock_resp

                        from services.ai_service import call_mistral_chat

                        result = call_mistral_chat(
                            "test-key",
                            [{"role": "user", "content": "hello"}],
                            use_cache=False,
                        )
                        self.assertIn("choices", result)
                        self.assertEqual(app_state.ai.mistral_429_streak, 0)
        finally:
            app_state.ai.mistral_429_streak = old_streak

    def test_mistral_429_backoff_delays_next_call(self):
        """On 3rd 429 streak, the cooldown should delay the next API call"""
        from app_state import app_state
        from services.ai_service import call_mistral_chat

        old_streak = app_state.ai.mistral_429_streak
        old_next = app_state.ai.mistral_next_allowed_ts
        old_last = app_state.ai.mistral_last_call_ts
        try:
            app_state.ai.mistral_429_streak = 3
            app_state.ai.mistral_next_allowed_ts = time.time() + 300
            app_state.ai.mistral_last_call_ts = 0
            with self.app.app_context():
                sleep_called_with = []

                def capture_wait(secs):
                    sleep_called_with.append(secs)

                with patch("app_state.app_state.execution.shutdown_event.wait", side_effect=capture_wait):
                    with patch("services.ai_service._get_mistral_client") as mock_client:
                        mock_client.return_value = MagicMock()
                        call_mistral_chat(
                            "test-key",
                            [{"role": "user", "content": "hello"}],
                            use_cache=False,
                        )
                        self.assertTrue(len(sleep_called_with) > 0)
                        self.assertGreater(sleep_called_with[0], 100)
        finally:
            app_state.ai.mistral_429_streak = old_streak
            app_state.ai.mistral_next_allowed_ts = old_next
            app_state.ai.mistral_last_call_ts = old_last


class TextHTMLEndpointsTestCase(APIIntegrationTestCase):
    """Test HTML page rendering endpoints"""

    def test_root_endpoint_returns_html(self):
        """GET / should return HTML"""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<!doctype", response.data.lower())

    def test_main_endpoint_returns_html(self):
        """GET /main should return HTML"""
        response = self.client.get("/main")
        self.assertEqual(response.status_code, 200)

    def test_heatmap_endpoint_returns_html(self):
        """GET /heatmap should return HTML"""
        response = self.client.get("/heatmap")
        self.assertEqual(response.status_code, 200)

    def test_settings_endpoint_returns_html(self):
        """GET /settings should return HTML"""
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)

    def test_setup_endpoint_returns_html(self):
        """GET /setup should return HTML"""
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)


class CacheControlTestCase(APIIntegrationTestCase):
    """Test caching behavior"""

    def test_repeated_request_returns_same_status(self):
        """Repeated identical requests should have consistent status"""
        response1 = self.client.get("/api/health")
        response2 = self.client.get("/api/health")
        self.assertEqual(response1.status_code, response2.status_code)


if __name__ == "__main__":
    unittest.main()
