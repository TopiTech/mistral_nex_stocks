"""U14 second-pass regression tests.

Covers:
- K-10: OpenAI circuit-breaker classification. Only provider-side conditions
  (communication failures: timeouts / transport errors; explicit 5xx
  responses) may report circuit failures. Application bugs (exceptions
  without an HTTP response raised while building the payload or
  post-processing the response) must NOT open the circuit, otherwise three
  consecutive application bugs would disable every OpenAI feature for the
  full circuit window even though the provider is healthy.
- K-06: the scraper market-state fallback ref is cleared as soon as the live
  ``app_state.market`` is gone, so a discarded state object can never leak
  back in, while the interpreter-teardown fallback (failed import) is kept.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app_state import app_state
from services.openai_service import call_openai_chat, stream_openai_chat
from services.realtime import utils as rt_utils
from tests.test_support.state_reset import reset_app_state_internals


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    reset_app_state_internals()


_CHAT_URL = "https://api.openai.com/v1/chat/completions"


def _status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", _CHAT_URL)
    resp = httpx.Response(
        status, request=req, json={"error": {"message": f"HTTP {status}"}}
    )
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


# ---------------------------------------------------------------------------
# K-10: call_openai_chat circuit classification
# ---------------------------------------------------------------------------


def test_call_openai_chat_application_bug_does_not_trip_circuit():
    """A non-communication exception (no HTTP response) must not open the circuit."""
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.side_effect = TypeError("boom: payload bug")
        mock_get_client.return_value = client_mock

        result = call_openai_chat(
            "fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False
        )

        assert "error" in result
        # The error envelope still surfaces a 500 for the caller.
        assert result["error"]["status_code"] == 500
        for call_args in mock_report.call_args_list:
            assert call_args[1].get("success") is not False


def test_call_openai_chat_invalid_json_body_does_not_trip_circuit():
    """A 200 response with a garbage body must not open the circuit either."""
    req = httpx.Request("POST", _CHAT_URL)
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.return_value = httpx.Response(200, request=req, text="{not-json")
        mock_get_client.return_value = client_mock

        result = call_openai_chat(
            "fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False
        )

        assert "error" in result
        for call_args in mock_report.call_args_list:
            assert call_args[1].get("success") is not False


def test_call_openai_chat_timeout_trips_circuit():
    """Timeouts are communication failures and must open the circuit (504)."""
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.side_effect = httpx.ConnectTimeout("timed out")
        mock_get_client.return_value = client_mock

        result = call_openai_chat(
            "fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False
        )

        assert "error" in result
        assert result["error"]["status_code"] == 504
        mock_report.assert_any_call("openai_compatible", success=False)


def test_call_openai_chat_5xx_status_error_trips_circuit():
    """An explicit 5xx response must open the circuit with its real status."""
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.side_effect = _status_error(503)
        mock_get_client.return_value = client_mock

        result = call_openai_chat(
            "fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False
        )

        assert "error" in result
        assert result["error"]["status_code"] == 503
        mock_report.assert_any_call("openai_compatible", success=False)


def test_call_openai_chat_4xx_status_error_does_not_trip_circuit():
    """4xx responses with a response object must keep not tripping the circuit."""
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.post.side_effect = _status_error(401)
        mock_get_client.return_value = client_mock

        result = call_openai_chat(
            "fake-sk-key-12345678", [{"role": "user", "content": "hi"}], use_cache=False
        )

        assert "error" in result
        assert result["error"]["status_code"] == 401
        for call_args in mock_report.call_args_list:
            assert call_args[1].get("success") is not False


# ---------------------------------------------------------------------------
# K-10: stream_openai_chat circuit classification
# ---------------------------------------------------------------------------


def test_stream_openai_chat_application_bug_does_not_trip_circuit():
    """A non-communication exception during streaming must not open the circuit."""
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.stream.side_effect = KeyError("boom: stream setup bug")
        mock_get_client.return_value = client_mock

        chunks = list(
            stream_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}])
        )

        assert any(c.get("type") == "error" for c in chunks)
        for call_args in mock_report.call_args_list:
            assert call_args[1].get("success") is not False


def test_stream_openai_chat_5xx_status_error_trips_circuit():
    """An explicit 5xx response during streaming must open the circuit."""
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.stream.side_effect = _status_error(502)
        mock_get_client.return_value = client_mock

        chunks = list(
            stream_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}])
        )

        assert any(c.get("type") == "error" for c in chunks)
        mock_report.assert_any_call("openai_compatible", success=False)


def test_stream_openai_chat_timeout_trips_circuit():
    """Timeouts during streaming keep tripping the circuit."""
    with patch("services.openai_service._get_openai_client") as mock_get_client, \
         patch.object(app_state.market, "report_circuit_result") as mock_report:
        client_mock = MagicMock()
        client_mock.stream.side_effect = httpx.ReadTimeout("read timed out")
        mock_get_client.return_value = client_mock

        chunks = list(
            stream_openai_chat("fake-sk-key-12345678", [{"role": "user", "content": "hi"}])
        )

        assert any(c.get("type") == "error" for c in chunks)
        mock_report.assert_any_call("openai_compatible", success=False)


# ---------------------------------------------------------------------------
# K-06: scraper market-state fallback ref lifecycle
# ---------------------------------------------------------------------------


def test_scraper_market_state_clears_stale_ref_when_market_none():
    """When the live state is gone, the cached ref must be dropped, not reused."""
    stale_market = MagicMock()
    rt_utils._scraper_market_ref = stale_market
    original_market = getattr(app_state, "market", None)
    app_state.market = None
    try:
        assert rt_utils._scraper_market_state() is None
        assert rt_utils._scraper_market_ref is None
        assert rt_utils._is_scraper_blocked() is False
    finally:
        app_state.market = original_market


def test_scraper_market_state_import_failure_keeps_fallback_ref():
    """A failed app_state import (teardown) still falls back to the cached ref."""
    fallback_market = MagicMock()
    rt_utils._scraper_market_ref = fallback_market
    # Capture the real module object up front: while ``None`` sits in
    # sys.modules, any ``import app_state`` raises, so restoration must be a
    # pure dict assignment.
    real_module = sys.modules["app_state"]
    try:
        # None in sys.modules makes ``from app_state import app_state`` raise
        # ImportError, simulating interpreter teardown.
        sys.modules["app_state"] = None
        assert rt_utils._scraper_market_state() is fallback_market
    finally:
        sys.modules["app_state"] = real_module


def test_scraper_market_state_refreshes_ref_on_success():
    """A successful import always wins over any stale cached ref."""
    stale_market = MagicMock()
    fresh_market = MagicMock()
    rt_utils._scraper_market_ref = stale_market
    original_market = getattr(app_state, "market", None)
    app_state.market = fresh_market
    try:
        assert rt_utils._scraper_market_state() is fresh_market
        assert rt_utils._scraper_market_ref is fresh_market
    finally:
        app_state.market = original_market
