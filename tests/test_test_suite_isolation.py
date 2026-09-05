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


if __name__ == "__main__":
    unittest.main()
