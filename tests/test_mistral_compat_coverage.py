import sys
import importlib
import unittest
from unittest.mock import patch
import mistral_compat


class MistralCompatCoverageTestCase(unittest.TestCase):
    def test_mistral_compat_fallback(self):
        """Verify that fallback classes behave correctly when mistralai is missing."""
        # Hide real mistralai modules from sys.modules
        with patch.dict(sys.modules, {
            "mistralai": None,
            "mistralai.client": None,
            "mistralai.client.errors": None,
            "mistralai.errors": None,
        }):
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

    def test_message_helpers(self):
        """Verify message helper dict builders."""
        self.assertEqual(mistral_compat.SystemMessage("sys"), {"role": "system", "content": "sys"})
        self.assertEqual(mistral_compat.UserMessage("usr"), {"role": "user", "content": "usr"})
        self.assertEqual(mistral_compat.AssistantMessage("ast"), {"role": "assistant", "content": "ast"})


if __name__ == "__main__":
    unittest.main()
