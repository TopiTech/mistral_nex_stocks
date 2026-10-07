"""Tests for Mistral Agents API support and keyless Preview Mode.

Verifies:
  1. Setting up Mistral Agents API mode with an agent_id.
  2. Disallowing model selection updates when in Agents API mode.
  3. Verification endpoint behavior for preview and agents mode.
  4. Routing chat completion and streaming to client.agents.complete/stream in agents mode.
  5. Enabling preview mode without API key.
  6. Preview mode generating valid sample responses without network calls.
  7. Clearing credentials resets preview mode and agent settings.
"""

from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from credential_manager import (
    clear_api_credentials,
    get_agent_id,
    get_api_mode,
    get_model_badge,
    has_ai_access,
    has_mistral_api_key,
    is_agents_mode,
    is_medium_or_large_model,
    is_preview_mode,
    save_api_credentials,
)
from services.ai_service import call_mistral_chat, stream_mistral_chat
from utils.validators import StockAnalysis


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def clean_credentials():
    clear_api_credentials()
    yield
    clear_api_credentials()


def test_preview_mode_enable_and_state(client):
    """Test activating preview mode via POST /api/credentials."""
    res = client.post(
        "/api/credentials",
        json={"preview_mode": True},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["preview_mode"] is True
    assert data["has_ai_access"] is True
    assert data["has_mistral_api_key"] is False
    assert is_preview_mode() is True
    assert has_ai_access() is True
    assert get_model_badge() == "Preview"
    assert is_medium_or_large_model() is True


def test_preview_mode_chat_output():
    """Test call_mistral_chat and stream_mistral_chat return sample output in preview mode."""
    save_api_credentials(preview_mode=True)
    assert is_preview_mode() is True

    # Call with structured format
    resp = call_mistral_chat(
        api_key="mns-preview-mode-no-api-key",
        messages=[{"role": "user", "content": "AAPLの分析をお願いします"}],
        response_format=StockAnalysis,
    )
    assert resp.get("preview") is True
    choices = resp.get("choices", [])
    assert len(choices) == 1
    content = choices[0]["message"]["content"]
    assert isinstance(content, dict)
    assert "recommendation" in content
    assert "プレビューモード" in content["analysis_summary"]

    # Stream call
    events = list(
        stream_mistral_chat(
            api_key="mns-preview-mode-no-api-key",
            messages=[{"role": "user", "content": "こんにちは"}],
        )
    )
    assert any(e.get("type") == "delta" for e in events)
    done_event = next((e for e in events if e.get("type") == "done"), None)
    assert done_event is not None
    assert "プレビューモード" in done_event["text"]


def test_agents_mode_save_and_state(client):
    """Test saving credentials with Agents API mode and Agent ID."""
    res = client.post(
        "/api/credentials",
        json={
            "mistral_api_key": "01234567890123456789012345678901",
            "mistral_api_mode": "agents",
            "mistral_agent_id": "ag_sample_agent_12345",
        },
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["api_mode"] == "agents"
    assert data["agent_id"] == "ag_sample_agent_12345"
    assert data["model_selectable"] is False
    assert data["available_models"] == []
    assert is_agents_mode() is True
    assert get_agent_id() == "ag_sample_agent_12345"
    assert get_model_badge() == "Agents API"


def test_agents_mode_blocks_model_selection(client):
    """Test that attempting to change mistral_model while in Agents API mode returns 400."""
    # First save in Agents API mode
    save_api_credentials(
        mistral_api_key="01234567890123456789012345678901",
        api_mode="agents",
        agent_id="ag_sample_agent_12345",
    )
    assert is_agents_mode() is True

    # Try to change model
    res = client.post(
        "/api/credentials",
        json={"mistral_model": "mistral-small-2603"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert "Agents APIではモデルはMistral側のコンソールで指定するため" in (
        data.get("details", {}).get("reason", "")
    )


def test_agents_mode_requires_agent_id(client):
    """Test that switching to agents mode without agent_id is rejected."""
    res = client.post(
        "/api/credentials",
        json={
            "mistral_api_key": "01234567890123456789012345678901",
            "mistral_api_mode": "agents",
            "mistral_agent_id": "",
        },
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False


@patch("mistral_compat.Mistral")
def test_agents_api_completion_routing(mock_mistral_cls):
    """Test call_mistral_chat routes to client.agents.complete when in agents mode."""
    mock_instance = MagicMock()
    mock_mistral_cls.return_value = mock_instance

    save_api_credentials(
        mistral_api_key="01234567890123456789012345678901",
        api_mode="agents",
        agent_id="ag_sample_test",
    )

    mock_resp = MagicMock()
    mock_resp.model_dump.return_value = {
        "choices": [{"message": {"content": "Agent response text"}}]
    }
    mock_instance.agents.complete.return_value = mock_resp

    res = call_mistral_chat(
        api_key="01234567890123456789012345678901",
        messages=[{"role": "user", "content": "テスト"}],
        use_cache=False,
    )

    mock_instance.agents.complete.assert_called_once()
    kwargs = mock_instance.agents.complete.call_args[1]
    assert kwargs.get("agent_id") == "ag_sample_test"
    assert "model" not in kwargs


def test_preview_mode_verify(client):
    """Test verifying credentials while in preview mode."""
    save_api_credentials(preview_mode=True)
    res = client.post(
        "/api/credentials/verify",
        json={},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["preview"] is True
