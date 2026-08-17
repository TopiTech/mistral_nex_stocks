"""Additional coverage for shutdown_manager.py.

Covers paths the original suite never exercised:
 - reloading a persisted (encrypted) token from disk
 - one-time legacy project-root token/marker migration
 - plaintext/legacy token-file regeneration
 - consume/validate edge cases and marker-persist failures
 - rotate_shutdown_token failure and restore paths

A fixed MNS_MASTER_KEY makes Fernet protect/unprotect deterministic so the
encrypted on-disk round-trip is real, not just in-memory.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shutdown_manager import ShutdownTokenManager

TEST_MASTER_KEY = "Ij2VbZwpP-Du-IHWL5VUPKL8BHUXUbddJY7JNj4xJ6g="


class ShutdownManagerExtraTests(unittest.TestCase):
    def setUp(self):
        self._old_key = os.environ.get("MNS_MASTER_KEY")
        os.environ["MNS_MASTER_KEY"] = TEST_MASTER_KEY
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.mgr = ShutdownTokenManager()
        self.mgr.token_file = self.tmp_path / ".mns_shutdown_token"
        self.mgr.used_marker = self.tmp_path / ".mns_shutdown_token.used"
        self.mgr.runtime_state_dir = self.tmp_path
        # Point legacy paths inside the temp dir so migration tests are hermetic.
        self.mgr._legacy_token_file = self.tmp_path / "legacy.mns_shutdown_token"
        self.mgr._legacy_used_marker = self.tmp_path / "legacy.mns_shutdown_token.used"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("MNS_MASTER_KEY", None)
        else:
            os.environ["MNS_MASTER_KEY"] = self._old_key
        self.tmp.cleanup()

    def _new_manager_same_files(self):
        mgr2 = ShutdownTokenManager()
        mgr2.token_file = self.mgr.token_file
        mgr2.used_marker = self.mgr.used_marker
        mgr2.runtime_state_dir = self.tmp_path
        mgr2._legacy_token_file = self.mgr._legacy_token_file
        mgr2._legacy_used_marker = self.mgr._legacy_used_marker
        return mgr2

    def test_reloads_persisted_token_from_disk(self):
        """A fresh manager must load the previously persisted encrypted token."""
        token1 = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(self.mgr.token_file.exists(), "token file must be persisted")

        mgr2 = self._new_manager_same_files()
        token2 = mgr2.get_or_create_shutdown_token()
        self.assertEqual(token1, token2)
        self.assertTrue(mgr2.validate_shutdown_token(token1))

    def test_reload_failure_regenerates_secure_token(self):
        """Unreadable/corrupt persisted token must fall back to regeneration."""
        # Corrupt JSON that unprotect_data cannot decrypt.
        self.mgr.token_file.write_text('{"scheme": "fernet", "value": "garbage"}', encoding="utf-8")
        token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token)
        self.assertNotEqual(token, "")

    def test_plaintext_token_file_regenerates_with_warning(self):
        """A legacy plaintext token file is ignored and replaced by a secure one."""
        self.mgr.token_file.write_text("legacy-plaintext-token", encoding="utf-8")
        with patch.object(self.mgr.logger, "warning") as mock_warn:
            token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token)
        self.assertNotEqual(token, "legacy-plaintext-token")
        mock_warn.assert_called()

    def test_migrates_legacy_token_file(self):
        """One-time migration moves a legacy project-root token into the runtime dir."""
        legacy_token = "legacy-token-content"
        self.mgr._legacy_token_file.write_text(legacy_token, encoding="utf-8")
        self.mgr._legacy_used_marker.write_text("1234", encoding="utf-8")

        token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token)
        # Runtime files now exist; legacy files were moved away.
        self.assertTrue(self.mgr.token_file.exists())
        self.assertTrue(self.mgr.used_marker.exists())
        self.assertFalse(self.mgr._legacy_token_file.exists())
        self.assertFalse(self.mgr._legacy_used_marker.exists())

    def test_migration_replace_oserror_swallowed(self):
        """OSError from the legacy token replace must be swallowed."""
        self.mgr._legacy_token_file.write_text("legacy", encoding="utf-8")
        real_replace = Path.replace

        def fail_legacy_replace(self, target):
            if str(self).endswith("legacy.mns_shutdown_token"):
                raise OSError("denied")
            return real_replace(self, target)

        with patch.object(Path, "replace", fail_legacy_replace):
            token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token)

    def test_reload_read_oserror_is_ignored(self):
        """OSError while reading a persisted token must fall back to generation."""
        token1 = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(self.mgr.token_file.exists())
        mgr2 = self._new_manager_same_files()
        with patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            token2 = mgr2.get_or_create_shutdown_token()
        self.assertTrue(token2)
        self.assertNotEqual(token1, token2)

    def test_token_write_failure_is_logged(self):
        """A failed token-file write must log an error but still return the token."""
        with patch(
            "shutdown_manager._write_atomic_restricted", side_effect=OSError("disk full")
        ), patch.object(self.mgr.logger, "error") as mock_error:
            token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token)
        mock_error.assert_called()
        self.assertFalse(self.mgr.token_file.exists())

    def test_consume_without_token_returns_false(self):
        """Consuming before any token exists returns False with a warning."""
        with patch.object(self.mgr.logger, "warning") as mock_warn:
            self.assertFalse(self.mgr.consume_shutdown_token("anything"))
        mock_warn.assert_called()

    def test_consume_non_string_token_returns_false(self):
        token = self.mgr.get_or_create_shutdown_token()
        self.assertFalse(self.mgr.consume_shutdown_token(None))  # type: ignore[arg-type]
        self.assertFalse(self.mgr.consume_shutdown_token(""))  # type: ignore[arg-type]
        self.assertTrue(self.mgr.consume_shutdown_token(token))

    def test_consume_persist_failure_returns_false(self):
        """If the used-marker cannot be persisted, consumption must fail closed."""
        token = self.mgr.get_or_create_shutdown_token()
        with patch(
            "shutdown_manager._write_atomic_restricted", side_effect=OSError("disk full")
        ), patch.object(self.mgr.logger, "error") as mock_error:
            self.assertFalse(self.mgr.consume_shutdown_token(token))
        mock_error.assert_called()
        self.assertFalse(self.mgr.shutdown_token_used)

    def test_persist_used_marker_oserror_path(self):
        with patch(
            "shutdown_manager._write_atomic_restricted", side_effect=OSError("disk full")
        ):
            self.assertFalse(self.mgr._persist_used_marker())

    def test_validate_returns_false_when_token_used_or_missing(self):
        self.assertFalse(self.mgr.validate_shutdown_token("x"))
        token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(self.mgr.validate_shutdown_token(token))
        self.assertFalse(self.mgr.validate_shutdown_token("wrong-token"))
        self.mgr.commit_shutdown_token()
        self.assertFalse(self.mgr.validate_shutdown_token(token))
        self.assertTrue(self.mgr.shutdown_token_used)

    def test_consume_already_used_returns_false(self):
        token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(self.mgr.consume_shutdown_token(token))
        with patch.object(self.mgr.logger, "warning") as mock_warn:
            self.assertFalse(self.mgr.consume_shutdown_token(token))
        mock_warn.assert_called()

    def test_consume_wrong_token_returns_false(self):
        token = self.mgr.get_or_create_shutdown_token()
        self.assertFalse(self.mgr.consume_shutdown_token("wrong"))
        self.assertFalse(self.mgr.shutdown_token_used)
        self.assertTrue(self.mgr.consume_shutdown_token(token))

    def test_cached_token_returned_when_marker_absent(self):
        """The in-memory token short-circuits when no used-marker exists (R1)."""
        token1 = self.mgr.get_or_create_shutdown_token()
        self.assertFalse(self.mgr.used_marker.exists())
        self.assertEqual(self.mgr.get_or_create_shutdown_token(), token1)

    def test_marker_unlink_oserror_is_tolerated(self):
        """Failure to remove a stale used-marker must not block regeneration."""
        self.mgr.used_marker.write_text("1234", encoding="utf-8")
        real_unlink = Path.unlink

        def fail_marker_unlink(self, *a, **kw):
            if self.name == ".mns_shutdown_token.used":
                raise OSError("locked")
            return real_unlink(self, *a, **kw)

        with patch.object(Path, "unlink", fail_marker_unlink):
            token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token)

    def test_rotate_mkdir_failure_raises(self):
        with patch.object(
            Path, "mkdir", side_effect=OSError("cannot create")
        ), self.assertRaises(RuntimeError):
            self.mgr.rotate_shutdown_token()

    def test_rotate_read_failure_raises(self):
        self.mgr.get_or_create_shutdown_token()
        with patch.object(
            Path, "read_bytes", side_effect=OSError("unreadable")
        ), self.assertRaises(RuntimeError):
            self.mgr.rotate_shutdown_token()

    def test_rotate_write_failure_restores_previous_state(self):
        """A failed rotation must restore the previous token/marker state."""
        token1 = self.mgr.get_or_create_shutdown_token()
        self.mgr.commit_shutdown_token()
        self.assertTrue(self.mgr.used_marker.exists())
        old_bytes = self.mgr.token_file.read_bytes()

        with patch(
            "shutdown_manager._write_atomic_restricted", side_effect=OSError("disk full")
        ), self.assertRaises(RuntimeError):
            self.mgr.rotate_shutdown_token()

        # Old token file and used marker are restored byte-for-byte.
        self.assertEqual(self.mgr.token_file.read_bytes(), old_bytes)
        self.assertTrue(self.mgr.used_marker.exists())
        # The restored used-marker means the consumed token is NOT revived:
        # a fresh manager regenerates instead of resurrecting the old one.
        mgr2 = self._new_manager_same_files()
        self.assertNotEqual(mgr2.get_or_create_shutdown_token(), token1)

    def test_rotate_write_failure_restores_when_no_previous_files(self):
        """Rotation with no pre-existing state leaves no files behind on failure."""
        with patch(
            "shutdown_manager._write_atomic_restricted", side_effect=OSError("disk full")
        ), self.assertRaises(RuntimeError):
            self.mgr.rotate_shutdown_token()
        self.assertFalse(self.mgr.token_file.exists())

    def test_fsync_failure_is_tolerated(self):
        """fsync OSError must not abort the atomic write."""
        with patch("os.fsync", side_effect=OSError("fsync failed")):
            token = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token)
        self.assertTrue(self.mgr.token_file.exists())


if __name__ == "__main__":
    unittest.main()
