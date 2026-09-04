"""Additional coverage for utils/storage.py.

Fills the gaps left by test_storage_coverage.py / test_user_stock_io.py /
test_storage_extra.py:
 - legacy plaintext migration success path
 - locked-read failure retry with unlocked fallback
 - encrypted-envelope decrypt success on load
 - Windows lock retry exhaustion and fail-closed behavior
 - full save -> reload round trip including rate sanitization
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app_state import app_state
from utils import storage


class NormalizeJpHoldingKeysTests(unittest.TestCase):
    def test_canonicalizes_bare_jp_ticker(self):
        out = storage._normalize_jp_holding_keys({"7203": {"shares": 1}})
        self.assertIn("7203.T", out)
        self.assertNotIn("7203", out)

    def test_keeps_both_when_ambiguous(self):
        with patch.object(storage.logger, "warning") as mock_warn:
            out = storage._normalize_jp_holding_keys(
                {"7203": {"shares": 1}, "7203.T": {"shares": 2}}
            )
        self.assertEqual(set(out), {"7203", "7203.T"})
        mock_warn.assert_called()

    def test_leaves_non_jp_keys_untouched(self):
        out = storage._normalize_jp_holding_keys({"AAPL": {"shares": 1}})
        self.assertEqual(out, {"AAPL": {"shares": 1}})


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrates_and_removes_legacy_plaintext(self):
        legacy = self.tmp_path / "legacy_user_stocks.json"
        legacy.write_text(json.dumps({"us": {"AAPL": {}}, "jp": {}, "idx": {}}), encoding="utf-8")
        target = self.tmp_path / "user_stocks.json"

        with (
            patch.object(storage, "LEGACY_USER_STOCKS_FILE", str(legacy)),
            patch.object(storage, "USER_STOCKS_FILE", str(target)),
            patch.object(storage.config_store, "APP_DATA_DIR", self.tmp_path),
            patch.object(
                storage.config_store,
                "get_or_create_master_key",
                return_value="Ij2VbZwpP-Du-IHWL5VUPKL8BHUXUbddJY7JNj4xJ6g=",
            ),
        ):
            storage._migrate_legacy_user_stocks()

        self.assertTrue(target.exists(), "migrated target must exist")
        self.assertFalse(legacy.exists(), "legacy plaintext must be removed")
        # Target holds a Fernet envelope, not plaintext.
        envelope = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(envelope["scheme"], "fernet")
        self.assertTrue(envelope["value"])

    def test_migration_failure_warns_and_cleans_tmp(self):
        legacy = self.tmp_path / "legacy_user_stocks.json"
        legacy.write_text("not json", encoding="utf-8")
        target = self.tmp_path / "user_stocks.json"

        with (
            patch.object(storage, "LEGACY_USER_STOCKS_FILE", str(legacy)),
            patch.object(storage, "USER_STOCKS_FILE", str(target)),
            patch.object(storage.config_store, "APP_DATA_DIR", self.tmp_path),
            patch.object(storage.logger, "warning") as mock_warn,
        ):
            storage._migrate_legacy_user_stocks()

        mock_warn.assert_called()
        self.assertFalse(target.exists())
        self.assertFalse(list(self.tmp_path.glob("*.tmp")))

    def test_load_migration_failure_blocks_destructive_save(self):
        """A failed legacy migration must not be treated as an empty portfolio."""
        legacy = self.tmp_path / "legacy_user_stocks.json"
        legacy.write_text("not json", encoding="utf-8")
        target = self.tmp_path / "user_stocks.json"
        original_us = {"KEEP": {"shares": 2.0, "avg_price": 100.0}}

        with (
            patch.object(storage, "LEGACY_USER_STOCKS_FILE", str(legacy)),
            patch.object(storage, "USER_STOCKS_FILE", str(target)),
            patch.object(storage.config_store, "APP_DATA_DIR", self.tmp_path),
        ):
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = original_us.copy()
                app_state.market.user_jp = {}
                app_state.market.user_idx = {}
                app_state.market.user_stocks_load_error = False

            storage.load_user_stocks(force=True)

            self.assertEqual(app_state.market.user_us, original_us)
            self.assertTrue(app_state.market.user_stocks_load_error)
            with self.assertRaises(storage.UserStocksPersistError):
                storage.save_user_stocks()
            self.assertFalse(target.exists())
            self.assertTrue(legacy.exists())


class LockedReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.stocks_file = self.tmp_path / "user_stocks.json"
        self.lock_file = self.stocks_file.with_suffix(".lock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_retry_then_unlocked_read_recovers(self):
        """Locked read fails twice; the unlocked plaintext fallback recovers."""
        self.stocks_file.write_text(
            json.dumps({"us": {"AAPL": {}}, "jp": {}, "idx": {}}), encoding="utf-8"
        )
        with (
            patch.object(storage, "USER_STOCKS_FILE", str(self.stocks_file)),
            patch.object(
                storage,
                "_locked_read_user_stocks",
                side_effect=[storage._USER_STOCKS_READ_FAILED] * 2,
            ),
            patch("time.sleep"),
        ):
            with app_state.market.user_stocks_lock:
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
                app_state.market.user_stocks_load_error = False
            storage.load_user_stocks(force=True)
            with app_state.market.user_stocks_lock:
                self.assertIn("AAPL", app_state.market.user_us)
                self.assertFalse(app_state.market.user_stocks_load_error)

    def test_retry_exhausted_marks_load_failure(self):
        self.stocks_file.write_text("placeholder", encoding="utf-8")
        with (
            patch.object(storage, "USER_STOCKS_FILE", str(self.stocks_file)),
            patch.object(
                storage,
                "_locked_read_user_stocks",
                side_effect=[storage._USER_STOCKS_READ_FAILED] * 2,
            ),
            patch("time.sleep"),
            patch.object(storage, "_mark_user_stocks_load_failure") as mock_mark,
        ):
            with app_state.market.user_stocks_lock:
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
            storage.load_user_stocks(force=True)
        mock_mark.assert_called_once()

    def test_decrypt_success_loads_envelope(self):
        """A valid encrypted envelope is decrypted and loaded."""
        self.stocks_file.write_text("placeholder", encoding="utf-8")
        envelope = {"scheme": "fernet", "value": "ciphertext"}
        with (
            patch.object(storage, "USER_STOCKS_FILE", str(self.stocks_file)),
            patch.object(storage, "_locked_read_user_stocks", return_value=envelope),
            patch.object(
                storage,
                "unprotect_data",
                return_value=json.dumps({"us": {"MSFT": {}}, "jp": {}, "idx": {}}),
            ),
        ):
            with app_state.market.user_stocks_lock:
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
                app_state.market.user_stocks_load_error = False
            storage.load_user_stocks(force=True)
            with app_state.market.user_stocks_lock:
                self.assertIn("MSFT", app_state.market.user_us)
                self.assertFalse(app_state.market.user_stocks_load_error)


class LoadFailureBackupTests(unittest.TestCase):
    def test_backup_failure_is_debug_logged(self):
        with (
            patch.object(storage, "_backup_unreadable_user_stocks", side_effect=OSError("denied")),
            patch.object(storage.logger, "debug") as mock_debug,
            patch.object(storage.logger, "error"),
        ):
            storage._mark_user_stocks_load_failure("test reason")
        self.assertTrue(app_state.market.user_stocks_load_error)
        mock_debug.assert_called()


class BackupUnreadableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backs_up_and_rotates(self):
        stocks_file = self.tmp_path / "user_stocks.json"
        stocks_file.write_text('{"us": {}}', encoding="utf-8")
        with (
            patch.object(storage, "USER_STOCKS_FILE", str(stocks_file)),
            patch.object(storage, "_rotate_user_stocks_backups") as mock_rotate,
        ):
            storage._backup_unreadable_user_stocks()
        backups = list(self.tmp_path.glob("user_stocks.bak.*"))
        self.assertEqual(len(backups), 1)
        mock_rotate.assert_called_once()

    def test_rotate_oserror_warns(self):
        for i in range(6):
            (self.tmp_path / f"user_stocks.bak.2026010{i}0000").write_text("x", encoding="utf-8")
        with (
            patch.object(Path, "stat", side_effect=OSError("stat failed")),
            patch.object(storage.logger, "warning") as mock_warn,
        ):
            storage._rotate_user_stocks_backups(self.tmp_path)
        mock_warn.assert_called()


class WriteWithLockWindowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.target = self.tmp_path / "user_stocks.json"
        self.tmp_file = self.tmp_path / "user_stocks.abc123.tmp"
        self.lock_file = self.tmp_path / "user_stocks.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_windows_lock_busy_exhausts_retries(self):
        """LK_NBLCK busy on every attempt raises UserStocksPersistError."""
        import msvcrt

        real_locking = msvcrt.locking

        def busy_locking(fd, mode, nbytes):
            if mode == msvcrt.LK_NBLCK:
                raise OSError("lock busy")
            return real_locking(fd, mode, nbytes)

        with (
            patch.object(storage.os, "name", "nt"),
            patch("time.sleep"),
            patch("msvcrt.locking", side_effect=busy_locking),
            patch.object(storage.logger, "error"),
        ):
            with self.assertRaises(storage.UserStocksPersistError):
                storage._write_user_stocks_with_lock(
                    '{"x": 1}', self.tmp_file, self.target, self.lock_file
                )
        self.assertFalse(self.target.exists())

    def test_windows_success_path_writes_and_promotes(self):
        import msvcrt

        real_locking = msvcrt.locking

        def tracking_locking(fd, mode, nbytes):
            if mode == msvcrt.LK_NBLCK:
                return None  # pretend we acquired the lock
            if mode == msvcrt.LK_UNLCK:
                return real_locking(fd, mode, nbytes)
            return None

        with (
            patch.object(storage.os, "name", "nt"),
            patch("msvcrt.locking", side_effect=tracking_locking),
        ):
            storage._write_user_stocks_with_lock(
                '{"x": 1}', self.tmp_file, self.target, self.lock_file
            )
        self.assertTrue(self.target.exists())
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"x": 1})
        self.assertFalse(self.tmp_file.exists())


class WriteWithLockPosixTests(unittest.TestCase):
    """The POSIX branch is never exercised on Windows CI; cover it with a fake fcntl."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.target = self.tmp_path / "user_stocks.json"
        self.tmp_file = self.tmp_path / "user_stocks.abc123.tmp"
        self.lock_file = self.tmp_path / "user_stocks.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_posix_success_path_writes_and_promotes(self):
        import builtins

        fake_fcntl = MagicMock()
        fake_fcntl.LOCK_EX = 1
        fake_fcntl.LOCK_UN = 2
        fake_fcntl.flock.return_value = None
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fcntl":
                return fake_fcntl
            return real_import(name, *args, **kwargs)

        with (
            patch.object(storage.os, "name", "posix"),
            patch("builtins.__import__", side_effect=fake_import),
        ):
            storage._write_user_stocks_with_lock(
                '{"x": 1}', self.tmp_file, self.target, self.lock_file
            )
        self.assertTrue(self.target.exists())
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"x": 1})
        self.assertFalse(self.tmp_file.exists())
        fake_fcntl.flock.assert_called()

    def test_posix_lock_unlock_oserror_tolerated(self):
        import builtins

        fake_fcntl = MagicMock()
        fake_fcntl.LOCK_EX = 1
        fake_fcntl.LOCK_UN = 2
        fake_fcntl.flock.side_effect = OSError("flock unavailable")
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fcntl":
                return fake_fcntl
            return real_import(name, *args, **kwargs)

        with (
            patch.object(storage.os, "name", "posix"),
            patch("builtins.__import__", side_effect=fake_import),
            patch.object(storage.logger, "error"),
        ):
            with self.assertRaises(storage.UserStocksPersistError):
                storage._write_user_stocks_with_lock(
                    '{"x": 1}', self.tmp_file, self.target, self.lock_file
                )
        self.assertFalse(self.target.exists())


class SaveRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.stocks_file = self.tmp_path / "user_stocks.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_then_reload_round_trip(self):
        with (
            patch.object(storage, "USER_STOCKS_FILE", str(self.stocks_file)),
            patch.object(
                storage.config_store,
                "get_or_create_master_key",
                return_value="Ij2VbZwpP-Du-IHWL5VUPKL8BHUXUbddJY7JNj4xJ6g=",
            ),
            patch("services.realtime_engine.realtime_market_engine.register_symbols"),
        ):
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {"AAPL": {"shares": 5}}
                app_state.market.user_jp = {}
                app_state.market.user_idx = {}
                app_state.market.user_stocks_load_error = False
                app_state.market.last_usdjpy_rate = 152.5
                app_state.market.last_usdjpy_rate_ts = 123.0
            storage.save_user_stocks()

            self.assertTrue(self.stocks_file.exists())
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {}
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
            storage.load_user_stocks(force=True)
            with app_state.market.user_stocks_lock:
                self.assertEqual(app_state.market.user_us, {"AAPL": {"shares": 5}})
                self.assertEqual(app_state.market.last_usdjpy_rate, 152.5)

    def test_save_sanitizes_non_finite_rate(self):
        with (
            patch.object(storage, "USER_STOCKS_FILE", str(self.stocks_file)),
            patch.object(
                storage.config_store,
                "get_or_create_master_key",
                return_value="Ij2VbZwpP-Du-IHWL5VUPKL8BHUXUbddJY7JNj4xJ6g=",
            ),
            patch("services.realtime_engine.realtime_market_engine.register_symbols"),
        ):
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {}
                app_state.market.user_jp = {}
                app_state.market.user_idx = {}
                app_state.market.user_stocks_load_error = False
                app_state.market.last_usdjpy_rate = float("inf")
                app_state.market.last_usdjpy_rate_ts = float("nan")
            storage.save_user_stocks()

            with app_state.market.user_stocks_lock:
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
            storage.load_user_stocks(force=True)
            with app_state.market.user_stocks_lock:
                self.assertEqual(app_state.market.last_usdjpy_rate, 150.00)
                self.assertEqual(app_state.market.last_usdjpy_rate_ts, 0.0)


if __name__ == "__main__":
    unittest.main()
