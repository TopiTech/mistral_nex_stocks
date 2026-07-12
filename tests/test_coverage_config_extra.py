"""Coverage tests for credential_manager, crypto_utils, config_store, config_utils, messaging, app_state filters."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import credential_manager
import crypto_utils
import config_store
import config_utils
import messaging
import app_state


class CryptoUtilsTestCase(unittest.TestCase):
    def test_encode_decode_roundtrip_keyring(self):
        entry = crypto_utils._encode_secret("super-secret-value", "test_key")
        self.assertIsInstance(entry, dict)
        self.assertIn("scheme", entry)
        decoded = crypto_utils._decode_secret(entry, "test_key")
        self.assertEqual(decoded, "super-secret-value")

    def test_encode_secret_empty(self):
        self.assertEqual(crypto_utils._encode_secret("", "k"), "")

    def test_decode_secret_none_and_legacy_plaintext(self):
        self.assertEqual(crypto_utils._decode_secret(None, "k"), "")
        self.assertEqual(crypto_utils._decode_secret("", "k"), "")
        self.assertEqual(crypto_utils._decode_secret("plaintext-legacy", "k"), "")
        self.assertEqual(crypto_utils._decode_secret(123, "k"), "")

    def test_decode_secret_unknown_scheme(self):
        self.assertEqual(crypto_utils._decode_secret({"scheme": "bogus", "value": "x"}, "k"), "")

    def test_decode_secret_bad_base64(self):
        self.assertEqual(crypto_utils._decode_secret({"scheme": "fernet", "value": "!!!notb64!!!", "key_name": "k"}, "k"), "")

    def test_decode_secret_plaintext_scheme_rejected(self):
        self.assertEqual(crypto_utils._decode_secret({"scheme": "plaintext", "value": "x"}, "k"), "")

    def test_protect_unprotect_fernet(self):
        protected = crypto_utils.protect_data("hello world", "general_data", config_store)
        self.assertEqual(protected["scheme"], "fernet")
        self.assertNotEqual(protected["value"], "hello world")
        self.assertEqual(crypto_utils.unprotect_data(protected, "general_data", config_store), "hello world")

    def test_protect_data_empty(self):
        protected = crypto_utils.protect_data("", "general_data", config_store)
        self.assertEqual(protected["value"], "")

    def test_unprotect_data_non_dict(self):
        self.assertEqual(crypto_utils.unprotect_data(None, "k", config_store), "")
        self.assertEqual(crypto_utils.unprotect_data("legacy", "k", config_store), "")

    def test_enforce_secure_permissions_non_windows(self):
        # On Windows this is a no-op; on POSIX it chmods. Just ensure no error.
        with patch.object(crypto_utils, "_is_windows", return_value=False):
            with patch("utils.storage.os") as mock_os:
                crypto_utils.enforce_secure_permissions("/tmp/x")
                mock_os.path.Path.return_value.exists.return_value = True

    def test_get_or_create_master_key_from_env(self):
        with patch.dict("os.environ", {"MNS_MASTER_KEY": "env-master-key-value"}, clear=False):
            self.assertEqual(crypto_utils.get_or_create_master_key(config_store), "env-master-key-value")


class ConfigStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = config_store._CONFIG_CACHE.copy()

    def tearDown(self):
        config_store._CONFIG_CACHE.clear()
        config_store._CONFIG_CACHE.update(self._saved)

    def test_load_config_returns_default_when_missing(self):
        with patch.object(config_store, "CONFIG_FILE") as mock_file:
            mock_file.exists.return_value = False
            with patch.object(config_store, "save_config") as mock_save:
                cfg = config_store.load_config()
                self.assertIn("mistral_model", cfg)
                mock_save.assert_called_once()

    def test_load_config_corrupt_json(self):
        with patch.object(config_store, "CONFIG_FILE") as mock_file:
            mock_file.exists.return_value = True
            mock_file.chmod.return_value = None
            with patch("builtins.open", side_effect=__import__("json").JSONDecodeError("e", "d", 0)):
                with patch("config_store.shutil") as mock_shutil:
                    cfg = config_store.load_config()
                    self.assertIn("mistral_model", cfg)
                    mock_shutil.copy2.assert_called_once()

    def test_rotate_corrupt_backups(self):
        cfg_dir = Path(__file__).parent
        # create 7 fake backups and ensure rotation keeps latest 5
        import glob
        created = []
        for i in range(7):
            p = cfg_dir / f"config.json.corrupt.{1000 + i}.bak"
            p.write_text("x")
            created.append(p)
        try:
            config_store._rotate_corrupt_backups(cfg_dir, limit=5)
            remaining = sorted(glob.glob(str(cfg_dir / "config.json.corrupt.*.bak")))
            self.assertLessEqual(len(remaining), 5)
        finally:
            for p in created:
                try:
                    p.unlink()
                except OSError:
                    pass

    def test_config_cache_invalidated_on_save(self):
        config_store._CONFIG_CACHE.clear()
        config_store.save_config({"mistral_model": "mistral-small-latest"})
        self.assertIsNone(config_store._CONFIG_CACHE["data"])


class ConfigUtilsTestCase(unittest.TestCase):
    def test_resolve_model_target_by_index_and_alias(self):
        self.assertEqual(config_utils.resolve_model_target("1")["name"], "mistral-small-2603")
        self.assertEqual(config_utils.resolve_model_target("mistral-small-latest")["name"], "mistral-small-2603")
        self.assertIsNone(config_utils.resolve_model_target("nonexistent-model"))

    def test_get_or_create_master_key_facade(self):
        with patch.dict("os.environ", {}, clear=False):
            key = config_utils.get_or_create_master_key()
            self.assertTrue(key)

    def test_build_mistral_legacy_aliases(self):
        derived = config_utils._build_mistral_legacy_aliases()
        self.assertIsInstance(derived, dict)


class CredentialManagerTestCase(unittest.TestCase):
    def test_env_key_precedence(self):
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "env-key-123"}, clear=False):
            self.assertEqual(credential_manager.get_mistral_api_key(), "env-key-123")

    def test_has_key_false(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(credential_manager, "_get_api_credentials_blob", return_value={}):
                self.assertFalse(credential_manager.has_mistral_api_key())

    def test_save_and_get_credentials(self):
        with patch.dict("os.environ", {}, clear=True):
            credential_manager.save_api_credentials(mistral_api_key="mkey", langsearch_api_key="lkey", tavily_api_key="tkey")
            try:
                self.assertTrue(credential_manager.has_mistral_api_key())
                self.assertEqual(credential_manager.get_mistral_api_key(), "mkey")
                self.assertTrue(credential_manager.has_tavily_api_key())
                # empty/whitespace value leaves the existing key intact
                credential_manager.save_api_credentials(mistral_api_key="   ")
                self.assertEqual(credential_manager.get_mistral_api_key(), "mkey")
                # saving a new value updates it
                credential_manager.save_api_credentials(mistral_api_key="mkey2")
                self.assertEqual(credential_manager.get_mistral_api_key(), "mkey2")
            finally:
                credential_manager.clear_api_credentials()

    def test_custom_ai_prompt(self):
        credential_manager.set_custom_ai_prompt("  my prompt  ")
        self.assertEqual(credential_manager.get_custom_ai_prompt(), "my prompt")
        credential_manager.set_custom_ai_prompt(None)
        self.assertEqual(credential_manager.get_custom_ai_prompt(), "")

    def test_get_model_name_and_badge(self):
        self.assertIsInstance(credential_manager.get_model_name(), str)
        self.assertIsInstance(credential_manager.get_model_badge(), str)

    def test_flask_secret_key_generated(self):
        with patch.dict("os.environ", {}, clear=True):
            key = credential_manager.get_or_create_flask_secret_key()
            self.assertGreaterEqual(len(key), 32)
            # second call returns same stored key
            self.assertEqual(credential_manager.get_or_create_flask_secret_key(), key)

    def test_extension_api_token_generated(self):
        with patch.dict("os.environ", {}, clear=True):
            token = credential_manager.get_or_create_extension_api_token()
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(credential_manager.get_or_create_extension_api_token(), token)

    def test_extension_token_rotation_by_age(self):
        with patch.dict("os.environ", {"MNS_EXTENSION_TOKEN_MAX_AGE_DAYS": "0.0000001"}, clear=False):
            first = credential_manager.get_or_create_extension_api_token()
            # age is now exceeded -> rotate
            with patch("credential_manager.time.time", return_value=__import__("time").time() + 10000):
                second = credential_manager.get_or_create_extension_api_token()
            self.assertNotEqual(first, second)


class MessagingTestCase(unittest.TestCase):
    def test_listen_unlisten(self):
        ann = messaging.MessageAnnouncer()
        q = ann.listen()
        self.assertEqual(ann.listener_count(), 1)
        ann.unlisten(q)
        self.assertEqual(ann.listener_count(), 0)
        ann.unlisten(q)  # already removed -> no error

    def test_announce_to_listener(self):
        ann = messaging.MessageAnnouncer()
        q = ann.listen()
        ann.announce("msg")
        self.assertEqual(q.get_nowait(), "msg")

    def test_announce_backpressure_drops_slow_listener(self):
        ann = messaging.MessageAnnouncer()
        q = ann.listen()
        # fill the listener queue to maxsize to trigger overload drop
        for _ in range(q.maxsize):
            q.put_nowait("old")
        ann.announce("new")
        self.assertNotIn(q, ann.listeners)

    def test_listener_context(self):
        ann = messaging.MessageAnnouncer()
        with ann.listener_context():
            self.assertEqual(ann.listener_count(), 1)
        self.assertEqual(ann.listener_count(), 0)

    def test_too_many_listeners(self):
        ann = messaging.MessageAnnouncer()
        qs = [ann.listen() for _ in range(messaging.MAX_SSE_LISTENERS)]
        try:
            with self.assertRaises(RuntimeError):
                ann.listen()
        finally:
            for q in qs:
                ann.unlisten(q)


class AppStateFiltersTestCase(unittest.TestCase):
    def test_backend_log_filter(self):
        f = app_state.BackendLogFilter()
        import logging
        warn = logging.LogRecord("x", logging.WARNING, "p", 1, "warning msg", None, None)
        self.assertTrue(f.filter(warn))
        info_match = logging.LogRecord("x", logging.INFO, "p", 1, "REQ start handled", None, None)
        self.assertTrue(f.filter(info_match))
        info_nomatch = logging.LogRecord("x", logging.INFO, "p", 1, "verbose noise", None, None)
        self.assertFalse(f.filter(info_nomatch))
        debug = logging.LogRecord("x", logging.DEBUG, "p", 1, "debug stuff", None, None)
        self.assertFalse(f.filter(debug))

    def test_polling_filter(self):
        f = app_state.PollingFilter()
        import logging
        poll = logging.LogRecord("x", logging.INFO, "p", 1, "GET /api/health - 200 -", None, None)
        self.assertFalse(f.filter(poll))
        nonpoll = logging.LogRecord("x", logging.INFO, "p", 1, "GET /api/stocks - 200 -", None, None)
        self.assertFalse(f.filter(nonpoll))
        other = logging.LogRecord("x", logging.INFO, "p", 1, "some other log", None, None)
        self.assertTrue(f.filter(other))


if __name__ == "__main__":
    unittest.main()
