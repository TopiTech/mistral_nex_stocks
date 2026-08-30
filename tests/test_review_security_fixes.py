"""
Regression tests for code-review findings (R1, R3, R7).

These tests verify that the security hardening fixes work correctly:
- R1: Ephemeral fallback credentials are encrypted at rest in process memory.
- R3: Client-provided API keys require explicit MNS_ALLOW_CLIENT_API_KEY opt-in.
- R7: Native host caller authorization fails closed and ignores legacy test bypasses.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class EphemeralCredentialEncryptionTestCase(unittest.TestCase):
    """R1: MNS_EPHEMERAL_FALLBACK must encrypt credentials, not store plaintext.

    The test environment normally has a MemoryKeyring available, so we must
    disable both keyring and DPAPI to force the ephemeral fallback path.
    """

    def setUp(self):
        os.environ["MNS_EPHEMERAL_FALLBACK"] = "1"
        import crypto_utils

        self.crypto = crypto_utils
        # Reset module-level state between tests.
        with crypto_utils._EPHEMERAL_LOCK:
            crypto_utils._EPHEMERAL_CREDENTIALS.clear()
        crypto_utils._EPHEMERAL_KEY = None

    def tearDown(self):
        with self.crypto._EPHEMERAL_LOCK:
            self.crypto._EPHEMERAL_CREDENTIALS.clear()
        self.crypto._EPHEMERAL_KEY = None
        os.environ.pop("MNS_EPHEMERAL_FALLBACK", None)

    def _encode_and_get_stored(self, secret, key_name):
        """Encode a secret via the ephemeral path and return stored ciphertext."""
        with (
            patch.object(self.crypto, "KEYRING_AVAILABLE", False),
            patch.object(self.crypto, "_is_windows", return_value=False),
        ):
            return self.crypto._encode_secret(secret, key_name)

    def test_encode_returns_ephemeral_scheme(self):
        result = self._encode_and_get_stored("my-secret-key-12345", "mistral_api_key")
        self.assertEqual(result["scheme"], "ephemeral")
        self.assertEqual(result["value"], "")

    def test_stored_value_is_not_plaintext(self):
        secret = "super-secret-api-key-do-not-leak"
        self._encode_and_get_stored(secret, "mistral_api_key")
        with self.crypto._EPHEMERAL_LOCK:
            stored = self.crypto._EPHEMERAL_CREDENTIALS.get("mistral_api_key", "")
        self.assertNotEqual(stored, secret)
        self.assertTrue(len(stored) > 0)

    def test_decode_round_trip_returns_original(self):
        secret = "round-trip-secret-value-abcdef"
        self._encode_and_get_stored(secret, "mistral_api_key")
        decoded = self.crypto._decode_secret(
            {"scheme": "ephemeral", "value": ""}, "mistral_api_key"
        )
        self.assertEqual(decoded, secret)

    def test_clear_removes_credentials_and_key(self):
        self._encode_and_get_stored("to-be-cleared", "mistral_api_key")
        self.assertIsNotNone(self.crypto._EPHEMERAL_KEY)
        self.crypto.clear_ephemeral_credentials()
        with self.crypto._EPHEMERAL_LOCK:
            self.assertNotIn("mistral_api_key", self.crypto._EPHEMERAL_CREDENTIALS)
        self.assertIsNone(self.crypto._EPHEMERAL_KEY)

    def test_separate_process_key_isolation(self):
        """After clearing and re-encoding, a new key is generated (old data unreadable)."""
        self._encode_and_get_stored("old-secret", "mistral_api_key")
        with self.crypto._EPHEMERAL_LOCK:
            old_ciphertext = self.crypto._EPHEMERAL_CREDENTIALS["mistral_api_key"]
        self.crypto.clear_ephemeral_credentials()
        self._encode_and_get_stored("new-secret", "mistral_api_key")
        with self.crypto._EPHEMERAL_LOCK:
            new_ciphertext = self.crypto._EPHEMERAL_CREDENTIALS["mistral_api_key"]
        self.assertNotEqual(old_ciphertext, new_ciphertext)


class ClientApiKeyOptInTestCase(unittest.TestCase):
    """R3: Client-provided API keys require MNS_ALLOW_CLIENT_API_KEY=1."""

    def _make_request(self, auth_header=None):
        req = MagicMock()
        req.headers = {"Authorization": auth_header} if auth_header else {}
        return req

    def _call_extract_api_key(self, req):
        """Invoke extract_api_key with a mocked flask current_app and g."""
        mock_app = MagicMock()
        mock_app.config.get.return_value = True  # TESTING=True
        mock_app.logger = MagicMock()
        mock_g = MagicMock()
        mock_g.request_id = "test-123"
        with patch("flask.current_app", mock_app), patch("route_helpers.g", mock_g):
            from route_helpers import extract_api_key

            return extract_api_key(req)

    def _call_provider_extract(self, extractor_name, req):
        """Invoke a provider-key extractor with TESTING enabled."""
        mock_app = MagicMock()
        mock_app.config.get.return_value = True
        with patch("flask.current_app", mock_app):
            from route_helpers import extract_langsearch_api_key, extract_tavily_api_key

            extractors = {
                "langsearch": extract_langsearch_api_key,
                "tavily": extract_tavily_api_key,
            }
            return extractors[extractor_name](req)

    @patch("route_helpers.get_mistral_api_key", return_value="")
    def test_testing_without_opt_in_rejects_header_key(self, _mock_stored):
        os.environ.pop("MNS_ALLOW_CLIENT_API_KEY", None)
        result = self._call_extract_api_key(self._make_request("Bearer header-provided-key"))
        self.assertEqual(result, "")

    @patch("route_helpers.get_mistral_api_key", return_value="")
    def test_testing_with_opt_in_accepts_header_key(self, _mock_stored):
        os.environ["MNS_ALLOW_CLIENT_API_KEY"] = "1"
        try:
            result = self._call_extract_api_key(self._make_request("Bearer header-provided-key"))
            self.assertEqual(result, "header-provided-key")
        finally:
            os.environ.pop("MNS_ALLOW_CLIENT_API_KEY", None)

    @patch("route_helpers.get_mistral_api_key", return_value="stored-key")
    def test_stored_key_takes_priority(self, _mock_stored):
        os.environ.pop("MNS_ALLOW_CLIENT_API_KEY", None)
        result = self._call_extract_api_key(self._make_request("Bearer header-key"))
        self.assertEqual(result, "stored-key")

    def test_provider_headers_are_rejected_in_production_even_with_test_opt_in(self):
        """Provider header keys must have the same production guard as Mistral."""
        request = MagicMock()
        request.headers = {
            "X-LangSearch-Key": "langsearch-header-key",
            "X-Tavily-Key": "tavily-header-key",
        }
        environment = {
            "MNS_PROD": "1",
            "MNS_ALLOW_REMOTE_API": "0",
            "MNS_PROXY_FIX": "0",
            "MNS_ALLOW_CLIENT_API_KEY": "1",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("route_helpers.get_langsearch_api_key", return_value=""),
            patch("route_helpers.get_tavily_api_key", return_value=""),
        ):
            self.assertEqual(self._call_provider_extract("langsearch", request), "")
            self.assertEqual(self._call_provider_extract("tavily", request), "")

    def test_provider_headers_remain_available_for_explicit_local_test_opt_in(self):
        """The production guard must not remove the documented test-only path."""
        request = MagicMock()
        request.headers = {
            "X-LangSearch-Key": "langsearch-header-key",
            "X-Tavily-Key": "tavily-header-key",
        }
        environment = {
            "MNS_PROD": "0",
            "MNS_ALLOW_REMOTE_API": "0",
            "MNS_PROXY_FIX": "0",
            "MNS_ALLOW_CLIENT_API_KEY": "1",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("route_helpers.get_langsearch_api_key", return_value=""),
            patch("route_helpers.get_tavily_api_key", return_value=""),
        ):
            self.assertEqual(
                self._call_provider_extract("langsearch", request), "langsearch-header-key"
            )
            self.assertEqual(
                self._call_provider_extract("tavily", request), "tavily-header-key"
            )


class NativeHostCallerAuthorizationTestCase(unittest.TestCase):
    """R7: Legacy test bypasses must never authorize an unverified caller."""

    def test_legacy_bypass_env_vars_cannot_authorize_empty_ancestry(self):
        from native_host.native_host import _is_caller_authorized_browser

        bypass_configs = (
            {"MNS_PROD": "", "NATIVE_HOST_ALLOW_ANY_PARENT": "1", "MNS_TEST_MODE": ""},
            {"MNS_PROD": "", "NATIVE_HOST_ALLOW_ANY_PARENT": "", "MNS_TEST_MODE": "1"},
            {"MNS_PROD": "1", "NATIVE_HOST_ALLOW_ANY_PARENT": "1", "MNS_TEST_MODE": "1"},
        )
        for env in bypass_configs:
            with (
                self.subTest(env=env),
                patch.dict(os.environ, env, clear=False),
                patch("native_host.native_host._get_ancestor_process_names", return_value=[]),
            ):
                self.assertFalse(_is_caller_authorized_browser())


if __name__ == "__main__":
    unittest.main()
