"""
test_code_review_goal_audit_2026_10_v9.py - Tests for comprehensive code review fixes:
1. ExecutionState thread-safe registration and snapshot-based iteration during shutdown.
2. OpenAI service stream context cleanup helper and accurate status code propagation.
3. AI portfolio error response HTTPStatusError accurate status code mapping.
4. Mistral batch embeddings loop early break on 429 rate limit or open circuit.
"""

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from flask import Flask

from execution_state import ExecutionState
from routes.stocks.ai_portfolio import _ai_portfolio_error_response
from services.embeddings_service import get_mistral_embeddings_batch
from services.openai_service import _close_stream_context, stream_openai_chat


def test_execution_state_background_threads_safety():
    """Verify ExecutionState background threads registration and safe shutdown iteration."""
    exec_state = ExecutionState()

    t1 = threading.Thread(target=lambda: time.sleep(0.01), name="t1", daemon=True)
    t2 = threading.Thread(target=lambda: time.sleep(0.01), name="t2", daemon=True)

    exec_state.register_background_thread(t1)
    exec_state.register_background_thread(t2)
    # Deduplication
    exec_state.register_background_thread(t1)

    threads = exec_state.get_background_threads()
    assert len(threads) == 2
    assert t1 in threads
    assert t2 in threads

    # Verify modifying the returned list does not affect internal state
    threads.clear()
    assert len(exec_state.get_background_threads()) == 2

    # Simulate concurrent thread registration during shutdown
    t1.start()
    t2.start()

    def concurrent_adder():
        for i in range(10):
            t_extra = threading.Thread(target=lambda: None, name=f"extra_{i}", daemon=True)
            exec_state.register_background_thread(t_extra)
            time.sleep(0.002)

    adder_thread = threading.Thread(target=concurrent_adder, daemon=True)
    adder_thread.start()

    # shutdown should complete cleanly without RuntimeError (e.g. Set/list changed size during iteration)
    exec_state.shutdown(wait=False, max_join_timeout=0.2)
    adder_thread.join(timeout=1.0)
    assert exec_state.shutdown_event.is_set()


def test_close_stream_context_safety():
    """Verify _close_stream_context safely handles None, normal objects, and error-raising objects."""
    # Handles None arguments cleanly
    _close_stream_context(None, None)

    # Handles normal mocks
    mock_cm = MagicMock()
    mock_stream = MagicMock()
    _close_stream_context(mock_cm, mock_stream)
    mock_stream.close.assert_called_once()
    mock_cm.__exit__.assert_called_once_with(None, None, None)

    # Handles exceptions without propagating
    broken_stream = MagicMock()
    broken_stream.close.side_effect = RuntimeError("Broken stream close")
    broken_cm = MagicMock()
    broken_cm.__exit__.side_effect = AttributeError("Broken context exit")

    # Should not raise
    _close_stream_context(broken_cm, broken_stream)


def test_stream_openai_chat_error_status_code_propagation():
    """Verify stream_openai_chat extracts status codes and accurate public messages."""
    req = httpx.Request("POST", "http://localhost:5000/v1/chat/completions")
    resp_401 = httpx.Response(401, request=req)
    err_401 = httpx.HTTPStatusError("Unauthorized", request=req, response=resp_401)

    with patch("services.openai_service.get_openai_model", return_value="gpt-4o-mini"), \
         patch("services.openai_service._get_openai_client") as mock_client:
        mock_client.return_value.stream.side_effect = err_401

        events = list(stream_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "hello"}],
        ))

        assert len(events) == 1
        err_event = events[0]
        assert err_event.get("type") == "error"
        assert err_event.get("status_code") == 401
        assert "認証に失敗" in err_event.get("message", "")


def test_stream_openai_chat_timeout_status_code():
    """Verify stream_openai_chat maps TimeoutException to status code 504."""
    with patch("services.openai_service.get_openai_model", return_value="gpt-4o-mini"), \
         patch("services.openai_service._get_openai_client") as mock_client:
        mock_client.return_value.stream.side_effect = httpx.TimeoutException("Read timed out")

        events = list(stream_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "hello"}],
        ))

        assert len(events) == 1
        err_event = events[0]
        assert err_event.get("type") == "error"
        assert err_event.get("status_code") == 504
        assert "タイムアウト" in err_event.get("message", "")


def test_ai_portfolio_error_response_http_status_error():
    """Verify _ai_portfolio_error_response accurately returns HTTP status code for HTTPStatusError."""
    app = Flask(__name__)
    req = httpx.Request("POST", "http://localhost:5000/api/ai-portfolio")
    resp_401 = httpx.Response(401, request=req, json={"error": "Invalid API key"})
    err_401 = httpx.HTTPStatusError("Unauthorized", request=req, response=resp_401)

    mock_g = MagicMock()
    mock_g.request_id = "test-req-401"

    with app.test_request_context():
        response_obj, status = _ai_portfolio_error_response(
            err_401,
            mock_g,
            default_reason="AI portfolio error",
        )
        assert status == 401
        data = response_obj.get_json()
        assert "401" in str(data.get("details", {}).get("reason", ""))


def test_embeddings_service_early_break_on_429():
    """Verify get_mistral_embeddings_batch stops processing remaining chunks on 429."""
    class MockRateLimitError(RuntimeError):
        status_code = 429
        response: Any = None

    client = MagicMock()
    resp_obj = MagicMock()
    resp_obj.status_code = 429
    exc_429 = MockRateLimitError("Rate limited")
    exc_429.response = resp_obj

    client.embeddings.create.side_effect = exc_429

    with patch("services.embeddings_service._get_client", return_value=client), \
         patch("credential_manager.is_openai_provider", return_value=False), \
         patch("app_state.app_state.market.is_circuit_open", return_value=False), \
         patch("services.ai_service._acquire_mistral_call_slot", return_value=0.0), \
         patch("services.ai_service._wait_for_rate_limit_slot", return_value=False), \
         patch("app_state.app_state.ai.mark_mistral_429") as mock_mark:

        texts = [f"article_{i}" for i in range(10)]
        results = get_mistral_embeddings_batch(texts, api_key="sk-test", batch_size=2)

        # batch_size=2 with 10 items means 5 chunks.
        # It should break after chunk 1, so client.embeddings.create is called only once!
        assert client.embeddings.create.call_count == 1
        mock_mark.assert_called_once()
        assert len(results) == 10
        assert all(r is None for r in results)
