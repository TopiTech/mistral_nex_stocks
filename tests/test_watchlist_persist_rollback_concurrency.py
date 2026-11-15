"""Regression tests for watchlist persistence rollback rev-consistency (add_ext and copy-to-my).

Verifies that when save_user_stocks fails with UserStocksPersistError:
1. If user_stocks_rev is unchanged, own uncommitted writes are rolled back cleanly.
2. If user_stocks_rev changed (e.g. concurrent save committed), on-disk state is
   authoritative and load_user_stocks(force=True) is invoked to resynchronize in-memory cache.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app_state import app_state
from utils.storage import UserStocksPersistError

ORIGIN = {"Origin": "http://localhost:5000"}


@pytest.fixture(scope="module")
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def test_add_ext_rollback_when_rev_unchanged(client):
    symbol = "REVEXT1"
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.pop(symbol, None)

    token = "test-token-ext-rev-1"
    with (
        patch("utils.env_helpers._is_remote_api_enabled", return_value=False),
        patch("utils.networking._is_local_request", return_value=True),
        patch("utils.networking._is_loopback_ip", return_value=True),
        patch("utils.networking.is_allowed_trusted_origin", return_value=True),
        patch(
            "credential_manager.get_or_create_extension_api_token",
            return_value=token,
        ),
        patch(
            "routes.stocks.views.save_user_stocks",
            side_effect=UserStocksPersistError("disk full"),
        ),
    ):
        res = client.post(
            "/api/stocks/add_ext",
            headers={
                "X-MNS-Extension-Request": "true",
                "Authorization": f"Bearer {token}",
                "Origin": "http://127.0.0.1:5000",
            },
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            json={"symbol": symbol, "market": "us", "name": "Rev Ext 1"},
        )

    assert res.status_code == 503
    assert res.get_json()["ok"] is False
    with app_state.market.user_stocks_lock:
        assert symbol not in app_state.market.user_us


def test_add_ext_rollback_when_rev_changed_concurrently(client):
    symbol = "REVEXT2"
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.pop(symbol, None)

    token = "test-token-ext-rev-2"

    def failing_save_with_concurrent_bump():
        with app_state.market.user_stocks_lock:
            app_state.market.user_stocks_rev += 1
        raise UserStocksPersistError("disk full")

    mock_load = MagicMock()
    with (
        patch("utils.env_helpers._is_remote_api_enabled", return_value=False),
        patch("utils.networking._is_local_request", return_value=True),
        patch("utils.networking._is_loopback_ip", return_value=True),
        patch("utils.networking.is_allowed_trusted_origin", return_value=True),
        patch(
            "credential_manager.get_or_create_extension_api_token",
            return_value=token,
        ),
        patch(
            "routes.stocks.views.save_user_stocks",
            side_effect=failing_save_with_concurrent_bump,
        ),
        patch("routes.stocks.views.load_user_stocks", mock_load),
    ):
        res = client.post(
            "/api/stocks/add_ext",
            headers={
                "X-MNS-Extension-Request": "true",
                "Authorization": f"Bearer {token}",
                "Origin": "http://127.0.0.1:5000",
            },
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            json={"symbol": symbol, "market": "us", "name": "Rev Ext 2"},
        )

    assert res.status_code == 503
    assert res.get_json()["ok"] is False
    mock_load.assert_called_once_with(force=True)


def test_ai_portfolio_copy_to_my_rollback_when_rev_unchanged(client):
    symbol = "7753.T"
    with app_state.market.user_stocks_lock:
        app_state.market.user_jp.pop(symbol, None)

    payload = {
        "items": [
            {"symbol": symbol, "market": "jp", "target_price": 1000, "weight_pct": 20}
        ]
    }
    with (
        patch("utils.networking.require_trusted_or_admin", return_value=(True, None)),
        patch(
            "routes.stocks.ai_portfolio.save_user_stocks",
            side_effect=UserStocksPersistError("disk full"),
        ),
    ):
        res = client.post("/api/ai-portfolio/copy-to-my", json=payload, headers=ORIGIN)

    assert res.status_code == 503
    assert res.get_json()["ok"] is False
    with app_state.market.user_stocks_lock:
        assert symbol not in app_state.market.user_jp


def test_ai_portfolio_copy_to_my_rollback_when_rev_changed_concurrently(client):
    symbol = "7754.T"
    with app_state.market.user_stocks_lock:
        app_state.market.user_jp.pop(symbol, None)

    payload = {
        "items": [
            {"symbol": symbol, "market": "jp", "target_price": 1000, "weight_pct": 20}
        ]
    }

    def failing_save_with_concurrent_bump():
        with app_state.market.user_stocks_lock:
            app_state.market.user_stocks_rev += 1
        raise UserStocksPersistError("disk full")

    mock_load = MagicMock()
    with (
        patch("utils.networking.require_trusted_or_admin", return_value=(True, None)),
        patch(
            "routes.stocks.ai_portfolio.save_user_stocks",
            side_effect=failing_save_with_concurrent_bump,
        ),
        patch("routes.stocks.ai_portfolio.load_user_stocks", mock_load),
    ):
        res = client.post("/api/ai-portfolio/copy-to-my", json=payload, headers=ORIGIN)

    assert res.status_code == 503
    assert res.get_json()["ok"] is False
    mock_load.assert_called_once_with(force=True)
