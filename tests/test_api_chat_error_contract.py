"""Contract test: /api/chat error response formatting and envelope conformity.

Verifies that _chat_error_response conforms strictly to the pinned chat error contract:
- ok: False, success: False, error_flag: True
- code: status_code (int)
- error_code: f"ERR_CHAT_{status_code}" (str)
- error, reply, message equal
- details containing reason and error_type
- disclaimer and request_id present
- request_token present only when operation_token is supplied
"""

from __future__ import annotations

import httpx
import requests
from flask import g

from app import app
from routes.api_analysis import _chat_error_response


def test_chat_error_response_auth_failure():
    """Auth-related errors return 401 with ERR_CHAT_401."""
    with app.test_request_context("/api/chat"):
        g.request_id = "req-test-auth-001"
        resp, status = _chat_error_response(
            Exception("401 Unauthorized: Invalid API key"), g, operation_token="op-tok-123"
        )
        assert status == 401
        data = resp.get_json()
        assert data is not None

        # Envelope verification
        assert data["ok"] is False
        assert data["success"] is False
        assert data["error_flag"] is True
        assert data["error_code"] == "ERR_CHAT_401"
        assert data["code"] == 401
        assert "details" in data
        assert isinstance(data["details"], dict)
        assert data["details"]["error_type"] == "Exception"
        assert "reason" in data["details"]

        # Backwards compatibility fields
        assert data["reply"] == data["message"] == data["error"]
        assert "disclaimer" in data
        assert data["request_id"] == "req-test-auth-001"
        assert data["request_token"] == "op-tok-123"


def test_chat_error_response_network_failure():
    """Network connection errors return 503 with ERR_CHAT_503."""
    with app.test_request_context("/api/chat"):
        g.request_id = "req-test-net-002"
        resp, status = _chat_error_response(
            requests.ConnectionError("Failed to establish connection"), g
        )
        assert status == 503
        data = resp.get_json()
        assert data is not None

        assert data["ok"] is False
        assert data["error_code"] == "ERR_CHAT_503"
        assert data["code"] == 503
        assert data["details"]["error_type"] == "ConnectionError"
        assert "接続できませんでした" in data["message"]
        assert data["reply"] == data["message"]
        assert "request_token" not in data


def test_chat_error_response_timeout_failure():
    """Timeout exceptions return 504 with ERR_CHAT_504."""
    with app.test_request_context("/api/chat"):
        g.request_id = "req-test-timeout-003"
        resp, status = _chat_error_response(
            httpx.TimeoutException("Read timed out after 30s"), g
        )
        assert status == 504
        data = resp.get_json()
        assert data is not None

        assert data["ok"] is False
        assert data["error_code"] == "ERR_CHAT_504"
        assert data["code"] == 504
        assert "タイムアウト" in data["message"]
        assert data["reply"] == data["message"]


def test_chat_error_response_value_error():
    """Invalid input/type errors return 400 with ERR_CHAT_400."""
    with app.test_request_context("/api/chat"):
        g.request_id = "req-test-val-004"
        resp, status = _chat_error_response(
            ValueError("Unsupported parameter value"), g
        )
        assert status == 400
        data = resp.get_json()
        assert data is not None

        assert data["ok"] is False
        assert data["error_code"] == "ERR_CHAT_400"
        assert data["code"] == 400
        assert data["details"]["error_type"] == "ValueError"


def test_chat_error_response_generic_exception():
    """Unexpected runtime exceptions return 500 with ERR_CHAT_500."""
    with app.test_request_context("/api/chat"):
        g.request_id = "req-test-sys-005"
        resp, status = _chat_error_response(
            RuntimeError("Unexpected LLM internal fault"), g
        )
        assert status == 500
        data = resp.get_json()
        assert data is not None

        assert data["ok"] is False
        assert data["error_code"] == "ERR_CHAT_500"
        assert data["code"] == 500
        assert data["details"]["error_type"] == "RuntimeError"
