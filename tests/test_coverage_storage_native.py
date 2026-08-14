"""
test_coverage_storage_native.py - Unit tests for utils/storage.py and native_host/native_host.py
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_state import app_state
from native_host import native_host
from utils import storage
from utils.storage import (
    UserStocksPersistError,
    _mark_user_stocks_load_failure,
    _migrate_legacy_user_stocks,
    save_user_stocks,
)


class StorageCoverageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.test_stocks_file = self.tmp_path / "user_stocks.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_mark_user_stocks_load_failure_and_backup(self):
        with (
            patch.object(storage, "USER_STOCKS_FILE", self.test_stocks_file),
            patch("config_store.USER_STOCKS_FILE", self.test_stocks_file),
            patch("config_store.APP_DATA_DIR", self.tmp_path),
        ):
            self.test_stocks_file.write_text("corrupt data!", encoding="utf-8")
            _mark_user_stocks_load_failure("Corrupted file test")
            self.assertTrue(app_state.market.user_stocks_load_error)

            # Verify backup was created
            baks = list(self.tmp_path.glob("user_stocks.bak.*"))
            self.assertTrue(len(baks) >= 1)

    def test_save_user_stocks_refuses_when_load_error_set(self):
        app_state.market.user_stocks_load_error = True
        with self.assertRaises(UserStocksPersistError):
            save_user_stocks()
        app_state.market.user_stocks_load_error = False

    def test_migrate_legacy_user_stocks(self):
        legacy_path = self.tmp_path / "legacy_user_stocks.json"
        legacy_path.write_text(json.dumps({"us": {"AAPL": "Apple"}}), encoding="utf-8")
        with (
            patch("utils.storage.LEGACY_USER_STOCKS_FILE", legacy_path),
            patch.object(storage, "USER_STOCKS_FILE", self.test_stocks_file),
            patch("config_store.USER_STOCKS_FILE", self.test_stocks_file),
        ):
            _migrate_legacy_user_stocks()
            self.assertTrue(self.test_stocks_file.exists())


class NativeHostCoverageTestCase(unittest.TestCase):
    def test_sanitize_log_message(self):
        msg = native_host._sanitize_log_message("Request with api_key='sk-1234567890' and token=secret")
        self.assertNotIn("sk-1234567890", msg)
        self.assertNotIn("secret", msg)
        self.assertIn("[REDACTED]", msg)

    def test_safe_env_helpers(self):
        with patch.dict(os.environ, {"TEST_INT": "100", "TEST_FLOAT": "2.5", "BAD_INT": "xyz"}):
            self.assertEqual(native_host._safe_int_env("TEST_INT", 10), 100)
            self.assertEqual(native_host._safe_float_env("TEST_FLOAT", 1.0), 2.5)
            self.assertEqual(native_host._safe_int_env("BAD_INT", 10), 10)
            self.assertEqual(native_host._safe_int_env("TEST_INT", 10, min_value=200), 200)

    def test_token_action_rate_limiter(self):
        with native_host._rate_limit_lock:
            native_host._token_action_timestamps.clear()
        # First 3 should pass
        for _ in range(3):
            self.assertTrue(native_host._token_action_allowed())
        # 4th should fail
        self.assertFalse(native_host._token_action_allowed())

    def test_read_and_send_message_io(self):
        import io
        import struct

        msg = {"action": "ping"}
        raw_msg = json.dumps(msg).encode("utf-8")
        header = struct.pack("@I", len(raw_msg))

        input_stream = io.BytesIO(header + raw_msg)
        output_stream = io.BytesIO()

        with (
            patch.object(native_host, "RAW_STDIN", input_stream),
            patch.object(native_host, "RAW_STDOUT", output_stream),
        ):
            received = native_host.read_message()
            self.assertEqual(received, msg)

            native_host.send_message({"ok": True, "pong": True})
            out_bytes = output_stream.getvalue()
            self.assertTrue(len(out_bytes) > 4)
            resp_len = struct.unpack("@I", out_bytes[:4])[0]
            resp_data = json.loads(out_bytes[4 : 4 + resp_len].decode("utf-8"))
            self.assertTrue(resp_data.get("ok"))


if __name__ == "__main__":
    unittest.main()
