"""
Tests for Mistral AI hardening and model selection in Settings.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

import config_store
import utils.storage as storage_mod
from app import create_app
from app_state import app_state
from config_utils import get_model_catalog, resolve_model_target
from credential_manager import get_model_name
from services import ai_service
from tests.test_support.patches import apply_storage_and_config_patches
from tests.test_support.state_reset import reset_app_state_internals


class MistralHardeningTestCase(unittest.TestCase):
    """Verify that httpx.HTTPError and timeout exceptions are safely caught."""

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-medium-2604")
    @patch("services.ai_service._get_mistral_client")
    def test_call_mistral_chat_catches_read_timeout(self, mock_get_client, mock_get_name):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.complete.side_effect = httpx.ReadTimeout("The read operation timed out")

        with patch.object(app_state.market, "report_circuit_result") as mock_circuit:
            result = ai_service.call_mistral_chat(
                "test-api-key",
                [{"role": "user", "content": "hello"}],
                use_cache=False,
            )

        self.assertIn("error", result)
        self.assertEqual(result["error"]["status_code"], 504)
        self.assertIn("タイムアウト", result["error"]["message"])
        mock_circuit.assert_called_with("mistral", success=False, threshold=3, open_sec=60)

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-medium-2604")
    @patch("services.ai_service._get_mistral_client")
    def test_call_mistral_chat_catches_connect_error(self, mock_get_client, mock_get_name):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.complete.side_effect = httpx.ConnectError("Connection refused")

        with patch.object(app_state.market, "report_circuit_result") as mock_circuit:
            result = ai_service.call_mistral_chat(
                "test-api-key",
                [{"role": "user", "content": "hello"}],
                use_cache=False,
            )

        self.assertIn("error", result)
        self.assertEqual(result["error"]["status_code"], 503)
        mock_circuit.assert_called_with("mistral", success=False, threshold=3, open_sec=60)

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-medium-2604")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_mistral_chat_catches_timeout(self, mock_get_client, mock_get_name):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.stream.side_effect = httpx.ReadTimeout("The read operation timed out")

        events = list(
            ai_service.stream_mistral_chat(
                "test-api-key",
                [{"role": "user", "content": "hello"}],
            )
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["status_code"], 504)
        self.assertIn("タイムアウト", events[0]["message"])


class ModelCatalogTestCase(unittest.TestCase):
    """Verify model catalog, aliases, and resolution."""

    def test_get_model_catalog_structure(self):
        catalog = get_model_catalog()
        self.assertIsInstance(catalog, list)
        self.assertGreaterEqual(len(catalog), 5)

        names = [m["name"] for m in catalog]
        self.assertIn("mistral-medium-2604", names)
        self.assertIn("mistral-small-2603", names)
        self.assertIn("mistral-large-2512", names)

        # Check recommended model
        recommended = [m for m in catalog if m.get("recommended")]
        self.assertEqual(len(recommended), 1)
        self.assertEqual(recommended[0]["name"], "mistral-medium-2604")

    def test_resolve_model_target(self):
        self.assertEqual(resolve_model_target("1")["name"], "mistral-small-2603")
        self.assertEqual(resolve_model_target("2")["name"], "mistral-medium-2604")
        self.assertEqual(resolve_model_target("3")["name"], "mistral-large-2512")
        self.assertEqual(
            resolve_model_target("mistral-medium-latest")["name"], "mistral-medium-2604"
        )
        self.assertEqual(resolve_model_target("mistral-large-latest")["name"], "mistral-large-2512")
        self.assertEqual(resolve_model_target("mistral-small-latest")["name"], "mistral-small-2603")
        # Test supported models not in 1..6 catalog
        self.assertEqual(resolve_model_target("devstral-2512")["name"], "devstral-2512")
        self.assertEqual(resolve_model_target("devstral-2512")["badge"], "devstral-2512")
        self.assertEqual(
            resolve_model_target("ministral-14b-latest")["name"], "ministral-14b-latest"
        )
        # Test newly added models from official docs
        self.assertEqual(resolve_model_target("9")["name"], "mistral-large-4-0")
        self.assertEqual(resolve_model_target("mistral-large-4")["name"], "mistral-large-4-0")
        self.assertEqual(resolve_model_target("10")["name"], "mistral-ocr-latest")
        self.assertEqual(resolve_model_target("ocr-4-1")["name"], "mistral-ocr-4-1")
        self.assertEqual(resolve_model_target("zai-glm-5-3")["name"], "zai-glm-5-3")
        self.assertEqual(resolve_model_target("voxtral-mini-transcribe-3-0")["name"], "voxtral-mini-transcribe-3-0")
        self.assertEqual(resolve_model_target("timestral-1-0-209m")["name"], "timestral-1-0-209m")
        self.assertIsNone(resolve_model_target("completely-fake-model"))


class ApiCredentialsModelSelectionTestCase(unittest.TestCase):
    """Verify GET and POST /api/credentials model selection endpoints."""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_paths = {
            "app_data_dir": config_store.APP_DATA_DIR,
            "config_file": config_store.CONFIG_FILE,
            "cs_user_stocks": config_store.USER_STOCKS_FILE,
            "cs_legacy_config": config_store.LEGACY_CONFIG_FILE,
            "st_user_stocks": storage_mod.USER_STOCKS_FILE,
            "st_legacy_user_stocks": storage_mod.LEGACY_USER_STOCKS_FILE,
            "stock_disk_cache": app_state.stock_disk_cache,
            "payload_disk_cache": app_state.payload_disk_cache,
            "token_file": app_state.shutdown_manager.token_file,
            "used_marker": app_state.shutdown_manager.used_marker,
            "runtime_state_dir": app_state.shutdown_manager.runtime_state_dir,
            "legacy_token_file": app_state.shutdown_manager._legacy_token_file,
            "legacy_used_marker": app_state.shutdown_manager._legacy_used_marker,
        }
        apply_storage_and_config_patches(Path(self._tmp_dir.name))
        self.app = create_app(
            config_override={"TESTING": True, "WTF_CSRF_ENABLED": False},
            skip_bootstrap=True,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        config_store.APP_DATA_DIR = self._orig_paths["app_data_dir"]
        config_store.CONFIG_FILE = self._orig_paths["config_file"]
        config_store.USER_STOCKS_FILE = self._orig_paths["cs_user_stocks"]
        config_store.LEGACY_CONFIG_FILE = self._orig_paths["cs_legacy_config"]
        storage_mod.USER_STOCKS_FILE = self._orig_paths["st_user_stocks"]
        storage_mod.LEGACY_USER_STOCKS_FILE = self._orig_paths["st_legacy_user_stocks"]
        app_state.stock_disk_cache = self._orig_paths["stock_disk_cache"]
        app_state.payload_disk_cache = self._orig_paths["payload_disk_cache"]
        app_state.shutdown_manager.token_file = self._orig_paths["token_file"]
        app_state.shutdown_manager.used_marker = self._orig_paths["used_marker"]
        app_state.shutdown_manager.runtime_state_dir = self._orig_paths["runtime_state_dir"]
        app_state.shutdown_manager._legacy_token_file = self._orig_paths["legacy_token_file"]
        app_state.shutdown_manager._legacy_used_marker = self._orig_paths["legacy_used_marker"]
        reset_app_state_internals()
        self._tmp_dir.cleanup()

    def test_get_credentials_includes_available_models(self):
        resp = self.client.get("/api/credentials")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIn("available_models", data)
        self.assertIsInstance(data["available_models"], list)
        self.assertIn("mistral_model", data)
        self.assertIn("model_badge", data)
        self.assertIn("model_label", data)
        self.assertIsInstance(data["model_label"], str)

    def test_post_credentials_selects_model(self):
        resp = self.client.post(
            "/api/credentials",
            json={"mistral_model": "mistral-small-2603"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("mistral_model"), "mistral-small-2603")
        self.assertEqual(get_model_name(), "mistral-small-2603")
        self.assertEqual(data.get("model_label"), "Mistral Small 4 (高速・軽量)")

        # Test selecting via alias
        resp2 = self.client.post(
            "/api/credentials",
            json={"mistral_model": "mistral-medium-latest"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.get_json()
        self.assertTrue(data2.get("ok"))
        self.assertEqual(data2.get("mistral_model"), "mistral-medium-2604")
        self.assertEqual(get_model_name(), "mistral-medium-2604")
        self.assertEqual(data2.get("model_label"), "Mistral Medium 3.5 (推奨・バランス)")

        # Test selecting a supported model outside 1..6 catalog
        resp3 = self.client.post(
            "/api/credentials",
            json={"mistral_model": "devstral-2512"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.get_json()
        self.assertTrue(data3.get("ok"))
        self.assertEqual(data3.get("mistral_model"), "devstral-2512")
        self.assertEqual(get_model_name(), "devstral-2512")
        self.assertEqual(data3.get("model_label"), "devstral-2512")

    def test_post_credentials_rejects_invalid_model(self):
        resp = self.client.post(
            "/api/credentials",
            json={"mistral_model": "invalid-unsupported-model-xyz"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data.get("ok"))
        self.assertIn("未対応", data.get("details", {}).get("reason", ""))


if __name__ == "__main__":
    unittest.main()
