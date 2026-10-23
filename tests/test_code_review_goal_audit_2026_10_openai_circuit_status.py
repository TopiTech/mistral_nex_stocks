"""Unit tests verifying OpenAI-compatible circuit breaker reporting rules.

Ensures:
- 4xx client errors (400, 401, 404) do NOT trip the circuit breaker in call_openai_chat or stream_openai_chat.
- 5xx server errors, network errors, and timeouts DO trip the circuit breaker.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app_state import app_state
from services.openai_service import call_openai_chat, stream_openai_chat
from tests.test_support.state_reset import reset_app_state_internals


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    reset_app_state_internals()


def test_call_openai_chat_404_does_not_trip_circuit():
    """A 404 (e.g. model not found) should return error without reporting circuit failure."""
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(404, request=req, json={"error": {"message": "The model `foo` does not exist"}})
    err = httpx.HTTPStatusError("Not Found", request=req, response=resp)

    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.side_effect = err
        mock_get_client.return_value = client_mock

        result = call_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False)

        assert "error" in result
        # Circuit report should NOT have been called with success=False
        for call_args in mock_report.call_args_list:
            if call_args[0] == ("openai_compatible",):
                assert call_args[1].get("success") is not False


def test_call_openai_chat_500_trips_circuit():
    """A 500 server error should report circuit failure."""
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(500, request=req, json={"error": {"message": "Internal Server Error"}})
    err = httpx.HTTPStatusError("Server Error", request=req, response=resp)

    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.side_effect = err
        mock_get_client.return_value = client_mock

        result = call_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False)

        assert "error" in result
        mock_report.assert_any_call("openai_compatible", success=False)


def test_call_openai_chat_network_error_trips_circuit():
    """A network connection failure should report circuit failure."""
    err = httpx.ConnectError("Connection refused")

    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.side_effect = err
        mock_get_client.return_value = client_mock

        result = call_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False)

        assert "error" in result
        mock_report.assert_any_call("openai_compatible", success=False)


def test_stream_openai_chat_400_does_not_trip_circuit():
    """A 400 Bad Request exception should not trip the circuit breaker during streaming."""
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(400, request=req, json={"error": {"message": "Invalid temperature"}})
    err = httpx.HTTPStatusError("Bad Request", request=req, response=resp)

    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.stream.side_effect = err
        mock_get_client.return_value = client_mock

        chunks = list(stream_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}]))

        assert any(c.get("type") == "error" for c in chunks)
        for call_args in mock_report.call_args_list:
            if call_args[0] == ("openai_compatible",):
                assert call_args[1].get("success") is not False


def test_stream_openai_chat_timeout_trips_circuit():
    """A timeout exception during streaming should trip the circuit breaker."""
    err = httpx.ReadTimeout("Read timed out")

    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.stream.side_effect = err
        mock_get_client.return_value = client_mock

        chunks = list(stream_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}]))

        assert any(c.get("type") == "error" for c in chunks)
        mock_report.assert_any_call("openai_compatible", success=False)
