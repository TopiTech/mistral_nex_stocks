"""Regression test: AI Portfolio generation and rebalance background job failure caching.

Verifies that when a background AI generation or rebalance job raises an exception after
a client has timed out and transitioned to polling:
1. The error is retained in ``ai_portfolio_result_cache`` so polling clients receive the
   failure immediately instead of repeatedly re-submitting new background jobs.
2. The error entry is popped once consumed, permitting fresh retry attempts afterwards.
"""

from __future__ import annotations

import threading
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


def test_ai_portfolio_generate_failure_cached_and_does_not_loop(client):
    """When portfolio generation fails during async execution, subsequent polls retrieve the error without re-running."""
    theme = "failure_test_theme"
    scope = "test_conversation_scope_gen_err"

    unblock_job = threading.Event()

    def slow_failing_generation(*args, **kwargs):
        unblock_job.wait(timeout=5.0)
        raise RuntimeError("AI generation backend down")

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        with patch(
            "routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme",
            side_effect=slow_failing_generation,
        ) as mock_gen:
            # Mock wait to simulate a 3-second timeout returning fetching: True immediately
            with patch("threading.Event.wait", return_value=False):
                resp1 = client.post(
                    "/api/ai-portfolio/generate",
                    json={"theme": theme, "conversation_scope": scope},
                )
                assert resp1.status_code == 200
                body1 = resp1.get_json()
                assert body1.get("fetching") is True

            # Now allow the background thread to fail and complete its finally block
            unblock_job.set()
            time.sleep(0.1)

            # 2. Subsequent poll MUST receive the error response (500)
            # and MUST NOT trigger another call to generate_ai_portfolio_by_theme!
            resp2 = client.post(
                "/api/ai-portfolio/generate",
                json={"theme": theme, "conversation_scope": scope},
            )
            assert resp2.status_code == 500
            body2 = resp2.get_json()
            assert body2["ok"] is False
            assert body2.get("details", {}).get("error_type") == "RuntimeError"
            assert "AI ポートフォリオの生成に失敗しました" in str(body2.get("details", {}).get("reason", ""))
            # mock_gen should only have been called once, NOT twice!
            assert mock_gen.call_count == 1

            # 3. Once consumed, the error cache entry was popped, so an explicit retry
            # later can initiate a new job cleanly rather than being permanently blocked.
            mock_gen.side_effect = None
            mock_gen.return_value = {"id": theme, "items": [], "theme": theme}

            resp3 = client.post(
                "/api/ai-portfolio/generate",
                json={"theme": theme, "conversation_scope": scope},
            )
            assert resp3.status_code == 200
            body3 = resp3.get_json()
            assert body3["ok"] is True
            assert mock_gen.call_count == 2


def test_ai_portfolio_rebalance_failure_cached_and_does_not_loop(client):
    """When portfolio rebalance fails during async execution, subsequent polls retrieve the error without re-running."""
    theme = "failure_test_theme_rebal"
    scope = "test_conversation_scope_rebal_err"

    unblock_job = threading.Event()

    def slow_failing_rebalance(*args, **kwargs):
        unblock_job.wait(timeout=5.0)
        raise RuntimeError("AI rebalance rate limit reached")

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        with patch(
            "routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme",
            side_effect=slow_failing_rebalance,
        ) as mock_gen:
            with patch("threading.Event.wait", return_value=False):
                resp1 = client.post(
                    "/api/ai-portfolio/rebalance",
                    json={"theme": theme, "conversation_scope": scope},
                )
                assert resp1.status_code == 200
                body1 = resp1.get_json()
                assert body1.get("fetching") is True

            unblock_job.set()
            time.sleep(0.1)

            # 2. Subsequent poll MUST receive the cached error
            resp2 = client.post(
                "/api/ai-portfolio/rebalance",
                json={"theme": theme, "conversation_scope": scope},
            )
            assert resp2.status_code == 500
            body2 = resp2.get_json()
            assert body2["ok"] is False
            assert body2.get("details", {}).get("error_type") == "RuntimeError"
            assert "AI ポートフォリオのリバランスに失敗しました" in str(body2.get("details", {}).get("reason", ""))
            assert mock_gen.call_count == 1
