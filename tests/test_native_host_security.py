"""
Native Host IPC Security Tests

Tests cover:
- Action whitelist validation
- Extension ID format validation
- Message size limits
- Input sanitization
"""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from native_host.native_host import (
    ALLOWED_ACTIONS,
    MAX_MESSAGE_BYTES,
    _is_caller_authorized_browser,
    _require_valid_extension_id,
    _validate_extension_id,
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
            ALLOWED_ACTIONS.add("malicious_action")


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

    def test_read_message_excessive_length_rejected_without_drain(self):
        """Excessively large length header (> MAX_DRAIN_BYTES) should return SKIP_FRAME immediately without reading stdin further"""
        import io
        import struct

        from native_host.native_host import FATAL_FRAME, MAX_DRAIN_BYTES, read_message

        # Pack length header = 2.5MB (> MAX_DRAIN_BYTES=2MB)
        huge_len = MAX_DRAIN_BYTES + 500000
        header_bytes = struct.pack("<I", huge_len)
        mock_stdin = io.BytesIO(header_bytes)

        with patch("native_host.native_host.RAW_STDIN", mock_stdin):
            result = read_message()
            self.assertIs(result, FATAL_FRAME)
            # Verify stdin position was not advanced past header (4 bytes)
            self.assertEqual(mock_stdin.tell(), 4)

    def test_read_message_fully_drained_oversized_frame_can_be_skipped(self):
        """A complete oversized frame preserves alignment and is safe to skip."""
        import io
        import struct

        from native_host.native_host import MAX_MESSAGE_BYTES, SKIP_FRAME, read_message

        length = MAX_MESSAGE_BYTES + 1
        mock_stdin = io.BytesIO(struct.pack("<I", length) + (b"x" * length))
        with patch("native_host.native_host.RAW_STDIN", mock_stdin):
            self.assertIs(read_message(), SKIP_FRAME)
            self.assertEqual(mock_stdin.tell(), length + 4)

    def test_read_message_truncated_payload_is_fatal(self):
        """EOF within a payload loses framing alignment and must close the channel."""
        import io
        import struct

        from native_host.native_host import FATAL_FRAME, read_message

        mock_stdin = io.BytesIO(struct.pack("<I", 10) + b"short")
        with patch("native_host.native_host.RAW_STDIN", mock_stdin):
            self.assertIs(read_message(), FATAL_FRAME)

    def test_main_exits_after_fatal_frame(self):
        """The host must not attempt to interpret bytes after a framing error."""
        from native_host import native_host

        with patch.object(native_host, "read_message", return_value=native_host.FATAL_FRAME) as read:
            native_host.main()
        read.assert_called_once_with()


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
        import native_host.native_host as nh_module
        from native_host.native_host import _check_rate_limit

        old_timestamps = nh_module._rate_limit_timestamps.copy()
        try:
            nh_module._rate_limit_timestamps.clear()
            self.assertTrue(_check_rate_limit())
        finally:
            nh_module._rate_limit_timestamps.clear()
            nh_module._rate_limit_timestamps.extend(old_timestamps)

    def test_rate_limit_blocks_excessive_traffic(self):
        """Excessive traffic should be blocked"""
        import native_host.native_host as nh_module
        from native_host.native_host import _check_rate_limit

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


class RequireValidExtensionIdTestCase(unittest.TestCase):
    """Test require_valid_extension_id with sys.argv mocked"""

    @patch(
        "native_host.native_host._load_allowed_manifest_origins",
        return_value={"abcdefghijklmnopqrstuvwxyz123456"},
    )
    @patch("native_host.native_host.send_message")
    def test_require_valid_extension_id_chrome_scheme(self, mock_send, mock_origins):
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        req = {"extensionId": valid_id, "action": "ping"}
        with patch(
            "sys.argv", ["native_host.py", "chrome-extension://abcdefghijklmnopqrstuvwxyz123456/"]
        ), patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["cmd.exe", "chrome.exe"],
        ):
            result = _require_valid_extension_id(req)
            self.assertEqual(result, valid_id)
            mock_send.assert_not_called()

    @patch(
        "native_host.native_host._load_allowed_manifest_origins",
        return_value={"abcdefghijklmnopqrstuvwxyz123456"},
    )
    @patch("native_host.native_host.send_message")
    def test_require_valid_extension_id_edge_uses_chrome_extension_scheme(
        self, mock_send, mock_origins
    ):
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        req = {"extensionId": valid_id, "action": "ping"}
        with patch(
            "sys.argv", ["native_host.py", "chrome-extension://abcdefghijklmnopqrstuvwxyz123456/"]
        ), patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["cmd.exe", "msedge.exe"],
        ):
            result = _require_valid_extension_id(req)
            self.assertEqual(result, valid_id)
            mock_send.assert_not_called()

    @patch(
        "native_host.native_host._load_allowed_manifest_origins",
        return_value={"abcdefghijklmnopqrstuvwxyz123456"},
    )
    @patch("native_host.native_host.send_message")
    def test_require_valid_extension_id_mismatch(self, mock_send, mock_origins):
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        req = {"extensionId": valid_id, "action": "ping"}
        with patch(
            "sys.argv", ["native_host.py", "chrome-extension://differentid_for_security_check__/"]
        ), patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["cmd.exe", "chrome.exe"],
        ):
            result = _require_valid_extension_id(req)
            self.assertIsNone(result)
            mock_send.assert_called_once_with({"ok": False, "error": "Origin mismatch"})

    @patch(
        "native_host.native_host._load_allowed_manifest_origins",
        return_value={"abcdefghijklmnopqrstuvwxyz123456"},
    )
    @patch("native_host.native_host.send_message")
    def test_require_valid_extension_id_missing_argv(self, mock_send, mock_origins):
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        req = {"extensionId": valid_id, "action": "ping"}
        with patch("sys.argv", ["native_host.py"]), patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["cmd.exe", "chrome.exe"],
        ):
            result = _require_valid_extension_id(req)
            self.assertIsNone(result)
            mock_send.assert_called_once_with({"ok": False, "error": "Missing process origin"})

    @patch(
        "native_host.native_host._load_allowed_manifest_origins",
        return_value={"abcdefghijklmnopqrstuvwxyz123456"},
    )
    @patch("native_host.native_host.send_message")
    def test_require_valid_extension_id_unrecognized_origin(self, mock_send, mock_origins):
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        req = {"extensionId": valid_id, "action": "ping"}
        with patch("sys.argv", ["native_host.py", "file://not-an-extension"]), patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["cmd.exe", "chrome.exe"],
        ):
            result = _require_valid_extension_id(req)
            self.assertIsNone(result)
            mock_send.assert_called_once_with({"ok": False, "error": "Unrecognized process origin"})

    @patch(
        "native_host.native_host._load_allowed_manifest_origins",
        return_value={"abcdefghijklmnopqrstuvwxyz123456"},
    )
    @patch("native_host.native_host.send_message")
    def test_require_valid_extension_id_rejects_nonstandard_extension_scheme(
        self, mock_send, mock_origins
    ):
        valid_id = "abcdefghijklmnopqrstuvwxyz123456"
        req = {"extensionId": valid_id, "action": "ping"}
        with patch("sys.argv", ["native_host.py", f"extension://{valid_id}/"]), patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["cmd.exe", "msedge.exe"],
        ):
            result = _require_valid_extension_id(req)
            self.assertIsNone(result)
            mock_send.assert_called_once_with({"ok": False, "error": "Unrecognized process origin"})


class CallerAuthorizationTestCase(unittest.TestCase):
    """Native Messaging callers must trace to a supported browser process."""

    def test_chrome_ancestor_is_authorized(self):
        with patch(
            "native_host.native_host._get_ancestor_process_names", return_value=["chrome.exe"]
        ):
            self.assertTrue(_is_caller_authorized_browser())

    def test_edge_ancestor_is_authorized(self):
        with patch(
            "native_host.native_host._get_ancestor_process_names", return_value=["msedge.exe"]
        ):
            self.assertTrue(_is_caller_authorized_browser())

    def test_cmd_wrapper_chain_is_authorized_only_with_browser_ancestor(self):
        with patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["cmd.exe", "chrome.exe"],
        ):
            self.assertTrue(_is_caller_authorized_browser())

    def test_empty_ancestor_list_is_rejected(self):
        with patch("native_host.native_host._get_ancestor_process_names", return_value=[]):
            self.assertFalse(_is_caller_authorized_browser())

    def test_generic_wrapper_chain_without_browser_is_rejected(self):
        with patch(
            "native_host.native_host._get_ancestor_process_names",
            return_value=["python.exe", "cmd.exe", "powershell.exe"],
        ):
            self.assertFalse(_is_caller_authorized_browser())


class CallerAuthorizationActionGateTestCase(unittest.TestCase):
    """The caller gate must run before regular and secret-bearing actions."""

    VALID_ID = "abcdefghijklmnopqrstuvwxyz123456"

    def _run_main_request(self, action, ancestors):
        from native_host import native_host

        sent: list[dict[str, object]] = []
        request = {"action": action, "extensionId": self.VALID_ID}
        with (
            patch.object(native_host, "read_message", side_effect=[request, None]),
            patch.object(native_host, "send_message", side_effect=sent.append),
            patch.object(native_host, "_check_rate_limit", return_value=True),
            patch.object(native_host, "_get_ancestor_process_names", return_value=ancestors),
            patch.object(native_host, "_load_allowed_manifest_origins", return_value={self.VALID_ID}),
            patch("sys.argv", ["native_host.py", f"chrome-extension://{self.VALID_ID}/"]),
        ):
            native_host.main()
        return sent

    def test_browser_wrapper_chain_allows_regular_ping(self):
        sent = self._run_main_request(
            "ping", ["cmd.exe", "chrome.exe"]
        )
        self.assertEqual(sent, [{"ok": True, "message": "pong"}])

    def test_untrusted_caller_cannot_dispatch_regular_action(self):
        from native_host import native_host

        with patch.object(native_host, "start") as start:
            sent = self._run_main_request("start_backend", [])
        self.assertEqual(sent, [{"ok": False, "error": "Unauthorized parent process"}])
        start.assert_not_called()

    def test_untrusted_caller_cannot_dispatch_token_actions(self):
        from native_host import native_host

        for action in ("get_shutdown_token", "get_extension_api_token"):
            with self.subTest(action=action), patch.object(
                native_host, "_token_action_allowed"
            ) as token_budget, patch.object(native_host, "is_backend_healthy_once") as health_check:
                sent = self._run_main_request(action, [])
            self.assertEqual(sent, [{"ok": False, "error": "Unauthorized parent process"}])
            token_budget.assert_not_called()
            health_check.assert_not_called()


class LauncherScriptForwardingTestCase(unittest.TestCase):
    """Test launcher batch scripts include argument forwarding"""

    def test_launcher_cmd_template_forwards_arguments(self):
        template_file = Path(__file__).parent.parent / "native_host" / "host_launcher.cmd.template"
        self.assertTrue(template_file.exists(), "host_launcher.cmd.template missing")
        content = template_file.read_text(encoding="utf-8")
        self.assertIn("%*", content, "host_launcher.cmd.template must pass %* to forward browser origin arguments")

    def test_native_host_cmd_forwards_arguments(self):
        cmd_file = Path(__file__).parent.parent / "native_host" / "native_host.cmd"
        if cmd_file.exists():
            content = cmd_file.read_text(encoding="utf-8")
            self.assertIn("%*", content, "native_host.cmd must pass %* to forward browser origin arguments")

    def test_read_only_windows_validator_checks_generated_manifest(self):
        validator = Path(__file__).parent.parent / "native_host" / "validate_native_host_windows.ps1"
        content = validator.read_text(encoding="utf-8")
        self.assertIn("Read-only validation", content)
        self.assertIn("ConvertFrom-Json", content)
        self.assertNotIn("CreateSubKey", content)
        self.assertNotIn("SetValue", content)

    def test_validator_accepts_template_with_relative_and_absolute_paths(self):
        """A relative template path must not be mistaken for a generated manifest."""
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is required to execute the Windows validator")

        root = Path(__file__).parent.parent
        validator = root / "native_host" / "validate_native_host_windows.ps1"
        template = root / "native_host" / "com.mistral_nex_stocks.host.json.template"
        for manifest_path in (r".\native_host\com.mistral_nex_stocks.host.json.template", str(template)):
            with self.subTest(manifest_path=manifest_path):
                result = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(validator),
                        "-ManifestPath",
                        manifest_path,
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                output = f"{result.stdout}\n{result.stderr}"
                self.assertEqual(result.returncode, 0, output)
                self.assertIn("structurally valid", output)


if __name__ == "__main__":
    unittest.main()



class ShutdownTokenGateTestCase(unittest.TestCase):
    """R10: get_shutdown_token is gated on a healthy backend."""

    VALID_ID = "abcdefghijklmnopqrstuvwxyz123456"

    def _run_token_request(self, healthy):
        import json as _json
        import tempfile

        from native_host import native_host

        req = {"extensionId": self.VALID_ID, "action": "get_shutdown_token"}
        sent = []
        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / ".mns_shutdown_token"
            token_file.write_text(
                _json.dumps({"scheme": "fernet", "value": "enc"}), encoding="utf-8"
            )
            with (
                patch.object(native_host, "read_message", side_effect=[req, None]),
                patch.object(
                    native_host, "send_message", side_effect=lambda m: sent.append(m)
                ),
                patch.object(native_host, "_token_action_allowed", return_value=True),
                patch.object(
                    native_host, "is_backend_healthy_once", return_value=healthy
                ),
                patch.object(native_host, "unprotect_data", return_value="tok-123"),
                patch.object(
                    native_host,
                    "_load_allowed_manifest_origins",
                    return_value={self.VALID_ID},
                ),
                patch(
                    "sys.argv",
                    ["native_host.py", f"chrome-extension://{self.VALID_ID}/"],
                ),
                patch.object(
                    native_host,
                    "_get_ancestor_process_names",
                    return_value=["cmd.exe", "chrome.exe"],
                ),
                patch("config_store.APP_DATA_DIR", Path(tmpdir)),
            ):
                native_host.main()
        return sent

    def test_shutdown_token_refused_when_backend_down(self):
        """The token must never be handed out while the backend is unhealthy:
        the file may linger on disk but the secret is not disclosed."""
        sent = self._run_token_request(healthy=False)
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("not running", sent[0]["error"])
        self.assertNotIn("token", sent[0])

    def test_shutdown_token_returned_when_backend_healthy(self):
        """With a healthy backend and a valid token file, the flow still works."""
        sent = self._run_token_request(healthy=True)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["ok"])
        self.assertEqual(sent[0]["token"], "tok-123")
