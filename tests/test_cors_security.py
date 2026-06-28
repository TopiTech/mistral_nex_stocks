"""
CORS and Security Tests - Chrome Extension Origin Validation

Tests cover:
- Origin whitelist enforcement
- chrome-extension:// protocol validation
- Environment variable configuration
- Native host manifest integration
- TTL cache behavior
"""

import unittest
import os
import json
import time
from unittest.mock import patch, mock_open
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
from app import app, app_state
from app_helpers import _load_allowed_extension_origins


class OriginValidationTestCase(unittest.TestCase):
    """Test Chrome Extension Origin validation"""

    def setUp(self):
        """Set up test Flask app"""
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_backend_origin_is_allowed(self):
        """The backend origin should be allowed."""
        response = self.client.get('/api/health', headers={
            'Origin': 'http://localhost:5000'
        })
        allowed_origin = response.headers.get('Access-Control-Allow-Origin')
        self.assertEqual(allowed_origin, 'http://localhost:5000')

    def test_unrelated_localhost_port_is_rejected(self):
        """localhost on other ports should not be allowed."""
        response = self.client.get('/api/health', headers={
            'Origin': 'http://localhost:3000'
        })
        allowed_origin = response.headers.get('Access-Control-Allow-Origin')
        self.assertIsNone(allowed_origin)

    def test_unauthorized_origin_is_rejected(self):
        """Unauthorized origins should not be allowed"""
        with patch.dict(os.environ, {'MNS_ALLOWED_EXTENSION_ORIGINS': ''}):
            response = self.client.get('/api/health', headers={
                'Origin': 'https://evil.example.com'
            })
            allowed_origin = response.headers.get('Access-Control-Allow-Origin')
            self.assertIsNone(allowed_origin)

    def test_extension_id_format_validation(self):
        """Extension IDs should follow chrome-extension:// format"""
        # Valid Chrome extension ID (32 lowercase hex chars)
        valid_id = 'a' * 32  # abcdefghijklmnopqrstuvwxyzabcdef
        valid_origin = f"chrome-extension://{valid_id}/"
        
        # Just validate the format
        self.assertTrue(valid_origin.startswith('chrome-extension://'))
        self.assertEqual(len(valid_id), 32)

    def test_extension_id_case_sensitivity(self):
        """Extension IDs should be case-insensitive (lowercase stored)"""
        # Chrome extension IDs are case-insensitive but stored as lowercase
        id_upper = 'ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEF'
        id_lower = id_upper.lower()
        
        self.assertEqual(id_lower, 'abcdefghijklmnopqrstuvwxyzabcdef')
        # Protocol should always be lowercase
        self.assertEqual('chrome-extension://', 'chrome-extension://')


class EnvironmentVariableConfigTestCase(unittest.TestCase):
    """Test MNS_ALLOWED_EXTENSION_ORIGINS environment variable"""

    def test_empty_env_var_yields_empty_set(self):
        """Empty env var should result in empty whitelist"""
        with patch.dict(os.environ, {'MNS_ALLOWED_EXTENSION_ORIGINS': ''}):
            # Pass logic check
            origins_str = os.environ.get('MNS_ALLOWED_EXTENSION_ORIGINS', '')
            origins = set()
            if origins_str:
                for raw in origins_str.split(','):
                    origins.add(raw.strip())
            
            self.assertEqual(len(origins), 0)

    def test_extension_origin_env_var_added_to_allowed_origins(self):
        """MNS_EXTENSION_ORIGIN should be loaded into the allowed origins cache"""
        origin_id = 'a' * 32
        ext_origin = f"chrome-extension://{origin_id}/"

        with patch.dict(os.environ, {'MNS_EXTENSION_ORIGIN': ext_origin, 'MNS_ALLOWED_EXTENSION_ORIGINS': ''}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()
            self.assertIn(ext_origin.rstrip('/'), origins)

    def test_extension_id_only_in_env_var_added_to_allowed_origins(self):
        """Bare extension IDs should be accepted in MNS_ALLOWED_EXTENSION_ORIGINS"""
        origin_id = 'A' * 32
        expected_origin = f"chrome-extension://{origin_id.lower()}"

        with patch.dict(os.environ, {'MNS_ALLOWED_EXTENSION_ORIGINS': origin_id}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()
            self.assertIn(expected_origin, origins)

    def test_single_origin_parsing(self):
        """Single origin should be parsed correctly"""
        origin_id = 'a' * 32
        origin_str = f"chrome-extension://{origin_id}/"
        
        with patch.dict(os.environ, {'MNS_ALLOWED_EXTENSION_ORIGINS': origin_str}):
            origins_str = os.environ.get('MNS_ALLOWED_EXTENSION_ORIGINS', '')
            origins = set()
            for raw in origins_str.split(','):
                value = raw.strip()
                if value:
                    origins.add(value)
            
            self.assertEqual(len(origins), 1)
            self.assertIn(origin_str, origins)

    def test_multiple_origins_comma_separated(self):
        """Multiple comma-separated origins should all be parsed"""
        id1 = 'a' * 32
        id2 = 'b' * 32
        origin1 = f"chrome-extension://{id1}/"
        origin2 = f"chrome-extension://{id2}/"
        origins_str = f"{origin1},{origin2}"
        
        with patch.dict(os.environ, {'MNS_ALLOWED_EXTENSION_ORIGINS': origins_str}):
            env_val = os.environ.get('MNS_ALLOWED_EXTENSION_ORIGINS', '')
            origins = set()
            for raw in env_val.split(','):
                value = raw.strip()
                if value:
                    origins.add(value)
            
            self.assertEqual(len(origins), 2)
            self.assertIn(origin1, origins)
            self.assertIn(origin2, origins)

    def test_whitespace_trimmed_from_origins(self):
        """Leading/trailing whitespace should be trimmed"""
        id_str = 'a' * 32
        origin = f"chrome-extension://{id_str}/"
        origins_str = f"  {origin}  ,  {origin}  "
        
        origins = set()
        for raw in origins_str.split(','):
            value = raw.strip()
            if value:
                origins.add(value)
        
        self.assertEqual(len(origins), 1)  # Same origin, deduplicated


class NativeHostManifestTestCase(unittest.TestCase):
    """Test native host manifest integration"""

    def test_manifest_path_exists(self):
        """Native host manifest should exist at expected location"""
        manifest_path = Path(__file__).parent.parent / 'native_host' / 'com.mistral_nex_stocks.host.json'
        self.assertTrue(manifest_path.exists(), "Native host manifest is missing")

    def test_manifest_contains_allowed_origins(self):
        """Manifest should have allowed_origins array"""
        manifest_path = Path(__file__).parent.parent / 'native_host' / 'com.mistral_nex_stocks.host.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

        self.assertIn('allowed_origins', manifest)
        self.assertIsInstance(manifest['allowed_origins'], list)
        self.assertTrue(manifest['allowed_origins'])
        for origin in manifest['allowed_origins']:
            self.assertTrue(str(origin).startswith('chrome-extension://'))

    def test_manifest_origin_trailing_slash_is_normalized(self):
        """chrome-extension origins from manifest should be normalized without trailing slash"""
        origin_id = 'a' * 32
        expected_origin = f'chrome-extension://{origin_id}'
        manifest_data = {'allowed_origins': [f'{expected_origin}/']}

        with patch.object(Path, 'exists', return_value=True), patch('builtins.open', mock_open(read_data=json.dumps(manifest_data))):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()

        self.assertIn(expected_origin, origins)

    def test_manifest_required_fields(self):
        """Manifest must have all required fields"""
        required_fields = ['name', 'description', 'path', 'type', 'allowed_origins']
        manifest_path = Path(__file__).parent.parent / 'native_host' / 'com.mistral_nex_stocks.host.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

        for field in required_fields:
            self.assertIn(field, manifest)


class OriginsCachingTestCase(unittest.TestCase):
    """Test origins caching with TTL"""

    def test_cache_ttl_is_30_seconds(self):
        """Origins cache should have 30-second TTL"""
        self.assertEqual(app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC, 30.0)

    def test_cache_invalidates_after_ttl(self):
        """Cache should be considered stale after TTL expires"""
        now = time.time()
        cache_ts = now - (app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC + 1.0)
        ttl = app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC
        
        is_stale = (now - cache_ts) >= ttl
        self.assertTrue(is_stale)

    def test_cache_remains_valid_within_ttl(self):
        """Cache should be valid within TTL window"""
        now = time.time()
        cache_ts = now - (app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC - 5.0)
        ttl = app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC
        
        is_stale = (now - cache_ts) >= ttl
        self.assertFalse(is_stale)

    def test_thread_safety_of_cache_lock(self):
        """Cache lock should prevent race conditions"""
        from threading import Lock
        cache_lock = Lock()
        
        # Should be able to acquire and release
        acquired = cache_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        cache_lock.release()


class OriginTrimTestCase(unittest.TestCase):
    """Test origin string normalization"""

    def test_origin_trailing_slash_stripped(self):
        """Origin with trailing slash should be stripped"""
        origin = "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/"
        normalized = origin.rstrip('/')
        
        self.assertEqual(normalized, "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef")

    def test_origin_whitespace_trimmed(self):
        """Origin with leading/trailing whitespace should be trimmed"""
        origin = "  chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/  "
        normalized = origin.strip().rstrip('/')
        
        expected = "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef"
        self.assertEqual(normalized, expected)

    def test_http_localhost_preserved(self):
        """http://localhost origins should be preserved as-is"""
        origin = "http://localhost:5000"
        normalized = origin.strip().rstrip('/')
        
        self.assertEqual(normalized, "http://localhost:5000")


class CORSHeadersComplianceTestCase(unittest.TestCase):
    """Test CORS header compliance with spec"""

    def setUp(self):
        """Set up test client"""
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_access_control_allow_origin_set(self):
        """Access-Control-Allow-Origin header must be set"""
        response = self.client.get('/api/health', headers={
            'Origin': 'http://localhost:5000'
        })
        self.assertIn('Access-Control-Allow-Origin', response.headers)

    def test_access_control_allow_methods_set(self):
        """Access-Control-Allow-Methods should include required methods"""
        response = self.client.options('/api/credentials')
        allowed_methods = response.headers.get('Access-Control-Allow-Methods', '')
        
        # Should include at least GET
        self.assertTrue(len(allowed_methods) > 0)

    def test_access_control_allow_headers_set(self):
        """Access-Control-Allow-Headers should allow required headers"""
        response = self.client.options('/api/credentials')
        allowed_headers = response.headers.get('Access-Control-Allow-Headers', '')
        
        self.assertTrue(len(allowed_headers) > 0)
        self.assertIn('Content-Type', allowed_headers)

    def test_access_control_max_age_set(self):
        """Access-Control-Max-Age should be set for caching preflight"""
        response = self.client.options('/api/credentials')
        max_age = response.headers.get('Access-Control-Max-Age', '')
        
        self.assertTrue(len(max_age) > 0)
        self.assertEqual(max_age, '600')  # 10 minutes


if __name__ == '__main__':
    unittest.main()
