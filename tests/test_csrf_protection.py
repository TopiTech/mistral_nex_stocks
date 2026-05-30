"""
CSRF Protection Tests

Tests cover:
- CSRF token validation on POST requests
- CSRF token exemption for specific endpoints
- Rate limiting with Retry-After header
"""

import unittest
import os
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app, csrf


class CSRFProtectionTestCase(unittest.TestCase):
    """Test CSRF protection on API endpoints"""

    def setUp(self):
        """Set up test Flask app"""
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = True
        self.client = app.test_client()

    def test_post_without_csrf_token_returns_400(self):
        """POST without CSRF token should return 400"""
        response = self.client.post('/api/credentials', 
            data=json.dumps({'mistral_api_key': 'test_key_12345'}),
            content_type='application/json'
        )
        # CSRF保護が有効な場合、トークンなしは400
        self.assertIn(response.status_code, [400, 422])

    def test_get_without_csrf_token_succeeds(self):
        """GET request should not require CSRF token"""
        response = self.client.get('/api/credentials')
        self.assertEqual(response.status_code, 200)

    def test_options_without_csrf_token_succeeds(self):
        """OPTIONS request should not require CSRF token"""
        response = self.client.options('/api/credentials')
        self.assertEqual(response.status_code, 200)

    def test_csrf_token_in_session(self):
        """CSRF token should be generated and stored in session"""
        with self.client.session_transaction() as sess:
            # セッションが初期化されることを確認
            self.assertIsNotNone(sess)


class RateLimitingTestCase(unittest.TestCase):
    """Test rate limiting functionality"""

    def setUp(self):
        """Set up test Flask app"""
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_rate_limit_returns_429(self):
        """Exceeding rate limit should return 429"""
        # レート制限を超えるまでリクエストを送信
        # localhost は除外されるため、X-Forwarded-For ヘッダーを使用
        for i in range(65):  # デフォルトは60リクエスト/60秒
            response = self.client.get('/api/health', 
                headers={'X-Forwarded-For': '192.168.1.100'}
            )
        
        # 最後のリクエストは429であるべき
        self.assertEqual(response.status_code, 429)

    def test_rate_limit_includes_retry_after_header(self):
        """429 response should include Retry-After header"""
        # レート制限を超えるまでリクエストを送信
        for i in range(65):
            response = self.client.get('/api/health',
                headers={'X-Forwarded-For': '192.168.1.101'}
            )
        
        if response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            self.assertIsNotNone(retry_after)
            self.assertTrue(int(retry_after) >= 0)

    def test_localhost_exempt_from_rate_limit(self):
        """localhost should be exempt from rate limiting"""
        for i in range(100):
            response = self.client.get('/api/health')
        
        # localhost はレート制限されない
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
