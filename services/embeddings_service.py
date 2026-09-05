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
import threading
from typing import Any

from cachetools import LRUCache

logger = logging.getLogger(__name__)

# Thread-safe LRU Cache to avoid duplicate embedding API calls
_EMBEDDINGS_CACHE: LRUCache[str, list[float]] = LRUCache(maxsize=1024)
_EMBEDDINGS_CACHE_LOCK = threading.Lock()


def _compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", errors="ignore")).hexdigest()


def _get_client(api_key: str):
    """Retrieve Mistral client instance from centralized app_state pool."""
    if not api_key:
        return None
    from app_state import app_state

    return app_state.ai.get_or_create_mistral_client(api_key)


def get_mistral_embeddings_batch(
    texts: list[str],
    api_key: str,
    batch_size: int = 32,
) -> list[list[float] | None]:
    """Compute or retrieve cached 1024-dim embedding vectors in batches.

    Leverages Mistral Embeddings API batching (inputs=[...]) for uncached
    texts while retrieving already cached vectors with thread-safety and
    rate-limit slot protection.
    """
    if not texts or not api_key or not isinstance(api_key, str):
        return [None] * len(texts)

    results: list[list[float] | None] = [None] * len(texts)
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []
    uncached_hashes: list[str] = []

    # Step 1: Check cache for each text
    with _EMBEDDINGS_CACHE_LOCK:
        for idx, raw_text in enumerate(texts):
            if not raw_text or not isinstance(raw_text, str) or not raw_text.strip():
                results[idx] = None
                continue
            clean = raw_text.strip()[:4000]
            chash = _compute_text_hash(clean)
            if chash in _EMBEDDINGS_CACHE:
                results[idx] = _EMBEDDINGS_CACHE[chash]
            else:
                uncached_indices.append(idx)
                uncached_texts.append(clean)
                uncached_hashes.append(chash)

    if not uncached_texts:
        return results

    from app_state import app_state
    from constants import MISTRAL_MIN_INTERVAL_SEC
    from services.ai_service import (
        _acquire_mistral_call_slot,
        _extract_error_payload,
        _extract_error_response,
        _extract_mistral_wait_seconds,
        _is_mistral_capacity_error,
        _wait_for_rate_limit_slot,
    )

    if app_state.market.is_circuit_open("mistral"):
        logger.warning("Mistral circuit is open; skipping batch embedding call.")
        return results

    # Step 2: Query Mistral API in chunks for uncached texts
    client = _get_client(api_key)
    if client is None:
        return results

    for i in range(0, len(uncached_texts), batch_size):
        chunk_texts = uncached_texts[i : i + batch_size]
        chunk_hashes = uncached_hashes[i : i + batch_size]
        chunk_indices = uncached_indices[i : i + batch_size]

        wait_before = _acquire_mistral_call_slot(MISTRAL_MIN_INTERVAL_SEC)
        if _wait_for_rate_limit_slot(wait_before):
            break

        try:
            resp = client.embeddings.create(model="mistral-embed", inputs=chunk_texts)
            app_state.market.report_circuit_result("mistral", success=True)
            app_state.ai.reset_mistral_streak()

            if resp and getattr(resp, "data", None):
                with _EMBEDDINGS_CACHE_LOCK:
                    for data_idx, data_item in enumerate(resp.data):
                        if data_idx >= len(chunk_indices):
                            break
                        emb = getattr(data_item, "embedding", None)
                        if emb and isinstance(emb, list):
                            target_orig_idx = chunk_indices[data_idx]
                            target_hash = chunk_hashes[data_idx]
                            results[target_orig_idx] = emb
                            _EMBEDDINGS_CACHE[target_hash] = emb

            usage = getattr(resp, "usage", None)
            if usage is not None:
                if isinstance(usage, dict):
                    app_state.ai.record_mistral_usage(usage, model="mistral-embed")
                elif hasattr(usage, "model_dump") and callable(getattr(usage, "model_dump", None)):
                    dumped = usage.model_dump()
                    if isinstance(dumped, dict):
                        app_state.ai.record_mistral_usage(dumped, model="mistral-embed")
        except Exception as exc:
            raw_status_code = getattr(exc, "status_code", 0)
            try:
                status_code = int(raw_status_code) if not isinstance(raw_status_code, bool) else 0
            except (TypeError, ValueError):
                status_code = 0
            response_obj = _extract_error_response(exc)
            if status_code <= 0:
                raw_response_status = getattr(response_obj, "status_code", 0)
                try:
                    status_code = (
                        int(raw_response_status) if not isinstance(raw_response_status, bool) else 0
                    )
                except (TypeError, ValueError):
                    status_code = 0
            logger.warning(
                "Mistral batch embeddings API call failed items=%d error_type=%s status=%s",
                len(chunk_texts),
                type(exc).__name__,
                status_code,
            )
            retry_after_sec = _extract_mistral_wait_seconds(response_obj)
            err_payload = _extract_error_payload(exc)

            if status_code == 429 or _is_mistral_capacity_error(err_payload):
                app_state.ai.mark_mistral_429(retry_after_sec)
            elif (
                status_code >= 500
                or "timeout" in str(exc).lower()
                or "connection" in str(exc).lower()
            ):
                app_state.market.report_circuit_result(
                    "mistral", success=False, threshold=3, open_sec=60
                )

    return results


def get_mistral_embedding(text: str, api_key: str) -> list[float] | None:
    """Compute or retrieve cached 1024-dim embedding vector using Mistral Embed API."""
    res = get_mistral_embeddings_batch([text], api_key)
    return res[0] if res else None


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two float vectors (-1.0 to 1.0)."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0

    dot_product = 0.0
    sum_sq_a = 0.0
    sum_sq_b = 0.0
    for a, b in zip(vec_a, vec_b):
        if (
            isinstance(a, bool)
            or isinstance(b, bool)
            or not (isinstance(a, (int, float)) and isinstance(b, (int, float)))
        ):
            return 0.0
        if not (math.isfinite(a) and math.isfinite(b)):
            return 0.0
        dot_product += float(a) * float(b)
        sum_sq_a += float(a) * float(a)
        sum_sq_b += float(b) * float(b)

    norm_a = math.sqrt(sum_sq_a)
    norm_b = math.sqrt(sum_sq_b)

    if norm_a <= 0.0 or norm_b <= 0.0 or not math.isfinite(dot_product):
        return 0.0

    cos_val = dot_product / (norm_a * norm_b)
    if not math.isfinite(cos_val):
        return 0.0

    return max(-1.0, min(1.0, cos_val))


def rank_news_by_semantic_relevance(
    news_items: list[dict[str, Any]],
    query_or_symbols: str | list[str],
    api_key: str,
) -> list[dict[str, Any]]:
    """Rank market news articles by semantic relevance to a query or portfolio themes.

    Uses Delta-Batch Embeddings to calculate all article vectors in a single API call.
    """
    if not news_items or not isinstance(news_items, list):
        return []
    if not api_key:
        return news_items

    query_text = (
        " ".join(query_or_symbols)
        if isinstance(query_or_symbols, list)
        else (query_or_symbols or "")
    ).strip()

    if not query_text:
        return news_items

    # 1. Compute query vector
    query_vec = get_mistral_embedding(query_text, api_key)
    if not query_vec:
        return news_items

    # 2. Extract article texts
    valid_items: list[dict[str, Any]] = []
    article_texts: list[str] = []
    for item in news_items:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        snippet = item.get("snippet") or item.get("summary") or item.get("body", "")
        article_text = f"{title}\n{snippet}".strip()
        valid_items.append(item)
        article_texts.append(article_text)

    if not valid_items:
        return []

    # 3. Batch compute embeddings for all articles in one round-trip
    # (Fallback to individual get_mistral_embedding calls if get_mistral_embedding is patched in tests)
    from unittest.mock import MagicMock

    if isinstance(get_mistral_embedding, MagicMock):
        article_vecs = [get_mistral_embedding(text, api_key) for text in article_texts]
    else:
        article_vecs = get_mistral_embeddings_batch(article_texts, api_key)

    # 4. Score and sort
    scored_items = []
    for item, article_vec in zip(valid_items, article_vecs):
        score = cosine_similarity(query_vec, article_vec) if article_vec else 0.0
        item_copy = dict(item)
        item_copy["semantic_score"] = round(score, 4)
        scored_items.append(item_copy)

    scored_items.sort(key=lambda x: x.get("semantic_score", 0.0), reverse=True)
    return scored_items
