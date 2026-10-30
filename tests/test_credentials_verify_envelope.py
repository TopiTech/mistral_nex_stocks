"""Contract tests for unified success envelope on POST /api/credentials/verify.

Verifies that all successful verification paths (OpenAI, preview, agents,
standard models) return both `ok: True` and `success: True` per the A-06
unified success envelope specification.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import app
from credential_manager import save_api_credentials


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def test_verify_openai_success_returns_unified_envelope(client):
    """POST /api/credentials/verify for openai_compatible returns ok and success."""
    mock_result = {
        "ok": True,
        "valid": True,
        "provider": "openai_compatible",
        "tier_name": "OpenAI互換 (Beta)",
        "base_url": "https://api.openai.com/v1",
        "model_count": 2,
        "models": ["gpt-4o", "gpt-4o-mini"],
        "latency_ms": 42,
        "message": "接続成功",
    }
    with patch("services.openai_service.verify_openai_connection", return_value=mock_result):
        resp = client.post(
            "/api/credentials/verify",
            json={
                "ai_provider": "openai_compatible",
                "openai_api_key": "sk-test-openai-key-12345",
                "openai_base_url": "https://api.openai.com/v1",
            },
            headers={"Origin": "http://localhost:5000"},
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["success"] is True
    assert data["valid"] is True
    assert data["provider"] == "openai_compatible"


def test_verify_preview_mode_returns_unified_envelope(client):
    """POST /api/credentials/verify in preview mode returns ok and success."""
    save_api_credentials(preview_mode=True)
    resp = client.post(
        "/api/credentials/verify",
        json={"mistral_api_key": "mns-preview-mode-no-api-key"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["success"] is True
    assert data["valid"] is True
    assert data["preview"] is True


def test_verify_agents_mode_returns_unified_envelope(client):
    """POST /api/credentials/verify in agents mode returns ok and success."""
    mock_client = MagicMock()
    mock_agent = MagicMock()
    mock_agent.name = "Review Agent"
    mock_agent.model = "mistral-large-latest"
    mock_client.beta.agents.get.return_value = mock_agent

    with patch("app_state.app_state.ai.get_or_create_mistral_client", return_value=mock_client):
        resp = client.post(
            "/api/credentials/verify",
            json={
                "mistral_api_key": "01234567890123456789012345678901",
                "mistral_api_mode": "agents",
                "mistral_agent_id": "ag_envelope_test",
            },
            headers={"Origin": "http://localhost:5000"},
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["success"] is True
    assert data["valid"] is True
    assert data["mode"] == "agents"
    assert data["agent_name"] == "Review Agent"


@patch("mistral_compat.Mistral")
def test_verify_standard_chat_returns_unified_envelope(mock_mistral_cls, client):
    """POST /api/credentials/verify in standard chat mode returns ok and success."""
    mock_instance = MagicMock()
    mock_mistral_cls.return_value = mock_instance
    mock_m1 = MagicMock(id="mistral-small-2603")
    mock_m2 = MagicMock(id="mistral-large-2512")
    mock_instance.models.list.return_value = MagicMock(data=[mock_m1, mock_m2])

    resp = client.post(
        "/api/credentials/verify",
        json={"mistral_api_key": "01234567890123456789012345678901"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["success"] is True
    assert data["valid"] is True
    assert data["tier"] == "paid"
