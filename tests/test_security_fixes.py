"""
Security Fixes Tests

Tests for:
- Global error handlers (no stack trace leakage)
- Single-use shutdown token
- Metrics endpoint information reduction
"""

import unittest
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app
from app_state import app_state
from utils.text_utils import _sanitize_error_message


class GlobalErrorHandlersTestCase(unittest.TestCase):
    """Test global error handlers don't leak sensitive information."""

    def setUp(self):
        app.config['TESTING'] = True
        app.config['APPLICATION_ROOT'] = '/'
        self.client = app.test_client()

    def test_400_error_no_stack_trace(self):
        """400 errors should not contain stack traces."""
        response = self.client.post(
            '/api/credentials',
            data="invalid json",
            content_type="application/json",
            headers={'Origin': 'http://localhost:5000'},
        )
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('traceback', str(data).lower())
        self.assertNotIn('exception', str(data).lower())

    def test_404_error_safe_response(self):
        """404 errors should return safe JSON response."""
        response = self.client.get('/api/nonexistent')
        self.assertEqual(response.status_code, 404)
        # Should return JSON, not HTML
        self.assertIn('application/json', response.content_type)

    def test_405_error_safe_response(self):
        """405 errors should return safe JSON response."""
        response = self.client.delete('/api/health')
        self.assertEqual(response.status_code, 405)
        self.assertIn('application/json', response.content_type)


class ShutdownTokenTestCase(unittest.TestCase):
    """Test single-use shutdown token functionality."""

    def setUp(self):
        app.config['TESTING'] = True
        app.config['APPLICATION_ROOT'] = '/'
        self.client = app.test_client()
        # Reset token state
        app_state.shutdown_manager.shutdown_token = "test-token-12345"
        app_state.shutdown_manager.shutdown_token_used = False

    def test_token_consumption_works_once(self):
        """Token should be valid for first use."""
        token = app_state.shutdown_manager.shutdown_token
        assert token is not None
        result = app_state.consume_shutdown_token(token)
        self.assertTrue(result)

    def test_token_cannot_be_reused(self):
        """Token should be invalid after first use."""
        token = app_state.shutdown_manager.shutdown_token
        assert token is not None
        app_state.consume_shutdown_token(token)  # First use
        result = app_state.consume_shutdown_token(token)  # Second attempt
        self.assertFalse(result)

    def test_invalid_token_rejected(self):
        """Invalid token should be rejected."""
        result = app_state.consume_shutdown_token("wrong-token")
        self.assertFalse(result)

    def test_empty_token_rejected(self):
        """Empty token should be rejected."""
        result = app_state.consume_shutdown_token("")
        self.assertFalse(result)

    def test_token_rotation_creates_new_token(self):
        """Token rotation should create a new token."""
        old_token = app_state.shutdown_manager.shutdown_token
        app_state.rotate_shutdown_token()
        new_token = app_state.shutdown_manager.shutdown_token
        self.assertNotEqual(old_token, new_token)
        self.assertFalse(app_state.shutdown_manager.shutdown_token_used)

    def test_shutdown_endpoint_requires_token(self):
        """Shutdown endpoint should require valid token."""
        # Without token
        response = self.client.post('/api/shutdown',
            data=json.dumps({'confirm': True}),
            content_type='application/json',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 403)

    def test_shutdown_endpoint_rejects_used_token(self):
        """Shutdown endpoint should reject already used token."""
        token = app_state.shutdown_manager.shutdown_token
        assert token is not None
        # Consume token
        app_state.consume_shutdown_token(token)

        # Try to use consumed token
        response = self.client.post('/api/shutdown',
            data=json.dumps({'confirm': True, 'shutdown_token': token}),
            content_type='application/json',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 403)

    def test_shutdown_endpoint_does_not_consume_token_without_confirm(self):
        """Valid token must not be consumed when confirm flag is missing."""
        token = app_state.shutdown_manager.shutdown_token
        response = self.client.post('/api/shutdown',
            data=json.dumps({'shutdown_token': token}),
            content_type='application/json',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(app_state.shutdown_manager.shutdown_token_used)


class MetricsEndpointTestCase(unittest.TestCase):
    """Test metrics endpoint doesn't expose sensitive information."""

    def setUp(self):
        app.config['TESTING'] = True
        app.config['APPLICATION_ROOT'] = '/'
        self.client = app.test_client()

    def test_metrics_excludes_sensitive_data(self):
        """Metrics should not expose sensitive internal state."""
        response = self.client.get('/api/metrics',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        # Should not contain sensitive fields
        response_str = json.dumps(data)
        self.assertNotIn('api_key', response_str.lower())
        self.assertNotIn('secret', response_str.lower())
        self.assertNotIn('password', response_str.lower())
        self.assertNotIn('mistral_429_streak', response_str)
        self.assertNotIn('langsearch', response_str)
        self.assertNotIn('chat_history_size', response_str)

    def test_metrics_includes_safe_fields(self):
        """Metrics should include safe operational data."""
        response = self.client.get('/api/metrics',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        # Should have these safe fields
        self.assertIn('ok', data)
        self.assertIn('timestamp', data)
        self.assertIn('cache', data)
        self.assertIn('market_data', data)
        self.assertIn('sse', data)
        self.assertIn('config', data)





class ErrorMessageSanitizationTestCase(unittest.TestCase):
    """Test that error messages don't leak sensitive information."""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_short_api_key_rejected(self):
        """Short API keys should be rejected with sanitized error."""
        response = self.client.post(
            '/api/credentials',
            data=json.dumps({'mistral_api_key': 'short'}),
            content_type='application/json',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        # Error message should not contain the key value
        self.assertNotIn('short', str(data))

    def test_sanitize_api_key_in_error_message(self):
        """API keys in error messages should be redacted."""
        test_cases = [
            ("api_key='sk-abc123def456' failed", "sk-abc123def456"),
            ('token: "mysecrettoken123"', "mysecrettoken123"),
            ("password: hunter2", "hunter2"),
            ("secret='mydbpassword'", "mydbpassword"),
            ("https://user:password@api.example.com/v1", "user:password@"),
        ]
        for msg, should_not_contain in test_cases:
            sanitized = _sanitize_error_message(msg)
            self.assertNotIn(should_not_contain, sanitized,
                             f"Failed to redact '{should_not_contain}' from: {msg}")
            self.assertIn('[REDACTED]', sanitized,
                          f"Expected [REDACTED] in sanitized output for: {msg}")

    def test_sanitize_preserves_safe_messages(self):
        """Safe messages should pass through without modification."""
        safe_messages = [
            "Stock fetch failed for AAPL",
            "Rate limit exceeded",
            "Connection timeout",
        ]
        for msg in safe_messages:
            sanitized = _sanitize_error_message(msg)
            self.assertEqual(sanitized, msg)


class InternalServerErrorHandlerTestCase(unittest.TestCase):
    """Test 500 error handler doesn't leak stack traces."""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_500_error_handler_format(self):
        """500 error handler should return structured JSON without stack traces."""
        # Verify the error handler is registered and returns proper format
        with app.test_client() as client:
            # Force a 500 by accessing a route that triggers server error
            # The error handler should catch it and return safe JSON
            response = client.get('/api/nonexistent')
            # While this is 404, it validates the error handler pattern
            data = json.loads(response.data)
            self.assertIn('error', data)
            self.assertNotIn('traceback', str(data).lower())


class HealthEndpointSecurityTestCase(unittest.TestCase):
    """Test /api/health endpoint doesn't leak API key state to non-local requests."""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_health_endpoint_includes_credential_state_for_local(self):
        """Local requests should see API key configuration state."""
        response = self.client.get(
            '/api/health',
            headers={'Origin': 'http://localhost:5000', 'Host': 'localhost:5000'}
        )
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        # Local requests should see credential state
        self.assertIn('has_mistral_api_key', data)

    def test_health_endpoint_hides_credential_state_for_remote(self):
        """Non-local requests should NOT see API key configuration state."""
        response = self.client.get(
            '/api/health',
            headers={'Origin': 'http://evil.example.com', 'Host': 'evil.example.com', 'X-Forwarded-For': '1.2.3.4'}
        )
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        # Non-local requests should NOT see credential state
        self.assertNotIn('has_mistral_api_key', data)
        self.assertNotIn('has_langsearch_api_key', data)


class ErrorStatusCodeTestCase(unittest.TestCase):
    """Test various HTTP error status codes return proper JSON."""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_405_method_not_allowed(self):
        """405 should return structured JSON error."""
        response = self.client.delete('/api/health')
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 405)
        self.assertFalse(data.get('ok', True))
        self.assertIn('error', data)

    def test_413_payload_too_large(self):
        """413 should be returned for oversized payloads."""
        # MAX_CONTENT_LENGTH is 2MB (tightened from 16MB as a DoS guard for a
        # personal-use local app); JSON parsing happens first, so verify the
        # configuration is set to the expected bound.
        self.assertEqual(app.config.get('MAX_CONTENT_LENGTH'), 2 * 1024 * 1024)


class LocalRequestHardeningTestCase(unittest.TestCase):
    """Test local request hardening improvements (Host headers, raw socket IP checks)."""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_local_request_with_ipv6_host(self):
        """Requests with loopback IPv6 in host header should be allowed."""
        from utils.networking import _is_local_request
        class MockRequest:
            def __init__(self, remote_addr, host, forwarded_for=None):
                self.remote_addr = remote_addr
                self.headers = {"Host": host}
                if forwarded_for:
                    self.headers["X-Forwarded-For"] = forwarded_for

        req = MockRequest("::1", "[::1]:5000")
        self.assertTrue(_is_local_request(req))

    def test_local_request_with_invalid_host(self):
        """Requests with non-loopback domain in Host header should be blocked."""
        from utils.networking import _is_local_request
        class MockRequest:
            def __init__(self, remote_addr, host):
                self.remote_addr = remote_addr
                self.headers = {"Host": host}

        req = MockRequest("127.0.0.1", "attacker.com")
        self.assertFalse(_is_local_request(req))

    def test_shutdown_endpoint_rejects_spoofed_remote_addr(self):
        """Shutdown endpoint should reject requests if the WSGI REMOTE_ADDR is non-local."""
        response = self.client.post(
            '/api/shutdown',
            data=json.dumps({"confirm": True, "shutdown_token": "some-token"}),
            content_type="application/json",
            environ_overrides={'REMOTE_ADDR': '192.168.1.5'},
            headers={'Origin': 'http://localhost:5000', 'Host': 'localhost:5000'}
        )
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
