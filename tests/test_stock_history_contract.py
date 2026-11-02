"""Contract test: stock history response envelope conformity.

Verifies that make_history_response delegates to ok_response and includes both ok: True
and success: True in its payload.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def test_stock_history_ok_envelope(client):
    """Stock history responses must carry both ok: True and success: True."""
    with patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, None)):
        with patch("routes.stocks.quotes.get_cached") as mock_cache:
            mock_cache.return_value = {
                "symbol": "AAPL",
                "history": [{"date": "2026-01-01", "close": 150.0}],
            }
            resp = client.get("/api/stock-history?symbol=AAPL&market=us&period=1d")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data is not None
            assert data["ok"] is True
            assert data["success"] is True
            assert data["symbol"] == "AAPL"
