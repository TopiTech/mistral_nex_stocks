"""Unit tests for the Simple Stock Screener page and API endpoint."""

import pytest
from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_screener_page_route(client):
    """Test that /screener route renders successfully."""
    response = client.get("/screener")
    assert response.status_code == 200
    assert b"Stock Screener" in response.data or b"screener" in response.data.lower()


def test_api_screener_default(client):
    """Test /api/screener endpoint with default parameters."""
    response = client.get("/api/screener")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert "stocks" in data
    assert isinstance(data["stocks"], list)


def test_api_screener_filters(client):
    """Test /api/screener with market, sector, and query filters."""
    # US market filter
    res_us = client.get("/api/screener?market=us")
    assert res_us.status_code == 200
    data_us = res_us.get_json()
    assert data_us["ok"] is True
    for item in data_us["stocks"]:
        assert item["market"] == "us"

    # JP market filter
    res_jp = client.get("/api/screener?market=jp")
    assert res_jp.status_code == 200
    data_jp = res_jp.get_json()
    assert data_jp["ok"] is True
    for item in data_jp["stocks"]:
        assert item["market"] == "jp"

    # Query search filter
    res_q = client.get("/api/screener?q=AAPL")
    assert res_q.status_code == 200
    data_q = res_q.get_json()
    assert data_q["ok"] is True
    assert any(s["symbol"] == "AAPL" for s in data_q["stocks"])


def test_api_screener_sorting(client):
    """Test /api/screener sorting options."""
    res_sort = client.get("/api/screener?sort_by=symbol&sort_order=asc")
    assert res_sort.status_code == 200
    data = res_sort.get_json()
    assert data["ok"] is True
    symbols = [s["symbol"] for s in data["stocks"]]
    assert symbols == sorted(symbols)
