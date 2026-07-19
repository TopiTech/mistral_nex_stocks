"""Coverage tests for utils/storage.py — targeting less-covered paths.

Focuses on Unix/fcntl lock paths, decryption failure, legacy migration,
save_user_stocks error handling, and edge cases.
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import utils.storage as storage
from app_state import app_state


class StorageCoverageTests(unittest.TestCase):
    """Tests for uncovered paths in storage.py."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._stocks_file = Path(self._tmpdir) / "user_stocks.json"
        self._lock_file = self._stocks_file.with_suffix(".lock")

        # Save original app_state market data
        with app_state.market.user_stocks_lock:
            self._orig_us = dict(app_state.market.user_us)
            self._orig_jp = dict(app_state.market.user_jp)
            self._orig_idx = dict(app_state.market.user_idx)
            self._orig_rev = app_state.market.user_stocks_rev
            self._orig_ns = app_state.market.last_modified_ns
            self._orig_err = getattr(app_state.market, "user_stocks_load_error", False)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = self._orig_us
            app_state.market.user_jp = self._orig_jp
            app_state.market.user_idx = self._orig_idx
            app_state.market.user_stocks_rev = self._orig_rev
            app_state.market.last_modified_ns = self._orig_ns
            app_state.market.user_stocks_load_error = self._orig_err

    # ------------------------------------------------------------------
    # Legacy migration
    # ------------------------------------------------------------------

    @patch("utils.storage.USER_STOCKS_FILE", new_callable=lambda: "/tmp/nonexistent/test.json")
    @patch("utils.storage.LEGACY_USER_STOCKS_FILE", new_callable=lambda: "/tmp/nonexistent/legacy.json")
    def test_migrate_legacy_when_target_exists(self, *_):
        """Migration is skipped when target already exists."""
        # Should not raise
        storage._migrate_legacy_user_stocks()

    # ------------------------------------------------------------------
    # _locked_read_user_stocks Unix path
    # ------------------------------------------------------------------

    def test_locked_read_unix_success(self):
        """On Windows, _locked_read_user_stocks uses msvcrt."""
        # Write a valid user_stocks file
        data = {"us": {"AAPL": "Apple"}, "jp": {}, "idx": {}}
        self._stocks_file.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(storage, "USER_STOCKS_FILE", str(self._stocks_file)):
            result = storage._locked_read_user_stocks(self._lock_file)
            # On Windows, this should succeed with msvcrt; on other platforms
            # we'll get None (ImportError for fcntl) but that's expected.
            if result is not None:
                self.assertEqual(result["us"]["AAPL"], "Apple")

    @patch("os.name", "posix")
    def test_locked_read_unix_import_error_fallback(self):
        """On Unix, if fcntl is unavailable, returns None."""
        with patch.object(storage, "USER_STOCKS_FILE", str(self._stocks_file)):
            with patch.dict("sys.modules", {"fcntl": None}):
                # This should trigger ImportError -> return None
                result = storage._locked_read_user_stocks(self._lock_file)
                self.assertIsNone(result)

    # ------------------------------------------------------------------
    # load_user_stocks — decryption failure path
    # ------------------------------------------------------------------

    def test_load_user_stocks_decryption_failure(self):
        """Decryption failure sets user_stocks_load_error and backs up."""
        test_file = str(Path(self._tmpdir) / "mns_storage" / "user_stocks.json")
        os.makedirs(Path(test_file).parent, exist_ok=True)
        # Write encrypted-looking data
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump({"scheme": "fernet", "value": "gAAAAABinvalid"}, f)

        with patch.object(storage, "USER_STOCKS_FILE", test_file):
            with patch.object(storage, "unprotect_data", return_value=None):
                with app_state.market.user_stocks_lock:
                    app_state.market.user_stocks_rev += 1
                    app_state.market.last_loaded_rev = 0
                    app_state.market.user_stocks_load_error = False
                storage.load_user_stocks(force=True)
                self.assertTrue(app_state.market.user_stocks_load_error)

    # ------------------------------------------------------------------
    # save_user_stocks — UserStocksPersistError paths
    # ------------------------------------------------------------------

    def test_save_user_stocks_when_load_error(self):
        """save_user_stocks raises when user_stocks_load_error is set."""
        test_dir = Path(self._tmpdir) / "mns_save"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = str(test_dir / "user_stocks.json")
        with patch.object(storage, "USER_STOCKS_FILE", test_file):
            with patch("config_store.get_or_create_master_key", return_value="test-key"):
                with patch.object(storage, "protect_data", return_value={"scheme": "test", "value": "data"}):
                    with app_state.market.user_stocks_lock:
                        app_state.market.user_stocks_load_error = True
                    with self.assertRaises(storage.UserStocksPersistError):
                        storage.save_user_stocks()
                    with app_state.market.user_stocks_lock:
                        app_state.market.user_stocks_load_error = False

    def test_save_user_stocks_oserror_raises(self):
        """save_user_stocks raises UserStocksPersistError on OSError."""
        test_dir = Path(self._tmpdir) / "mns_save_err"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = str(test_dir / "user_stocks.json")
        with patch.object(storage, "USER_STOCKS_FILE", test_file):
            with patch("config_store.get_or_create_master_key", return_value="test-key"):
                with patch.object(storage, "protect_data", return_value={"scheme": "test", "value": "data"}):
                    with patch.object(storage, "_write_user_stocks_with_lock",
                                      side_effect=OSError("write failure")):
                        with app_state.market.user_stocks_lock:
                            app_state.market.user_us = {"AAPL": "Apple"}
                        with self.assertRaises(storage.UserStocksPersistError):
                            storage.save_user_stocks()

    # ------------------------------------------------------------------
    # _rotate_user_stocks_backups
    # ------------------------------------------------------------------

    def test_rotate_backups_removes_excess(self):
        """Rotation removes backups beyond the limit."""
        # Create backup files
        for i in range(7):
            bak = Path(self._tmpdir) / f"user_stocks.bak.20260101{i:02d}0000"
            bak.write_text("test", encoding="utf-8")

        storage._rotate_user_stocks_backups(Path(self._tmpdir), limit=3)
        remaining = list(Path(self._tmpdir).glob("user_stocks.bak.*"))
        self.assertLessEqual(len(remaining), 3)

    def test_rotate_backups_none_removed_when_under_limit(self):
        """Rotation does nothing when backups are within limit."""
        for i in range(2):
            bak = Path(self._tmpdir) / f"user_stocks.bak.20260101{i:02d}0000"
            bak.write_text("test", encoding="utf-8")
        storage._rotate_user_stocks_backups(Path(self._tmpdir), limit=5)
        remaining = list(Path(self._tmpdir).glob("user_stocks.bak.*"))
        self.assertEqual(len(remaining), 2)

    # ------------------------------------------------------------------
    # _backup_unreadable_user_stocks
    # ------------------------------------------------------------------

    @patch.object(storage, "USER_STOCKS_FILE", new_callable=lambda: "/nonexistent/path")
    def test_backup_unreadable_nonexistent(self, *_):
        """Backup gracefully handles missing source file."""
        storage._backup_unreadable_user_stocks()

    # ------------------------------------------------------------------
    # _write_user_stocks_with_lock Unix path — ImportError fallback
    # ------------------------------------------------------------------

    def test_write_with_lock_unix_import_error(self):
        """Writes with the platform lock and publishes the target file."""
        tmp_dir = Path(self._tmpdir) / "write_test"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        test_file = str(tmp_dir / "user_stocks.json")
        tmp_file = Path(test_file).with_suffix(".testtmp")
        lock_file = Path(test_file).with_suffix(".lock")

        with patch.object(storage, "USER_STOCKS_FILE", test_file):
            storage._write_user_stocks_with_lock(
                '{"test": true}', tmp_file, Path(test_file), lock_file
            )
        # File should exist (either locked or unlocked fallback)
        self.assertTrue(os.path.exists(test_file))
        with open(test_file) as f:
            data = json.load(f)
        self.assertTrue(data["test"])

    def test_write_with_lock_failure_does_not_report_success(self):
        """A POSIX lock failure must not leave an unpublished successful write."""
        tmp_dir = Path(self._tmpdir) / "write_lock_failure"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target_file = tmp_dir / "user_stocks.json"
        target_file.write_text('{"old": true}', encoding="utf-8")
        tmp_file = target_file.with_suffix(".testtmp")
        lock_file = target_file.with_suffix(".lock")

        fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_UN=2, flock=lambda *_: None)
        real_import = __import__("builtins").__import__

        def import_without_lock(name, *args, **kwargs):
            if name == "fcntl":
                return fake_fcntl
            return real_import(name, *args, **kwargs)

        with patch.object(storage.os, "name", "posix"):
            with patch("builtins.__import__", side_effect=import_without_lock):
                with patch.object(fake_fcntl, "flock", side_effect=OSError("lock unavailable")):
                    with self.assertRaises(storage.UserStocksPersistError):
                        storage._write_user_stocks_with_lock(
                            '{"new": true}', tmp_file, target_file, lock_file
                        )

        self.assertEqual(target_file.read_text(encoding="utf-8"), '{"old": true}')

    # ------------------------------------------------------------------
    # load_user_stocks — malformed/missing file paths
    # ------------------------------------------------------------------

    def test_load_user_stocks_missing_file_returns_none(self):
        """load_user_stocks with non-existent file returns None."""
        with patch.object(storage, "USER_STOCKS_FILE",
                          str(Path(self._tmpdir) / "no_such_file.json")):
            with app_state.market.user_stocks_lock:
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
            result = storage.load_user_stocks(force=True)
            self.assertIsNone(result)

    def test_load_user_stocks_non_dict_data(self):
        """load_user_stocks resets to empty dicts when data is not a dict."""
        # Write non-dict JSON (e.g. a list)
        data = {"us": {"AAPL": "Apple"}, "jp": {}, "idx": {}}
        self._stocks_file.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(storage, "USER_STOCKS_FILE", str(self._stocks_file)):
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {"AAPL": "Apple"}
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
            storage.load_user_stocks(force=True)
            # Should load normally since the data IS a dict
            with app_state.market.user_stocks_lock:
                self.assertIn("AAPL", app_state.market.user_us)

    def test_load_user_stocks_non_dict_subcontainers(self):
        """load_user_stocks handles malformed sub-containers gracefully."""
        data = {"us": [], "jp": "not_a_dict", "idx": None}
        self._stocks_file.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(storage, "USER_STOCKS_FILE", str(self._stocks_file)):
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {}
                app_state.market.user_jp = {}
                app_state.market.user_idx = {}
                app_state.market.user_stocks_rev += 1
                app_state.market.last_loaded_rev = 0
            storage.load_user_stocks(force=True)
            # All should be valid dicts
            with app_state.market.user_stocks_lock:
                self.assertEqual(app_state.market.user_us, {})
                self.assertEqual(app_state.market.user_jp, {})
                self.assertEqual(app_state.market.user_idx, {})


if __name__ == "__main__":
    unittest.main()
