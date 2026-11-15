"""Regression test: AI Portfolio generation and rebalance background job failure caching.

Verifies that when a background AI generation or rebalance job raises an exception after
a client has timed out and transitioned to polling:
1. The error is retained in ``ai_portfolio_result_cache`` so polling clients receive the
   failure immediately instead of repeatedly re-submitting new background jobs.
2. The error entry is popped once consumed, permitting fresh retry attempts afterwards.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app import app
from app_state import app_state
from routes.stocks.ai_portfolio import (
    ai_portfolio_fetch_inflight,
    ai_portfolio_fetch_lock,
    ai_portfolio_result_cache,
)


@pytest.fixture
def client():
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with app.test_client() as test_client:
            yield test_client
    finally:
        if orig_testing is None:
            app.config.pop("TESTING", None)
        else:
            app.config["TESTING"] = orig_testing
        if orig_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = orig_csrf


@pytest.fixture(autouse=True)
def _clear_state():
    with ai_portfolio_fetch_lock:
        ai_portfolio_fetch_inflight.clear()
        ai_portfolio_result_cache.clear()
    yield
    with ai_portfolio_fetch_lock:
        ai_portfolio_fetch_inflight.clear()
        ai_portfolio_result_cache.clear()


@contextmanager
def _poll_until_error_cached(client, endpoint, inflight_prefix, theme, scope, error_msg):
    """Trigger *endpoint* with an async executor until polling begins, release error, then yield poll response."""
    unblock_job = threading.Event()
    orig_wait = threading.Event.wait

    def slow_failing_job(*_args, **_kwargs):
        unblock_job.wait(timeout=5.0)
        raise RuntimeError(error_msg)

    def selective_wait(ev_self, timeout=None):
        if ev_self is unblock_job:
            return orig_wait(ev_self, timeout=timeout)
        return False

    with (
        ThreadPoolExecutor(max_workers=2) as real_pool,
        patch.object(app_state.execution, "executor", real_pool),
        patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)),
    ):
        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        with patch(
            "routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme",
            side_effect=slow_failing_job,
        ) as mock_gen:
            try:
                # Bypass only the request-handler wait so the route returns fetching: True
                # while the worker thread stays blocked cleanly on unblock_job.
                with patch("threading.Event.wait", new=selective_wait):
                    resp1 = client.post(
                        endpoint,
                        json={"theme": theme, "conversation_scope": scope},
                    )
                    assert resp1.status_code == 200
                    body1 = resp1.get_json()
                    assert body1.get("fetching") is True

                with ai_portfolio_fetch_lock:
                    inflight_entry = ai_portfolio_fetch_inflight.get(
                        f"{inflight_prefix}:{scope}:{theme}"
                    )
                    assert inflight_entry is not None
                    done_ev = inflight_entry["done"]
            finally:
                # Always release the worker thread before waiting or aborting on failure.
                unblock_job.set()
            assert orig_wait(done_ev, timeout=5.0), f"Background job for {endpoint} did not finish"

            resp2 = client.post(
                endpoint,
                json={"theme": theme, "conversation_scope": scope},
            )
            yield resp2, mock_gen


def test_ai_portfolio_generate_failure_cached_and_does_not_loop(client):
    """When portfolio generation fails during async execution, subsequent polls retrieve the error without re-running."""
    theme = "failure_test_theme"
    scope = "test_conversation_scope_gen_err"

    with _poll_until_error_cached(
        client,
        "/api/ai-portfolio/generate",
        "generate",
        theme,
        scope,
        "AI generation backend down",
    ) as (resp2, mock_gen):
        # 2. Subsequent poll MUST receive the error response (500)
        # and MUST NOT trigger another call to generate_ai_portfolio_by_theme!
        assert resp2.status_code == 500
        body2 = resp2.get_json()
        assert body2["ok"] is False
        assert body2.get("details", {}).get("error_type") == "RuntimeError"
        assert "AI ポートフォリオの生成に失敗しました" in str(body2.get("details", {}).get("reason", ""))
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

    with _poll_until_error_cached(
        client,
        "/api/ai-portfolio/rebalance",
        "rebalance",
        theme,
        scope,
        "AI rebalance rate limit reached",
    ) as (resp2, mock_gen):
        assert resp2.status_code == 500
        body2 = resp2.get_json()
        assert body2["ok"] is False
        assert body2.get("details", {}).get("error_type") == "RuntimeError"
        assert "AI ポートフォリオのリバランスに失敗しました" in str(body2.get("details", {}).get("reason", ""))
        assert mock_gen.call_count == 1
