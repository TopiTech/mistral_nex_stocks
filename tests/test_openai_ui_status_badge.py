"""tests/test_openai_ui_status_badge.py
Unit and integration tests for OpenAI-compatible API UI status badge and SSR rendering.
Verifies that when OpenAI-compatible provider is active, UI correctly identifies and displays
the OpenAI provider rather than defaulting to Mistral API.
"""

import unittest

from app import create_app
from credential_manager import (
    PROVIDER_MISTRAL,
    PROVIDER_OPENAI,
    clear_api_credentials,
    save_api_credentials,
)


class TestOpenAiUiStatusBadge(unittest.TestCase):
    """Test AI API status badge reporting and templates SSR for OpenAI-compatible provider."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()

    def tearDown(self):
        clear_api_credentials()

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
        with open("static/js/settings.js", "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("function setMistralModelSectionBlackout(isBlackout)", content)
        self.assertIn("section.classList.toggle(\"is-blacked-out\", isBlackout)", content)
        self.assertIn("setMistralModelSectionBlackout(isOpenAI)", content)
        self.assertIn("go-to-system-settings-btn", content)

    def test_chart_js_contains_dynamic_provider_status_logic(self):
        """Verify static/js/chart.js implements dynamic provider label logic in updateApiStatus."""
        with open("static/js/chart.js", "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("async function updateApiStatus()", content)
        self.assertIn("resolvedProvider === \"openai_compatible\"", content)
        self.assertIn("OpenAI API", content)
        self.assertIn("Mistral API", content)
        self.assertIn("${resolvedLabel}: Connected", content)
        self.assertIn("${initialLabel}: Disconnected", content)


if __name__ == "__main__":
    unittest.main()
