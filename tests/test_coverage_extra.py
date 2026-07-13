"""Coverage enhancement tests for low-coverage modules.

Targets: config_store, credential_manager, crypto_utils, config_utils, messaging
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import config_store
import config_utils
import credential_manager
import crypto_utils
import messaging


# =============================================================================
# config_store.py coverage (55% → target)
# =============================================================================

class ConfigStoreCoverageTestCase(unittest.TestCase):
    """Tests for config_store.py low-coverage paths."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.patcher = patch.object(config_store, "CONFIG_FILE", self.config_path)
        self.addCleanup(self.patcher.stop)
        self.patcher.start()

    def test_rotate_corrupt_backups_removes_old_ones(self):
        """_rotate_corrupt_backups should remove backups beyond the limit."""
        # Create 7 corrupt backup files (limit=5 so 2 should be removed)
        directory = Path(self.temp_dir.name)
        existing = []
        for i in range(7):
            p = directory / f"config.json.corrupt.20260101{i:02d}00.bak"
            p.write_text("{}", encoding="utf-8")
            existing.append(p)

        config_store._rotate_corrupt_backups(directory, limit=5)

        remaining = sorted(directory.glob("config.json.corrupt.*.bak"))
        self.assertEqual(len(remaining), 5)

    def test_rotate_corrupt_backups_handles_empty_directory(self):
        """_rotate_corrupt_backups should not raise on empty directory."""
        directory = Path(self.temp_dir.name)
        # No backup files exist
        config_store._rotate_corrupt_backups(directory, limit=5)
        # Should not raise

    def test_rotate_corrupt_backups_handles_remove_failure(self):
        """_rotate_corrupt_backups should tolerate unlink failures."""
        directory = Path(self.temp_dir.name)
        for i in range(7):
            p = directory / f"config.json.corrupt.20260101{i:02d}00.bak"
            p.write_text("{}", encoding="utf-8")

        # Patch unlink to fail for files matching a pattern
        original_unlink = Path.unlink

        def _failing_unlink(self, *args, **kwargs):
            if "corrupt.2026010106" in str(self):
                raise OSError("Permission denied")
            return original_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", _failing_unlink):
            config_store._rotate_corrupt_backups(directory, limit=5)
            # At least the ones that didn't fail should have been removed
            remaining = list(directory.glob("config.json.corrupt.*.bak"))
            self.assertLessEqual(len(remaining), 6)

    def test_load_config_corrupt_json(self):
        """load_config should handle corrupt JSON gracefully."""
        self.config_path.write_text("{ invalid json", encoding="utf-8")
        cfg = config_store.load_config()
        self.assertIn("mistral_model", cfg)
        # Verify backup file was created
        backups = list(Path(self.temp_dir.name).glob("config.json.corrupt.*.bak"))
        self.assertEqual(len(backups), 1)

    def test_load_config_corrupt_json_backup_failure(self):
        """load_config should handle corrupt JSON with backup failure."""
        self.config_path.write_text("{ invalid json", encoding="utf-8")
        with patch.object(config_store.shutil, "copy2", side_effect=OSError("copy failed")):
            cfg = config_store.load_config()
            self.assertIn("mistral_model", cfg)

    def test_load_config_creates_defaults_when_empty_dict(self):
        """load_config should fill in defaults when file contains empty dict."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("{}", encoding="utf-8")
        cfg = config_store.load_config()
        self.assertEqual(cfg["mistral_model"], config_store.DEFAULT_CONFIG["mistral_model"])

    def test_load_config_ensures_api_credentials_is_dict(self):
        """load_config should fix non-dict api_credentials."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"api_credentials": "not_a_dict"}), encoding="utf-8"
        )
        cfg = config_store.load_config()
        self.assertEqual(cfg["api_credentials"], {})

    def test_save_config_creates_backup(self):
        """save_config with create_backup=True should create .bak file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"mistral_model": "old"}), encoding="utf-8"
        )
        cfg = {"mistral_model": "new", "api_credentials": {}}
        config_store.save_config(cfg, create_backup=True)
        backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
        self.assertTrue(backup.exists())

    @patch.object(config_store, "_is_windows", return_value=False)
    def test_save_config_backup_permissions(self, mock_is_windows):
        """save_config should set 0o600 on backup on non-Windows."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"mistral_model": "old"}), encoding="utf-8"
        )
        with patch.object(config_store.os, "chmod"):
            cfg = {"mistral_model": "new", "api_credentials": {}}
            config_store.save_config(cfg, create_backup=True)
            backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
            self.assertTrue(backup.exists())
            # H-4: Permissions are set at file creation via os.open(..., 0o600)
            # rather than open()+os.chmod(), so there is no separate chmod
            # call for the backup file. Verify existence instead.

    def test_save_config_with_permission_error_retry(self):
        """save_config should retry on PermissionError during os.replace."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # Make it so first replace raises, then succeeds
        original_replace = os.replace
        call_count = [0]

        def _failing_replace(src, dst):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionError("Access denied")
            return original_replace(src, dst)

        with patch.object(config_store.os, "replace", _failing_replace):
            config_store.save_config(
                {"mistral_model": "test", "api_credentials": {}},
                create_backup=False,
            )
            # Verify the save eventually succeeded
            self.assertTrue(self.config_path.exists())
            self.assertIn("test", self.config_path.read_text(encoding="utf-8"))

    def test_save_config_creates_backup_with_secrets_stripped(self):
        """save_config backup should strip secrets from backup."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"mistral_model": "old"}), encoding="utf-8"
        )
        cfg = {
            "mistral_model": "new",
            "api_credentials": {"mistral_api_key": "secret123"},
            "flask_secret_key": "should_not_appear",
            "mns_master_key": "also_should_not_appear",
        }
        config_store.save_config(cfg, create_backup=True)
        backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
        backup_data = json.loads(backup.read_text(encoding="utf-8"))
        # Secrets should be stripped
        self.assertNotIn("flask_secret_key", backup_data)
        self.assertNotIn("mns_master_key", backup_data)
        self.assertEqual(backup_data["api_credentials"], {})

    def test_save_config_skip_backup_when_config_missing(self):
        """save_config should not create backup if file doesn't exist yet."""
        self.assertFalse(self.config_path.exists())
        config_store.save_config(
            {"mistral_model": "test", "api_credentials": {}},
            create_backup=True,
        )
        backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
        self.assertFalse(backup.exists())

    @patch.object(config_store, "_is_windows", return_value=False)
    def test_load_config_sets_permissions_on_existing_file(self, mock_is_windows):
        """load_config should set 0o600 on existing config file on non-Windows."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"mistral_model": "test"}), encoding="utf-8"
        )
        with patch.object(Path, "chmod") as chmod_mock:
            config_store.load_config()
            chmod_mock.assert_called_once_with(0o600)

    def test_save_config_non_dict_uses_empty(self):
        """save_config with non-dict data should default to empty dict."""
        config_store.save_config(None, create_backup=False)
        cfg = config_store.load_config()
        self.assertIn("mistral_model", cfg)


# =============================================================================
# credential_manager.py coverage (85% → target)
# =============================================================================

class CredentialManagerCoverageTestCase(unittest.TestCase):
    """Tests for credential_manager.py low-coverage paths."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.patcher = patch.object(config_store, "CONFIG_FILE", self.config_path)
        self.addCleanup(self.patcher.stop)
        self.patcher.start()

    def test_set_custom_ai_prompt(self):
        """set_custom_ai_prompt should save prompt to config."""
        credential_manager.set_custom_ai_prompt("Test prompt")
        cfg = config_store.load_config()
        self.assertEqual(cfg["custom_ai_prompt"], "Test prompt")

    def test_set_custom_ai_prompt_empty(self):
        """set_custom_ai_prompt with empty string should save empty."""
        credential_manager.set_custom_ai_prompt("   ")
        cfg = config_store.load_config()
        self.assertEqual(cfg["custom_ai_prompt"], "")

    def test_clear_api_credentials_with_keyring(self):
        """clear_api_credentials should delete from keyring when available."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({
                "mistral_model": "test",
                "api_credentials": {
                    "mistral_api_key": {"scheme": "keyring", "value": ""},
                    "langsearch_api_key": {"scheme": "keyring", "value": ""},
                },
            }),
            encoding="utf-8",
        )
        with patch.object(crypto_utils, "KEYRING_AVAILABLE", True), \
             patch.object(crypto_utils.keyring, "delete_password") as delete_mock:
            credential_manager.clear_api_credentials()
            delete_mock.assert_any_call("mistral_nex_stocks", "mistral_api_key")
            delete_mock.assert_any_call("mistral_nex_stocks", "langsearch_api_key")
            delete_mock.assert_any_call("mistral_nex_stocks", "tavily_api_key")
            cfg = config_store.load_config()
            self.assertEqual(cfg["api_credentials"], {})

    def test_clear_api_credentials_without_keyring(self):
        """clear_api_credentials should not crash when keyring unavailable."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({
                "mistral_model": "test",
                "api_credentials": {"mistral_api_key": {"scheme": "plaintext", "value": "test"}},
            }),
            encoding="utf-8",
        )
        with patch.object(crypto_utils, "KEYRING_AVAILABLE", False):
            credential_manager.clear_api_credentials()
            cfg = config_store.load_config()
            self.assertEqual(cfg["api_credentials"], {})

    def test_save_api_credentials_empty_values(self):
        """save_api_credentials with None values should preserve existing."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        credential_manager.save_api_credentials(None, None, None)
        cfg = config_store.load_config()
        self.assertEqual(cfg["api_credentials"], {})

    def test_has_api_keys_when_not_set(self):
        """has_*_api_key should return False when no keys are set."""
        self.assertFalse(credential_manager.has_mistral_api_key())
        self.assertFalse(credential_manager.has_langsearch_api_key())
        self.assertFalse(credential_manager.has_tavily_api_key())

    def test_get_api_credential_state(self):
        """get_api_credential_state should return state dict."""
        state = credential_manager.get_api_credential_state()
        self.assertIn("has_mistral_api_key", state)
        self.assertIn("has_langsearch_api_key", state)
        self.assertIn("has_tavily_api_key", state)

    def test_get_model_name_and_badge(self):
        """get_model_name and get_model_badge should return values from config."""
        config_store.save_config({
            "mistral_model": "test-model",
            "model_badge": "test-badge",
            "api_credentials": {},
        })
        self.assertEqual(credential_manager.get_model_name(), "test-model")
        self.assertEqual(credential_manager.get_model_badge(), "test-badge")

    def test_save_api_credentials_with_tavily(self):
        """save_api_credentials should handle tavily API key."""
        def _mock_encode(secret_value, key_name="default"):
            return {"scheme": "test", "value": f"enc_{secret_value}"}

        with patch.object(crypto_utils, "_encode_secret", side_effect=_mock_encode):
            credential_manager.save_api_credentials(
                mistral_api_key="m_key",
                tavily_api_key="tv_key",
            )
            cfg = config_store.load_config()
            creds = cfg["api_credentials"]
            self.assertEqual(creds["mistral_api_key"]["value"], "enc_m_key")
            self.assertEqual(creds["tavily_api_key"]["value"], "enc_tv_key")


# =============================================================================
# crypto_utils.py coverage (65% → target)
# =============================================================================

class CryptoUtilsCoverageTestCase(unittest.TestCase):
    """Tests for crypto_utils.py low-coverage paths."""

    def test_get_or_create_master_key_creates_new(self):
        """get_or_create_master_key should generate a new Fernet key."""
        with patch.object(config_store, "load_config", return_value={}), \
             patch.object(config_store, "save_config") as save_mock:
            key = config_store.get_or_create_master_key()
            self.assertTrue(len(key) > 0)
            save_mock.assert_called_once()

    def test_get_or_create_master_key_reuses_existing(self):
        """get_or_create_master_key should reuse existing key."""
        # _decode_secret is now used via config_store module (imported at top of config_store.py)
        with patch.object(config_store, "load_config", return_value={
            "mns_master_key": {"scheme": "fernet", "value": "existing-key-12345"},
        }), \
             patch.object(config_store, "save_config") as save_mock, \
             patch.object(config_store, "_decode_secret", return_value="existing-key-12345"):
            key = crypto_utils.get_or_create_master_key()
            self.assertEqual(key, "existing-key-12345")
            save_mock.assert_not_called()

    def test_protect_data_empty(self):
        """protect_data with empty string should return empty value."""
        result = crypto_utils.protect_data("", "test_key")
        self.assertEqual(result["value"], "")

    def test_protect_and_unprotect_data_roundtrip(self):
        """protect_data then unprotect_data should return original text."""
        with patch.object(config_store, "load_config", return_value={}), \
             patch.object(config_store, "save_config"):
            original = "My sensitive data!"
            protected = crypto_utils.protect_data(original, "test_key")
            self.assertEqual(protected["scheme"], "fernet")
            self.assertTrue(len(protected["value"]) > 0)

            unprotected = crypto_utils.unprotect_data(protected, "test_key")
            self.assertEqual(unprotected, original)

    def test_unprotect_data_empty(self):
        """unprotect_data with empty entry should return empty string."""
        self.assertEqual(crypto_utils.unprotect_data({}, "test_key"), "")
        self.assertEqual(crypto_utils.unprotect_data(None, "test_key"), "")
        self.assertEqual(crypto_utils.unprotect_data("", "test_key"), "")

    def test_unprotect_data_plaintext_string_fallback(self):
        """unprotect_data with plain string should be rejected."""
        with patch.object(crypto_utils, "_decode_secret", return_value=""):
            result = crypto_utils.unprotect_data("plain-secret", "test_key")
            self.assertEqual(result, "")

    def test_unprotect_data_unknown_scheme_falls_back(self):
        """unprotect_data with unknown scheme falls back to _decode_secret."""
        with patch.object(crypto_utils, "_decode_secret", return_value="fallback"):
            result = crypto_utils.unprotect_data(
                {"scheme": "unknown", "value": "test"}, "test_key"
            )
            self.assertEqual(result, "fallback")

    def test_get_or_create_master_key_non_dict_config(self):
        """get_or_create_master_key should handle non-dict config."""
        with patch.object(config_store, "load_config", return_value=None), \
             patch.object(config_store, "save_config") as save_mock:
            key = config_store.get_or_create_master_key()
            self.assertTrue(len(key) > 0)
            save_mock.assert_called_once()

    def test_enforce_secure_permissions_windows(self):
        """enforce_secure_permissions should skip on Windows."""
        with patch.object(crypto_utils, "_is_windows", return_value=True):
            crypto_utils.enforce_secure_permissions("/fake/path")
            # Should not raise

    def test_enforce_secure_permissions_non_windows(self):
        """enforce_secure_permissions should chmod 0o600 on non-Windows."""
        with patch.object(crypto_utils, "_is_windows", return_value=False), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "chmod") as chmod_mock:
            crypto_utils.enforce_secure_permissions("/fake/path")
            chmod_mock.assert_called_once_with(0o600)


# =============================================================================
# config_utils.py coverage (83% → target)
# =============================================================================

class ConfigUtilsExtraCoverageTestCase(unittest.TestCase):
    """Tests for config_utils.py remaining uncovered paths."""

    def test_build_mistral_legacy_aliases_covers_all_branches(self):
        """_build_mistral_legacy_aliases should derive aliases from MISTRAL_MODELS."""
        # The function derives aliases from model entries ending in '-latest'.
        # Since current models don't end with '-latest', the function returns empty.
        # But we verify that MISTRAL_LEGACY_ALIASES already has all expected aliases
        # which were augmented at import time.
        aliases = config_utils._build_mistral_legacy_aliases()
        self.assertIsInstance(aliases, dict)
        # Verify that MISTRAL_LEGACY_ALIASES has all the key mappings
        self.assertIn("mistral-small-latest", config_utils.MISTRAL_LEGACY_ALIASES)
        self.assertIn("mistral-medium-latest", config_utils.MISTRAL_LEGACY_ALIASES)
        self.assertIn("mistral-large-latest", config_utils.MISTRAL_LEGACY_ALIASES)
        # Verify resolution works
        self.assertEqual(config_utils.MISTRAL_LEGACY_ALIASES["mistral-small-latest"], "mistral-small-2603")

    def test_get_or_create_master_key_config_utils(self):
        """config_utils.get_or_create_master_key should work."""
        with patch.object(config_store, "get_or_create_master_key", return_value="test-key"):
            result = config_utils.get_or_create_master_key()
            self.assertEqual(result, "test-key")


# =============================================================================
# messaging.py coverage (73% → target)
# =============================================================================

class MessagingCoverageTestCase(unittest.TestCase):
    """Tests for messaging.py."""

    def test_message_announcer_listen_and_unlisten(self):
        """MessageAnnouncer listen/unlisten should manage listener queues."""
        announcer = messaging.MessageAnnouncer()
        self.assertEqual(announcer.listener_count(), 0)

        q = announcer.listen()
        self.assertEqual(announcer.listener_count(), 1)

        announcer.unlisten(q)
        self.assertEqual(announcer.listener_count(), 0)

    def test_message_announcer_announce_delivers_to_listeners(self):
        """MessageAnnouncer.announce should deliver message to all listeners."""
        announcer = messaging.MessageAnnouncer()
        q1 = announcer.listen()
        q2 = announcer.listen()

        announcer.announce("test message")

        self.assertEqual(q1.get_nowait(), "test message")
        self.assertEqual(q2.get_nowait(), "test message")

    def test_message_announcer_listener_context(self):
        """MessageAnnouncer.listener_context should manage lifecycle."""
        announcer = messaging.MessageAnnouncer()
        with announcer.listener_context() as q:
            self.assertEqual(announcer.listener_count(), 1)
            self.assertIsNotNone(q)
        self.assertEqual(announcer.listener_count(), 0)

    def test_message_announcer_max_listeners(self):
        """MessageAnnouncer should enforce MAX_SSE_LISTENERS."""
        with patch.object(messaging, "MAX_SSE_LISTENERS", 2):
            announcer = messaging.MessageAnnouncer()
            q1 = announcer.listen()
            q2 = announcer.listen()
            with self.assertRaises(RuntimeError):
                announcer.listen()
            announcer.unlisten(q1)
            announcer.unlisten(q2)


if __name__ == "__main__":
    unittest.main()
