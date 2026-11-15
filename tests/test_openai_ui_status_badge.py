"""tests/test_openai_ui_status_badge.py
Unit and integration tests for OpenAI-compatible API UI status badge and SSR rendering.
Verifies that when OpenAI-compatible provider is active, UI correctly identifies and displays
the OpenAI provider rather than defaulting to Mistral API.
"""

import tempfile
import unittest
from pathlib import Path

import config_store
import utils.storage as storage_mod
from app import create_app
from app_state import app_state
from credential_manager import (
    PROVIDER_MISTRAL,
    PROVIDER_OPENAI,
    save_api_credentials,
)
from tests.test_support.patches import apply_storage_and_config_patches
from tests.test_support.state_reset import reset_app_state_internals

_ROOT = Path(__file__).resolve().parent.parent


class TestOpenAiUiStatusBadge(unittest.TestCase):
    """Test AI API status badge reporting and templates SSR for OpenAI-compatible provider."""

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

    def test_api_health_includes_ai_provider_and_preview_mode(self):
        """Verify /api/health always includes ai_provider and preview_mode."""
        # 1. Mistral default
        save_api_credentials(ai_provider=PROVIDER_MISTRAL)
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("ai_provider", data)
        self.assertIn("preview_mode", data)
        self.assertEqual(data["ai_provider"], "mistral")

        # 2. Switch to OpenAI compatible
        save_api_credentials(
            ai_provider=PROVIDER_OPENAI,
            openai_base_url="http://localhost:11434/v1",
            openai_model="llama3.2",
            openai_api_key="",
        )
        res2 = self.client.get("/api/health")
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()
        self.assertEqual(data2["ai_provider"], "openai_compatible")
        self.assertIn("badge", data2)
        self.assertIn("llama3.2", data2["badge"])

    def test_settings_ssr_renders_openai_provider_correctly(self):
        """Verify /settings renders OpenAI compatible information when provider is active."""
        save_api_credentials(
            ai_provider=PROVIDER_OPENAI,
            openai_base_url="http://localhost:11434/v1",
            openai_model="test-gpt-model",
            openai_api_key="",
        )
        res = self.client.get("/settings")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Check radio input is checked for openai_compatible
        self.assertIn('name="aiProvider" value="openai_compatible" checked', html)

        # Check connection mode label contains OpenAI互換 (Beta)
        self.assertIn("OpenAI互換 (Beta): test-gpt-model", html)
        self.assertIn("OpenAI Beta", html)

        # Check Mistral model section is blacked out
        self.assertIn('id="mistral-model-section"', html)
        self.assertIn('class="section mistral-model-section is-blacked-out"', html)
        self.assertIn('id="mistral-model-blackout-overlay"', html)
        self.assertIn("Mistral AI モデル設定は無効化されています", html)
        self.assertIn('id="save-model-btn"', html)
        self.assertIn('id="save-model-btn"\n              disabled', html)

    def test_settings_ssr_renders_mistral_provider_without_blackout(self):
        """Verify /settings renders Mistral model settings without blackout when Mistral is active."""
        save_api_credentials(ai_provider=PROVIDER_MISTRAL)
        res = self.client.get("/settings")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Check radio input is checked for mistral
        self.assertIn('name="aiProvider" value="mistral" checked', html)

        # Check Mistral model section is NOT blacked out
        self.assertIn('class="section mistral-model-section"', html)
        self.assertNotIn("is-blacked-out", html)
        self.assertIn('id="mistral-model-blackout-overlay"', html)
        self.assertIn('id="mistral-model-blackout-overlay"\n          class="model-section-blackout-overlay"\n          style="display: none;"', html)

    def test_settings_js_contains_blackout_logic(self):
        """Verify static/js/settings.js contains blackout handling for OpenAI compatible mode."""
        content = (_ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")

        self.assertIn("function setMistralModelSectionBlackout(isBlackout)", content)
        self.assertIn("section.classList.toggle(\"is-blacked-out\", isBlackout)", content)
        self.assertIn("setMistralModelSectionBlackout(isOpenAI)", content)
        self.assertIn("go-to-system-settings-btn", content)

    def test_chart_js_contains_dynamic_provider_status_logic(self):
        """Verify static/js/chart.js implements dynamic provider label logic in updateApiStatus."""
        content = (_ROOT / "static" / "js" / "chart.js").read_text(encoding="utf-8")

        self.assertIn("async function updateApiStatus()", content)
        self.assertIn("resolvedProvider === \"openai_compatible\"", content)
        self.assertIn("OpenAI API", content)
        self.assertIn("Mistral API", content)
        self.assertIn("${resolvedLabel}: Connected", content)
        self.assertIn("${initialLabel}: Disconnected", content)


if __name__ == "__main__":
    unittest.main()
