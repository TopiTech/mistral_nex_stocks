"""Coverage tests for app_state.py and crypto_utils.py."""

import unittest
from unittest.mock import MagicMock, patch

import app_state
import crypto_utils


class AppStateCryptoCoverageTests(unittest.TestCase):
    def test_initialize_yfinance_cache_exception_handling(self):
        # Verify that initialize_yfinance_cache logs warning and does not crash if yfinance throws
        state = app_state.AppState()
        with patch("yfinance.set_tz_cache_location", side_effect=Exception("Simulated yfinance error")):
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
