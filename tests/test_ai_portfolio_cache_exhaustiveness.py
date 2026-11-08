"""Regression test: AI Portfolio result cache exhaustiveness & empty result handling.

Verifies that:
1. When background generation or rebalance returns None or empty dict, a structured 500
   error envelope is returned and the cache is not left poisoned.
2. If a cached entry has (ts, None, None), reading it evicts the cache entry and returns
   a typed 500 error envelope so subsequent attempts can proceed cleanly.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app import app
from routes.stocks.ai_portfolio import (
    ai_portfolio_fetch_inflight,
    ai_portfolio_fetch_lock,
    ai_portfolio_result_cache,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clear_state():
    with ai_portfolio_fetch_lock:
        ai_portfolio_fetch_inflight.clear()
        ai_portfolio_result_cache.clear()
    yield
    with ai_portfolio_fetch_lock:
        ai_portfolio_fetch_inflight.clear()
        ai_portfolio_result_cache.clear()


def test_ai_portfolio_generate_empty_result_returns_500_and_cleans_cache(client):
    """When portfolio generation returns None, returns 500 and cache is not poisoned."""
    theme = "empty_test_theme"
    scope = "test_scope_gen_empty"

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        with patch(
            "routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme",
            return_value=None,
        ):
            resp = client.post(
                "/api/ai-portfolio/generate",
                json={"theme": theme, "conversation_scope": scope},
            )
            assert resp.status_code == 500
            body = resp.get_json()
            assert body["ok"] is False
            assert body.get("details", {}).get("error_type") == "ValueError"
            assert "AI ポートフォリオの生成に失敗しました" in str(
                body.get("details", {}).get("reason", "")
            )

        # Cache must not retain a poisoned empty entry
        inflight_key = f"generate:{scope}:{theme}"
        with ai_portfolio_fetch_lock:
            cached = ai_portfolio_result_cache.get(inflight_key)
            # Since _ai_portfolio_error_response popped it on consumption:
            assert cached is None or cached[2] is not None


def test_ai_portfolio_generate_cached_empty_tuple_cleans_cache_and_returns_500(client):
    """When cached entry contains (ts, None, None), it is popped and returns 500."""
    theme = "cached_none_theme"
    scope = "test_scope_gen_cached_none"
    inflight_key = f"generate:{scope}:{theme}"

    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache[inflight_key] = (time.time(), None, None)

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        resp = client.post(
            "/api/ai-portfolio/generate",
            json={"theme": theme, "conversation_scope": scope},
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["ok"] is False
        assert "AIポートフォリオの生成結果が空でした" in str(
            body.get("details", {}).get("reason", "")
        )

        # Cache entry MUST have been popped
        with ai_portfolio_fetch_lock:
            assert inflight_key not in ai_portfolio_result_cache


def test_ai_portfolio_rebalance_empty_result_returns_500_and_cleans_cache(client):
    """When portfolio rebalance returns None, returns 500 and cache is clean."""
    theme = "empty_test_theme_rebal"
    scope = "test_scope_rebal_empty"

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        with patch(
            "routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme",
            return_value=None,
        ):
            resp = client.post(
                "/api/ai-portfolio/rebalance",
                json={"theme": theme, "conversation_scope": scope},
            )
            assert resp.status_code == 500
            body = resp.get_json()
            assert body["ok"] is False
            assert body.get("details", {}).get("error_type") == "ValueError"
            assert "AI ポートフォリオのリバランスに失敗しました" in str(
                body.get("details", {}).get("reason", "")
            )

        inflight_key = f"rebalance:{scope}:{theme}"
        with ai_portfolio_fetch_lock:
            assert inflight_key not in ai_portfolio_result_cache


def test_ai_portfolio_rebalance_cached_empty_tuple_returns_500_and_does_not_poison(client):
    """When cached rebalance entry contains (ts, None, None), it pops and returns 500."""
    theme = "cached_rebal_none_theme"
    scope = "test_scope_rebal_cached_none"
    inflight_key = f"rebalance:{scope}:{theme}"

    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache[inflight_key] = (time.time(), None, None)

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        resp = client.post(
            "/api/ai-portfolio/rebalance",
            json={"theme": theme, "conversation_scope": scope},
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["ok"] is False
        assert "AIポートフォリオリバランスの結果が空でした" in str(
            body.get("details", {}).get("reason", "")
        )

        with ai_portfolio_fetch_lock:
            assert inflight_key not in ai_portfolio_result_cache
