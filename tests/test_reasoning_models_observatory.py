"""
Unit & Integration tests for Mistral AI Reasoning Models and Market Observatory Stability.
"""

from unittest.mock import MagicMock, patch

import httpx

from services.ai_service import (
    _extract_stream_delta,
    _supports_reasoning_effort,
    call_mistral_chat,
)
from utils.validators import (
    extract_chat_content,
    extract_json_payload,
    normalize_chat_parse_payload,
    safe_parse_analysis_result,
)


def test_supports_reasoning_effort_models():
    """Verify primary-source supported reasoning models."""
    assert _supports_reasoning_effort("mistral-small-2603") is True
    assert _supports_reasoning_effort("mistral-small-4") is True
    assert _supports_reasoning_effort("mistral-small-latest") is True
    assert _supports_reasoning_effort("mistral-medium-2604") is True
    assert _supports_reasoning_effort("mistral-medium-3.5") is True
    assert _supports_reasoning_effort("magistral-small-latest") is True
    assert _supports_reasoning_effort("magistral-medium-latest") is True

    # Non-reasoning models
    assert _supports_reasoning_effort("ministral-8b-latest") is False
    assert _supports_reasoning_effort("ministral-3b-latest") is False
    assert _supports_reasoning_effort("codestral-latest") is False


def test_extract_chat_content_strips_thought_tags():
    """Verify that extract_chat_content cleanly strips <thought> and <thinking> tags for display."""
    # Tagged string content
    raw = "<thought>Analyzing revenue growth of NVDA...\nChecking margins {margin: 0.65}...</thought>NVDA is exhibiting strong bullish momentum."
    resp = {"choices": [{"message": {"role": "assistant", "content": raw}}]}
    result = extract_chat_content(resp, preserve_for_history=False)
    assert result == "NVDA is exhibiting strong bullish momentum."
    assert "<thought>" not in result

    # <thinking> tag
    raw_thinking = "<thinking>Step 1: check PE ratio</thinking>Analysis indicates healthy fundamentals."
    resp_thinking = {"choices": [{"message": {"role": "assistant", "content": raw_thinking}}]}
    assert extract_chat_content(resp_thinking, preserve_for_history=False) == "Analysis indicates healthy fundamentals."

    # preserve_for_history=True retains raw text
    history_result = extract_chat_content(resp, preserve_for_history=True)
    assert "<thought>" in history_result


def test_extract_chat_content_reasoning_content_fallback():
    """Verify that if message.content is empty but reasoning_content exists, it is extracted."""
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "Deep reasoning conclusion: Stock is undervalued.",
                }
            }
        ]
    }
    assert extract_chat_content(resp) == "Deep reasoning conclusion: Stock is undervalued."


def test_extract_json_payload_with_thought_tags_and_nested_braces():
    """Verify that extract_json_payload safely ignores braces inside thoughts."""
    raw = (
        "<thought>Evaluating candidate dict {temp: 1, invalid: true} before final JSON output.</thought>"
        '```json\n{\n  "recommendation": "買い",\n  "sentiment": "強気",\n  "target_price_3m": 150.0\n}\n```'
    )
    extracted = extract_json_payload(raw)
    assert extracted is not None
    import json
    parsed = json.loads(extracted)
    assert parsed.get("recommendation") == "買い"
    assert parsed.get("target_price_3m") == 150.0


def test_normalize_chat_parse_payload_with_string_and_fences():
    """Verify normalize_chat_parse_payload extracts valid dict from strings with reasoning tags."""
    raw = (
        "<thought>Formulating analysis JSON.</thought>\n"
        '{\n  "recommendation": "強い買い",\n  "sentiment": "強気",\n  "target_price_3m": 200.0\n}'
    )
    resp = {"choices": [{"message": {"role": "assistant", "content": raw}}]}
    res = normalize_chat_parse_payload(resp)
    assert isinstance(res, dict)
    assert res.get("recommendation") == "強い買い"


def test_safe_parse_analysis_result_local_fast_path():
    """Verify safe_parse_analysis_result extracts and validates locally without calling remote LLM repair."""
    raw = (
        "<thought>Reasoning on AAPL financials...</thought>\n"
        "```json\n"
        "{\n"
        '  "recommendation": "買い",\n'
        '  "sentiment": "強気",\n'
        '  "target_price_3m": 250,\n'
        '  "analysis_summary": "堅調な業績と成長性"\n'
        "}\n"
        "```"
    )
    resp = {"choices": [{"message": {"role": "assistant", "content": raw}}]}

    repair_mock = MagicMock()
    result = safe_parse_analysis_result(resp, "test-api-key", repair_func=repair_mock)

    assert result["recommendation"] == "買い"
    assert result["sentiment"] == "強気"
    assert result["target_price_3m"] == 250.0
    # Confirm local fast path succeeded without calling LLM repair
    assert repair_mock.call_count == 0


@patch("services.ai_service._get_mistral_client")
@patch("services.ai_service._get_mistral_model_name", return_value="custom-model-thinking")
def test_call_mistral_chat_400_retry_without_reasoning_effort(mock_model_name, mock_get_client):
    """Verify that if Mistral API returns 400 Bad Request on reasoning_effort, call_mistral_chat retries without it."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    call_count = [0]

    def side_effect(**kwargs):
        call_count[0] += 1
        if "reasoning_effort" in kwargs and kwargs["reasoning_effort"] != "none":
            # Simulate 400 Bad Request from Mistral API
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.json.return_value = {"error": {"message": "reasoning_effort is not supported for model ministral-8b-latest"}}
            raise httpx.HTTPStatusError("400 Bad Request: reasoning_effort unsupported", request=MagicMock(), response=mock_resp)

        mock_resp = MagicMock()
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Success after retry without reasoning_effort"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        return mock_resp

    mock_client.chat.complete.side_effect = side_effect

    res = call_mistral_chat("test-key", [{"role": "user", "content": "Hello"}], reasoning_effort="high", use_cache=False)
    assert call_count[0] == 2
    assert res["choices"][0]["message"]["content"] == "Success after retry without reasoning_effort"


def test_extract_stream_delta_reasoning_content():
    """Verify that _extract_stream_delta ignores reasoning by default but extracts when include_thinking=True."""
    # Plain chunk with content
    chunk1 = {"choices": [{"delta": {"content": "Hello"}}]}
    assert _extract_stream_delta(chunk1) == "Hello"

    # Chunk with reasoning_content only
    chunk2 = {"choices": [{"delta": {"content": None, "reasoning_content": "Thinking step..."}}]}
    # By default, internal thinking is omitted to prevent leaking into user chat
    assert _extract_stream_delta(chunk2) is None
    # When explicitly requested, thinking is extracted
    assert _extract_stream_delta(chunk2, include_thinking=True) == "Thinking step..."


def test_observatory_controller_polling_limits():
    """Verify that Market Observatory JS files have expanded polling limits (>= 20 attempts)."""
    import os
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ai_dive_path = os.path.join(base_dir, "static", "js", "experimental", "ai-dive-controller.js")
    constellation_path = os.path.join(base_dir, "static", "js", "experimental", "constellation-controller.js")

    with open(ai_dive_path, "r", encoding="utf-8") as f:
        ai_dive_content = f.read()

    with open(constellation_path, "r", encoding="utf-8") as f:
        constellation_content = f.read()

    # /api/analyze-v2 polling in ai-dive
    assert "maxAttempts = 25" in ai_dive_content or "attempt < 20" in ai_dive_content
    # /api/news polling in ai-dive
    assert "attempt < 20" in ai_dive_content
    # /api/chat in constellation
    assert "attempt < 20" in constellation_content


@patch("services.ai_service._get_mistral_client")
def test_call_mistral_chat_struct_chat_typeerror_fallback(mock_get_client):
    """Verify call_mistral_chat catches chat.parse list TypeError and falls back to chat.complete."""
    from utils.validators import StockAnalysis

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    # chat.parse raises TypeError from struct_chat.py
    mock_client.chat.parse.side_effect = TypeError("Unexpected type for message.content: <class 'list'>")

    mock_complete_resp = MagicMock()
    mock_complete_resp.model_dump.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"recommendation": "買い", "sentiment": "強気", "target_price_3m": 120.0}'}],
                }
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 30},
    }
    mock_client.chat.complete.return_value = mock_complete_resp

    res = call_mistral_chat(
        "test-api-key",
        [{"role": "user", "content": "Analyze NVDA"}],
        response_format=StockAnalysis,
        use_cache=False,
    )

    assert mock_client.chat.parse.call_count == 1
    assert mock_client.chat.complete.call_count == 1
    # Verify response was returned
    assert "choices" in res


def test_normalize_chat_parse_payload_with_chunk_list():
    """Verify normalize_chat_parse_payload extracts JSON when content is a list of chunks."""
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Analyzing financial statements..."},
                        {"type": "text", "text": '{\n  "recommendation": "買い",\n  "sentiment": "強気",\n  "target_price_3m": 300.0\n}'},
                    ],
                }
            }
        ]
    }
    payload = normalize_chat_parse_payload(resp)
    assert isinstance(payload, dict)
    assert payload.get("recommendation") == "買い"
    assert payload.get("target_price_3m") == 300.0


def test_safe_parse_analysis_result_with_chunk_list():
    """Verify safe_parse_analysis_result normalizes stock analysis when response has list chunks."""
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": '{"recommendation": "強い買い", "sentiment": "強気", "target_price_3m": 450.0, "analysis_summary": "好決算"}'},
                    ],
                }
            }
        ]
    }
    result = safe_parse_analysis_result(resp, "dummy-key")
    assert result["recommendation"] == "強い買い"
    assert result["sentiment"] == "強気"
    assert result["target_price_3m"] == 450.0
    assert result["analysis_summary"] == "好決算"


def test_extract_chat_content_with_thinking_only_chunk_list():
    """Verify extract_chat_content extracts thinking text when model returns only thinking chunks."""
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "thinking": [
                                {
                                    "text": "OK, the user wants a technical analysis of SoftBank Corp (9434.T). They provided price data and want me to focus on technical indicators."
                                }
                            ]
                        }
                    ],
                }
            }
        ]
    }
    extracted = extract_chat_content(resp, preserve_for_history=False)
    assert "technical analysis of SoftBank Corp (9434.T)" in extracted

    # preserve_for_history wraps in thinking tags
    history_extracted = extract_chat_content(resp, preserve_for_history=True)
    assert "<thinking>" in history_extracted
    assert "technical analysis of SoftBank Corp (9434.T)" in history_extracted


def test_extract_chat_content_with_thinking_and_text_sdk_objects():
    """Verify extract_chat_content handles Python SDK object shapes for ThinkChunk and TextChunk."""
    class DummyThinkItem:
        def __init__(self, text):
            self.text = text

    class DummyThinkChunk:
        def __init__(self, text):
            self.type = "thinking"
            self.thinking = [DummyThinkItem(text)]

    class DummyTextChunk:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        DummyThinkChunk("Analyzing balance sheet fundamentals..."),
                        DummyTextChunk("AAPL is showing strong cash flow."),
                    ],
                }
            }
        ]
    }
    # Display extracts only text chunk
    assert extract_chat_content(resp, preserve_for_history=False) == "AAPL is showing strong cash flow."

    # History preserves both
    hist = extract_chat_content(resp, preserve_for_history=True)
    assert "<thinking>" in hist
    assert "Analyzing balance sheet" in hist
    assert "AAPL is showing strong cash flow." in hist


@patch("services.ai_service._get_mistral_client")
def test_call_mistral_chat_populates_parsed_on_complete_fallback(mock_get_client):
    """Verify call_mistral_chat populates parsed and content models when falling back from chat.parse."""
    from utils.validators import StockAnalysis

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    mock_client.chat.parse.side_effect = TypeError("Unexpected type for message.content: <class 'list'>")

    mock_complete_resp = MagicMock()
    mock_complete_resp.model_dump.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": [{"text": "Reasoning on stock..."}]},
                        {"type": "text", "text": '{"recommendation": "買い", "sentiment": "強気", "target_price_3m": 120.0}'},
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 30},
    }
    mock_client.chat.complete.return_value = mock_complete_resp

    res = call_mistral_chat(
        "test-api-key",
        [{"role": "user", "content": "Analyze NVDA"}],
        response_format=StockAnalysis,
        use_cache=False,
    )

    assert "choices" in res
    msg = res["choices"][0]["message"]
    assert "parsed" in msg
    assert msg["parsed"]["recommendation"] == "買い"
    assert msg["content"]["recommendation"] == "買い"


