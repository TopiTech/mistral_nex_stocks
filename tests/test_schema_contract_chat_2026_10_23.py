"""HTTP contract tests for the canonical chat request schema (U1/chat slice).

``POST /api/chat`` now validates only through
``schemas.analysis.AIChatRequest`` (via ``route_helpers._parse_chat_request``).
These tests pin the historic error envelope so a future schema edit cannot
silently change an error code, message, or status code.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import app
from app_state import app_state
from constants import CHAT_MAX_MSG_LENGTH
from error_codes import ErrorCode
from schemas.analysis import AIChatRequest

TOKEN = "a" * 24


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _api_key():
    # Validation runs after the API-key gate; present a test key so error
    # contracts (not auth) are exercised. Success-path behavior is covered
    # by existing provider-mocked tests.
    with patch(
        "routes.api_analysis.extract_api_key", return_value="test-key-32-chars"
    ):
        yield


@pytest.fixture(autouse=True)
def reset_chat_state():
    with app_state.ai.chat_history_lock:
        app_state.ai.chat_history.clear()
    from routes.api_analysis import chat_fetch_inflight, chat_result_cache

    chat_result_cache.clear()
    chat_fetch_inflight.clear()
    yield
    with app_state.ai.chat_history_lock:
        app_state.ai.chat_history.clear()
    chat_result_cache.clear()
    chat_fetch_inflight.clear()
    app_state.ai.chat_history.close()


def _post(client, payload, raw=None):
    if raw is not None:
        return client.post(
            "/api/chat",
            data=raw,
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
    return client.post(
        "/api/chat",
        data=json.dumps(payload),
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
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
    assert body["error"] == body["message"]
    return body


def _valid(symbol="AAPL", market="us", message="Hello", token=TOKEN):
    return {"symbol": symbol, "market": market, "message": message, "request_token": token}


def test_missing_symbol_and_message_reports_both_fields(client):
    resp = _post(client, {"market": "us", "request_token": TOKEN})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol", "message"]}


def test_blank_symbol_alone_still_reports_both_fields(client):
    # Historic behavior: the presence check reports [symbol, message] even
    # when only one of them is blank. Preserved byte-identical.
    resp = _post(client, _valid(symbol="   "))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol", "message"]}


def test_blank_message_alone_still_reports_both_fields(client):
    resp = _post(client, _valid(message="   "))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol", "message"]}


def test_non_string_market_is_invalid_market(client):
    resp = _post(client, _valid(market=5))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_MARKET


def test_unknown_market_string_is_invalid_market(client):
    resp = _post(client, _valid(market="xx"))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_MARKET


def test_non_string_symbol_is_invalid_symbol(client):
    resp = _post(client, _valid(symbol=123))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_symbol_with_bad_characters_is_invalid_symbol(client):
    resp = _post(client, _valid(symbol="AAPL!"))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_path_traversal_symbol_is_invalid_symbol(client):
    resp = _post(client, _valid(symbol="../x"))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_over_long_symbol_is_invalid_symbol(client):
    resp = _post(client, _valid(symbol="A" * 21))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_non_string_message_keeps_legacy_message(client):
    resp = _post(client, _valid(message=123))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {"reason": "message must be a string", "fields": ["message"]}


def test_over_long_message_keeps_japanese_message(client):
    resp = _post(client, _valid(message="x" * (CHAT_MAX_MSG_LENGTH + 1)))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": f"メッセージは{CHAT_MAX_MSG_LENGTH}文字以内で入力してください。"
    }


def test_short_request_token_keeps_legacy_message(client):
    resp = _post(client, _valid(token="short"))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "request_token must be a 16-128 character URL-safe token"
    }


def test_missing_request_token_keeps_legacy_message(client):
    payload = _valid()
    del payload["request_token"]
    resp = _post(client, payload)
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "request_token must be a 16-128 character URL-safe token"
    }


def test_non_string_request_token_keeps_legacy_message(client):
    resp = _post(client, _valid(token=12345))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "request_token must be a 16-128 character URL-safe token"
    }


def test_malformed_json_body_is_rejected(client):
    resp = _post(client, None, raw="{not json")
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT


def test_blank_message_beats_invalid_symbol_shape(client):
    # Historic order: the combined presence check runs before the symbol-shape
    # check, so this must stay MISSING, not INVALID_SYMBOL.
    resp = _post(client, _valid(symbol="AAPL!", message="  "))
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol", "message"]}


def test_over_long_message_beats_invalid_market(client):
    # Historic order: the length check runs before the market check.
    resp = _post(
        client,
        _valid(market="xx", message="y" * (CHAT_MAX_MSG_LENGTH + 1)),
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": f"メッセージは{CHAT_MAX_MSG_LENGTH}文字以内で入力してください。"
    }


def test_schema_normalizes_jp_and_fullwidth_symbols():
    assert (
        AIChatRequest.model_validate(
            {"symbol": "7203", "market": "jp", "message": "hi", "request_token": TOKEN}
        ).symbol
        == "7203.T"
    )
    assert (
        AIChatRequest.model_validate(
            {"symbol": "ＡＡＰＬ", "market": "us", "message": "hi", "request_token": TOKEN}
        ).symbol
        == "AAPL"
    )
    assert (
        AIChatRequest.model_validate(
            {"symbol": "aapl", "market": "us", "message": "  hi  ", "request_token": TOKEN}
        ).message
        == "hi"
    )


def test_valid_chat_body_reaches_provider_with_normalized_symbol(client):
    with (
        patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"),
        patch(
            "routes.api_analysis.get_stock_info_cached",
            return_value={"regularMarketPreviousClose": 150.0},
        ),
        patch(
            "routes.api_analysis._call_mistral_chat_with_retry",
            return_value="Mocked AI Response",
        ) as mock_chat,
    ):
        resp = _post(client, _valid(symbol="aapl", message="What is the price?"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["reply"] == "Mocked AI Response"
    assert mock_chat.call_count == 1
