"""Regression tests for N20-01..N20-03 (2026-11-20 comprehensive review).

N20-01: OpenAI-compatible embeddings must record token usage on 200.
N20-02: Mistral news bundle cache key must include the effective model.
N20-03: AlphaVantage Note/Information/Error must be sanitized before logging.
"""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app_state import app_state


def _embedding_response(usage=None):
    return SimpleNamespace(
        status_code=200,
        json=lambda: {
            "data": [{"index": 0, "embedding": [0.1, 0.2]}],
            **({"usage": usage} if usage is not None else {}),
        },
        text="",
    )


def test_n20_01_openai_embeddings_records_usage_on_200():
    from services import openai_service

    before = app_state.ai.openai_usage_stats()["call_count"]
    mock_client = MagicMock()
    mock_client.post.return_value = _embedding_response(
        {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7}
    )
    with (
        patch(
            "services.openai_service.get_openai_base_url",
            return_value="http://example.test/v1",
        ),
        patch(
            "services.openai_service._get_openai_client",
            return_value=mock_client,
        ),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch(
            "utils.networking.socket.getaddrinfo",
            return_value=[(__import__("socket").AF_INET, 1, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        out = openai_service.get_openai_embeddings_batch(["hello"], "sk-test")
    assert out == [[0.1, 0.2]]
    after = app_state.ai.openai_usage_stats()["call_count"]
    assert after == before + 1


def test_n20_01_openai_embeddings_missing_usage_still_returns_vectors():
    from services import openai_service

    before = app_state.ai.openai_usage_stats()["call_count"]
    mock_client = MagicMock()
    mock_client.post.return_value = _embedding_response(None)
    with (
        patch(
            "services.openai_service.get_openai_base_url",
            return_value="http://example.test/v1",
        ),
        patch("services.openai_service._get_openai_client", return_value=mock_client),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch(
            "utils.networking.socket.getaddrinfo",
            return_value=[(__import__("socket").AF_INET, 1, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        out = openai_service.get_openai_embeddings_batch(["hello"], "sk-test")
    assert out == [[0.1, 0.2]]
    assert app_state.ai.openai_usage_stats()["call_count"] == before


def test_n20_01_openai_embeddings_malformed_usage_does_not_raise():
    from services import openai_service

    mock_client = MagicMock()
    mock_client.post.return_value = _embedding_response({"prompt_tokens": "bad!!"})
    with (
        patch(
            "services.openai_service.get_openai_base_url",
            return_value="http://example.test/v1",
        ),
        patch("services.openai_service._get_openai_client", return_value=mock_client),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch(
            "utils.networking.socket.getaddrinfo",
            return_value=[(__import__("socket").AF_INET, 1, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        out = openai_service.get_openai_embeddings_batch(["hello"], "sk-test")
    assert out == [[0.1, 0.2]]


def test_n20_02_mistral_news_cache_key_includes_model():
    from services import news_service

    seen_keys: list[str] = []

    def _fake_get_cached(key, fn, duration=0, valid_func=None):
        seen_keys.append(key)
        return {"us": "x", "jp": "y", "trends": "z"}

    svc = news_service.NewsService()
    with (
        patch("services.news_service._determine_search_strategy", return_value="default"),
        patch(
            "services.news_service.get_cached_context_with_negative_cache",
            side_effect=lambda cache_key, fn, ttl=0, neg_ttl=0, force=False: "ctx",
        ),
        patch("services.news_service.collect_market_trending_titles", return_value=[]),
        patch("services.news_service.get_cached", side_effect=_fake_get_cached),
        patch("services.news_service.is_openai_provider", return_value=False),
        patch("services.news_service.get_model_name", return_value="mistral-small-2603"),
    ):
        svc.get_synchronized_market_news("real-key", "", "", force_refresh=False)
    assert seen_keys, "expected the LLM bundle cache to be consulted"
    bundle_keys = [k for k in seen_keys if k.startswith("news_bundle_llm_")]
    assert bundle_keys, f"no bundle key recorded: {seen_keys}"
    llm_hash = hashlib.sha256(b"ctx|ctx|").hexdigest()
    assert bundle_keys[0] == f"news_bundle_llm_mistral_mistral-small-2603_{llm_hash}"


def test_n20_02_mistral_news_cache_key_changes_with_model():
    from services import news_service

    seen: dict[str, list[str]] = {"keys": []}

    def _fake_get_cached(key, fn, duration=0, valid_func=None):
        seen["keys"].append(key)
        return {"us": "x", "jp": "y", "trends": "z"}

    svc = news_service.NewsService()
    for model in ("mistral-small-2603", "mistral-medium-3.5"):
        seen["keys"].clear()
        with (
            patch("services.news_service._determine_search_strategy", return_value="default"),
            patch(
                "services.news_service.get_cached_context_with_negative_cache",
                side_effect=lambda cache_key, fn, ttl=0, neg_ttl=0, force=False: "ctx",
            ),
            patch("services.news_service.collect_market_trending_titles", return_value=[]),
            patch("services.news_service.get_cached", side_effect=_fake_get_cached),
            patch("services.news_service.is_openai_provider", return_value=False),
            patch("services.news_service.get_model_name", return_value=model),
        ):
            svc.get_synchronized_market_news("real-key", "", "", force_refresh=False)
        assert any(model in k for k in seen["keys"] if k.startswith("news_bundle_llm_"))


def test_n20_03_alphavantage_note_is_sanitized_before_log(caplog):
    from services.fallback_provider import AlphaVantageProvider

    provider = AlphaVantageProvider()
    evil = "rate limit\nnext line\x1b[31mred"
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"Note": evil}
    fake_resp.text = '{"Note": "x"}'
    with (
        patch("services.fallback_provider.get_alphavantage_api_key", return_value="k"),
        patch("utils.normalization.is_valid_symbol", return_value=True),
        patch("requests.get", return_value=fake_resp),
        caplog.at_level(logging.WARNING, logger="services.fallback_provider"),
    ):
        assert provider.get_latest_quote("AAPL") is None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "\x1b[31m" not in logged
    assert "rate limit" in logged
