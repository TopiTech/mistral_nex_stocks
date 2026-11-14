"""Tests to verify test suite environment isolation and cross-test hermeticity.

These tests ensure that:
1. Tests modifying or deleting os.environ keys do not leak into subsequent tests.
2. The `isolate_and_restore_environment` fixture in conftest.py restores all
   baseline test environment variables (MNS_ALLOW_CLIENT_API_KEY, MNS_SKIP_BOOTSTRAP, etc.).
3. Authorization header processing remains reliable regardless of test execution order.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from route_helpers import extract_api_key


class EnvironmentIsolationPrecursorTestCase(unittest.TestCase):
    """Mutate environment in a potentially destructive way to test cleanup."""

    def test_01_deliberately_pollute_and_strip_environment(self):
        # Deliberately pollute with a new key
        os.environ["MNS_POLLUTION_CANARY"] = "leaked_canary_value"
        # Deliberately strip baseline environment keys
        os.environ.pop("MNS_ALLOW_CLIENT_API_KEY", None)
        os.environ.pop("MNS_SKIP_BOOTSTRAP", None)
        os.environ.pop("MNS_MISTRAL_MIN_INTERVAL", None)
        # Assert they are currently mutated in this test
        self.assertNotIn("MNS_ALLOW_CLIENT_API_KEY", os.environ)
        self.assertNotIn("MNS_SKIP_BOOTSTRAP", os.environ)
        self.assertNotIn("MNS_MISTRAL_MIN_INTERVAL", os.environ)
        self.assertEqual(os.environ.get("MNS_POLLUTION_CANARY"), "leaked_canary_value")


class EnvironmentIsolationVerificationTestCase(unittest.TestCase):
    """Verify that the isolate_and_restore_environment fixture repaired the environment."""

    def test_02_verify_environment_was_completely_restored(self):
        # Canary must have been purged
        self.assertNotIn(
            "MNS_POLLUTION_CANARY",
            os.environ,
            "Pollution canary leaked from preceding test across fixture boundary!",
        )
        # Baseline variables must have been restored
        self.assertEqual(
            os.environ.get("MNS_ALLOW_CLIENT_API_KEY"),
            "1",
            "MNS_ALLOW_CLIENT_API_KEY was not restored after preceding test stripped it!",
        )
        self.assertEqual(
            os.environ.get("MNS_SKIP_BOOTSTRAP"),
            "1",
            "MNS_SKIP_BOOTSTRAP was not restored after preceding test stripped it!",
        )
        self.assertEqual(
            os.environ.get("MNS_MISTRAL_MIN_INTERVAL"),
            "0",
            "MNS_MISTRAL_MIN_INTERVAL was not restored after preceding test stripped it!",
        )

    @patch("route_helpers.get_mistral_api_key", return_value="")
    def test_03_extract_api_key_works_with_baseline_environment(self, _mock_get_key):
        """extract_api_key must extract Bearer header key in test mode with baseline env."""
        from app import app

        orig_testing = app.config.get("TESTING")
        app.config["TESTING"] = True
        try:
            with app.app_context():
                mock_req = MagicMock()
                mock_req.headers = {"Authorization": "Bearer test-suite-key-12345"}
                key = extract_api_key(mock_req)
                self.assertEqual(key, "test-suite-key-12345")
        finally:
            app.config["TESTING"] = orig_testing


class KeyringIsolationPrecursorTestCase(unittest.TestCase):
    """Mutate keyring backend and stored credentials to verify cross-test cleanup."""

    def test_01_deliberately_swap_keyring_and_store_password(self):
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        from tests.test_support import get_shared_memory_keyring

        mem_keyring = get_shared_memory_keyring()
        mem_keyring.set_password("mistral-nex-stocks", "leaked-key", "leaked-secret")
        fail_keyring = FailKeyring()
        keyring.set_keyring(fail_keyring)

        self.assertIs(keyring.get_keyring(), fail_keyring)
        self.assertEqual(
            mem_keyring.get_password("mistral-nex-stocks", "leaked-key"),
            "leaked-secret",
        )


class KeyringIsolationVerificationTestCase(unittest.TestCase):
    """Verify reset_app_state restores the shared MemoryKeyring and clears secrets."""

    def test_02_verify_keyring_backend_and_passwords_restored(self):
        import keyring

        from tests.test_support import get_shared_memory_keyring

        mem_keyring = get_shared_memory_keyring()
        self.assertIs(
            keyring.get_keyring(),
            mem_keyring,
            "Swapped keyring backend leaked across test fixture boundary!",
        )
        self.assertEqual(mem_keyring.passwords, {})
        self.assertIsNone(keyring.get_password("mistral-nex-stocks", "leaked-key"))

    def test_03_reset_app_state_internals_restores_swapped_failing_keyring(self):
        """reset_app_state_internals must restore MemoryKeyring before credential cleanup."""
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        from tests.test_support import get_shared_memory_keyring, reset_app_state_internals

        mem_keyring = get_shared_memory_keyring()
        mem_keyring.set_password("mistral-nex-stocks", "temp-key", "temp-secret")
        fail_keyring = FailKeyring()
        keyring.set_keyring(fail_keyring)
        self.assertIs(keyring.get_keyring(), fail_keyring)

        reset_app_state_internals()

        self.assertIs(keyring.get_keyring(), mem_keyring)
        self.assertEqual(mem_keyring.passwords, {})
        self.assertIsNone(keyring.get_password("mistral-nex-stocks", "temp-key"))


if __name__ == "__main__":
    unittest.main()
