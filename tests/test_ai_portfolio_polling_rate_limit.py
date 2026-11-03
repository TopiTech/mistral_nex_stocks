"""Regression tests: AI portfolio polls must not exhaust the endpoint rate limit.

The AI-portfolio UI re-POSTs ``/api/ai-portfolio/generate`` (and
``/api/ai-portfolio/rebalance``) every few seconds while the background job
runs. Before the fix every poll was charged against the endpoint's 20/min
budget, so any generation longer than roughly a minute received HTTP 429
mid-operation and the client could time out even though the job was healthy.

The endpoints now use the same ``skip_polling_duplicates`` mechanism as
``/api/chat``: one stable ``request_token`` marks the polls of a single
operation, while distinct tokens keep consuming the quota.
"""

from __future__ import annotations

import pathlib
import threading
import time
from unittest.mock import patch

import pytest

from app import app
from route_helpers import (
    _rate_limit_distinct_token_counts,
    _rate_limit_lock,
    _rate_limit_store,
    _rate_limit_window_by_key,
)
from routes.stocks.ai_portfolio import (
    ai_portfolio_fetch_inflight,
    ai_portfolio_fetch_lock,
    ai_portfolio_result_cache,
)

TOKEN = "polling-token-0123456789abcdef"
SCOPE = "polling-regression-scope"
REMOTE = {"REMOTE_ADDR": "192.168.1.215"}


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def _reset_state() -> None:
    with ai_portfolio_fetch_lock:
        ai_portfolio_fetch_inflight.clear()
        ai_portfolio_result_cache.clear()
    with _rate_limit_lock:
        _rate_limit_store.clear()
        _rate_limit_window_by_key.clear()
        _rate_limit_distinct_token_counts.clear()


@pytest.fixture(autouse=True)
def _clear_state():
    _reset_state()
    yield
    _reset_state()


def _seed_cached_result(operation: str, theme: str, portfolio: dict) -> None:
    key = f"{operation}:{SCOPE}:{theme}"
    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache[key] = (time.time(), portfolio, None)


def _seed_inflight_job(operation: str, theme: str) -> None:
    key = f"{operation}:{SCOPE}:{theme}"
    with ai_portfolio_fetch_lock:
        ai_portfolio_fetch_inflight[key] = {
            "result": None,
            "error": None,
            "done": threading.Event(),
            "step": "generating",
            "start_time": time.time(),
            "message": "生成中...",
        }


def _post(client, path: str, body: dict):
    return client.post(path, json=body, environ_base=REMOTE)


def _set_scope(client) -> None:
    with client.session_transaction() as sess:
        sess["mns_analysis_conversation"] = SCOPE


def test_generate_polls_with_one_token_never_return_429(client):
    """25 polls (more than the 20/min quota) of one operation all succeed."""
    theme = "polling_theme"
    portfolio = {"id": "polling-id", "theme": theme, "items": []}
    _seed_cached_result("generate", theme, portfolio)
    _set_scope(client)

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with patch(
            "routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme"
        ) as mock_generate:
            statuses = []
            for _ in range(25):
                resp = _post(
                    client,
                    "/api/ai-portfolio/generate",
                    {
                        "theme": theme,
                        "conversation_scope": SCOPE,
                        "request_token": TOKEN,
                    },
                )
                statuses.append(resp.status_code)
                assert resp.get_json().get("ok") is True

    assert set(statuses) == {200}, statuses
    mock_generate.assert_not_called()


def test_rebalance_polls_with_one_token_never_return_429(client):
    """The rebalance endpoint has the same polling contract as generate."""
    theme = "rebalance_polling_theme"
    _seed_inflight_job("rebalance", theme)
    _set_scope(client)

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        with patch.object(threading.Event, "wait", return_value=False):
            statuses = []
            for _ in range(25):
                resp = _post(
                    client,
                    "/api/ai-portfolio/rebalance",
                    {
                        "theme": theme,
                        "conversation_scope": SCOPE,
                        "request_token": TOKEN,
                    },
                )
                statuses.append(resp.status_code)
                body = resp.get_json()
                assert body.get("ok") is True
                assert body.get("fetching") is True

    assert set(statuses) == {200}, statuses


def test_distinct_operation_tokens_still_consume_the_quota(client):
    """The polling exemption must not weaken the endpoint quota for new work."""
    theme = "distinct_theme"
    _seed_cached_result("generate", theme, {"id": "distinct-id", "theme": theme, "items": []})
    _set_scope(client)

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        statuses = []
        for index in range(21):
            resp = _post(
                client,
                "/api/ai-portfolio/generate",
                {
                    "theme": theme,
                    "conversation_scope": SCOPE,
                    "request_token": f"distinct-operation-token-{index:04d}",
                },
            )
            statuses.append(resp.status_code)

    assert statuses[:20] == [200] * 20, statuses
    assert statuses[20] == 429, statuses


def test_frontend_reuses_one_token_for_the_operation_and_its_polls():
    """The client half of the contract: a stable token on every poll body."""
    source = pathlib.Path("static/js/ai_portfolio.js").read_text(encoding="utf-8")
    assert source.count("const requestToken = createRequestToken();") == 2
    # Initial request + poll for generate and for rebalance.
    assert source.count("request_token: requestToken") == 4
    assert source.count("theme: presetOrTheme") == 2
    assert source.count("theme: theme") == 2


def test_generate_polling_token_bucket_is_bound_to_the_endpoint(client):
    """A polling token accepted on one endpoint must not bypass another's quota."""
    theme = "cross_endpoint_theme"
    _seed_cached_result("generate", theme, {"id": "x", "theme": theme, "items": []})
    _set_scope(client)

    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        for _ in range(3):
            resp = _post(
                client,
                "/api/ai-portfolio/generate",
                {"theme": theme, "conversation_scope": SCOPE, "request_token": TOKEN},
            )
            assert resp.status_code == 200

    with _rate_limit_lock:
        token_keys = [key for key in _rate_limit_store if ":token:" in key]
    assert token_keys, "polling token was never registered"
    assert all("api_generate_ai_portfolio:token:" in key for key in token_keys), token_keys
