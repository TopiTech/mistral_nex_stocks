"""
Native Host IPC Security Tests

Tests cover:
- Action whitelist validation
- Extension ID format validation
- Message size limits
- Input sanitization
"""

import unittest
import sys
from pathlib import Path
from unittest.mock import patch
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from native_host.native_host import (
    ALLOWED_ACTIONS,
    _validate_extension_id,
    MAX_MESSAGE_BYTES,
)


class ActionWhitelistTestCase(unittest.TestCase):
    """Test action whitelist validation"""

    def test_allowed_actions_are_defined(self):
        """Allowed actions should be defined"""
        self.assertIn("start_backend", ALLOWED_ACTIONS)
        self.assertIn("get_shutdown_token", ALLOWED_ACTIONS)
        self.assertIn("get_backend_port", ALLOWED_ACTIONS)
        self.assertIn("ping", ALLOWED_ACTIONS)

    def test_unknown_action_not_in_whitelist(self):
        """Unknown actions should not be in whitelist"""
        self.assertNotIn("delete_all_data", ALLOWED_ACTIONS)
        self.assertNotIn("execute_command", ALLOWED_ACTIONS)
        self.assertNotIn("", ALLOWED_ACTIONS)

    def test_whitelist_is_frozen_set(self):
        """Whitelist should be immutable"""
        self.assertIsInstance(ALLOWED_ACTIONS, frozenset)
        with self.assertRaises(AttributeError):
            getattr(ALLOWED_ACTIONS, "add")("malicious_action")


class ExtensionIdValidationTestCase(unittest.TestCase):
    """Test Chrome extension ID format validation"""

    patcher: Any

    @classmethod
    def setUpClass(cls):
        cls.patcher = patch(
            "native_host.native_host._load_allowed_manifest_origins",
            return_value={"abcdefghijklmnopqrstuvwxyz123456"},
        )
        cls.patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()

    def test_valid_extension_id(self):
        """Valid 32-char lowercase alphanumeric ID should be accepted"""
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        result = _validate_extension_id(valid_id)
        self.assertEqual(result, valid_id)

    def test_invalid_extension_id_too_short(self):
        """ID shorter than 32 chars should be rejected"""
        short_id = "abc123"
        result = _validate_extension_id(short_id)
        self.assertIsNone(result)

    def test_invalid_extension_id_too_long(self):
        """ID longer than 32 chars should be rejected"""
        long_id = "abcdefghijklmnopqrstuvwxyz1234567890"
        result = _validate_extension_id(long_id)
        self.assertIsNone(result)

    def test_invalid_extension_id_uppercase(self):
        """ID with uppercase letters should be rejected"""
        upper_id = "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        result = _validate_extension_id(upper_id)
        self.assertIsNone(result)

    def test_invalid_extension_id_special_chars(self):
        """ID with special characters should be rejected"""
        special_id = "abcdefghijklmnopqrstuvwxyz12345!"
        result = _validate_extension_id(special_id)
        self.assertIsNone(result)

    def test_none_extension_id(self):
        """None should return None"""
        result = _validate_extension_id(None)
        self.assertIsNone(result)

    def test_empty_extension_id(self):
        """Empty string should be rejected"""
        result = _validate_extension_id("")
        self.assertIsNone(result)

    def test_extension_id_with_whitespace(self):
        """ID with whitespace should be stripped and validated"""
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        result = _validate_extension_id(f"  {valid_id}  ")
        self.assertEqual(result, valid_id)


class MessageSizeLimitTestCase(unittest.TestCase):
    """Test message size limits"""

    def test_max_message_bytes_is_defined(self):
        """MAX_MESSAGE_BYTES should be defined"""
        self.assertIsInstance(MAX_MESSAGE_BYTES, int)
        self.assertGreater(MAX_MESSAGE_BYTES, 0)

    def test_max_message_bytes_default_value(self):
        """Default MAX_MESSAGE_BYTES should be 1MB"""
        self.assertEqual(MAX_MESSAGE_BYTES, 1024 * 1024)


class InputSanitizationTestCase(unittest.TestCase):
    """Test input sanitization"""

    def test_malicious_action_rejected(self):
        """Malicious action names should be rejected"""
        malicious_actions = [
            "start_backend; rm -rf /",
            "start_backend && cat /etc/passwd",
            "../../etc/passwd",
            'start_backend\nos.system("rm -rf /")',
        ]
        for action in malicious_actions:
            self.assertNotIn(action, ALLOWED_ACTIONS)


class NativeHostRateLimitTestCase(unittest.TestCase):
    """Test IPC rate limiting"""

    def test_rate_limit_allows_normal_traffic(self):
        """Normal traffic within limits should be allowed"""
        from native_host.native_host import _check_rate_limit
        import native_host.native_host as nh_module

        old_timestamps = nh_module._rate_limit_timestamps.copy()
        try:
            nh_module._rate_limit_timestamps.clear()
            self.assertTrue(_check_rate_limit())
        finally:
            nh_module._rate_limit_timestamps.clear()
            nh_module._rate_limit_timestamps.extend(old_timestamps)

    def test_rate_limit_blocks_excessive_traffic(self):
        """Excessive traffic should be blocked"""
        from native_host.native_host import _check_rate_limit
        import native_host.native_host as nh_module

        old_timestamps = nh_module._rate_limit_timestamps.copy()
        old_max = nh_module._NATIVE_RATE_LIMIT_MAX
        try:
            nh_module._rate_limit_timestamps.clear()
            nh_module._NATIVE_RATE_LIMIT_MAX = 3
            self.assertTrue(_check_rate_limit())
            self.assertTrue(_check_rate_limit())
            self.assertTrue(_check_rate_limit())
            self.assertFalse(_check_rate_limit())
        finally:
            nh_module._rate_limit_timestamps.clear()
            nh_module._rate_limit_timestamps.extend(old_timestamps)
            nh_module._NATIVE_RATE_LIMIT_MAX = old_max


if __name__ == "__main__":
    unittest.main()
