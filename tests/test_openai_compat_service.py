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
from utils.validators import extract_chat_content


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


def test_call_openai_chat_reasoning_fallback_in_message():
    """Test call_openai_chat extracts JSON and text from OpenRouter reasoning field when content is None."""
    sample_analysis = {
        "summary": "AI semiconductor boom continues",
        "score": 9,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": json.dumps(sample_analysis),
                }
            }
        ]
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Analyze NVDA"}],
            response_format=SampleSchema,
            use_cache=False,
        )

    msg = result["choices"][0]["message"]
    assert msg["parsed"]["summary"] == "AI semiconductor boom continues"
    assert msg["parsed"]["score"] == 9
    assert msg["content"] == msg["parsed"]


def test_call_openai_chat_retries_on_reasoning_effort_error():
    """Test call_openai_chat automatically retries when endpoint rejects reasoning_effort (HTTP 400)."""
    resp_400 = MagicMock()
    resp_400.status_code = 400
    resp_400.text = "reasoning_effort is not supported on this endpoint"
    resp_400.json.return_value = {
        "error": {"message": "reasoning_effort is not supported on this endpoint"}
    }

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [
            {"message": {"role": "assistant", "content": "Analysis completed successfully."}}
        ]
    }

    with patch("httpx.Client.post", side_effect=[resp_400, resp_200]) as mock_post:
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Analyze"}],
            reasoning_effort="high",
            use_cache=False,
        )

    assert mock_post.call_count == 2
    assert result["choices"][0]["message"]["content"] == "Analysis completed successfully."


def test_call_openai_chat_omits_none_reasoning_effort():
    """Test call_openai_chat automatically omits reasoning_effort='none' to avoid mandatory reasoning errors."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {"message": {"role": "assistant", "content": "Success"}}
        ]
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Analyze"}],
            reasoning_effort="none",
            use_cache=False,
        )

    sent_json = mock_post.call_args[1]["json"]
    assert "reasoning_effort" not in sent_json


def test_call_openai_chat_retries_on_response_format_error():
    """Test call_openai_chat automatically falls back when response_format is rejected."""
    resp_400 = MagicMock()
    resp_400.status_code = 400
    resp_400.text = "'response_format' is not supported by this model"
    resp_400.json.return_value = {
        "error": {"message": "'response_format' is not supported by this model"}
    }

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps({"summary": "OK", "score": 5})}}
        ]
    }

    with patch("httpx.Client.post", side_effect=[resp_400, resp_200]) as mock_post:
        result = call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "user", "content": "Analyze"}],
            response_format=SampleSchema,
            use_cache=False,
        )

    assert mock_post.call_count == 2
    second_call_json = mock_post.call_args_list[1][1]["json"]
    assert "response_format" not in second_call_json
    assert result["choices"][0]["message"]["parsed"]["summary"] == "OK"


def test_call_openai_chat_structured_output_injects_schema_instructions():
    """Test that call_openai_chat enriches prompt messages with schema instructions for non-native providers."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps({"summary": "Injected", "score": 7})}}
        ]
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        call_openai_chat(
            api_key="sk-test",
            messages=[{"role": "system", "content": "Base system."}, {"role": "user", "content": "Run"}],
            response_format=SampleSchema,
            use_cache=False,
        )

    sent_payload = mock_post.call_args[1]["json"]
    system_msg = sent_payload["messages"][0]["content"]
    assert "Base system." in system_msg
    assert "必須出力形式" in system_msg
    assert "SampleSchema" in system_msg or "summary" in system_msg


def test_extract_chat_content_openrouter_reasoning_field():
    """Test extract_chat_content successfully extracts reasoning text when content is None."""
    # OpenRouter reasoning field
    resp_or = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "Reasoning thought: Stock is overbought.",
                }
            }
        ]
    }
    assert extract_chat_content(resp_or) == "Reasoning thought: Stock is overbought."

    # OpenRouter reasoning_details list
    resp_rd = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_details": [{"text": "Detailed "}, {"text": "rationale"}],
                }
            }
        ]
    }
    assert extract_chat_content(resp_rd) == "Detailed rationale"


def test_clean_reasoning_tags_think_tags():
    """Test _clean_reasoning_tags strips <think>...</think> tags commonly produced by DeepSeek-R1."""
    from utils.validators import _clean_reasoning_tags

    text = "<think>\nThinking about balance sheet...\n</think>Target price is $150."
    assert _clean_reasoning_tags(text) == "Target price is $150."

    # Unclosed think tag (due to token truncation)
    unclosed = "<think>Thinking endlessly until token limit"
    assert _clean_reasoning_tags(unclosed) == ""


def test_safe_parse_analysis_result_with_openai_reasoning_and_aliases():
    """Test safe_parse_analysis_result extracts and validates AI stock analysis from reasoning payload with aliases."""
    from utils.validators import safe_parse_analysis_result

    raw_ai_payload = {
        "rating": "買い",
        "market_sentiment": "強気",
        "target_price": 420.5,
        "summary": "AI需要の急拡大に伴い売上高が前年比40%成長しており強気を維持。",
        "catalysts": ["次世代GPU出荷開始", "データセンター投資増加"],
        "risks": ["半導体規制強化"],
    }
    # Response where content is None, JSON is in reasoning (OpenRouter pattern)
    mock_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": f"<think>Model internal CoT</think>\n{json.dumps(raw_ai_payload)}",
                }
            }
        ]
    }

    result = safe_parse_analysis_result(mock_response, "sk-test")
    assert result["recommendation"] == "買い"
    assert result["sentiment"] == "強気"
    assert result["target_price_3m"] == 420.5
    assert "AI需要" in result["analysis_summary"]
    assert result["fallback_used"] is False


def test_build_openai_headers_omits_authorization_for_ollama_local():
    """Verify _build_openai_headers does not set Authorization header for 'ollama-local' sentinel."""
    from services.openai_service import _build_openai_headers

    headers_local = _build_openai_headers("ollama-local")
    assert "Authorization" not in headers_local

    headers_auth = _build_openai_headers("sk-real-test-key")
    assert headers_auth.get("Authorization") == "Bearer sk-real-test-key"


def test_stream_openai_chat_filters_none_reasoning():
    """Test stream_openai_chat omits 'none' reasoning_effort upfront."""
    captured_payloads = []

    mock_ok_resp = MagicMock()
    mock_ok_resp.status_code = 200
    mock_ok_resp.iter_lines.return_value = [
        b'data: {"choices": [{"delta": {"content": "Hello!"}}]}',
        b'data: [DONE]',
    ]
    mock_ok_resp.__enter__.return_value = mock_ok_resp

    with patch("httpx.Client.stream", side_effect=lambda *args, **kwargs: captured_payloads.append(kwargs.get("json", {})) or mock_ok_resp):
        events = list(
            stream_openai_chat(
                api_key="test-key",
                messages=[{"role": "user", "content": "hi"}],
                reasoning_effort="none",  # Should be omitted upfront
            )
        )

    assert "reasoning_effort" not in captured_payloads[0]
    assert any(e.get("type") == "done" for e in events)


def test_stream_openai_chat_retries_on_reasoning_rejection_400():
    """Test stream_openai_chat retries without reasoning_effort when model rejects it with 400."""
    captured_payloads = []

    mock_err_resp = MagicMock()
    mock_err_resp.status_code = 400
    mock_err_resp.text = json.dumps({"error": {"message": "Reasoning is not supported by this model"}})
    mock_err_resp.__enter__.return_value = mock_err_resp

    mock_ok_resp = MagicMock()
    mock_ok_resp.status_code = 200
    mock_ok_resp.iter_lines.return_value = [
        b'data: {"choices": [{"delta": {"content": "Hello after retry!"}}]}',
        b'data: [DONE]',
    ]
    mock_ok_resp.__enter__.return_value = mock_ok_resp

    def mock_stream(method, url, json=None, headers=None):
        captured_payloads.append(dict(json or {}))
        if len(captured_payloads) == 1:
            return mock_err_resp
        return mock_ok_resp

    with patch("httpx.Client.stream", side_effect=mock_stream):
        events = list(
            stream_openai_chat(
                api_key="test-key",
                messages=[{"role": "user", "content": "hi"}],
                reasoning_effort="high",
            )
        )

    # First attempt had reasoning_effort
    assert captured_payloads[0].get("reasoning_effort") == "high"
    # Second attempt retried without reasoning_effort
    assert "reasoning_effort" not in captured_payloads[1]
    assert any(e.get("type") == "done" for e in events)


