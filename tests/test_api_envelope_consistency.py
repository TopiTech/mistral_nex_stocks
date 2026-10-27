"""Unit tests verifying API envelope consistency (ok: True, success: True) across quotes, analysis, and core endpoints."""

from unittest.mock import patch

import pytest

from app import create_app
from app_state import app_state


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_api_indices_envelope_consistency(client):
    """GET /api/indices must include ok: True and success: True in response."""
    with patch("routes.stocks.quotes.resolve_indices_for_response", return_value={"N225": {"price": 38000}}):
        resp = client.get("/api/indices")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data.get("ok") is True
        assert data.get("success") is True
        assert "N225" in data


def test_api_indices_fetching_envelope_consistency(client):
    """GET /api/indices fetching state must include ok: True and success: True."""
    with patch("routes.stocks.quotes.resolve_indices_for_response", return_value={}):
        with patch("routes.stocks.quotes._wait_for_initial_market_snapshot"):
            resp = client.get("/api/indices")
            assert resp.status_code == 200
            data = resp.get_json()
            assert isinstance(data, dict)
            assert data.get("ok") is True
            assert data.get("success") is True
            assert data.get("fetching") is True


def test_api_stocks_envelope_consistency(client):
    """GET /api/stocks must include ok: True and success: True in response."""
    mock_stocks = {"us": [{"symbol": "AAPL"}], "jp": [], "idx": []}
    mock_indices = {"N225": {"price": 38000}}
    with (
        patch("routes.stocks.quotes.resolve_stocks_for_response", return_value=mock_stocks),
        patch("routes.stocks.quotes.resolve_indices_for_response", return_value=mock_indices),
    ):
        resp = client.get("/api/stocks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data.get("ok") is True
        assert data.get("success") is True
        assert "stocks" in data
        assert "indices" in data


def test_api_stock_details_envelope_consistency(client):
    """GET /api/stock-details must return ok: True on hit and ok: False on failure."""
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache["info_short_TESTOK"] = {
            "sector": "Tech",
            "industry": "Software",
            "marketCap": 1000000,
            "trailingPE": 25.5,
        }
        app_state.yfinance_short_cache["info_short_TESTFAIL"] = {"failed": True}

    try:
        # Hit
        resp_ok = client.get("/api/stock-details?symbol=TESTOK&market=us")
        assert resp_ok.status_code == 200
        data_ok = resp_ok.get_json()
        assert data_ok.get("ok") is True
        assert data_ok.get("success") is True
        assert data_ok.get("sector") == "Tech"

        # Failed short cache entry
        resp_fail = client.get("/api/stock-details?symbol=TESTFAIL&market=us")
        assert resp_fail.status_code == 200
        data_fail = resp_fail.get_json()
        assert data_fail.get("ok") is False
        assert data_fail.get("success") is False
        assert data_fail.get("failed") is True
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop("info_short_TESTOK", None)
            app_state.yfinance_short_cache.pop("info_short_TESTFAIL", None)


def test_api_stock_history_envelope_consistency(client):
    """GET /api/stock-history must include ok: True and success: True."""
    mock_payload = {
        "symbol": "AAPL",
        "history": [{"c": 150.0, "o": 149.0, "h": 151.0, "l": 148.5, "v": 10000, "x": 1700000000000}],
    }
    app_state.stock_disk_cache.set("hist_AAPL_us_1mo", mock_payload)
    resp = client.get("/api/stock-history?symbol=AAPL&market=us&period=1mo")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, dict)
    assert data.get("ok") is True
    assert data.get("success") is True
    assert data.get("symbol") == "AAPL"


def test_api_search_envelope_consistency(client):
    """GET /api/search must return ok: True and success: True."""
    fake_results = [{"symbol": "NVDA", "name": "NVIDIA Corporation"}]
    with patch.object(app_state.stock_provider, "search", return_value=fake_results):
        resp = client.get("/api/search?q=NVDA")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data.get("ok") is True
        assert data.get("success") is True
        assert "results" in data
        assert len(data["results"]) == 1


def test_api_trending_envelope_consistency(client):
    """GET /api/trending must return ok: True and success: True."""
    with patch("routes.api_analysis._get_market_trending_titles", return_value=["Trend 1", "Trend 2"]):
        resp = client.get("/api/trending")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data.get("ok") is True
        assert data.get("success") is True
        assert "trending" in data
