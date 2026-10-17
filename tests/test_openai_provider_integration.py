"""
Integration tests for OpenAI-compatible API Provider (Beta).
Tests provider switching, credential management, API endpoints, and ai_service dispatch.
"""

import os
from unittest.mock import patch

import pytest

from app import create_app
from credential_manager import (
    PROVIDER_MISTRAL,
    PROVIDER_OPENAI,
    clear_api_credentials,
    get_ai_provider,
    get_api_credential_state,
    get_openai_api_key,
    get_openai_base_url,
    get_openai_embeddings_model,
    get_openai_model,
    has_ai_access,
    has_openai_api_key,
    is_local_openai_base_url,
    is_openai_provider,
    save_api_credentials,
)
from services.ai_service import (
    analyze_chart_image_with_mistral,
    call_mistral_chat,
    call_mistral_chat_with_tools,
    stream_mistral_chat,
)


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


def test_credential_manager_provider_switching():
    """Test switching between Mistral and OpenAI providers in credential manager."""
    # 1. Switch to OpenAI local endpoint (e.g. Ollama)
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_base_url="http://localhost:11434/v1",
        openai_model="llama3.2",
        openai_embeddings_model="nomic-embed-text",
        openai_api_key="",  # local endpoints do not require key
    )

    assert get_ai_provider() == PROVIDER_OPENAI
    assert is_openai_provider() is True
    assert is_local_openai_base_url("http://localhost:11434/v1") is True
    assert is_local_openai_base_url() is True
    assert get_openai_model() == "llama3.2"
    assert get_openai_embeddings_model() == "nomic-embed-text"
    # Local endpoint grants AI access even without key
    assert has_ai_access() is True

    # 2. Switch to Cloud OpenAI endpoint without key -> has_ai_access is False
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4o-mini",
        openai_api_key="",
    )
    assert is_local_openai_base_url() is False
    assert has_openai_api_key() is False
    assert has_ai_access() is False

    # 3. Add API key for cloud endpoint
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4o-mini",
        openai_api_key="sk-test-secret-key-12345",
    )
    assert has_openai_api_key() is True
    assert has_ai_access() is True
    assert get_openai_api_key() == "sk-test-secret-key-12345"

    state = get_api_credential_state()
    assert state["ai_provider"] == PROVIDER_OPENAI
    assert state["is_beta_provider"] is True
    assert state["has_openai_api_key"] is True

    # 4. Switch back to Mistral
    save_api_credentials(ai_provider=PROVIDER_MISTRAL)
    assert get_ai_provider() == PROVIDER_MISTRAL
    assert is_openai_provider() is False


def test_api_credentials_endpoint_get_and_post(client):
    """Test GET and POST /api/credentials exposes and stores OpenAI config."""
    # GET endpoint
    res = client.get("/api/credentials")
    assert res.status_code == 200
    data = res.get_json()
    assert "ai_provider" in data
    assert "is_beta_provider" in data
    assert "openai_base_url" in data
    assert "openai_model" in data

    # POST endpoint with OpenAI configuration
    res = client.post(
        "/api/credentials",
        json={
            "ai_provider": "openai_compatible",
            "openai_base_url": "https://api.groq.com/openai/v1",
            "openai_model": "llama-3.3-70b-versatile",
            "openai_embeddings_model": "",
            "openai_api_key": "gsk_test1234567890",
        },
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 200
    res_data = res.get_json()
    assert res_data["ok"] is True

    assert get_ai_provider() == "openai_compatible"
    assert get_openai_base_url() == "https://api.groq.com/openai/v1"
    assert get_openai_model() == "llama-3.3-70b-versatile"
    assert get_openai_api_key() == "gsk_test1234567890"

    # Reset back to Mistral
    client.post(
        "/api/credentials",
        json={"ai_provider": "mistral"},
        headers={"Origin": "http://localhost:5000"},
    )


def test_api_credentials_verify_openai_endpoint(client):
    """Test POST /api/credentials/verify for openai_compatible provider."""
    mock_verify_res = {
        "ok": True,
        "valid": True,
        "provider": "openai_compatible",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "model_count": 2,
        "latency_ms": 145,
    }

    with patch("services.openai_service.verify_openai_connection", return_value=mock_verify_res):
        res = client.post(
            "/api/credentials/verify",
            json={
                "target_provider": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-valid-openai-key",
            },
            headers={"Origin": "http://localhost:5000"},
        )

    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["latency_ms"] == 145
    assert "gpt-4o-mini" in data["models"]


def test_ai_service_dispatches_to_openai_provider():
    """Test that existing functions in ai_service seamlessly dispatch to openai_service."""
    with patch("services.ai_service.is_openai_provider", return_value=True):
        # 1. call_mistral_chat dispatch
        with patch("services.openai_service.call_openai_chat", return_value={"mock": "chat"}) as mock_chat:
            res = call_mistral_chat("sk-test", [{"role": "user", "content": "hello"}])
            assert res == {"mock": "chat"}
            mock_chat.assert_called_once()

        # 2. stream_mistral_chat dispatch
        with patch("services.openai_service.stream_openai_chat", return_value=iter([{"type": "delta", "text": "hi"}])) as mock_stream:
            events = list(stream_mistral_chat("sk-test", [{"role": "user", "content": "hello"}]))
            assert events == [{"type": "delta", "text": "hi"}]
            mock_stream.assert_called_once()

        # 3. call_mistral_chat_with_tools dispatch
        with patch("services.openai_service.call_openai_chat_with_tools", return_value={"mock": "tools"}) as mock_tools:
            res = call_mistral_chat_with_tools("sk-test", [{"role": "user", "content": "check stocks"}])
            assert res == {"mock": "tools"}
            mock_tools.assert_called_once()

        # 4. analyze_chart_image_with_mistral dispatch
        with patch("services.openai_service.analyze_chart_image_with_openai", return_value={"mock": "vision"}) as mock_vision:
            res = analyze_chart_image_with_mistral("sk-test", "dummy_base64", symbol="AAPL")
            assert res == {"mock": "vision"}
            mock_vision.assert_called_once()


def test_openai_key_stored_priority_over_env(monkeypatch):
    """Test that stored OpenAI API key takes priority over ambient environment variables."""
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-env-key-from-os")
    assert get_openai_api_key() == "ambient-env-key-from-os"

    # User explicitly saves a new key via settings
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_api_key="new-user-saved-key-12345",
    )

    # Stored key must take precedence over ambient environment variable
    assert get_openai_api_key() == "new-user-saved-key-12345"
    assert has_openai_api_key() is True
    assert has_ai_access() is True

    # Clean up
    clear_api_credentials()


def test_openai_logout_clears_keys_and_env(client, monkeypatch):
    """Test DELETE /api/credentials resets provider and clears ambient env keys so logout succeeds."""
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-env-key-from-os")

    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_api_key="user-configured-key-12345",
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4o-mini",
    )
    assert has_openai_api_key() is True
    assert has_ai_access() is True

    # User triggers DELETE /api/credentials
    res = client.delete("/api/credentials", headers={"Origin": "http://localhost:5000"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["has_openai_api_key"] is False
    assert data["has_ai_access"] is False
    assert data["ai_provider"] == PROVIDER_MISTRAL
    assert "OPENAI_API_KEY" not in os.environ


def test_api_credentials_verify_with_empty_key_uses_stored_key(client):
    """Test that empty string for openai_api_key falls back to configured stored key."""
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_api_key="stored-secret-key-99999",
        openai_base_url="https://api.groq.com/openai/v1",
    )

    mock_verify_res = {
        "ok": True,
        "valid": True,
        "provider": "openai_compatible",
        "models": ["llama-3.3-70b-versatile"],
        "model_count": 1,
        "latency_ms": 100,
    }

    with patch("services.openai_service.verify_openai_connection", return_value=mock_verify_res) as mock_verify:
        res = client.post(
            "/api/credentials/verify",
            json={
                "ai_provider": "openai_compatible",
                "openai_base_url": "https://api.groq.com/openai/v1",
                "openai_api_key": "",  # Empty string as sent by UI when key is placeholder
            },
            headers={"Origin": "http://localhost:5000"},
        )

        assert res.status_code == 200
        mock_verify.assert_called_once_with(
            base_url="https://api.groq.com/openai/v1",
            api_key="stored-secret-key-99999",
        )

    # Clean up
    clear_api_credentials()

