"""
embeddings_service.py - Mistral Embeddings API (mistral-embed) Integration.

Provides 1024-dimensional semantic embeddings for:
  - Financial news & market intelligence relevance scoring
  - Natural language investment theme to stock catalog semantic matching
  - Watchlist & portfolio semantic alignment analysis
"""

import hashlib
import logging
import math
from typing import Any

from cachetools import LRUCache

logger = logging.getLogger(__name__)

# LRU Cache to avoid duplicate embedding API calls
_EMBEDDINGS_CACHE: LRUCache[str, list[float]] = LRUCache(maxsize=1024)


def _compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", errors="ignore")).hexdigest()


def get_mistral_embedding(text: str, api_key: str) -> list[float] | None:
    """Compute or retrieve cached 1024-dim embedding vector using Mistral Embed API."""
    if not text or not isinstance(text, str) or not text.strip():
        return None
    if not api_key or not isinstance(api_key, str):
        return None

    clean_text = text.strip()[:4000]
    cache_key = _compute_text_hash(clean_text)

    if cache_key in _EMBEDDINGS_CACHE:
        return _EMBEDDINGS_CACHE[cache_key]

    from constants import MISTRAL_API_TIMEOUT_SEC, MISTRAL_BASE_URL
    from mistral_compat import Mistral

    try:
        client = Mistral(
            api_key=api_key,
            server_url=MISTRAL_BASE_URL,
            timeout_ms=int(MISTRAL_API_TIMEOUT_SEC * 1000),
        )
        resp = client.embeddings.create(model="mistral-embed", inputs=[clean_text])
        if resp and getattr(resp, "data", None) and len(resp.data) > 0:
            embedding = getattr(resp.data[0], "embedding", None)
            if embedding and isinstance(embedding, list):
                _EMBEDDINGS_CACHE[cache_key] = embedding
                return embedding
    except Exception as exc:
        logger.warning("Mistral embeddings API call failed: %s", exc)
        return None

    return None


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two float vectors (-1.0 to 1.0)."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot_product / (norm_a * norm_b)


def rank_news_by_semantic_relevance(
    news_items: list[dict[str, Any]],
    query_or_symbols: str | list[str],
    api_key: str,
) -> list[dict[str, Any]]:
    """Rank market news articles by semantic relevance to a query or portfolio themes."""
    if not news_items or not isinstance(news_items, list):
        return []
    if not api_key:
        return news_items

    query_text = (
        " ".join(query_or_symbols)
        if isinstance(query_or_symbols, list)
        else str(query_or_symbols or "")
    ).strip()

    if not query_text:
        return news_items

    query_vec = get_mistral_embedding(query_text, api_key)
    if not query_vec:
        return news_items

    scored_items = []
    for item in news_items:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        snippet = item.get("snippet") or item.get("summary") or item.get("body", "")
        article_text = f"{title}\n{snippet}".strip()

        article_vec = get_mistral_embedding(article_text, api_key)
        score = cosine_similarity(query_vec, article_vec) if article_vec else 0.0

        item_copy = dict(item)
        item_copy["semantic_score"] = round(score, 4)
        scored_items.append(item_copy)

    scored_items.sort(key=lambda x: x.get("semantic_score", 0.0), reverse=True)
    return scored_items
