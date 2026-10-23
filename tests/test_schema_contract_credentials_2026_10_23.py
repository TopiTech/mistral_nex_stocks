"""HTTP contract tests for the canonical credentials-save schema (U1/cred slice).

``POST /api/credentials`` now validates field shapes only through
``schemas.CredentialsSaveRequest`` (via
``route_helpers._parse_credentials_save_request``). These tests pin the
historic error envelopes (code, message, fields, status) and the
normalization the old inline checks produced, so a future schema edit
cannot silently change the contract. Server-state-dependent rules
(Agents-mode gating, model catalog resolution) stay in the route. Cases
cover: malformed bodies, key types/formats (including the historic
``INVALID_API_KEY`` vs ``UNSAFE_INPUT`` split), prompt/mode/agent/
preview/model/provider/URL/model-name shapes, cross-field precedence, and
the valid path (stripping, exact save kwargs, no-op, 200 through a mocked
save).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import app
from constants import (
    LANGSEARCH_API_KEY_MIN_LENGTH,
    MISTRAL_API_KEY_MIN_LENGTH,
    TAVILY_API_KEY_MIN_LENGTH,
)
from error_codes import ErrorCode
from schemas.config import CredentialsSaveRequest


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def _post(client, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload)
    return client.post(
        "/api/credentials",
        data=data,
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )


def _error(resp):
    body = resp.get_json()
    assert body is not None
    assert set(body) == {
        "ok",
        "error",
        "error_flag",
        "code",
        "error_code",
        "message",
        "details",
    }
    assert body["ok"] is False
    assert body["error_flag"] is True
    assert body["code"] == str(body["error_code"])
    return body


def _mock_server_state():
    """Neutralize server-state reads so gating tests are hermetic."""
    return (
        patch("credential_manager.get_api_mode", return_value="chat"),
        patch("credential_manager.get_agent_id", return_value=""),
    )


# ---------------------------------------------------------------------------
# Malformed body
# ---------------------------------------------------------------------------


def test_malformed_json(client):
    resp = _post(client, raw='{"mistral_api_key": "foo",')
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MALFORMED_INPUT)
    assert body["details"] == {"reason": "JSON形式が不正です"}


# ---------------------------------------------------------------------------
# Key types: the first offender in field order wins
# ---------------------------------------------------------------------------


def test_key_not_string(client):
    resp = _post(client, {"mistral_api_key": 123})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "fields": ["mistral_api_key"],
        "reason": "APIキーは文字列で指定してください",
    }


def test_key_type_loop_order(client):
    resp = _post(client, {"mistral_api_key": 1, "openai_api_key": 2})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["details"]["fields"] == ["mistral_api_key"]


def test_key_type_beats_key_format(client):
    resp = _post(client, {"mistral_api_key": "short", "openai_api_key": 5})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"]["fields"] == ["openai_api_key"]


# ---------------------------------------------------------------------------
# Key formats: historic INVALID_API_KEY vs UNSAFE_INPUT split
# ---------------------------------------------------------------------------


def test_mistral_key_too_short(client):
    resp = _post(client, {"mistral_api_key": "short"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_API_KEY)
    assert body["details"] == {
        "fields": ["mistral_api_key"],
        "min_length": MISTRAL_API_KEY_MIN_LENGTH,
    }


def test_openai_key_too_short(client):
    resp = _post(client, {"openai_api_key": "short"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert body["details"] == {"fields": ["openai_api_key"], "min_length": 8}


def test_openai_key_with_space(client):
    resp = _post(client, {"openai_api_key": "sk-key with space 1234"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert body["details"]["fields"] == ["openai_api_key"]


def test_langsearch_key_too_short(client):
    resp = _post(client, {"langsearch_api_key": "x" * 5})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert body["details"] == {
        "fields": ["langsearch_api_key"],
        "min_length": LANGSEARCH_API_KEY_MIN_LENGTH,
    }


def test_tavily_key_too_short(client):
    resp = _post(client, {"tavily_api_key": "ab"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert body["details"] == {
        "fields": ["tavily_api_key"],
        "min_length": TAVILY_API_KEY_MIN_LENGTH,
    }


def test_alphavantage_key_too_short(client):
    resp = _post(client, {"alphavantage_api_key": "abcde"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert body["details"] == {"fields": ["alphavantage_api_key"], "min_length": 10}


# ---------------------------------------------------------------------------
# Prompt / mode / agent / preview shapes
# ---------------------------------------------------------------------------


def test_prompt_not_string(client):
    resp = _post(client, {"custom_ai_prompt": 5})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {"reason": "custom_ai_promptは文字列で指定してください"}


def test_prompt_too_long(client):
    resp = _post(client, {"custom_ai_prompt": "x" * 5001})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert body["details"] == {
        "reason": "カスタムプロンプトは5000文字以内で入力してください"
    }


def test_prompt_too_long_does_not_save(client):
    with patch("routes.api_system.save_api_credentials") as mock_save:
        resp = _post(client, {"custom_ai_prompt": "x" * 5001})
    assert resp.status_code == 400
    mock_save.assert_not_called()


def test_mode_invalid(client):
    resp = _post(client, {"mistral_api_mode": "pigeons"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "mistral_api_modeは chat または agents を指定してください"
    }


def test_mode_not_string(client):
    resp = _post(client, {"mistral_api_mode": 5})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "mistral_api_modeは chat または agents を指定してください"
    }


def test_agent_not_string(client):
    resp = _post(client, {"mistral_agent_id": 5})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {"reason": "mistral_agent_idは文字列で指定してください"}


def test_agent_bad_format(client):
    resp = _post(client, {"mistral_agent_id": "!!!"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "fields": ["mistral_agent_id"],
        "reason": "Agent IDの形式が不正です",
    }


def test_preview_not_bool(client):
    resp = _post(client, {"preview_mode": "yes"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {"reason": "preview_modeは真偽値で指定してください"}


# ---------------------------------------------------------------------------
# Model / provider / URL / model-name shapes
# ---------------------------------------------------------------------------


def test_model_blank(client):
    resp = _post(client, {"mistral_model": "   "})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {"reason": "mistral_modelは有効な文字列で指定してください"}


def test_model_not_string(client):
    resp = _post(client, {"mistral_model": 5})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "mistral_modelは有効な文字列で指定してください"
    }


def test_provider_invalid(client):
    resp = _post(client, {"ai_provider": "anthropic"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "ai_providerは mistral または openai_compatible を指定してください"
    }


def test_url_not_string(client):
    resp = _post(client, {"openai_base_url": 5})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "openai_base_urlは文字列で指定してください"
    }


def test_url_bad_scheme(client):
    resp = _post(client, {"openai_base_url": "ftp://example.com/v1"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "fields": ["openai_base_url"],
        "reason": "URLは http:// または https:// で始まっている必要があります",
    }


def test_openai_model_not_string(client):
    resp = _post(client, {"openai_model": 5})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "openai_modelは文字列で指定してください"
    }


def test_openai_emb_model_not_string(client):
    resp = _post(client, {"openai_embeddings_model": 5})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "openai_embeddings_modelは文字列で指定してください"
    }


# ---------------------------------------------------------------------------
# Precedence: the first historic check wins
# ---------------------------------------------------------------------------


def test_bad_key_beats_bad_prompt(client):
    resp = _post(client, {"mistral_api_key": "short", "custom_ai_prompt": "x" * 5001})
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_API_KEY)


def test_bad_prompt_beats_bad_mode(client):
    resp = _post(
        client, {"custom_ai_prompt": "x" * 5001, "mistral_api_mode": "pigeons"}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert "カスタムプロンプト" in body["details"]["reason"]


def test_bad_agent_beats_bad_preview(client):
    resp = _post(client, {"mistral_agent_id": "!!!", "preview_mode": "yes"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["details"] == {
        "fields": ["mistral_agent_id"],
        "reason": "Agent IDの形式が不正です",
    }


def test_bad_model_shape_beats_bad_provider(client):
    resp = _post(client, {"mistral_model": "   ", "ai_provider": "anthropic"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "mistral_modelは有効な文字列で指定してください"
    }


# ---------------------------------------------------------------------------
# Server-state-dependent rules (stay in the route)
# ---------------------------------------------------------------------------


def test_agents_mode_requires_agent_id(client):
    mode_patch, agent_patch = _mock_server_state()
    with mode_patch, agent_patch:
        resp = _post(
            client,
            {
                "mistral_api_key": "a" * 40,
                "mistral_api_mode": "agents",
                "mistral_agent_id": "",
            },
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "fields": ["mistral_agent_id"],
        "reason": "Agents APIを使用するにはAgent IDが必要です",
    }


def test_agents_mode_blocks_model_selection(client):
    mode_patch, agent_patch = _mock_server_state()
    with mode_patch, agent_patch, patch(
        "credential_manager.get_api_mode", return_value="agents"
    ):
        resp = _post(client, {"mistral_model": "mistral-small-2603"})
    assert resp.status_code == 400
    assert "Agents APIではモデルはMistral側のコンソールで指定するため" in (
        _error(resp)["details"].get("reason", "")
    )


def test_unsupported_model(client):
    resp = _post(client, {"mistral_model": "no-such-model-xyz"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {"reason": "未対応のMistralモデルです: no-such-model-xyz"}


# ---------------------------------------------------------------------------
# Valid path: stripping, exact save kwargs, no-op
# ---------------------------------------------------------------------------


def test_valid_save_passes_exact_kwargs(client):
    with patch("routes.api_system.save_api_credentials") as mock_save:
        resp = _post(
            client,
            {"mistral_api_key": "a" * 40, "custom_ai_prompt": "prompt"},
        )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    mock_save.assert_called_once_with(
        mistral_api_key="a" * 40,
        openai_api_key=None,
        langsearch_api_key=None,
        tavily_api_key=None,
        alphavantage_api_key=None,
        custom_ai_prompt="prompt",
        update_custom_ai_prompt=True,
        mistral_model=None,
        api_mode=None,
        agent_id=None,
        preview_mode=None,
        ai_provider=None,
        openai_base_url=None,
        openai_model=None,
        openai_embeddings_model=None,
    )


def test_valid_save_strips_keys_and_url(client):
    with patch("routes.api_system.save_api_credentials") as mock_save:
        resp = _post(
            client,
            {
                "openai_api_key": "  sk-test-valid-openai-key-2026-xyz  ",
                "openai_base_url": "  https://example.com/v1/  ",
                "openai_model": "  gpt-4o-mini  ",
            },
        )
    assert resp.status_code == 200
    assert mock_save.call_args.kwargs["openai_api_key"] == "sk-test-valid-openai-key-2026-xyz"
    assert mock_save.call_args.kwargs["openai_base_url"] == "https://example.com/v1"
    assert mock_save.call_args.kwargs["openai_model"] == "gpt-4o-mini"


def test_valid_empty_body_is_noop(client):
    with patch("routes.api_system.save_api_credentials") as mock_save:
        resp = _post(client, {})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    mock_save.assert_not_called()


def test_schema_normalizes_programmatic_input():
    req = CredentialsSaveRequest.model_validate(
        {
            "mistral_api_key": "  " + "a" * 40 + "  ",
            "mistral_api_mode": "chat",
            "preview_mode": False,
        }
    )
    assert req.mistral_api_key == "a" * 40
    assert req.mistral_api_mode == "chat"
    assert req.preview_mode is False
    assert req.openai_api_key is None
