"""
Security Fixes Tests

Tests for:
- Global error handlers (no stack trace leakage)
- Single-use shutdown token
- Metrics endpoint information reduction
"""

import unittest
import json
import time
from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
from app import app, _consume_shutdown_token, _rotate_shutdown_token


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
        app.config['SHUTDOWN_TOKEN'] = "test-token-12345"
        app.config['SHUTDOWN_TOKEN_USED'] = False

    def test_token_consumption_works_once(self):
        """Token should be valid for first use."""
        token = app.config['SHUTDOWN_TOKEN']
        result = _consume_shutdown_token(token)
        self.assertTrue(result)

    def test_token_cannot_be_reused(self):
        """Token should be invalid after first use."""
        token = app.config['SHUTDOWN_TOKEN']
        _consume_shutdown_token(token)  # First use
        result = _consume_shutdown_token(token)  # Second attempt
        self.assertFalse(result)

    def test_invalid_token_rejected(self):
        """Invalid token should be rejected."""
        result = _consume_shutdown_token("wrong-token")
        self.assertFalse(result)

    def test_empty_token_rejected(self):
        """Empty token should be rejected."""
        result = _consume_shutdown_token("")
        self.assertFalse(result)

    def test_token_rotation_creates_new_token(self):
        """Token rotation should create a new token."""
        old_token = app.config['SHUTDOWN_TOKEN']
        _rotate_shutdown_token()
        new_token = app.config['SHUTDOWN_TOKEN']
        self.assertNotEqual(old_token, new_token)
        self.assertFalse(app.config['SHUTDOWN_TOKEN_USED'])

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
        token = app.config['SHUTDOWN_TOKEN']
        # Consume token
        _consume_shutdown_token(token)
        
        # Try to use consumed token
        response = self.client.post('/api/shutdown',
            data=json.dumps({'confirm': True, 'shutdown_token': token}),
            content_type='application/json',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 403)

    def test_shutdown_endpoint_does_not_consume_token_without_confirm(self):
        """Valid token must not be consumed when confirm flag is missing."""
        token = app.config['SHUTDOWN_TOKEN']
        response = self.client.post('/api/shutdown',
            data=json.dumps({'shutdown_token': token}),
            content_type='application/json',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(app.config['SHUTDOWN_TOKEN_USED'])


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
    """Test error messages don't leak sensitive data."""

    def setUp(self):
        app.config['TESTING'] = True
        app.config['APPLICATION_ROOT'] = '/'
        self.client = app.test_client()

    def test_credentials_endpoint_validates_key_length(self):
        """Short API keys should be rejected."""
        response = self.client.post('/api/credentials',
            data=json.dumps({'mistral_api_key': 'short'}),
            content_type='application/json',
            headers={'Origin': 'http://localhost:5000'}
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        # Error message should not contain the key value
        self.assertNotIn('short', str(data))


if __name__ == '__main__':
    unittest.main()
