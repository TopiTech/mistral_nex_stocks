"""Regression tests for the copy-to-my persistence failure path.

``api_copy_ai_portfolio_to_my`` rolled the in-memory watchlist back on *any*
exception from ``save_user_stocks`` and reported 503.  Storage failures are the
only case the rollback and 503 are meant for; an unrelated programming error
must surface instead of being silently reported as a transient disk problem.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import app
from app_state import app_state
from utils.storage import UserStocksPersistError

_SYMBOL = "ZZ_PERSIST_FAIL"


def _post_copy(client):
    return client.post(
        "/api/ai-portfolio/copy-to-my",
        json={
            "items": [
                {
                    "symbol": _SYMBOL,
                    "market": "us",
                    "weight_pct": 20.0,
                    "target_price": 130.0,
                }
            ]
        },
    )


@pytest.fixture()
def client():
    original_csrf = app.config.get("WTF_CSRF_ENABLED")
    original_propagate = app.config.get("PROPAGATE_EXCEPTIONS")
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = True
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.pop(_SYMBOL, None)
    try:
        yield app.test_client()
    finally:
        app.config["WTF_CSRF_ENABLED"] = original_csrf
        app.config["PROPAGATE_EXCEPTIONS"] = original_propagate
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.pop(_SYMBOL, None)


def test_storage_failure_rolls_back_and_returns_503(client) -> None:
    with (
        patch("utils.networking.require_trusted_or_admin", return_value=(True, None)),
        patch(
            "routes.stocks.ai_portfolio.save_user_stocks",
            side_effect=UserStocksPersistError("disk full"),
        ),
    ):
        response = _post_copy(client)

    assert response.status_code == 503
    assert response.get_json()["ok"] is False
    with app_state.market.user_stocks_lock:
        assert _SYMBOL not in app_state.market.user_us


def test_unrelated_error_is_not_reported_as_a_storage_failure(client) -> None:
    with (
        patch("utils.networking.require_trusted_or_admin", return_value=(True, None)),
        patch(
            "routes.stocks.ai_portfolio.save_user_stocks",
            side_effect=RuntimeError("unexpected bug"),
        ),
    ):
        response = _post_copy(client)

    # The application error handler reports the real failure instead of the
    # endpoint disguising it as a retryable storage problem.
    assert response.status_code == 500
    assert response.get_json()["ok"] is False
