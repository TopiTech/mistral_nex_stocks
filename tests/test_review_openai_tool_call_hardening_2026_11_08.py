"""Regression tests for the OpenAI agent-loop tool-call boundary.

``call_openai_chat_with_tools`` echoed the provider's ``tool_calls`` entries
into the assistant message and then consumed ``id``/``function`` without any
type validation.  A provider (or an OpenAI-compatible proxy) that returned a
numeric ``id`` produced ``TypeError: object of type 'int' has no len()`` and a
non-object ``function`` produced ``AttributeError`` -- both escaped the agent
loop as an unhandled 500 instead of the typed 502 envelope used by the Mistral
twin.  Tool execution exceptions had the same problem: the Mistral loop
isolates them per tool call, the OpenAI loop did not.

The tests drive the real ``call_openai_chat_with_tools`` with a stubbed
``call_openai_chat`` and assert the assistant turn and the tool results keep a
consistent, string-typed tool_call_id.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from services.openai_service import call_openai_chat_with_tools

pytestmark = pytest.mark.unit


def _tool_turn(tool_calls):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                }
            }
        ]
    }


_FINAL_TURN = {
    "choices": [{"message": {"role": "assistant", "content": "final answer"}}]
}


def _user_messages():
    return [{"role": "user", "content": "check AAPL"}]


def _run_agent_loop(turn1, tool_exec):
    """Run two model turns and return ``(result, captured_requests)``."""
    captured: list[list[dict]] = []

    def _stub_chat(api_key, messages, **kwargs):
        captured.append(list(messages))
        return turn1 if len(captured) == 1 else _FINAL_TURN

    with (
        patch(
            "services.openai_service.call_openai_chat", side_effect=_stub_chat
        ) as mock_chat,
        patch(
            "services.openai_service.execute_mistral_tool_call", tool_exec
        ) as mock_exec,
    ):
        result = call_openai_chat_with_tools("sk-test", _user_messages())

    assert mock_chat.call_count == len(captured)
    return result, captured, mock_exec


def _assistant_tool_calls(messages):
    return [
        m
        for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]


def _tool_messages(messages):
    return [m for m in messages if m.get("role") == "tool"]


def test_non_string_tool_call_id_is_normalized_instead_of_raising():
    turn1 = _tool_turn(
        [
            {
                "id": 123,
                "type": "function",
                "function": {
                    "name": "get_stock_quote",
                    "arguments": json.dumps({"symbol": "AAPL"}),
                },
            }
        ]
    )

    result, captured, mock_exec = _run_agent_loop(
        turn1, MagicMock(return_value={"symbol": "AAPL", "price": 100.0})
    )

    assert result["choices"][0]["message"]["content"] == "final answer"
    assert "error" not in result
    mock_exec.assert_called_once()

    second_turn_messages = captured[1]
    assistant_msg = _assistant_tool_calls(second_turn_messages)[0]
    tool_msg = _tool_messages(second_turn_messages)[0]
    tc_id = assistant_msg["tool_calls"][0]["id"]
    assert isinstance(tc_id, str) and tc_id
    assert tool_msg["tool_call_id"] == tc_id


def test_non_dict_function_is_dropped_and_returns_typed_502():
    turn1 = _tool_turn([{"id": "call_1", "function": "not-an-object"}])

    result, captured, mock_exec = _run_agent_loop(turn1, MagicMock(return_value={}))

    assert result["error"]["status_code"] == 502
    mock_exec.assert_not_called()
    # No follow-up model call is made once the turn has no usable tool call.
    assert len(captured) == 1


def test_mixed_malformed_and_valid_tool_calls_keep_request_pairing():
    turn1 = _tool_turn(
        [
            {"id": "call_bad", "function": None},
            {
                "id": "call_good",
                "type": "function",
                "function": {
                    "name": "get_stock_quote",
                    "arguments": json.dumps({"symbol": "MSFT"}),
                },
            },
        ]
    )

    result, captured, mock_exec = _run_agent_loop(
        turn1, MagicMock(return_value={"symbol": "MSFT"})
    )

    assert result["choices"][0]["message"]["content"] == "final answer"
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0] == "get_stock_quote"

    second_turn_messages = captured[1]
    assistant_msg = _assistant_tool_calls(second_turn_messages)[0]
    tool_msgs = _tool_messages(second_turn_messages)
    assert len(assistant_msg["tool_calls"]) == 1
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == assistant_msg["tool_calls"][0]["id"]


def test_tool_execution_exception_is_isolated_per_call():
    turn1 = _tool_turn(
        [
            {
                "id": "call_boom",
                "type": "function",
                "function": {"name": "get_stock_quote", "arguments": "{}"},
            }
        ]
    )

    result, captured, _ = _run_agent_loop(
        turn1, MagicMock(side_effect=RuntimeError("provider exploded"))
    )

    assert result["choices"][0]["message"]["content"] == "final answer"
    tool_msg = _tool_messages(captured[1])[0]
    assert json.loads(tool_msg["content"])["error"] == "ツールの実行に失敗しました"
    assert "provider exploded" not in tool_msg["content"]
