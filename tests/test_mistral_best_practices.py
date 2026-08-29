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
