"""
CORS and Security Tests - Chrome Extension Origin Validation

Tests cover:
- Origin whitelist enforcement
- chrome-extension:// protocol validation
- Environment variable configuration
- Native host manifest integration
- TTL cache behavior
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app, app_state
from utils.networking import _load_allowed_extension_origins

#: Real ``open`` used to wrap calls that must stay functional.
_ORIGINAL_OPEN = open


def _manifest_absent_open(name, *args, **kwargs):
    """``open`` wrapper that hides the locally-installed native-host manifest so
    tests can assert purely on environment-driven origin loading."""
    if str(name).endswith("com.mistral_nex_stocks.host.json"):
        raise FileNotFoundError("native host manifest hidden in test")
    return _ORIGINAL_OPEN(name, *args, **kwargs)


class OriginValidationTestCase(unittest.TestCase):
    """Test Chrome Extension Origin validation"""

    def setUp(self):
        """Set up test Flask app"""
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_backend_origin_is_allowed(self):
        """The backend origin should be allowed."""
        response = self.client.get("/api/health", headers={"Origin": "http://localhost:5000"})
        allowed_origin = response.headers.get("Access-Control-Allow-Origin")
        self.assertEqual(allowed_origin, "http://localhost:5000")

    def test_unrelated_localhost_port_is_rejected(self):
        """localhost on other ports should not be allowed."""
        response = self.client.get("/api/health", headers={"Origin": "http://localhost:3000"})
        allowed_origin = response.headers.get("Access-Control-Allow-Origin")
        self.assertIsNone(allowed_origin)

    def test_loopback_ip_variant_of_allowed_origin_is_reflected(self):
        """127.0.0.1 and localhost must be treated as the same allowed origin (R3).

        Regression: ``get_allowed_cors_origins()`` canonicalizes loopback
        variants (127.0.0.1/[::1] -> localhost), but ``add_extension_cors_headers``
        compared the raw request Origin, so a 127.0.0.1 request got no
        Access-Control-Allow-Origin even though the shutdown/Sec-Fetch-Site gate
        (``_normalize_origin``) accepted it. The header must be reflected so the
        browser's exact-origin check passes.
        """
        response = self.client.get("/api/health", headers={"Origin": "http://127.0.0.1:5000"})
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:5000"
        )

    def test_unauthorized_origin_is_rejected(self):
        """Unauthorized origins should not be allowed"""
        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": ""}):
            response = self.client.get(
                "/api/health", headers={"Origin": "https://evil.example.com"}
            )
            allowed_origin = response.headers.get("Access-Control-Allow-Origin")
            self.assertIsNone(allowed_origin)

    def test_valid_extension_id_normalized_to_canonical_form(self):
        """A valid chrome-extension:// origin is normalized (trailing slash stripped)."""
        from utils.networking import _normalize_extension_origin

        valid_id = "a" * 32
        self.assertEqual(
            _normalize_extension_origin(f"chrome-extension://{valid_id}/"),
            f"chrome-extension://{valid_id}",
        )

    def test_extension_id_case_insensitive_normalization(self):
        """Uppercase extension IDs are normalized to lowercase canonical form."""
        from utils.networking import _normalize_extension_origin

        self.assertEqual(
            _normalize_extension_origin("chrome-extension://" + "A" * 32),
            "chrome-extension://" + "a" * 32,
        )

    def test_invalid_extension_id_rejected(self):
        """Malformed extension origins (wrong length / invalid chars) are rejected."""
        from utils.networking import _normalize_extension_origin

        self.assertIsNone(_normalize_extension_origin("chrome-extension://nothex!"))
        self.assertIsNone(_normalize_extension_origin("chrome-extension://" + "a" * 31))
        self.assertIsNone(_normalize_extension_origin("chrome-extension://"))
        self.assertIsNone(_normalize_extension_origin(""))

    def test_http_origin_not_truncated_to_extension_form(self):
        """Plain http:// origins must NOT be treated as extension origins."""
        from utils.networking import _normalize_extension_origin

        self.assertIsNone(_normalize_extension_origin("http://localhost:5000"))


class EnvironmentVariableConfigTestCase(unittest.TestCase):
    """Test MNS_ALLOWED_EXTENSION_ORIGINS environment variable"""

    def test_empty_env_var_yields_empty_set(self):
        """Empty env vars alone must not produce any chrome-extension origin.

        A locally installed native-host manifest is ignored here so the test
        asserts purely on environment handling.
        """
        with patch.dict(
            os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": "", "MNS_EXTENSION_ORIGIN": ""}
        ):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            with patch("builtins.open", side_effect=_manifest_absent_open):
                origins = _load_allowed_extension_origins()
            # The backend's loopback origins are constants, not extension origins:
            # no chrome-extension:// origin may come from an empty env alone.
            self.assertFalse(any(o.startswith("chrome-extension://") for o in origins))

    def test_extension_origin_env_var_added_to_allowed_origins(self):
        """MNS_EXTENSION_ORIGIN should be loaded into the allowed origins cache"""
        origin_id = "a" * 32
        ext_origin = f"chrome-extension://{origin_id}/"

        with patch.dict(
            os.environ, {"MNS_EXTENSION_ORIGIN": ext_origin, "MNS_ALLOWED_EXTENSION_ORIGINS": ""}
        ):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()
            self.assertIn(ext_origin.rstrip("/"), origins)

    def test_extension_id_only_in_env_var_added_to_allowed_origins(self):
        """Bare extension IDs should be accepted in MNS_ALLOWED_EXTENSION_ORIGINS"""
        origin_id = "A" * 32
        expected_origin = f"chrome-extension://{origin_id.lower()}"

        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": origin_id}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()
            self.assertIn(expected_origin, origins)

    def test_single_origin_parsing(self):
        """A single chrome-extension origin is normalized and added."""
        origin_id = "a" * 32
        origin_str = f"chrome-extension://{origin_id}/"

        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": origin_str}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()

        self.assertIn(f"chrome-extension://{origin_id}", origins)

    def test_multiple_origins_comma_separated(self):
        """Multiple comma-separated origins should all be loaded and normalized."""
        id1 = "a" * 32
        id2 = "b" * 32
        origin1 = f"chrome-extension://{id1}/"
        origin2 = f"chrome-extension://{id2}/"
        origins_str = f"{origin1},{origin2}"

        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": origins_str}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()

        self.assertIn(f"chrome-extension://{id1}", origins)
        self.assertIn(f"chrome-extension://{id2}", origins)

    def test_whitespace_trimmed_from_origins(self):
        """Whitespace around comma-separated origins is trimmed and deduplicated."""
        id_str = "a" * 32
        origin = f"chrome-extension://{id_str}/"
        origins_str = f"  {origin}  ,  {origin}  "

        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": origins_str}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()

        self.assertIn(f"chrome-extension://{id_str}", origins)


class NativeHostManifestTestCase(unittest.TestCase):
    """Test native host manifest integration"""

    def test_manifest_path_exists(self):
        """Native host manifest should exist at expected location"""
        manifest_path = (
            Path(__file__).parent.parent / "native_host" / "com.mistral_nex_stocks.host.json"
        )
        self.assertTrue(manifest_path.exists(), "Native host manifest is missing")

    def test_manifest_contains_allowed_origins(self):
        """Manifest should have allowed_origins array"""
        manifest_path = (
            Path(__file__).parent.parent / "native_host" / "com.mistral_nex_stocks.host.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertIn("allowed_origins", manifest)
        self.assertIsInstance(manifest["allowed_origins"], list)
        self.assertTrue(manifest["allowed_origins"])
        for origin in manifest["allowed_origins"]:
            self.assertTrue(str(origin).startswith("chrome-extension://"))

    def test_manifest_origin_trailing_slash_is_normalized(self):
        """chrome-extension origins from manifest should be normalized without trailing slash"""
        origin_id = "a" * 32
        expected_origin = f"chrome-extension://{origin_id}"
        manifest_data = {"allowed_origins": [f"{expected_origin}/"]}

        with (
            patch.object(Path, "exists", return_value=True),
            patch("builtins.open", mock_open(read_data=json.dumps(manifest_data))),
        ):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            origins = _load_allowed_extension_origins()

        self.assertIn(expected_origin, origins)

    def test_manifest_required_fields(self):
        """Manifest must have all required fields"""
        required_fields = ["name", "description", "path", "type", "allowed_origins"]
        manifest_path = (
            Path(__file__).parent.parent / "native_host" / "com.mistral_nex_stocks.host.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        for field in required_fields:
            self.assertIn(field, manifest)


class OriginsCachingTestCase(unittest.TestCase):
    """Test origins caching with TTL"""

    def test_cache_ttl_is_30_seconds(self):
        """Origins cache should have 30-second TTL"""
        self.assertEqual(app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC, 30.0)

    def test_cache_reloaded_after_ttl_expiry(self):
        """After the TTL expires, a fresh env value is picked up on reload."""
        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": ""}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            _load_allowed_extension_origins()

            # Simulate the TTL elapsing, then change the environment: the next
            # load must observe the new value (cache is stale).
            with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": "b" * 32}):
                app_state._extension_origins_cache_ts = time.time() - (
                    app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC + 1.0
                )
                origins = _load_allowed_extension_origins()
                self.assertIn(f"chrome-extension://{'b' * 32}", origins)

    def test_cache_within_ttl_serves_stale_value(self):
        """Within the TTL window the cached value is served without reload."""
        with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": "c" * 32}):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            _load_allowed_extension_origins()

            # Environment changes, but the cache is still fresh: the previously
            # loaded value is served.
            with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": "d" * 32}):
                app_state._extension_origins_cache_ts = time.time() - (
                    app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC - 5.0
                )
                origins = _load_allowed_extension_origins()
                self.assertNotIn(f"chrome-extension://{'d' * 32}", origins)

    def test_cache_lock_is_reentrant_safe(self):
        """The origins cache lock can be acquired/released repeatedly (no deadlock)."""
        for _ in range(2):
            app_state._extension_origins_cache_ts = 0.0
            app_state._extension_origins_cache.clear()
            with patch.dict(os.environ, {"MNS_ALLOWED_EXTENSION_ORIGINS": "e" * 32}):
                origins = _load_allowed_extension_origins()
            self.assertIn(f"chrome-extension://{'e' * 32}", origins)

    def tearDown(self):
        app_state._extension_origins_cache_ts = 0.0
        app_state._extension_origins_cache.clear()


class OriginTrimTestCase(unittest.TestCase):
    """Test origin normalization via the real _normalize_extension_origin."""

    def test_origin_trailing_slash_stripped(self):
        """Chrome extension origin with trailing slash is canonicalized."""
        from utils.networking import _normalize_extension_origin

        origin = "chrome-extension://" + "a" * 32 + "/"
        self.assertEqual(
            _normalize_extension_origin(origin),
            "chrome-extension://" + "a" * 32,
        )

    def test_edge_extension_scheme_normalized_to_chrome_form(self):
        """Edge's extension:// form is normalized to the canonical chrome-extension:// form."""
        from utils.networking import _normalize_extension_origin

        self.assertEqual(
            _normalize_extension_origin("extension://" + "b" * 32),
            "chrome-extension://" + "b" * 32,
        )

    def test_bare_extension_id_accepted(self):
        """A bare 32-hex extension id is normalized to chrome-extension:// form."""
        from utils.networking import _normalize_extension_origin

        self.assertEqual(
            _normalize_extension_origin("c" * 32),
            "chrome-extension://" + "c" * 32,
        )

    def test_moz_extension_uuid_preserved(self):
        """Firefox moz-extension:// UUID origins are preserved in canonical form."""
        from utils.networking import _normalize_extension_origin

        uuid_origin = "moz-extension://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.assertEqual(_normalize_extension_origin(uuid_origin), uuid_origin)


class CORSHeadersComplianceTestCase(unittest.TestCase):
    """Test CORS header compliance with spec"""

    def setUp(self):
        """Set up test client"""
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_access_control_allow_origin_set(self):
        """Access-Control-Allow-Origin header must be set"""
        response = self.client.get("/api/health", headers={"Origin": "http://localhost:5000"})
        self.assertIn("Access-Control-Allow-Origin", response.headers)

    def test_access_control_allow_methods_set(self):
        """Access-Control-Allow-Methods should include required methods"""
        response = self.client.options("/api/credentials")
        allowed_methods = response.headers.get("Access-Control-Allow-Methods", "")

        # Should include at least GET
        self.assertTrue(len(allowed_methods) > 0)

    def test_access_control_allow_headers_set(self):
        """Access-Control-Allow-Headers should allow required headers"""
        response = self.client.options("/api/credentials")
        allowed_headers = response.headers.get("Access-Control-Allow-Headers", "")

        self.assertTrue(len(allowed_headers) > 0)
        self.assertIn("Content-Type", allowed_headers)

    def test_access_control_max_age_set(self):
        """Access-Control-Max-Age should be set for caching preflight"""
        response = self.client.options("/api/credentials")
        max_age = response.headers.get("Access-Control-Max-Age", "")

        self.assertTrue(len(max_age) > 0)
        self.assertEqual(max_age, "600")  # 10 minutes


if __name__ == "__main__":
    unittest.main()
