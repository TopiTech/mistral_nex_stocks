"""Coverage tests for app_state.py and crypto_utils.py."""

import unittest
from unittest.mock import MagicMock, patch

import app_state
import crypto_utils


class AppStateCryptoCoverageTests(unittest.TestCase):
    def test_initialize_yfinance_cache_exception_handling(self):
        # Verify that initialize_yfinance_cache logs warning and does not crash if yfinance throws
        state = app_state.AppState()
        with patch(
            "yfinance.set_tz_cache_location", side_effect=Exception("Simulated yfinance error")
        ):
            state.initialize_yfinance_cache()

    def test_shutdown_executors_lock_timeout_handling(self):
        state = app_state.AppState()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        state.ai.mistral_clients_lock = mock_lock
        state.shutdown_executors()
        mock_lock.acquire.assert_called_with(timeout=2.0)

    def test_crypto_utils_ephemeral_fallback_and_edge_cases(self):
        # Test ephemeral credentials lock and fallback storage
        key_name = "test_ephemeral_key"
        encoded = crypto_utils._encode_secret("secret_val_123", key_name)
        decoded = crypto_utils._decode_secret(encoded, key_name)
        self.assertEqual(decoded, "secret_val_123")

        # Empty secret
        self.assertEqual(crypto_utils._encode_secret("", key_name), "")
        self.assertEqual(crypto_utils._decode_secret(None, key_name), "")

        # Invalid base64 payload handling
        self.assertEqual(crypto_utils._decode_secret("invalid-base64-payload!@", key_name), "")

    def test_crypto_utils_decode_secret_schemes(self):
        # 1. Ephemeral scheme test
        ephemeral_entry = {"scheme": "ephemeral"}
        with crypto_utils._EPHEMERAL_LOCK:
            ephemeral_key = crypto_utils._get_ephemeral_key()
            from cryptography.fernet import Fernet

            f = Fernet(ephemeral_key.encode("ascii"))
            crypto_utils._EPHEMERAL_CREDENTIALS["ephemeral_test_key"] = f.encrypt(
                b"my_ephemeral_secret"
            ).decode("ascii")

        decoded_ephemeral = crypto_utils._decode_secret(ephemeral_entry, "ephemeral_test_key")
        self.assertEqual(decoded_ephemeral, "my_ephemeral_secret")

        # 2. DPAPI fallback scheme test on Windows or mocked _is_windows
        dpapi_fallback_entry = {
            "scheme": "keyring",
            "dpapi_fallback": "AABBCD==",  # mock base64 payload
        }
        with (
            patch("crypto_utils.keyring.get_password", return_value=None),
            patch("crypto_utils._is_windows", return_value=True),
            patch("crypto_utils._dpapi_unprotect", return_value=b"fallback_secret"),
            patch("crypto_utils.KEYRING_AVAILABLE", False),
        ):
            decoded_fallback = crypto_utils._decode_secret(
                dpapi_fallback_entry, "fallback_test_key"
            )
            self.assertEqual(decoded_fallback, "fallback_secret")

        # 3. Direct DPAPI scheme test
        dpapi_entry = {
            "scheme": "dpapi",
            "value": "AABBCD==",
        }
        with (
            patch("crypto_utils._is_windows", return_value=True),
            patch("crypto_utils._dpapi_unprotect", return_value=b"dpapi_direct_secret"),
        ):
            decoded_dpapi = crypto_utils._decode_secret(dpapi_entry, "dpapi_test_key")
            self.assertEqual(decoded_dpapi, "dpapi_direct_secret")
