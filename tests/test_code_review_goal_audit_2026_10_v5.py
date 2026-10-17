"""
Regression and verification test suite for code review fixes (Unit 1 - Unit 6).
Covers:
- Robust SSRF/loopback validation in is_local_openai_base_url
- Provider credential isolation in extract_api_key
- OpenAI cache key hashing with cache_key_override
- Embeddings namespace isolation across providers/models
- AI technical lines provider eligibility and error messaging
- Credentials save audit logging
"""

from unittest.mock import MagicMock, patch

import pytest

from credential_manager import (
    PROVIDER_MISTRAL,
    PROVIDER_OPENAI,
    clear_api_credentials,
    is_local_openai_base_url,
    is_medium_or_large_model,
    is_openai_provider,
    save_api_credentials,
)
from route_helpers import extract_api_key
from services.embeddings_service import (
    _EMBEDDINGS_CACHE,
    _EMBEDDINGS_CACHE_LOCK,
    _compute_text_hash,
)
from services.openai_service import _build_openai_cache_key


@pytest.fixture(autouse=True)
def clean_credentials_and_cache():
    """Ensure clean credential state and cache before and after each test."""
    clear_api_credentials()
    with _EMBEDDINGS_CACHE_LOCK:
        _EMBEDDINGS_CACHE.clear()
    yield
    clear_api_credentials()
    with _EMBEDDINGS_CACHE_LOCK:
        _EMBEDDINGS_CACHE.clear()


def test_is_local_openai_base_url_robust_validation():
    """Test is_local_openai_base_url strictly identifies loopback hosts and rejects spoofing."""
    # Valid loopback targets
    assert is_local_openai_base_url("http://localhost:11434/v1") is True
    assert is_local_openai_base_url("http://127.0.0.1:11434") is True
    assert is_local_openai_base_url("http://0.0.0.0:8000/v1") is True
    assert is_local_openai_base_url("http://[::1]:11434/v1") is True
    assert is_local_openai_base_url("localhost:11434") is True
    assert is_local_openai_base_url("127.0.0.1:8000") is True

    # Rejection of spoofed hostnames or paths containing loopback terms
    assert is_local_openai_base_url("http://localhost.evil.com/v1") is False
    assert is_local_openai_base_url("http://evil.com/localhost") is False
    assert is_local_openai_base_url("http://notlocalhost.attacker.com:11434") is False
    assert is_local_openai_base_url("http://evil.com?param=127.0.0.1") is False
    assert is_local_openai_base_url("https://api.openai.com/v1") is False
    assert is_local_openai_base_url("https://api.groq.com/openai/v1") is False
    assert is_local_openai_base_url("") is False


def test_extract_api_key_cross_provider_isolation(monkeypatch):
    """Test extract_api_key never exposes Mistral key when OpenAI provider is active."""
    # Setup: user previously saved a Mistral API key
    save_api_credentials(
        ai_provider=PROVIDER_MISTRAL,
        mistral_api_key="sk-mistral-top-secret-key-12345",
    )

    req_mock = MagicMock()
    req_mock.headers = {}

    # Verify Mistral provider extracts the Mistral key
    assert extract_api_key(req_mock) == "sk-mistral-top-secret-key-12345"

    # User now switches to OpenAI provider with a remote endpoint, but has no OpenAI key
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4o-mini",
    )

    # Must NOT leak the stored Mistral key!
    extracted = extract_api_key(req_mock)
    assert extracted == ""
    assert extracted != "sk-mistral-top-secret-key-12345"

    # When user saves an OpenAI key, it extracts that key
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_api_key="sk-proj-openai-key-67890",
    )
    assert extract_api_key(req_mock) == "sk-proj-openai-key-67890"

    # When using local endpoint without key, returns ollama-local
    clear_api_credentials()
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_base_url="http://localhost:11434/v1",
    )
    assert extract_api_key(req_mock) == "ollama-local"


def test_openai_cache_key_hash_includes_override():
    """Test that _build_openai_cache_key includes cache_key_override in hash to avoid collisions."""
    key1 = _build_openai_cache_key(
        "gpt-4o-mini",
        [{"role": "user", "content": "AAPL analysis"}],
        500,
        0.0,
        None,
        None,
        cache_key_override="repair_analysis_json_v1",
    )
    key2 = _build_openai_cache_key(
        "gpt-4o-mini",
        [{"role": "user", "content": "TSLA analysis"}],
        500,
        0.0,
        None,
        None,
        cache_key_override="repair_analysis_json_v1",
    )
    key_no_override = _build_openai_cache_key(
        "gpt-4o-mini",
        [{"role": "user", "content": "AAPL analysis"}],
        500,
        0.0,
        None,
        None,
        cache_key_override=None,
    )

    # Different content with same override produces different cache keys
    assert key1 != key2
    # Same content with different override produces different cache keys
    assert key1 != key_no_override
    # Both start with openai_ prefix
    assert key1.startswith("openai_")
    assert key2.startswith("openai_")


def test_embeddings_cache_namespaces_isolated():
    """Test that embeddings cache namespaces isolate vectors between Mistral and OpenAI."""
    text = "AI semiconductor growth drivers"
    hash_mistral = _compute_text_hash(text, namespace="mistral:mistral-embed")
    hash_openai = _compute_text_hash(text, namespace="openai:text-embedding-3-small")

    assert hash_mistral.startswith("mistral:mistral-embed:")
    assert hash_openai.startswith("openai:text-embedding-3-small:")
    assert hash_mistral != hash_openai

    with _EMBEDDINGS_CACHE_LOCK:
        _EMBEDDINGS_CACHE[hash_mistral] = [1.0] * 1024
        _EMBEDDINGS_CACHE[hash_openai] = [2.0] * 1536

        assert len(_EMBEDDINGS_CACHE[hash_mistral]) == 1024
        assert len(_EMBEDDINGS_CACHE[hash_openai]) == 1536


def test_is_medium_or_large_model_openai_support():
    """Test is_medium_or_large_model returns True for OpenAI provider."""
    save_api_credentials(ai_provider=PROVIDER_OPENAI)
    assert is_openai_provider() is True
    assert is_medium_or_large_model("gpt-4o-mini") is True
    assert is_medium_or_large_model("mistral-small-2603") is True

    save_api_credentials(ai_provider=PROVIDER_MISTRAL)
    assert is_openai_provider() is False
    assert is_medium_or_large_model("mistral-small-2603") is False
    assert is_medium_or_large_model("mistral-medium-2604") is True


def test_api_ai_technical_lines_openai_provider(client):
    """Test /api/ai-technical-lines allows OpenAI provider without model restriction 403."""
    save_api_credentials(
        ai_provider=PROVIDER_OPENAI,
        openai_api_key="sk-test-openai-key",
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4o-mini",
    )

    mock_tech_res = {
        "ok": True,
        "symbol": "AAPL",
        "market": "us",
        "lines": [
            {
                "type": "support",
                "label": "Support S1",
                "start_date": "2026-01-01",
                "start_price": 220.0,
                "end_date": "2026-03-01",
                "end_price": 220.0,
                "color": "#10b981",
                "description": "Strong horizontal support level",
            }
        ],
    }

    with patch(
        "routes.api_analysis.generate_ai_technical_lines",
        return_value=mock_tech_res,
    ):
        res = client.post(
            "/api/ai-technical-lines",
            json={
                "symbol": "AAPL",
                "market": "us",
                "period": "3mo",
                "history_data": [
                    {"date": "2026-01-01", "o": 220, "h": 225, "l": 219, "c": 224}
                ],
            },
            headers={"Origin": "http://localhost:5000"},
        )

    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert len(data["lines"]) == 1
    assert data["lines"][0]["type"] == "support"


def test_api_credentials_save_logs_provider_and_key_fingerprint(client, caplog):
    """Test POST /api/credentials logs provider and openai token fingerprint."""
    import logging

    with caplog.at_level(logging.INFO):
        res = client.post(
            "/api/credentials",
            json={
                "ai_provider": "openai_compatible",
                "openai_api_key": "sk-proj-audit-test-fingerprint-key",
                "openai_base_url": "https://api.openai.com/v1",
                "openai_model": "gpt-4o-mini",
            },
            headers={"Origin": "http://localhost:5000"},
        )

    assert res.status_code == 200
    log_text = caplog.text
    assert "provider=openai_compatible" in log_text
    assert "openai=" in log_text
