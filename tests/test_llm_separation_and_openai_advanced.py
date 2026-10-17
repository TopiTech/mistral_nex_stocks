"""
tests/test_llm_separation_and_openai_advanced.py - Unit tests for LLM provider separation
and advanced OpenAI-compatible enhancements (max_completion_tokens, temperature resilience,
thinking stream separation, circuit breaker separation, and connection pooling).
"""

from unittest.mock import MagicMock, patch

from app_state import app_state
from services.openai_service import (
    call_openai_chat,
    call_openai_chat_with_tools,
    stream_openai_chat,
)


def test_openai_reasoning_model_uses_max_completion_tokens():
    """o1/o3 reasoning models should use max_completion_tokens parameter key."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Analysis complete"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Analyze IONQ"}],
            max_tokens=500,
            _model_override="o3-mini",
            use_cache=False,
        )

    assert "choices" in result
    sent_payload = mock_post.call_args[1]["json"]
    assert "max_completion_tokens" in sent_payload
    assert "max_tokens" not in sent_payload
    assert sent_payload["max_completion_tokens"] == 500


def test_openai_400_retry_swaps_max_tokens_to_max_completion_tokens():
    """When a model rejects max_tokens with 400, it should retry with max_completion_tokens."""
    fail_resp = MagicMock()
    fail_resp.status_code = 400
    fail_resp.text = "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."
    fail_resp.json.return_value = {"error": {"message": fail_resp.text}}

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Recovered successfully"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 15},
    }

    with patch("httpx.Client.post", side_effect=[fail_resp, success_resp]) as mock_post:
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Test"}],
            max_tokens=400,
            _model_override="custom-model",
            use_cache=False,
        )

    assert "choices" in result
    assert result["choices"][0]["message"]["content"] == "Recovered successfully"
    assert mock_post.call_count == 2
    # Verify retry used max_completion_tokens
    retry_payload = mock_post.call_args_list[1][1]["json"]
    assert "max_completion_tokens" in retry_payload
    assert "max_tokens" not in retry_payload


def test_openai_400_retry_removes_unsupported_temperature():
    """When a reasoning model rejects temperature with 400, it should retry without temperature."""
    fail_resp = MagicMock()
    fail_resp.status_code = 400
    fail_resp.text = "Unsupported parameter: 'temperature' cannot be set for this model."
    fail_resp.json.return_value = {"error": {"message": fail_resp.text}}

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Temperature removed answer"}}],
    }

    with patch("httpx.Client.post", side_effect=[fail_resp, success_resp]) as mock_post:
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Test"}],
            temperature=0.7,
            _model_override="o1-preview",
            use_cache=False,
        )

    assert "choices" in result
    assert mock_post.call_count == 2
    retry_payload = mock_post.call_args_list[1][1]["json"]
    assert "temperature" not in retry_payload


def test_stream_openai_chat_separates_thinking_when_include_thinking_false():
    """When include_thinking=False, reasoning deltas must NOT be leaked into delta text events."""
    sse_lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"Step 1: internal reasoning"}}]}\n\n',
        'data: {"choices":[{"delta":{"reasoning_content":"Step 2: evaluating valuation"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"Final answer for user."}}]}\n\n',
        "data: [DONE]\n\n",
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.iter_lines.return_value = sse_lines

    with patch("httpx.Client.stream", return_value=mock_resp):
        events = list(
            stream_openai_chat(
                api_key="sk-test",
                messages=[{"role": "user", "content": "Analyze"}],
                include_thinking=False,
            )
        )

    deltas = [e["text"] for e in events if e.get("type") == "delta"]
    # Reasoning steps must NOT appear in delta text
    assert "Step 1: internal reasoning" not in deltas
    assert "Step 2: evaluating valuation" not in deltas
    assert "".join(deltas) == "Final answer for user."

    # Final concatenated text should be clean
    done_event = next(e for e in events if e.get("type") == "done")
    assert done_event["text"] == "Final answer for user."


def test_stream_openai_chat_emits_thinking_events_when_include_thinking_true():
    """When include_thinking=True, reasoning deltas should be yielded as type='thinking'."""
    sse_lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"Thinking token A"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"Answer token B"}}]}\n\n',
        "data: [DONE]\n\n",
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.iter_lines.return_value = sse_lines

    with patch("httpx.Client.stream", return_value=mock_resp):
        events = list(
            stream_openai_chat(
                api_key="sk-test",
                messages=[{"role": "user", "content": "Analyze"}],
                include_thinking=True,
            )
        )

    thinking_events = [e["text"] for e in events if e.get("type") == "thinking"]
    delta_events = [e["text"] for e in events if e.get("type") == "delta"]

    assert thinking_events == ["Thinking token A"]
    assert delta_events == ["Answer token B"]


def test_ai_state_provider_separated_usage_accounting():
    """Token usage must be accounted separately for Mistral and OpenAI."""
    # Reset stats
    with app_state.ai.mistral_usage_lock:
        app_state.ai.mistral_call_count = 0
        app_state.ai.mistral_total_prompt_tokens = 0
        app_state.ai.mistral_total_completion_tokens = 0
        app_state.ai.mistral_model_usage.clear()

    with app_state.ai.openai_usage_lock:
        app_state.ai.openai_call_count = 0
        app_state.ai.openai_total_prompt_tokens = 0
        app_state.ai.openai_total_completion_tokens = 0
        app_state.ai.openai_model_usage.clear()

    # Record Mistral call
    app_state.ai.record_ai_usage(
        {"prompt_tokens": 100, "completion_tokens": 50},
        model="mistral-small-latest",
        provider="mistral",
    )

    # Record OpenAI call
    app_state.ai.record_ai_usage(
        {"prompt_tokens": 200, "completion_tokens": 80},
        model="gpt-4o-mini",
        provider="openai_compatible",
    )

    stats = app_state.ai.ai_usage_stats()
    assert stats["mistral"]["call_count"] == 1
    assert stats["mistral"]["prompt_tokens"] == 100
    assert stats["mistral"]["completion_tokens"] == 50

    assert stats["openai_compatible"]["call_count"] == 1
    assert stats["openai_compatible"]["prompt_tokens"] == 200
    assert stats["openai_compatible"]["completion_tokens"] == 80


def test_openai_circuit_breaker_separation():
    """Circuit breaker for openai_compatible must be independent from mistral."""
    # Ensure circuits are closed
    app_state.market.report_circuit_result("openai_compatible", success=True)
    app_state.market.report_circuit_result("mistral", success=True)

    # Force trip openai circuit
    for _ in range(6):
        app_state.market.report_circuit_result("openai_compatible", success=False)

    assert app_state.market.is_circuit_open("openai_compatible") is True
    # Mistral circuit must remain closed
    assert app_state.market.is_circuit_open("mistral") is False

    # call_openai_chat should immediately 503 without network call
    res = call_openai_chat("sk-test", [{"role": "user", "content": "hi"}], use_cache=False)
    assert "error" in res
    assert res["error"]["status_code"] == 503

    # Reset circuit for subsequent tests
    app_state.market.report_circuit_result("openai_compatible", success=True)
    assert app_state.market.is_circuit_open("openai_compatible") is False


def test_call_openai_chat_with_tools_parallel_execution():
    """call_openai_chat_with_tools should execute multiple requested tool calls and return synthesis."""
    # Turn 1: Model requests two tool calls
    turn1_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_stock_quote",
                                "arguments": '{"symbol": "AAPL"}',
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "get_stock_quote",
                                "arguments": '{"symbol": "NVDA"}',
                            },
                        },
                    ],
                }
            }
        ]
    }

    # Turn 2: Final response
    turn2_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "AAPL and NVDA analysis complete.",
                }
            }
        ]
    }

    with (
        patch("services.openai_service.call_openai_chat", side_effect=[turn1_resp, turn2_resp]),
        patch(
            "services.openai_service.execute_mistral_tool_call",
            return_value={"symbol": "TEST", "price": 150.0},
        ) as mock_exec,
    ):
        result = call_openai_chat_with_tools(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Compare AAPL and NVDA"}],
        )

    assert "choices" in result
    assert result["choices"][0]["message"]["content"] == "AAPL and NVDA analysis complete."
    assert mock_exec.call_count == 2


def test_ai_state_close_all_clients():
    """close_all_clients should safely close all cached Mistral and OpenAI sessions."""
    mock_mistral = MagicMock()
    mock_openai = MagicMock()

    with app_state.ai.mistral_clients_lock:
        app_state.ai.mistral_clients["key1"] = mock_mistral

    with app_state.ai.openai_clients_lock:
        app_state.ai.openai_clients[("http://localhost", "key2")] = mock_openai

    app_state.ai.close_all_clients()

    mock_mistral.close.assert_called_once()
    mock_openai.close.assert_called_once()
    assert len(app_state.ai.mistral_clients) == 0
    assert len(app_state.ai.openai_clients) == 0
