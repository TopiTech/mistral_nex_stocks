"""Tests for AI Chat UI resilience across stock cards, detail drawer, and assistant drawer."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_send_chat_supports_drawer_and_input_element():
    """sendChat must resolve input element from argument, wrapper, or detail drawer."""
    api_js = (ROOT_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")
    assert "async function sendChat(wrapper, inputEl = null)" in api_js
    assert "currentDrawerActiveWrapper" in api_js
    assert 'drawerAi?.querySelector(".chat-input")' in api_js
    assert 'document.querySelector("#stock-detail-drawer .chat-input")' in api_js
    assert "showToast(" in api_js
    assert "APIキーが未設定です" in api_js


def test_ui_js_wires_input_to_send_chat_and_checks_api_key():
    """ui.js passes input element to sendChat and guards sendAiDrawerMessage."""
    ui_js = (ROOT_DIR / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    assert 'setupBtn(".chat-send-btn", (e) => {' in ui_js
    assert "sendChat(wrapper, input);" in ui_js
    assert "sendChat(wrapper, e.currentTarget);" in ui_js
    assert "sendAiDrawerMessage" in ui_js
    assert "!HAS_MISTRAL_API_KEY" in ui_js
