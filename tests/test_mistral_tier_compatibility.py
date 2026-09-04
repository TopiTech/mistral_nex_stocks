"""
Unit & Integration tests for Mistral API Tier Compatibility & Automatic Fallbacks.
"""

from unittest.mock import MagicMock, patch

import httpx

from credential_manager import get_model_tier, is_free_tier_model, is_medium_or_large_model
from services.ai_service import (
    _is_mistral_tier_restriction_error,
    call_mistral_chat,
    stream_mistral_chat,
)


def make_mock_403_error(message="Access forbidden on Free Tier"):
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {}
    mock_resp.json.return_value = {
        "error": {
            "type": "permission_denied",
            "message": "Model mistral-large is not allowed on Free Tier",
        }
    }
    return httpx.HTTPStatusError(message, request=MagicMock(), response=mock_resp)


def test_model_tier_helpers():
    """Verify tier classification helpers."""
    assert is_free_tier_model("mistral-small-2603") is True
    assert is_free_tier_model("ministral-8b-latest") is True
    assert is_free_tier_model("codestral-latest") is True
    assert is_free_tier_model("mistral-large-2512") is False
    assert is_free_tier_model("mistral-medium-2604") is False

    assert get_model_tier("mistral-small-2603") == "free"
    assert get_model_tier("mistral-large-2512") == "paid"

    assert is_medium_or_large_model("mistral-large-2512") is True
    assert is_medium_or_large_model("mistral-medium-2604") is True
    assert is_medium_or_large_model("mistral-small-2603") is False


def test_is_mistral_tier_restriction_error():
    """Verify detection of Free Tier 403 and model restriction payloads."""
    assert _is_mistral_tier_restriction_error(status_code=403) is True
    assert _is_mistral_tier_restriction_error(status_code=200) is False

    err_payload_tier = {
        "error": {
            "type": "permission_denied",
            "message": "Model mistral-large is not allowed on current free tier plan",
        }
    }
    assert _is_mistral_tier_restriction_error(err_payload=err_payload_tier) is True

    err_payload_generic = {
        "error": {
            "type": "invalid_request_error",
            "message": "Invalid temperature parameter",
        }
    }
    assert _is_mistral_tier_restriction_error(err_payload=err_payload_generic) is False

    exc = Exception("HTTP 403: Forbidden - tier restricted on experiment plan")
    assert _is_mistral_tier_restriction_error(exc=exc) is True


@patch("services.ai_service._get_mistral_client")
@patch("services.ai_service._get_mistral_model_name", return_value="mistral-large-2512")
def test_call_mistral_chat_auto_fallback_on_403(mock_model_name, mock_get_client):
    """Test that when a large model receives 403 Forbidden, call_mistral_chat auto-falls back to mistral-small-2603."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    def side_effect(**kwargs):
        model = kwargs.get("model")
        if "large" in model:
            raise make_mock_403_error("Access to mistral-large is forbidden on Free Tier")
        # Fallback small model success
        mock_resp = MagicMock()
        mock_resp.model_dump.return_value = {
            "choices": [
                {"message": {"role": "assistant", "content": "Analysis generated via Small 4"}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        return mock_resp

    mock_client.chat.complete.side_effect = side_effect

    res = call_mistral_chat(
        "test-api-key", [{"role": "user", "content": "Analyze NVDA"}], use_cache=False
    )

    assert isinstance(res, dict)
    assert res.get("fallback_applied") is True
    assert res.get("original_model") == "mistral-large-2512"
    assert res.get("effective_model") == "mistral-small-2603"
    assert "choices" in res
    assert res["choices"][0]["message"]["content"] == "Analysis generated via Small 4"


@patch("services.ai_service._get_mistral_client")
@patch("services.ai_service._get_mistral_model_name", return_value="mistral-large-2512")
def test_stream_mistral_chat_auto_fallback_on_403(mock_model_name, mock_get_client):
    """Test that stream_mistral_chat auto-falls back to mistral-small-2603 when 403 occurs on start."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    def side_effect(**kwargs):
        model = kwargs.get("model")
        if "large" in model:
            raise make_mock_403_error("Access to mistral-large forbidden on Free Tier")

        chunk1 = MagicMock()
        chunk1.choices = [MagicMock(delta=MagicMock(content="Fallback stream chunk 1"))]
        chunk2 = MagicMock()
        chunk2.choices = [MagicMock(delta=MagicMock(content=" Fallback stream chunk 2"))]
        return [chunk1, chunk2]

    mock_client.chat.stream.side_effect = side_effect

    events = list(stream_mistral_chat("test-api-key", [{"role": "user", "content": "Hello"}]))

    deltas = [e for e in events if e.get("type") == "delta"]
    assert len(deltas) >= 2
    assert any("Small 4" in d.get("text", "") for d in deltas)
    done_events = [e for e in events if e.get("type") == "done"]
    assert len(done_events) == 1
