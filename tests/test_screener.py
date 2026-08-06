"""Unit tests for the Simple Stock Screener page and API endpoint."""

from unittest.mock import patch

import pytest

from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def _clear_screener_cache():
    """Isolate screener-enrichment cache entries between tests."""
    from utils.caching import clear_cache_prefix

    clear_cache_prefix("screener_enrich_")
    yield
    clear_cache_prefix("screener_enrich_")


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
    dummy_info = {
        "regularMarketPrice": 100.0,
        "regularMarketDayHigh": 105.0,
        "regularMarketDayLow": 95.0,
        "marketCap": 1_000_000_000,
        "volume": 100_000,
        "sector": "Technology",
        "shortName": "Sample Stock",
    }
    with patch("routes.api_stocks.get_stock_info_cached", return_value=dummy_info):
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


def test_api_screener_unregistered_stock_enrichment(client):
    """Test that unregistered stocks (popular stocks or query search) return enriched metrics."""
    sample_info = {
        "regularMarketPrice": 123.45,
        "regularMarketDayHigh": 126.0,
        "regularMarketDayLow": 122.0,
        "marketCap": 5_000_000_000,
        "volume": 1_000_000,
        "sector": "Technology",
        "shortName": "Sample Corp",
    }
    with patch("routes.api_stocks.get_stock_info_cached", return_value=sample_info) as mock_info:
        response = client.get("/api/screener?market=us")
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        # Verify every returned stock dictionary has required keys
        for stock in data["stocks"]:
            assert "symbol" in stock
            assert "price" in stock
            assert "change_percent" in stock
            assert "market_cap" in stock
            assert "sector" in stock

        # Querying an unregistered (non-popular) symbol specifically
        res_q = client.get("/api/screener?q=ZZZZ")
        assert res_q.status_code == 200
        data_q = res_q.get_json()
        assert data_q["ok"] is True
        zzzz_stock = next((s for s in data_q["stocks"] if s["symbol"] == "ZZZZ"), None)
        assert zzzz_stock is not None
        assert zzzz_stock["symbol"] == "ZZZZ"
        assert zzzz_stock["price"] == 123.45
        assert zzzz_stock["sector"] == "Technology"
        assert zzzz_stock["high"] == 126.0
        assert zzzz_stock["low"] == 122.0
        # Popular-symbol fallback is cache-only (no network); the explicitly
        # queried symbol is the only one allowed a full (network-capable) fetch.
        mock_info.assert_any_call("ZZZZ", cache_only=False)
        cache_only_calls = [
            c for c in mock_info.call_args_list if c.kwargs.get("cache_only") is True
        ]
        assert cache_only_calls, "expected cache-only info lookups for popular symbols"


def test_api_screener_batch_enrichment_path(client):
    """Test that the batch-enrichment path uses payloads from fetch_stocks_batch."""
    payload = {
        "symbol": "BRK-B",
        "name": "Berkshire Hathaway",
        "price": 456.78,
        "change_percent": 1.23,
        "change": 5.6,
        "market_cap": 9_000_000_000_000,
        "volume": 2_500_000,
        "high": 460.1,
        "low": 452.3,
        "sector": "Financial Services",
    }
    with patch("routes.api_stocks.fetch_stocks_batch", return_value=[payload]):
        res = client.get("/api/screener?market=us&q=BRK-B")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    brk = next((s for s in data["stocks"] if s["symbol"] == "BRK-B"), None)
    assert brk is not None
    assert brk["price"] == 456.78
    assert brk["change_percent"] == 1.23
    assert brk["market_cap"] == 9_000_000_000_000
    assert brk["sector"] == "Financial Services"
    assert brk["name"] == "Berkshire Hathaway"
    assert brk["high"] == 460.1
    assert brk["low"] == 452.3


def test_api_screener_negative_change_percent(client):
    """Test that negative daily change and change percent are preserved in screener responses."""
    payload = {
        "symbol": "NEG-STOCK",
        "name": "Negative Corp",
        "price": 100.0,
        "change_percent": -2.75,
        "change": -2.83,
        "market_cap": 1_000_000_000,
        "volume": 500_000,
        "high": 103.0,
        "low": 99.0,
        "sector": "Technology",
    }
    with patch("routes.api_stocks.fetch_stocks_batch", return_value=[payload]):
        res = client.get("/api/screener?market=us&q=NEG-STOCK")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    stk = next((s for s in data["stocks"] if s["symbol"] == "NEG-STOCK"), None)
    assert stk is not None
    assert stk["change_percent"] == -2.75
    assert stk["change_value"] == -2.83


def test_api_screener_query_by_sector(client):
    """Test that searching by sector name includes popular stocks matching that sector."""
    # Ensure BRK-B is processed correctly by the sector filter query.
    payload = {
        "symbol": "BRK-B",
        "name": "Berkshire Hathaway",
        "price": 150.0,
        "change_percent": 1.0,
        "change": 1.5,
        "market_cap": 1_000_000_000,
        "volume": 100_000,
        "high": 151.0,
        "low": 149.0,
        "sector": "Financial Services",
    }
    with patch("routes.api_stocks.fetch_stocks_batch", return_value=[payload]):
        res = client.get("/api/screener?market=us&q=Financial")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    stk = next((s for s in data["stocks"] if s["symbol"] == "BRK-B"), None)
    assert stk is not None, "BRK-B should be found when querying by its sector 'Financial'"
    assert stk["sector"] == "Financial Services"
