"""
Unit tests for Mistral Embeddings API integration (services/embeddings_service.py).
"""

from unittest.mock import MagicMock, patch

import pytest

from services.embeddings_service import (
    _EMBEDDINGS_CACHE,
    cosine_similarity,
    get_mistral_embedding,
    rank_news_by_semantic_relevance,
)


def test_cosine_similarity():
    """Verify cosine similarity calculation."""
    vec_a = [1.0, 0.0, 0.0]
    vec_b = [1.0, 0.0, 0.0]
    assert cosine_similarity(vec_a, vec_b) == 1.0

    vec_c = [0.0, 1.0, 0.0]
    assert cosine_similarity(vec_a, vec_c) == 0.0

    vec_d = [-1.0, 0.0, 0.0]
    assert pytest.approx(cosine_similarity(vec_a, vec_d)) == -1.0


@patch("mistral_compat.Mistral")
def test_get_mistral_embedding_and_cache(mock_mistral_cls):
    """Verify get_mistral_embedding calls mistral-embed and stores result in cache."""
    mock_instance = MagicMock()
    mock_mistral_cls.return_value = mock_instance

    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]
    mock_instance.embeddings.create.return_value = mock_resp

    _EMBEDDINGS_CACHE.clear()

    res = get_mistral_embedding("AI hardware market overview", "test-key")
    assert res == [0.1, 0.2, 0.3]
    assert len(_EMBEDDINGS_CACHE) == 1

    # Second call hits cache (no new API call)
    res2 = get_mistral_embedding("AI hardware market overview", "test-key")
    assert res2 == [0.1, 0.2, 0.3]
    assert mock_instance.embeddings.create.call_count == 1


@patch("services.embeddings_service.get_mistral_embedding")
def test_rank_news_by_semantic_relevance(mock_get_emb):
    """Verify semantic news ranking orders articles by cosine similarity score."""
    # Query vector
    def emb_side_effect(text, key):
        if "半導体" in text:
            return [1.0, 0.0, 0.0]
        elif "チップ" in text or "GPU" in text:
            return [0.9, 0.1, 0.0]
        elif "飲食" in text or "カフェ" in text:
            return [0.0, 1.0, 0.0]
        return [0.1, 0.1, 0.1]

    mock_get_emb.side_effect = emb_side_effect

    news = [
        {"title": "新しいカフェがオープン", "snippet": "飲食業界の動向"},
        {"title": "最新GPUチップが発売", "snippet": "次世代AI半導体市場が急伸"},
        {"title": "天候不順による影響", "snippet": "農作物の価格変動"},
    ]

    ranked = rank_news_by_semantic_relevance(news, "半導体 GPU", "test-key")
    assert len(ranked) == 3
    assert ranked[0]["title"] == "最新GPUチップが発売"
    assert ranked[0]["semantic_score"] > ranked[1]["semantic_score"]
