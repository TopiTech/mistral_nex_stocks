"""
Native Host IPC Security Tests

Tests cover:
- Action whitelist validation
- Extension ID format validation
- Message size limits
- Input sanitization
"""

import json
import shutil
import subprocess
import sys
import tempfile
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

    def test_read_message_str_path_round_trips(self):
        """read_message must accept str-mode stdin (test stubs) and not regress to O(N^2)."""
        import io
        import json
        import struct

        from native_host.native_host import read_message

        payload = json.dumps({"action": "health", "ok": True})
        payload_bytes = payload.encode("utf-8")
        # StringIO yields str in Python 3; the str branch of read_message
        # must round-trip the payload without corruption.
        mock_stdin = io.StringIO(struct.pack("<I", len(payload_bytes)).decode("latin-1") + payload)
        with patch("native_host.native_host.RAW_STDIN", mock_stdin):
            result = read_message()
        self.assertEqual(result, {"action": "health", "ok": True})

    def test_read_message_str_path_fragmented_chunks(self):
        """Str branch must accumulate byte-length across fragmented reads."""
        import io
        import json
        import struct

        from native_host.native_host import read_message

        payload = json.dumps({"action": "ping", "value": 42})
        payload_bytes = payload.encode("utf-8")

        class FragmentedStringIO(io.StringIO):
            def __init__(self, data: str, chunk_size: int = 1) -> None:
                super().__init__(data)
                self._chunk_size = chunk_size

            def read(self, size: int = -1) -> str:
                if size is None or size < 0:
                    return super().read(size)
                return super().read(min(size, self._chunk_size))

        # First header chunk (4 bytes) followed by the body.
        stream_data = struct.pack("<I", len(payload_bytes)).decode("latin-1") + payload
        mock_stdin = FragmentedStringIO(stream_data, chunk_size=2)
        with patch("native_host.native_host.RAW_STDIN", mock_stdin):
            result = read_message()
        self.assertEqual(result, {"action": "ping", "value": 42})

    def test_read_message_str_path_preserves_unicode_frame_alignment(self):
        """A multibyte text payload must not consume the next native frame."""
        import io
        import json
        import struct

        from native_host.native_host import read_message

        first_payload = json.dumps({"action": "ping", "text": "日本語"}, ensure_ascii=False)
        second_payload = json.dumps({"action": "ping", "value": 2})
        stream_data = (
            struct.pack("<I", len(first_payload.encode("utf-8"))).decode("latin-1")
            + first_payload
            + struct.pack("<I", len(second_payload.encode("utf-8"))).decode("latin-1")
            + second_payload
        )
        mock_stdin = io.StringIO(stream_data)
        with patch("native_host.native_host.RAW_STDIN", mock_stdin):
            first = read_message()
            second = read_message()

        self.assertEqual(first, {"action": "ping", "text": "日本語"})
        self.assertEqual(second, {"action": "ping", "value": 2})

    def test_read_message_binary_path_handles_fragmented_large_payload(self):
        """Binary frame accumulation remains correct when pipe reads are short."""
        import io
        import json
        import struct

        from native_host.native_host import read_message

        payload = json.dumps({"action": "ping", "value": "x" * 100_000}).encode("utf-8")

        class FragmentedBytesIO(io.BytesIO):
            def __init__(self, data: bytes, chunk_size: int = 3) -> None:
                super().__init__(data)
                self._chunk_size = chunk_size

            def read(self, size: int = -1) -> bytes:
                if size is None or size < 0:
                    return super().read(size)
                return super().read(min(size, self._chunk_size))

        mock_stdin = FragmentedBytesIO(struct.pack("<I", len(payload)) + payload)
        with patch("native_host.native_host.RAW_STDIN", mock_stdin):
            result = read_message()

        self.assertEqual(result, {"action": "ping", "value": "x" * 100_000})

    def test_main_exits_after_fatal_frame(self):
        """The host must not attempt to interpret bytes after a framing error."""
        from native_host import native_host

        with patch.object(
            native_host, "read_message", return_value=native_host.FATAL_FRAME
        ) as read:
            native_host.main()
        read.assert_called_once_with()

    def test_main_rejects_unhashable_action_without_closing_channel(self):
        """Malformed JSON action types must not escape the main loop."""
        from native_host import native_host

        sent = []
        messages = iter([{"action": []}, None])
        with (
            patch.object(native_host, "read_message", side_effect=lambda: next(messages)),
            patch.object(native_host, "send_message", side_effect=sent.append),
            patch.object(native_host, "_check_rate_limit", return_value=True),
        ):
            native_host.main()

        self.assertEqual(sent, [{"ok": False, "error": "Unknown or disallowed action"}])

    def test_main_does_not_echo_unknown_action_payload(self):
        """Unknown action errors must not reflect untrusted input."""
        from native_host import native_host

        sent = []
        messages = iter([{"action": "unknown\nforged-log-entry"}, None])
        with (
            patch.object(native_host, "read_message", side_effect=lambda: next(messages)),
            patch.object(native_host, "send_message", side_effect=sent.append),
            patch.object(native_host, "_check_rate_limit", return_value=True),
        ):
            native_host.main()

        self.assertEqual(sent, [{"ok": False, "error": "Unknown or disallowed action"}])


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
        with (
            patch(
                "sys.argv",
                ["native_host.py", "chrome-extension://abcdefghijklmnopqrstuvwxyz123456/"],
            ),
            patch(
                "native_host.native_host._get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
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
        with (
            patch(
                "sys.argv",
                ["native_host.py", "chrome-extension://abcdefghijklmnopqrstuvwxyz123456/"],
            ),
            patch(
                "native_host.native_host._get_ancestor_process_names",
                return_value=["cmd.exe", "msedge.exe"],
            ),
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
        with (
            patch(
                "sys.argv",
                ["native_host.py", "chrome-extension://differentid_for_security_check__/"],
            ),
            patch(
                "native_host.native_host._get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
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
        with (
            patch("sys.argv", ["native_host.py"]),
            patch(
                "native_host.native_host._get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
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
        with (
            patch("sys.argv", ["native_host.py", "file://not-an-extension"]),
            patch(
                "native_host.native_host._get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
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
        with (
            patch("sys.argv", ["native_host.py", f"extension://{valid_id}/"]),
            patch(
                "native_host.native_host._get_ancestor_process_names",
                return_value=["cmd.exe", "msedge.exe"],
            ),
        ):
            result = _require_valid_extension_id(req)
            self.assertIsNone(result)
            mock_send.assert_called_once_with({"ok": False, "error": "Unrecognized process origin"})


class CallerAuthorizationTestCase(unittest.TestCase):
    """Native Messaging callers must trace to a supported browser process."""

    _CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    _GOOGLE_SUBJECT = "CN=Google LLC, O=Google LLC, C=US"

    def _windows_caller_is_authorized(self, image_path, signature):
        from native_host import native_host

        with (
            patch.object(native_host.os, "name", "nt"),
            patch.object(
                native_host,
                "_get_windows_ancestor_processes",
                return_value=[
                    (100, "cmd.exe", r"C:\Windows\System32\cmd.exe"),
                    (99, "chrome.exe", image_path),
                ],
            ),
            patch.object(
                native_host,
                "_get_windows_browser_install_roots",
                return_value=(r"C:\Program Files",),
            ),
            patch.object(
                native_host, "_get_windows_authenticode_signature", return_value=signature
            ),
        ):
            return _is_caller_authorized_browser()

    def test_windows_allowed_name_with_noncanonical_path_is_rejected(self):
        self.assertFalse(
            self._windows_caller_is_authorized(
                r"C:\Users\attacker\chrome.exe", ("Valid", self._GOOGLE_SUBJECT)
            )
        )

    def test_windows_allowed_name_with_untrusted_signature_is_rejected(self):
        self.assertFalse(
            self._windows_caller_is_authorized(self._CHROME_PATH, ("Valid", "CN=Attacker"))
        )

    def test_windows_unverified_brave_name_is_rejected(self):
        from native_host import native_host

        with (
            patch.object(native_host.os, "name", "nt"),
            patch.object(
                native_host,
                "_get_windows_ancestor_processes",
                return_value=[(99, "brave.exe", r"C:\Users\attacker\brave.exe")],
            ),
        ):
            self.assertFalse(_is_caller_authorized_browser())

    def test_windows_cmd_wrapper_chain_is_authorized_with_canonical_signed_browser(self):
        self.assertTrue(
            self._windows_caller_is_authorized(self._CHROME_PATH, ("Valid", self._GOOGLE_SUBJECT))
        )

    def test_windows_ancestry_and_signature_failures_are_rejected(self):
        self.assertFalse(self._windows_caller_is_authorized(None, ("Valid", self._GOOGLE_SUBJECT)))
        self.assertFalse(self._windows_caller_is_authorized(self._CHROME_PATH, None))
        self.assertFalse(
            self._windows_caller_is_authorized(
                self._CHROME_PATH, ("NotSigned", self._GOOGLE_SUBJECT)
            )
        )

    def test_windows_authenticode_subprocess_failure_returns_none(self):
        from native_host import native_host

        with (
            patch.object(native_host.os, "name", "nt"),
            patch.object(native_host, "_get_windows_win32_signature", return_value=None),
            patch.object(
                native_host,
                "_get_windows_powershell_path",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            patch.object(native_host.subprocess, "run", side_effect=OSError("unavailable")),
        ):
            self.assertIsNone(native_host._get_windows_authenticode_signature(self._CHROME_PATH))

    def test_windows_authenticode_uses_fixed_arguments_and_timeout(self):
        from native_host import native_host

        with (
            patch.object(native_host.os, "name", "nt"),
            patch.object(native_host, "_get_windows_win32_signature", return_value=None),
            patch.object(
                native_host,
                "_get_windows_powershell_path",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            patch.object(
                native_host.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, '{"Status":"Valid","Subject":"CN=Google LLC"}'
                ),
            ) as run,
        ):
            self.assertEqual(
                native_host._get_windows_authenticode_signature(self._CHROME_PATH),
                ("Valid", "CN=Google LLC"),
            )
        command = run.call_args.args[0]
        # The image path must be embedded (single-quoted) in the -Command
        # script text: -Command appends a following argv entry to the script
        # itself, which previously produced a permanent ParserError.
        self.assertEqual(command[-2], "-Command")
        self.assertIn(
            "-LiteralPath '" + self._CHROME_PATH + "'",
            command[-1],
        )
        self.assertNotIn("shell", command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_windows_powershell_signature_rejects_injection_characters(self):
        """Single quotes in the path must be escaped, not used to break out."""
        from native_host import native_host

        evil_path = r"C:\Program Files\evil'; Remove-Item -Recurse C:\ ; 'x.exe"
        with (
            patch.object(native_host.os, "name", "nt"),
            patch.object(
                native_host,
                "_get_windows_powershell_path",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            patch.object(native_host.subprocess, "run") as run,
        ):
            native_host._get_windows_powershell_signature(evil_path)
        argv = run.call_args.args[0]
        script = argv[argv.index("-Command") + 1]
        # Every single quote in the path must be doubled (PowerShell escaping),
        # so the payload stays inside the quoted literal and cannot terminate
        # the -LiteralPath string early.
        escaped = script.replace("''", "\x00")
        self.assertIn(
            "-LiteralPath 'C:\\Program Files\\evil\x00; Remove-Item -Recurse C:\\ ; \x00x.exe'",
            escaped,
        )

    def test_windows_authenticode_win32_success(self):
        from native_host import native_host

        with (
            patch.object(native_host.os, "name", "nt"),
            patch.object(
                native_host,
                "_get_windows_win32_signature",
                return_value=("Valid", "Google LLC; DigiCert Trusted Root G4"),
            ),
            patch.object(native_host, "_get_windows_powershell_signature") as ps_mock,
        ):
            sig = native_host._get_windows_authenticode_signature(self._CHROME_PATH)
            self.assertEqual(sig, ("Valid", "Google LLC; DigiCert Trusted Root G4"))
            ps_mock.assert_not_called()

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
            patch.object(
                native_host, "_load_allowed_manifest_origins", return_value={self.VALID_ID}
            ),
            patch("sys.argv", ["native_host.py", f"chrome-extension://{self.VALID_ID}/"]),
        ):
            native_host.main()
        return sent

    def test_browser_wrapper_chain_allows_regular_ping(self):
        sent = self._run_main_request("ping", ["cmd.exe", "chrome.exe"])
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
            with (
                self.subTest(action=action),
                patch.object(native_host, "_token_action_allowed") as token_budget,
                patch.object(native_host, "is_backend_healthy_once") as health_check,
            ):
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
        self.assertIn(
            "%*",
            content,
            "host_launcher.cmd.template must pass %* to forward browser origin arguments",
        )

    def test_native_host_cmd_forwards_arguments(self):
        cmd_file = Path(__file__).parent.parent / "native_host" / "native_host.cmd"
        if cmd_file.exists():
            content = cmd_file.read_text(encoding="utf-8")
            self.assertIn(
                "%*", content, "native_host.cmd must pass %* to forward browser origin arguments"
            )

    def test_read_only_windows_validator_checks_generated_manifest(self):
        validator = (
            Path(__file__).parent.parent / "native_host" / "validate_native_host_windows.ps1"
        )
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
        for manifest_path in (
            r".\native_host\com.mistral_nex_stocks.host.json.template",
            str(template),
        ):
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


class NativeHostInstallerSafetyTestCase(unittest.TestCase):
    """Ensure installer previews cannot alter native-host artifacts."""

    VALID_ID = "abcdefghijklmnopqrstuvwxyz123456"

    def test_installer_whatif_preserves_generated_files_and_uninstaller(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is required to execute the Windows installer")

        root = Path(__file__).resolve().parents[1]
        source_dir = root / "native_host"
        required_sources = (
            "install_host_windows.ps1",
            "host_launcher.cmd.template",
            "native_host.py",
            "start_backend.py",
            "com.mistral_nex_stocks.host.json.template",
            "uninstall_host_windows.ps1",
        )
        sentinels = {
            "native_host.cmd": "launcher sentinel\n",
            "com.mistral_nex_stocks.host.json": "manifest sentinel\n",
            "uninstall_host_windows.ps1": "uninstaller sentinel\n",
        }

        with tempfile.TemporaryDirectory(prefix="mns-native-host-whatif-") as temp_dir:
            temp_root = Path(temp_dir) / "project"
            temp_native_dir = temp_root / "native_host"
            temp_native_dir.mkdir(parents=True)
            for filename in required_sources:
                shutil.copy2(source_dir / filename, temp_native_dir / filename)
            for filename, content in sentinels.items():
                (temp_native_dir / filename).write_text(content, encoding="utf-8")

            # The installer only needs an existing file at this point; -WhatIf
            # must return before it attempts to launch or modify anything.
            fake_python = temp_root / "python.exe"
            fake_python.write_bytes(b"")
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(temp_native_dir / "install_host_windows.ps1"),
                    "-ExtensionIds",
                    self.VALID_ID,
                    "-PythonPath",
                    str(fake_python),
                    "-WhatIf",
                ],
                cwd=temp_root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            output = f"{result.stdout}\n{result.stderr}"
            self.assertEqual(result.returncode, 0, output)
            for filename, expected in sentinels.items():
                with self.subTest(filename=filename):
                    self.assertEqual(
                        (temp_native_dir / filename).read_text(encoding="utf-8"), expected
                    )


class NativeHostInstallerAclSafetyTestCase(unittest.TestCase):
    """Exercise the installer ACL detector without touching an ACL or registry."""

    def test_local_machine_guard_rejects_authenticated_users_modify_acl(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is required to execute the Windows ACL detector")

        script = r"""
$installer = Get-Content -LiteralPath 'native_host/install_host_windows.ps1' -Raw -Encoding UTF8
$start = $installer.IndexOf('function Test-DirectoryUserWritable')
$end = $installer.IndexOf('function Resolve-PythonPath', $start)
if ($start -lt 0 -or $end -lt 0) { throw 'Installer function boundaries not found' }
Invoke-Expression $installer.Substring($start, $end - $start)
$authenticatedUsers = New-Object System.Security.Principal.NTAccount('NT AUTHORITY\Authenticated Users')
$mockRule = [pscustomobject]@{
  IdentityReference = $authenticatedUsers
  AccessControlType = [System.Security.AccessControl.AccessControlType]::Allow
  FileSystemRights = [System.Security.AccessControl.FileSystemRights]::Modify
}
$mockAcl = [pscustomobject]@{ Access = @($mockRule) }
function Get-Acl { param([string]$Path) return $mockAcl }
$result = Test-DirectoryUserWritable -Dir 'in-memory-acl'
[pscustomobject]@{
  authenticated_users_sid = $authenticatedUsers.Translate([System.Security.Principal.SecurityIdentifier]).Value
  detector_reports_user_writable = $result
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output = f"{result.stdout}\n{result.stderr}"
        self.assertEqual(result.returncode, 0, output)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["authenticated_users_sid"], "S-1-5-11")
        self.assertTrue(payload["detector_reports_user_writable"])


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
                patch.object(native_host, "send_message", side_effect=lambda m: sent.append(m)),
                patch.object(native_host, "_token_action_allowed", return_value=True),
                patch.object(native_host, "is_backend_healthy_once", return_value=healthy),
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

    def test_shutdown_token_refused_when_used_marker_exists(self):
        """R1: If .mns_shutdown_token.used exists, native host rejects token request."""
        import json as _json
        import tempfile

        from native_host import native_host

        req = {"extensionId": self.VALID_ID, "action": "get_shutdown_token"}
        sent = []
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            token_file = tmp_path / ".mns_shutdown_token"
            used_marker = tmp_path / ".mns_shutdown_token.used"
            token_file.write_text(
                _json.dumps({"scheme": "fernet", "value": "enc"}), encoding="utf-8"
            )
            used_marker.write_text("1234567890", encoding="utf-8")
            with (
                patch.object(native_host, "read_message", side_effect=[req, None]),
                patch.object(native_host, "send_message", side_effect=lambda m: sent.append(m)),
                patch.object(native_host, "_token_action_allowed", return_value=True),
                patch.object(native_host, "is_backend_healthy_once", return_value=True),
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
                patch("config_store.APP_DATA_DIR", tmp_path),
            ):
                native_host.main()

        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("already been consumed", sent[0]["error"])

    def test_shutdown_token_handles_file_lock_failure(self):
        """R2: If token file lock fails during read, error is handled safely without reading partial data."""
        import json as _json
        import tempfile

        from native_host import native_host

        req = {"extensionId": self.VALID_ID, "action": "get_shutdown_token"}
        sent = []
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            token_file = tmp_path / ".mns_shutdown_token"
            token_file.write_text(
                _json.dumps({"scheme": "fernet", "value": "enc"}), encoding="utf-8"
            )
            with (
                patch.object(native_host, "read_message", side_effect=[req, None]),
                patch.object(native_host, "send_message", side_effect=lambda m: sent.append(m)),
                patch.object(native_host, "_token_action_allowed", return_value=True),
                patch.object(native_host, "is_backend_healthy_once", return_value=True),
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
                patch("config_store.APP_DATA_DIR", tmp_path),
                patch("os.name", "nt"),
                patch("time.sleep", return_value=None),
                patch("msvcrt.locking", side_effect=OSError("Resource locked")),
            ):
                native_host.main()

        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("Failed to read token file", sent[0]["error"])
