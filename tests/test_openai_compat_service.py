"""
Unit tests for services/openai_service.py (OpenAI-compatible REST API engine).
"""

import json
from unittest.mock import MagicMock, patch

from pydantic import BaseModel, Field

from credential_manager import PREVIEW_API_KEY
from services.openai_service import (
    analyze_chart_image_with_openai,
    call_openai_chat,
    call_openai_chat_with_tools,
    get_openai_embeddings_batch,
    stream_openai_chat,
    verify_openai_connection,
)


class SampleSchema(BaseModel):
    summary: str = Field(description="Summary text")
    score: int = Field(default=5)


def test_verify_openai_connection_success():
    """Test verify_openai_connection with valid endpoint and model list."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": "gpt-4o-mini"},
            {"id": "gpt-4o"},
            {"id": "text-embedding-3-small"},
        ]
    }

    with patch("httpx.Client.get", return_value=mock_resp):
        res = verify_openai_connection(
            base_url="https://api.openai.com/v1",
            api_key="sk-test-key",
            timeout_sec=5.0,
        )

    assert res["ok"] is True
    assert "gpt-4o-mini" in res["models"]
    assert "gpt-4o" in res["models"]
    assert res["model_count"] == 3
    assert res["latency_ms"] >= 0


def test_verify_openai_connection_auth_error():
    """Test verify_openai_connection handles 401 Unauthorized."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = json.dumps({"error": {"message": "Incorrect API key provided"}})
    mock_resp.json.return_value = {"error": {"message": "Incorrect API key provided"}}

    with patch("httpx.Client.get", return_value=mock_resp):
        res = verify_openai_connection(
            base_url="https://api.openai.com/v1",
            api_key="sk-invalid",
            timeout_sec=5.0,
        )

    assert res["ok"] is False
    assert "認証エラー" in res["error"]


def test_verify_openai_connection_openrouter_auth_failure():
    """Test verify_openai_connection catches 401 on /auth/key even when /models returns 200."""
    mock_models_resp = MagicMock()
    mock_models_resp.status_code = 200
    mock_models_resp.json.return_value = {"data": [{"id": "meta-llama/llama-3-8b"}]}

    mock_auth_resp = MagicMock()
    mock_auth_resp.status_code = 401
    mock_auth_resp.json.return_value = {"error": {"message": "Missing Authentication header"}}

    def mock_get(url, **kwargs):
        if url.endswith("/models"):
            return mock_models_resp
        return mock_auth_resp

    with patch("httpx.Client.get", side_effect=mock_get):
        res = verify_openai_connection(
            base_url="https://openrouter.ai/api/v1",
            api_key="invalid_or_wrong_key",
            timeout_sec=5.0,
        )

    assert res["ok"] is False
    assert "認証エラー" in res["error"]
    assert "401" in res["error"]


def test_verify_openai_connection_openrouter_success():
    """Test verify_openai_connection succeeds when both /models and /auth/key succeed."""
    mock_models_resp = MagicMock()
    mock_models_resp.status_code = 200
    mock_models_resp.json.return_value = {"data": [{"id": "meta-llama/llama-3-8b"}]}

    mock_auth_resp = MagicMock()
    mock_auth_resp.status_code = 200
    mock_auth_resp.json.return_value = {"data": {"label": "valid key"}}

    def mock_get(url, **kwargs):
        if url.endswith("/models"):
            return mock_models_resp
        return mock_auth_resp

    with patch("httpx.Client.get", side_effect=mock_get):
        res = verify_openai_connection(
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-v1-valid-key",
            timeout_sec=5.0,
        )

    assert res["ok"] is True
    assert "meta-llama/llama-3-8b" in res["models"]


def test_stream_openai_chat_401_error():
    """Test stream_openai_chat yields structured error on HTTP 401."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = json.dumps({"error": {"message": "Missing Authentication header"}})
    mock_resp.__enter__.return_value = mock_resp

    with patch("httpx.Client.stream", return_value=mock_resp):
        events = list(
            stream_openai_chat(
                api_key="invalid-key",
                messages=[{"role": "user", "content": "hi"}],
            )
        )

    assert len(events) == 1
    err = events[0]
    assert err["type"] == "error"
    assert err["status_code"] == 401
    assert "認証に失敗" in err["message"]
    assert "Missing Authentication header" in err["detail"]


def test_call_openai_chat_plain_text():
    """Test call_openai_chat returns OpenAI-style response dictionary."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Market analysis is bullish.",
                }
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 10, "total_tokens": 25},
    }

    with patch("httpx.Client.post", return_value=mock_resp), patch(
        "services.openai_service.app_state.ai.record_mistral_usage"
    ) as mock_record:
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "How is the market?"}],
            use_cache=False,
        )

    assert "choices" in result
    msg = result["choices"][0]["message"]
    assert msg["content"] == "Market analysis is bullish."
    mock_record.assert_called_once()


def test_call_openai_chat_with_pydantic_schema():
    """Test call_openai_chat with Pydantic response_format validates output into parsed."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"summary": "Tech rally", "score": 8}),
                }
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Analyze rally"}],
            response_format=SampleSchema,
            use_cache=False,
        )

    assert "choices" in result
    msg = result["choices"][0]["message"]
    assert "parsed" in msg
    assert msg["parsed"]["summary"] == "Tech rally"
    assert msg["parsed"]["score"] == 8


def test_call_openai_chat_preview_key():
    """Test call_openai_chat with preview API key returns preview mock response."""
    result = call_openai_chat(
        api_key=PREVIEW_API_KEY,
        messages=[{"role": "user", "content": "Analyze market"}],
    )
    assert "choices" in result
    assert result["choices"][0]["message"]["role"] == "assistant"


def test_stream_openai_chat():
    """Test stream_openai_chat properly parses SSE delta events and returns done."""
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"Apple"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" is growing."}}]}\n\n',
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
                messages=[{"role": "user", "content": "AAPL status"}],
            )
        )

    deltas = [e["text"] for e in events if e.get("type") == "delta"]
    assert "".join(deltas) == "Apple is growing."

    done_event = [e for e in events if e.get("type") == "done"]
    assert len(done_event) == 1
    assert done_event[0]["text"] == "Apple is growing."


def test_stream_openai_chat_reasoning_content():
    """Test stream_openai_chat handles reasoning_content (DeepSeek-R1 / o1)."""
    sse_lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"Thinking deeply..."}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"Result is 42."}}]}\n\n',
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
                messages=[{"role": "user", "content": "What is 6x7?"}],
            )
        )

    deltas = [e["text"] for e in events if e.get("type") == "delta"]
    assert "Thinking deeply..." in deltas
    assert "Result is 42." in deltas


def test_call_openai_chat_with_tools_loop():
    """Test multi-turn function calling loop executes tools and returns final response."""
    # Round 1: Model calls tool
    resp1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "search_financial_news",
                                "arguments": json.dumps({"query": "NVDA news"}),
                            },
                        }
                    ],
                }
            }
        ]
    }

    # Round 2: Model returns final content after tool output
    resp2 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "NVDA announced record earnings.",
                }
            }
        ]
    }

    mock_tool_exec = MagicMock(return_value="[Search Results: NVDA revenue +200%]")

    with patch("services.openai_service.call_openai_chat", side_effect=[resp1, resp2]), patch(
        "services.openai_service.execute_mistral_tool_call", mock_tool_exec
    ):
        result = call_openai_chat_with_tools(
            api_key="sk-test",
            messages=[{"role": "user", "content": "What are NVDA news?"}],
        )

    assert result["choices"][0]["message"]["content"] == "NVDA announced record earnings."
    mock_tool_exec.assert_called_once()
    assert mock_tool_exec.call_args[0][0] == "search_financial_news"


def test_analyze_chart_image_with_openai():
    """Test analyze_chart_image_with_openai formats multimodal image_url and returns analysis dict."""
    mock_chat_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Chart shows a clear golden cross pattern.",
                }
            }
        ]
    }

    with patch("services.openai_service.call_openai_chat", return_value=mock_chat_resp) as mock_call:
        result = analyze_chart_image_with_openai(
            api_key="sk-test",
            image_data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            symbol="AAPL",
            market="US",
            custom_prompt="Detect technical lines",
        )

    assert result["provider"] == "openai_compatible"
    assert result["symbol"] == "AAPL"
    assert "golden cross" in result["analysis"]
    mock_call.assert_called_once()
    sent_messages = mock_call.call_args[1]["messages"]
    user_msg_content = sent_messages[1]["content"]
    assert any(item.get("type") == "image_url" for item in user_msg_content)


def test_get_openai_embeddings_batch_success():
    """Test get_openai_embeddings_batch returns embedding vectors."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
        ]
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        vectors = get_openai_embeddings_batch(
            texts=["First document", "Second document"],
            api_key="sk-test",
            model="text-embedding-3-small",
        )

    assert len(vectors) == 2
    assert vectors[0] == [0.1, 0.2, 0.3]
    assert vectors[1] == [0.4, 0.5, 0.6]


def test_get_openai_embeddings_batch_unsupported_fallback():
    """Test get_openai_embeddings_batch gracefully returns [None] when endpoint lacks embeddings."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "Embeddings not supported on this endpoint"

    with patch("httpx.Client.post", return_value=mock_resp):
        vectors = get_openai_embeddings_batch(
            texts=["doc1", "doc2"],
            api_key="sk-test",
            model="llama-3.3-70b-versatile",
        )

    assert vectors == [None, None]
