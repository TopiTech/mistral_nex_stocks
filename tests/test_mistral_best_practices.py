"""
tests/test_mistral_best_practices.py

Tests for Mistral SDK best practice enhancements:
- Delta-Batch Embeddings (get_mistral_embeddings_batch)
- Thread-safe embeddings cache
- Tool Calling Agent Loop (call_mistral_chat_with_tools)
- Semantic news ranking using batch embeddings
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from mistral_compat import ToolMessage
from services.ai_service import call_mistral_chat_with_tools
from services.embeddings_service import (
    _EMBEDDINGS_CACHE,
    _EMBEDDINGS_CACHE_LOCK,
    get_mistral_embedding,
    get_mistral_embeddings_batch,
    rank_news_by_semantic_relevance,
)


@pytest.fixture(autouse=True)
def clear_embeddings_cache():
    with _EMBEDDINGS_CACHE_LOCK:
        _EMBEDDINGS_CACHE.clear()
    yield
    with _EMBEDDINGS_CACHE_LOCK:
        _EMBEDDINGS_CACHE.clear()


def test_tool_message_helper():
    """ToolMessage creates correct dict structure with role='tool' and tool_call_id."""
    msg = ToolMessage(content='{"price": 150.0}', tool_call_id="call_123", name="get_quote")
    assert msg["role"] == "tool"
    assert msg["content"] == '{"price": 150.0}'
    assert msg["tool_call_id"] == "call_123"
    assert msg["name"] == "get_quote"


def test_batch_embeddings_delta_computation():
    """get_mistral_embeddings_batch should only call Mistral API for uncached texts."""
    mock_client = MagicMock()
    # Mock data item with embedding
    mock_item_1 = MagicMock()
    mock_item_1.embedding = [0.1] * 1024
    mock_item_2 = MagicMock()
    mock_item_2.embedding = [0.2] * 1024
    mock_resp = MagicMock()
    mock_resp.data = [mock_item_1, mock_item_2]
    mock_client.embeddings.create.return_value = mock_resp

    with patch("services.embeddings_service._get_client", return_value=mock_client):
        # 1. First call: 2 items uncached
        results = get_mistral_embeddings_batch(["text1", "text2"], api_key="test_key")
        assert len(results) == 2
        assert results[0] == [0.1] * 1024
        assert results[1] == [0.2] * 1024
        assert mock_client.embeddings.create.call_count == 1
        mock_client.embeddings.create.assert_called_with(
            model="mistral-embed", inputs=["text1", "text2"]
        )

        # 2. Second call: text1 and text2 are cached, text3 is new
        mock_item_3 = MagicMock()
        mock_item_3.embedding = [0.3] * 1024
        mock_resp_2 = MagicMock()
        mock_resp_2.data = [mock_item_3]
        mock_client.embeddings.create.return_value = mock_resp_2

        results2 = get_mistral_embeddings_batch(["text1", "text3", "text2"], api_key="test_key")
        assert len(results2) == 3
        assert results2[0] == [0.1] * 1024
        assert results2[1] == [0.3] * 1024
        assert results2[2] == [0.2] * 1024
        # Only 1 item ("text3") was queried in the second call
        assert mock_client.embeddings.create.call_count == 2
        mock_client.embeddings.create.assert_called_with(
            model="mistral-embed", inputs=["text3"]
        )


def test_batch_embeddings_thread_safety():
    """Concurrent threads requesting embeddings should not crash or corrupt cache."""
    mock_client = MagicMock()
    mock_item = MagicMock()
    mock_item.embedding = [0.5] * 1024
    mock_resp = MagicMock()
    mock_resp.data = [mock_item]
    mock_client.embeddings.create.return_value = mock_resp

    errors = []

    def worker(idx):
        try:
            res = get_mistral_embedding(f"concurrent_text_{idx % 5}", api_key="test_key")
            assert res is not None
        except Exception as e:
            errors.append(e)

    with patch("services.embeddings_service._get_client", return_value=mock_client):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors


def test_rank_news_by_semantic_relevance_batching():
    """rank_news_by_semantic_relevance should calculate embeddings in batch and sort."""
    mock_client = MagicMock()
    # Query embedding and 2 article embeddings
    query_item = MagicMock()
    query_item.embedding = [1.0, 0.0]
    art1_item = MagicMock()
    art1_item.embedding = [0.0, 1.0]  # Low similarity with query (corresponds to news[0])
    art2_item = MagicMock()
    art2_item.embedding = [0.9, 0.1]  # High similarity with query (corresponds to news[1])

    mock_resp_query = MagicMock()
    mock_resp_query.data = [query_item]
    mock_resp_articles = MagicMock()
    mock_resp_articles.data = [art1_item, art2_item]

    mock_client.embeddings.create.side_effect = [mock_resp_query, mock_resp_articles]

    news = [
        {"title": "Low match article", "snippet": "XYZ unrelated news"},
        {"title": "High match article", "snippet": "Semiconductor earnings beat"},
    ]

    with patch("services.embeddings_service._get_client", return_value=mock_client):
        ranked = rank_news_by_semantic_relevance(news, "Semiconductor earnings", api_key="test_key")
        assert len(ranked) == 2
        assert ranked[0]["title"] == "High match article"
        assert ranked[0]["semantic_score"] > ranked[1]["semantic_score"]


def test_call_mistral_chat_with_tools_agent_loop():
    """call_mistral_chat_with_tools should execute tool and feed result back to model."""
    # Turn 1: Model requests tool call
    turn1_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "get_stock_quote",
                                "arguments": '{"symbol": "AAPL"}',
                            },
                        }
                    ],
                }
            }
        ]
    }

    # Turn 2: Final response after receiving tool output
    turn2_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "AAPL is currently trading at $150.00.",
                }
            }
        ]
    }

    with patch(
        "services.ai_service.call_mistral_chat", side_effect=[turn1_resp, turn2_resp]
    ) as mock_chat:
        with patch(
            "services.ai_tools.execute_mistral_tool_call",
            return_value={"symbol": "AAPL", "price": 150.0},
        ) as mock_tool_exec:
            messages = [{"role": "user", "content": "What is the price of AAPL?"}]
            final = call_mistral_chat_with_tools("test_api_key", messages)

            assert mock_tool_exec.call_count == 1
            mock_tool_exec.assert_called_with("get_stock_quote", '{"symbol": "AAPL"}')

            assert mock_chat.call_count == 2
            assert final["choices"][0]["message"]["content"] == "AAPL is currently trading at $150.00."

            # Verify that second call to chat included the assistant tool call and tool response message
            second_call_messages = mock_chat.call_args_list[1][0][1]
            assert len(second_call_messages) == 3
            assert second_call_messages[0]["role"] == "user"
            assert second_call_messages[1]["role"] == "assistant"
            assert second_call_messages[2]["role"] == "tool"
            assert second_call_messages[2]["tool_call_id"] == "call_abc123"


def test_call_mistral_chat_with_tools_parallel_execution():
    """When model requests multiple tools, they should be executed in parallel and returned."""
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
                            "function": {"name": "get_stock_quote", "arguments": '{"symbol": "AAPL"}'},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "get_company_fundamentals", "arguments": '{"symbol": "MSFT"}'},
                        },
                    ],
                }
            }
        ]
    }
    turn2_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "AAPL is $150 and MSFT PER is 30.",
                }
            }
        ]
    }

    with patch("services.ai_service.call_mistral_chat", side_effect=[turn1_resp, turn2_resp]) as mock_chat:
        with patch("services.ai_tools.execute_mistral_tool_call") as mock_tool_exec:
            mock_tool_exec.side_effect = lambda name, args: {"name": name, "ok": True}
            messages = [{"role": "user", "content": "Compare AAPL and MSFT"}]
            final = call_mistral_chat_with_tools("test_api_key", messages)

            assert mock_tool_exec.call_count == 2
            assert mock_chat.call_count == 2
            assert final["choices"][0]["message"]["content"] == "AAPL is $150 and MSFT PER is 30."

            second_call_msgs = mock_chat.call_args_list[1][0][1]
            assert len(second_call_msgs) == 4
            assert second_call_msgs[2]["role"] == "tool"
            assert second_call_msgs[3]["role"] == "tool"


def test_embeddings_service_circuit_breaker():
    """Embeddings call should skip API and return None when circuit is open."""
    with patch("app_state.app_state.market.is_circuit_open", return_value=True):
        results = get_mistral_embeddings_batch(["Sample text"], api_key="test_key")
        assert results == [None]


def test_embeddings_service_records_usage():
    """Embeddings call should record token usage on success."""
    from app_state import app_state

    mock_client = MagicMock()
    mock_item = MagicMock()
    mock_item.embedding = [0.1] * 1024
    mock_resp = MagicMock()
    mock_resp.data = [mock_item]
    mock_resp.usage = {"prompt_tokens": 12, "completion_tokens": 0}
    mock_client.embeddings.create.return_value = mock_resp

    with patch("services.embeddings_service._get_client", return_value=mock_client):
        get_mistral_embeddings_batch(["Unique test embedding text for usage"], api_key="test_key")
        stats = app_state.ai.mistral_usage_stats()
        assert stats["prompt_tokens"] >= 12
        assert "mistral-embed" in stats["by_model"]


def test_analyze_chart_image_with_mistral():
    """analyze_chart_image_with_mistral should format image_url payload and invoke model."""
    from services.ai_service import analyze_chart_image_with_mistral

    mock_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Double bottom pattern confirmed at support level $120.",
                }
            }
        ]
    }

    with patch("services.ai_service.call_mistral_chat", return_value=mock_response) as mock_chat:
        res = analyze_chart_image_with_mistral(
            api_key="test_key",
            image_data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            symbol="NVDA",
            market="us",
        )
        assert res["symbol"] == "NVDA"
        assert "Double bottom" in res["analysis"]
        assert mock_chat.call_count == 1
        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["_model_override"] == "pixtral-large-latest"
        user_msg = call_kwargs["messages"][1]
        assert user_msg["content"][1]["type"] == "image_url"
        assert "data:image/png;base64," in user_msg["content"][1]["image_url"]


def test_api_analyze_chart_image_endpoint(client):
    """POST /api/analyze-chart-image endpoint validation and routing."""
    # 1. Missing api_key
    resp = client.post(
        "/api/analyze-chart-image",
        json={"image_data": "dGVzdA=="},
    )
    assert resp.status_code == 401

    # 2. Invalid market
    with patch("routes.api_analysis.extract_api_key", return_value="test_key"):
        resp = client.post(
            "/api/analyze-chart-image",
            json={"image_data": "dGVzdA==", "market": "invalid_market"},
        )
        assert resp.status_code == 400
        from error_codes import ErrorCode
        assert resp.get_json()["error_code"] == ErrorCode.INVALID_MARKET.value

    # 3. Valid market and successful call (defaulting to us)
    mock_res = {
        "symbol": "AAPL",
        "market": "us",
        "model": "pixtral-large-latest",
        "analysis": "Uptrend detected",
        "analyzed_at": "2026-08-30T00:00:00Z",
    }
    with patch("routes.api_analysis.extract_api_key", return_value="test_key"), \
         patch("routes.api_analysis.analyze_chart_image_with_mistral", return_value=mock_res) as mock_analyze:
        resp = client.post(
            "/api/analyze-chart-image",
            json={"image_data": "dGVzdA==", "symbol": "AAPL"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["symbol"] == "AAPL"
        assert mock_analyze.call_args[1]["market"] == "us"


def test_generate_ai_technical_lines_pydantic():
    """generate_ai_technical_lines should support structured Pydantic output."""
    from services.ai_service import generate_ai_technical_lines
    from utils.validators import TechnicalLinesResult

    mock_pydantic_res = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "parsed": {
                        "summary": "7203 is in an upward channel.",
                        "trend_bias": "Bullish",
                        "lines": [
                            {
                                "id": "line_1",
                                "type": "support",
                                "label": "SMA20 Support",
                                "color": "#00ff88",
                                "style": "solid",
                                "start_date": "2026-08-01",
                                "start_price": 2500.0,
                                "end_date": "2026-08-25",
                                "end_price": 2700.0,
                                "description": "20-day moving average support line",
                            }
                        ],
                    },
                    "content": "{\"summary\": \"7203 is in an upward channel.\"}",
                }
            }
        ]
    }

    with patch("services.ai_service.call_mistral_chat", return_value=mock_pydantic_res) as mock_chat:
        dummy_history = [
            {"date": "2026-08-01", "open": 2500, "high": 2550, "low": 2480, "close": 2530},
            {"date": "2026-08-02", "open": 2530, "high": 2600, "low": 2520, "close": 2580},
        ]
        result = generate_ai_technical_lines("test_key", "7203.T", "jp", "1mo", dummy_history)
        assert result["summary"] == "7203 is in an upward channel."
        assert result["trend_bias"] == "Bullish"
        assert len(result["lines"]) == 1
        assert result["lines"][0]["start_price"] == 2500.0
        assert mock_chat.call_args[1]["response_format"] == TechnicalLinesResult


def test_ai_usage_stats_and_endpoint(client):
    """GET /api/system/ai-usage should return token usage and cost breakdown."""
    from app_state import app_state

    app_state.ai.record_mistral_usage({"prompt_tokens": 1000, "completion_tokens": 500}, model="mistral-small-2603")
    app_state.ai.record_mistral_usage({"prompt_tokens": 2000, "completion_tokens": 1000}, model="mistral-large-2512")

    response = client.get("/api/system/ai-usage")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    usage = data["usage"]
    assert usage["prompt_tokens"] >= 3000
    assert usage["completion_tokens"] >= 1500
    assert usage["estimated_cost_usd"] > 0
    assert usage["estimated_cost_jpy"] > 0
    assert "mistral-small-2603" in usage["by_model"]
    assert "mistral-large-2512" in usage["by_model"]
