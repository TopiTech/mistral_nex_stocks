"""Coverage tests for core modules: config_store, credential_manager, crypto_utils, messaging, ai_state, execution_state."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_state
import config_store
import credential_manager
import crypto_utils
import execution_state
import messaging


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
        directory = Path(self.temp_dir.name)
        for i in range(7):
            p = directory / f"config.json.corrupt.20260101{i:02d}00.bak"
            p.write_text("{}", encoding="utf-8")

        config_store._rotate_corrupt_backups(directory, limit=5)

        remaining = sorted(directory.glob("config.json.corrupt.*.bak"))
        self.assertEqual(len(remaining), 5)

    def test_rotate_corrupt_backups_handles_remove_failure(self):
        directory = Path(self.temp_dir.name)
        for i in range(7):
            p = directory / f"config.json.corrupt.20260101{i:02d}00.bak"
            p.write_text("{}", encoding="utf-8")

        original_unlink = Path.unlink

        def _failing_unlink(self, *args, **kwargs):
            if "corrupt.2026010106" in str(self):
                raise OSError("Permission denied")
            return original_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", _failing_unlink):
            config_store._rotate_corrupt_backups(directory, limit=5)
            remaining = list(directory.glob("config.json.corrupt.*.bak"))
            self.assertLessEqual(len(remaining), 6)

    def test_load_config_corrupt_json(self):
        self.config_path.write_text("{ invalid json", encoding="utf-8")
        cfg = config_store.load_config()
        self.assertIn("mistral_model", cfg)
        backups = list(Path(self.temp_dir.name).glob("config.json.corrupt.*.bak"))
        self.assertEqual(len(backups), 1)

    def test_load_config_non_object_root_is_corrupt_and_preserved(self):
        """A valid JSON list must not be silently overwritten by defaults."""
        self.config_path.write_text(json.dumps([{"mistral_model": "lost"}]), encoding="utf-8")
        try:
            cfg = config_store.load_config()

            self.assertIn("mistral_model", cfg)
            self.assertTrue(config_store.is_config_corrupted())
            backups = list(Path(self.temp_dir.name).glob("config.json.corrupt.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                json.loads(self.config_path.read_text(encoding="utf-8")),
                [{"mistral_model": "lost"}],
            )
            with self.assertRaisesRegex(RuntimeError, "Refusing to save config"):
                config_store.save_config({"mistral_model": "replacement"})
        finally:
            config_store.clear_config_corruption_flag()
            config_store._CONFIG_CACHE["data"] = None
            config_store._CONFIG_CACHE["key"] = None

    def test_invalid_preference_types_use_safe_defaults(self):
        """Malformed scalar preferences must not crash callers using strings."""
        self.config_path.write_text(
            json.dumps({"mistral_model": {"name": "unexpected"}, "custom_ai_prompt": ["bad"]}),
            encoding="utf-8",
        )

        self.assertEqual(credential_manager.get_model_name(), "mistral-medium-3.5")
        self.assertEqual(credential_manager.get_custom_ai_prompt(), "")

    def test_load_config_merges_legacy_config_if_newer(self):
        legacy_path = Path(self.temp_dir.name) / "legacy_config.json"
        legacy_data = {
            "mistral_model": "mistral-medium-3-5",
            "custom_ai_prompt": "Legacy prompt",
            "api_credentials": {"some_key": "some_value"},
        }
        legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        runtime_data = {
            "mistral_model": "mistral-small-latest",
            "custom_ai_prompt": "Runtime prompt",
            "api_credentials": {},
            "flask_secret_key": {"scheme": "fernet", "value": "secret"},
        }
        self.config_path.write_text(json.dumps(runtime_data), encoding="utf-8")

        import time

        now = time.time()
        os.utime(self.config_path, (now - 10, now - 10))
        os.utime(legacy_path, (now, now))

        with (
            patch.object(config_store, "LEGACY_CONFIG_FILE", legacy_path),
            patch.object(config_store, "CONFIG_FILE", self.config_path),
            patch.object(config_store, "APP_DATA_DIR", self.config_path.parent),
        ):
            cfg = config_store.load_config()
            self.assertEqual(cfg["mistral_model"], "mistral-medium-3-5")
            self.assertEqual(cfg["custom_ai_prompt"], "Runtime prompt")
            self.assertEqual(cfg["flask_secret_key"]["value"], "secret")
            self.assertEqual(cfg["api_credentials"], {})
            self.assertFalse(legacy_path.exists())

    def test_load_config_merges_legacy_handles_corrupt_legacy_json(self):
        legacy_path = Path(self.temp_dir.name) / "legacy_config_corrupt.json"
        legacy_path.write_text("{ invalid json", encoding="utf-8")
        self.config_path.write_text(
            json.dumps({"mistral_model": "runtime-model"}), encoding="utf-8"
        )
        import time

        now = time.time()
        os.utime(self.config_path, (now - 10, now - 10))
        os.utime(legacy_path, (now, now))
        with (
            patch.object(config_store, "LEGACY_CONFIG_FILE", legacy_path),
            patch.object(config_store, "CONFIG_FILE", self.config_path),
            patch.object(config_store, "APP_DATA_DIR", self.config_path.parent),
        ):
            cfg = config_store.load_config()
            self.assertEqual(cfg["mistral_model"], "runtime-model")
            self.assertTrue(legacy_path.exists())

    def test_save_config_creates_backup(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps({"mistral_model": "old"}), encoding="utf-8")
        cfg = {"mistral_model": "new", "api_credentials": {}}
        config_store.save_config(cfg, create_backup=True)
        backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
        self.assertTrue(backup.exists())

    def test_save_config_creates_backup_with_secrets_stripped(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps({"mistral_model": "old"}), encoding="utf-8")
        cfg = {
            "mistral_model": "new",
            "api_credentials": {"mistral_api_key": "secret123"},
            "flask_secret_key": "should_not_appear",
        }
        config_store.save_config(cfg, create_backup=True)
        backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
        backup_data = json.loads(backup.read_text(encoding="utf-8"))
        self.assertNotIn("flask_secret_key", backup_data)
        self.assertEqual(backup_data["api_credentials"], {})


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
        credential_manager.set_custom_ai_prompt("Test prompt")
        cfg = config_store.load_config()
        self.assertEqual(cfg["custom_ai_prompt"], "Test prompt")

    def test_clear_api_credentials_with_keyring(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "mistral_model": "test",
                    "api_credentials": {
                        "mistral_api_key": {"scheme": "keyring", "value": ""},
                    },
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.object(crypto_utils, "KEYRING_AVAILABLE", True),
            patch.object(crypto_utils.keyring, "delete_password") as delete_mock,
        ):
            credential_manager.clear_api_credentials()
            delete_mock.assert_any_call("mistral_nex_stocks", "mistral_api_key")
            cfg = config_store.load_config()
            self.assertEqual(cfg["api_credentials"], {})

    def test_has_api_keys_when_not_set(self):
        self.assertFalse(credential_manager.has_mistral_api_key())
        self.assertFalse(credential_manager.has_langsearch_api_key())
        self.assertFalse(credential_manager.has_tavily_api_key())


class CryptoUtilsCoverageTestCase(unittest.TestCase):
    """Tests for crypto_utils.py low-coverage paths."""

    def test_get_or_create_master_key_creates_new(self):
        with (
            patch.object(config_store, "load_config", return_value={}),
            patch.object(config_store, "save_config") as save_mock,
        ):
            key = config_store.get_or_create_master_key()
            self.assertTrue(len(key) > 0)
            save_mock.assert_called_once()

    def test_protect_and_unprotect_data_roundtrip(self):
        with (
            patch.object(config_store, "load_config", return_value={}),
            patch.object(config_store, "save_config"),
        ):
            original = "My sensitive data!"
            protected = crypto_utils.protect_data(original, "test_key")
            self.assertEqual(protected["scheme"], "fernet")
            unprotected = crypto_utils.unprotect_data(protected, "test_key")
            self.assertEqual(unprotected, original)

    def test_unprotect_data_empty(self):
        self.assertEqual(crypto_utils.unprotect_data({}, "test_key"), "")
        self.assertEqual(crypto_utils.unprotect_data(None, "test_key"), "")

    def test_enforce_secure_permissions_non_windows(self):
        with (
            patch.object(crypto_utils, "_is_windows", return_value=False),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "chmod") as chmod_mock,
        ):
            crypto_utils.enforce_secure_permissions("/fake/path")
            chmod_mock.assert_called_once_with(0o600)

    def test_ephemeral_fallback_and_edge_cases(self):
        key_name = "test_ephemeral_key"
        encoded = crypto_utils._encode_secret("secret_val_123", key_name)
        decoded = crypto_utils._decode_secret(encoded, key_name)
        self.assertEqual(decoded, "secret_val_123")
        self.assertEqual(crypto_utils._encode_secret("", key_name), "")
        self.assertEqual(crypto_utils._decode_secret(None, key_name), "")
        self.assertEqual(crypto_utils._decode_secret("invalid-base64-payload!@", key_name), "")


class MessagingCoverageTestCase(unittest.TestCase):
    """Tests for messaging.py."""

    def test_message_announcer_listen_and_unlisten(self):
        announcer = messaging.MessageAnnouncer()
        self.assertEqual(announcer.listener_count(), 0)
        q = announcer.listen()
        self.assertEqual(announcer.listener_count(), 1)
        announcer.unlisten(q)
        self.assertEqual(announcer.listener_count(), 0)

    def test_message_announcer_announce_delivers_to_listeners(self):
        announcer = messaging.MessageAnnouncer()
        q1 = announcer.listen()
        q2 = announcer.listen()
        announcer.announce("test message")
        self.assertEqual(q1.get_nowait(), "test message")
        self.assertEqual(q2.get_nowait(), "test message")

    def test_message_announcer_max_listeners(self):
        with patch.object(messaging, "MAX_SSE_LISTENERS", 2):
            announcer = messaging.MessageAnnouncer()
            q1 = announcer.listen()
            q2 = announcer.listen()
            with self.assertRaises(RuntimeError):
                announcer.listen()
            announcer.unlisten(q1)
            announcer.unlisten(q2)


class AIStateCoverageTestCase(unittest.TestCase):
    """Tests for ai_state.py uncovered paths."""

    def test_init_and_add_history(self):
        st = ai_state.AIState()
        st.add_chat_history("k", [{"role": "user", "content": "hi"}])
        self.assertEqual(st.chat_history["k"], [{"role": "user", "content": "hi"}])

    def test_mark_mistral_429_with_valid_retry(self):
        st = ai_state.AIState()
        backoff = st.mark_mistral_429(retry_after_sec=10)
        self.assertGreater(backoff, 0)
        self.assertEqual(st.mistral_429_streak, 1)

    def test_reset_mistral_streak(self):
        st = ai_state.AIState()
        st.mistral_429_streak = 3
        st.mistral_next_allowed_ts = 123.0
        st.reset_mistral_streak()
        self.assertEqual(st.mistral_429_streak, 0)
        self.assertEqual(st.mistral_next_allowed_ts, 123.0)


class ExecutionStateCoverageTestCase(unittest.TestCase):
    """Tests for execution_state.py uncovered paths."""

    def test_shutdown_with_type_error_fallback(self):
        es = execution_state.ExecutionState()
        bad_exec = MagicMock()

        def _shutdown(*args, **kwargs):
            if kwargs.get("cancel_futures"):
                raise TypeError("boom")

        bad_exec.shutdown.side_effect = _shutdown
        es.executor = bad_exec
        es.news_executor = MagicMock()
        es.sync_refresh_executor = MagicMock()
        es.shutdown()
        self.assertEqual(bad_exec.shutdown.call_count, 2)


if __name__ == "__main__":
    unittest.main()
