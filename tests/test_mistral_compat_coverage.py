import importlib
import sys
import unittest
from unittest.mock import patch

import mistral_compat


class MistralCompatCoverageTestCase(unittest.TestCase):
    def test_mistral_compat_fallback(self):
        """Verify that fallback classes behave correctly when mistralai is missing."""
        # Hide real mistralai modules from sys.modules
        with patch.dict(
            sys.modules,
            {
                "mistralai": None,
                "mistralai.client": None,
                "mistralai.client.errors": None,
                "mistralai.errors": None,
            },
        ):
            # Reload to trigger the fallbacks
            importlib.reload(mistral_compat)

            # Test Mistral fallback
            fallback_client = mistral_compat.Mistral(api_key="test_key", test_param="val")
            self.assertEqual(fallback_client.api_key, "test_key")
            self.assertEqual(fallback_client.kwargs.get("test_param"), "val")

            # Test SDKError fallback
            fallback_error = mistral_compat.SDKError("simulated error", status_code=403)
            self.assertEqual(fallback_error.status_code, 403)
            self.assertEqual(str(fallback_error), "simulated error")
            self.assertIsNotNone(fallback_error.response)

    def test_rejects_backdoored_mistralai_version(self):
        """Importing mistral_compat must fail loudly when mistralai==2.4.6 is installed.

        The 2.4.6 PyPI release contained a malicious dropper (GHSA-wx9m-wx4f-4cmg)
        that executes at import time on Linux; the guard must refuse to import it.
        """
        with patch("importlib.metadata.version", return_value="2.4.6"):
            with self.assertRaises(ImportError) as ctx:
                importlib.reload(mistral_compat)
        self.assertIn("2.4.6", str(ctx.exception))
        self.assertIn("GHSA-wx9m-wx4f-4cmg", str(ctx.exception))
        # Restore the real import state for subsequent tests.
        importlib.reload(mistral_compat)

    def test_message_helpers(self):
        """Verify message helper dict builders."""
        self.assertEqual(mistral_compat.SystemMessage("sys"), {"role": "system", "content": "sys"})
        self.assertEqual(mistral_compat.UserMessage("usr"), {"role": "user", "content": "usr"})
        self.assertEqual(
            mistral_compat.AssistantMessage("ast"), {"role": "assistant", "content": "ast"}
        )


if __name__ == "__main__":
    unittest.main()
