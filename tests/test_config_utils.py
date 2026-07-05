import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import config_utils


class ConfigUtilsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_file = Path(self.temp_dir.name) / 'config.json'
        patcher = patch.object(config_utils, 'CONFIG_FILE', self.config_file)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_load_config_returns_defaults_when_missing(self):
        self.assertFalse(self.config_file.exists())
        cfg = config_utils.load_config()
        self.assertEqual(cfg['mistral_model'], config_utils.DEFAULT_CONFIG['mistral_model'])
        self.assertTrue(self.config_file.exists())

    def test_save_config_creates_backup_and_sets_permissions_on_unix(self):
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps({'mistral_model': 'mistral-large-latest', 'api_credentials': {}}), encoding='utf-8')

        with patch.object(config_utils, '_is_windows', return_value=False), patch.object(config_utils.os, 'chmod') as chmod_mock:
            cfg = {'mistral_model': 'mistral-small-latest', 'api_credentials': {}}
            config_utils.save_config(cfg, create_backup=True)

            backup_file = self.config_file.with_suffix(self.config_file.suffix + '.bak')
            self.assertTrue(backup_file.exists())
            chmod_mock.assert_any_call(self.config_file, 0o600)
            chmod_mock.assert_any_call(backup_file, 0o600)

    def test_save_api_credentials_stores_encoded_blob(self):
        with patch.object(config_utils, '_encode_secret', return_value={'scheme': 'test', 'value': 'abc123'}):
            config_utils.save_api_credentials('mistral-key', 'langsearch-key')

        saved = json.loads(self.config_file.read_text(encoding='utf-8'))
        self.assertIn('api_credentials', saved)
        self.assertEqual(saved['api_credentials']['mistral_api_key']['scheme'], 'test')
        self.assertEqual(saved['api_credentials']['langsearch_api_key']['scheme'], 'test')

    def test_clear_api_credentials_removes_keyring_entries(self):
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        starting_cfg = {
            'mistral_model': 'mistral-small-latest',
            'model_badge': 'mistral-small',
            'api_credentials': {
                'mistral_api_key': {'scheme': 'keyring', 'value': ''},
                'langsearch_api_key': {'scheme': 'keyring', 'value': ''},
            },
        }
        self.config_file.write_text(json.dumps(starting_cfg), encoding='utf-8')

        with patch.object(config_utils, 'KEYRING_AVAILABLE', True), patch.object(config_utils.keyring, 'delete_password') as delete_password_mock:
            config_utils.clear_api_credentials()

            content = json.loads(self.config_file.read_text(encoding='utf-8'))
            self.assertEqual(content.get('api_credentials'), {})
            delete_password_mock.assert_any_call('mistral_nex_stocks', 'mistral_api_key')
            delete_password_mock.assert_any_call('mistral_nex_stocks', 'langsearch_api_key')

    def test_get_api_credential_state_reflects_presence(self):
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        config_utils.save_config({'mistral_model': 'mistral-small-latest', 'model_badge': 'mistral-small', 'api_credentials': {}})
        self.assertFalse(config_utils.get_api_credential_state()['has_mistral_api_key'])

    def test_save_api_credentials_rejects_plaintext_without_keyring(self):
        # Ensure plaintext fallback is disallowed by default when keyring is absent
        with patch.object(config_utils, 'KEYRING_AVAILABLE', False), patch.object(config_utils, '_is_windows', return_value=False):
            with patch.dict(os.environ, {"MNS_ALLOW_INSECURE_PLAINTEXT": ""}, clear=False):
                with self.assertRaises(RuntimeError):
                    config_utils.save_api_credentials('mistral-key', 'langsearch-key')

    def test_decode_secret_ignores_legacy_plaintext_string(self):
        with patch.dict(os.environ, {"MNS_ALLOW_INSECURE_PLAINTEXT": ""}, clear=False):
            self.assertEqual(config_utils._decode_secret('plain-secret', 'mistral_api_key'), '')

    def test_decode_secret_plaintext_scheme_is_ignored_by_default(self):
        entry = {'scheme': 'plaintext', 'value': 'plain-secret'}
        with patch.dict(os.environ, {"MNS_ALLOW_INSECURE_PLAINTEXT": ""}, clear=False):
            self.assertEqual(config_utils._decode_secret(entry, 'mistral_api_key'), '')

    def test_plaintext_scheme_and_string_allowed_with_opt_in(self):
        # Opt-in via env var MNS_ALLOW_INSECURE_PLAINTEXT
        entry = {'scheme': 'plaintext', 'value': 'plain-secret'}
        with patch.dict(os.environ, {"MNS_ALLOW_INSECURE_PLAINTEXT": "1"}, clear=False):
            self.assertEqual(config_utils._decode_secret(entry, 'mistral_api_key'), 'plain-secret')
            self.assertEqual(config_utils._decode_secret('legacy-plain-string', 'mistral_api_key'), 'legacy-plain-string')

            # Test encoding
            with patch.object(config_utils, 'KEYRING_AVAILABLE', False), patch.object(config_utils, '_is_windows', return_value=False):
                encoded = config_utils._encode_secret('my-secret-key', 'mistral_api_key')
                self.assertEqual(encoded['scheme'], 'plaintext')
                self.assertEqual(encoded['value'], 'my-secret-key')
