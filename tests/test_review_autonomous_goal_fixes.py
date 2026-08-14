import io
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from native_host import native_host, start_backend


class NativeHostReadMessageRegressionTestCase(unittest.TestCase):
    """Regression tests for read_message typing and error handling in native_host.py."""

    def test_read_message_valid_bytes(self):
        payload_dict = {"action": "ping", "extensionId": "a" * 32}
        payload_bytes = json.dumps(payload_dict).encode("utf-8")
        header = struct.pack("<I", len(payload_bytes))
        stream = io.BytesIO(header + payload_bytes)

        with patch.object(native_host, "RAW_STDIN", stream):
            result = native_host.read_message()
            self.assertEqual(result, payload_dict)

    def test_read_message_json_decode_error_bytes_skip_frame(self):
        """Malformed JSON with bytes payload must return SKIP_FRAME and not raise UnboundLocalError."""
        bad_json = b"{not: a valid json}"
        header = struct.pack("<I", len(bad_json))
        stream = io.BytesIO(header + bad_json)

        with patch.object(native_host, "RAW_STDIN", stream):
            with self.assertLogs("native_host.native_host", level="ERROR") as cm:
                result = native_host.read_message()
                self.assertIs(result, native_host.SKIP_FRAME)
                self.assertTrue(any("payload_len=" in msg for msg in cm.output))

    def test_read_message_json_decode_error_str_header_skip_frame(self):
        """Malformed JSON with text mock stream must return SKIP_FRAME without type issues."""
        bad_json = "{not: valid json}"
        bad_json_bytes = bad_json.encode("utf-8")
        header_bytes = struct.pack("<I", len(bad_json_bytes))
        # Mocking header as str to exercise the str branch
        mock_stdin = MagicMock()
        mock_stdin.read.side_effect = [
            header_bytes.decode("latin1"),  # string header
            bad_json,  # string payload
        ]

        with patch.object(native_host, "RAW_STDIN", mock_stdin):
            with self.assertLogs("native_host.native_host", level="ERROR") as cm:
                result = native_host.read_message()
                self.assertIs(result, native_host.SKIP_FRAME)
                self.assertTrue(any("payload_len=" in msg for msg in cm.output))

    def test_read_message_clean_eof(self):
        stream = io.BytesIO(b"")
        with patch.object(native_host, "RAW_STDIN", stream):
            result = native_host.read_message()
            self.assertIsNone(result)

    def test_read_message_incomplete_header_fatal(self):
        stream = io.BytesIO(b"\x01\x02")
        with patch.object(native_host, "RAW_STDIN", stream):
            result = native_host.read_message()
            self.assertIs(result, native_host.FATAL_FRAME)

    def test_read_message_incomplete_payload_fatal(self):
        header = struct.pack("<I", 100)
        stream = io.BytesIO(header + b"short")
        with patch.object(native_host, "RAW_STDIN", stream):
            result = native_host.read_message()
            self.assertIs(result, native_host.FATAL_FRAME)


class FrontendSettingsConsistencyTestCase(unittest.TestCase):
    """Verify settings.js and frontend consistency."""

    def test_settings_js_uses_api_fetch_for_credentials(self):
        settings_js_path = Path(__file__).parent.parent / "static" / "js" / "settings.js"
        content = settings_js_path.read_text(encoding="utf-8")

        # Must not contain bare fetch("/api/credentials")
        self.assertNotIn(
            'fetch("/api/credentials")',
            content,
            "settings.js should not call bare fetch('/api/credentials')",
        )
        self.assertIn(
            'apiFetch("/api/credentials"',
            content,
            "settings.js must call apiFetch('/api/credentials'...",
        )


class StartBackendDeduplicationTestCase(unittest.TestCase):
    """Verify start_backend port deduplication."""

    def test_start_calls_get_backend_port_cleanly(self):
        with patch.object(start_backend, "get_backend_port", wraps=start_backend.get_backend_port) as mock_get_port:
            with patch.object(start_backend, "is_port_in_use", return_value=True):
                with patch.object(start_backend, "is_backend_healthy_once", return_value=True):
                    res = start_backend._start()
                    self.assertTrue(res.get("ok"))
                    self.assertEqual(mock_get_port.call_count, 1)


if __name__ == "__main__":
    unittest.main()
