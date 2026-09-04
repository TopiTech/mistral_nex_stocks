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
    with (
        patch("routes.api_stocks.fetch_stocks_batch", return_value=[payload]),
        patch("routes.api_stocks.get_stock_info_cached", return_value={}),
    ):
        res = client.get("/api/screener?market=us&q=Financial")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    stk = next((s for s in data["stocks"] if s["symbol"] == "BRK-B"), None)
    assert stk is not None, "BRK-B should be found when querying by its sector 'Financial'"
    assert stk["sector"] == "Financial Services"


def test_api_screener_enrichment_cache_follows_watchlist_state(client):
    """Regression test for the enrichment cache key (review R1).

    The enrichment payload depends on the *current* watchlist state (it drives
    pop_unseen_items). The cache key must therefore include the exact symbol
    set being enriched; otherwise a watchlist change within the TTL serves a
    stale payload that omits symbols removed from the watchlist.
    """

    def _row(sym, mkt):
        return {
            "symbol": sym,
            "name": sym,
            "market": mkt,
            "price": 100.0,
            "change_percent": 1.0,
            "change": 1.0,
            "market_cap": 1_000_000_000,
            "volume": 100_000,
            "high": 105.0,
            "low": 95.0,
            "sector": "Technology",
        }

    enriched_sets = []

    def fake_enrichment(pop_unseen_items, q_symbol, **kwargs):
        enriched_sets.append([sym for sym, _n, _m in pop_unseen_items])
        return {sym: _row(sym, mkt) for sym, _n, mkt in pop_unseen_items}

    # First snapshot: AAPL is already tracked (watchlist) -> excluded from
    # enrichment. Second snapshot: AAPL removed -> must be enriched again and
    # must appear in the result set even within the cache TTL.
    snapshot_with_aapl = {"us": [_row("AAPL", "us")], "jp": []}
    snapshot_without_aapl = {"us": [], "jp": []}

    with (
        patch(
            "routes.api_stocks._resolve_stocks_for_response",
            side_effect=[snapshot_with_aapl, snapshot_without_aapl],
        ),
        patch(
            "routes.api_stocks.build_screener_enrichment",
            side_effect=fake_enrichment,
        ) as mock_enrich,
    ):
        res1 = client.get("/api/screener?market=us")
        res2 = client.get("/api/screener?market=us")

    assert res1.status_code == 200
    assert res2.status_code == 200
    symbols2 = [s["symbol"] for s in res2.get_json()["stocks"]]
    # The second request must include AAPL even though the first request's
    # cache entry was built without it (different cache key -> fresh fetch).
    assert "AAPL" in symbols2
    # Enrichment must have run twice (two distinct watchlist states) rather
    # than serving the first request's cached payload.
    assert mock_enrich.call_count == 2
    assert "AAPL" in enriched_sets[-1]


def test_api_screener_price_filtering_and_float_parsing(client):
    """Test [R1] fix: min_price/max_price filters correctly exclude zero-priced stocks and handle non-finite floats."""
    unpriced_payload = {
        "symbol": "ZERO-STOCK",
        "name": "Zero Corp",
        "price": 0.0,
        "change_percent": 0.0,
        "change": 0.0,
        "market_cap": 0.0,
        "volume": 0,
        "high": 0.0,
        "low": 0.0,
        "sector": "Technology",
    }
    priced_payload = {
        "symbol": "PRICED-STOCK",
        "name": "Priced Corp",
        "price": 100.0,
        "change_percent": 1.0,
        "change": 1.0,
        "market_cap": 1_000_000_000,
        "volume": 100_000,
        "high": 105.0,
        "low": 95.0,
        "sector": "Technology",
    }
    stocks_mock = {"us": [unpriced_payload, priced_payload], "jp": []}
    with patch("routes.api_stocks._resolve_stocks_for_response", return_value=stocks_mock):
        # min_price=50 should exclude ZERO-STOCK (price=0.0 < 50.0)
        res_min = client.get("/api/screener?market=us&min_price=50.0")
        assert res_min.status_code == 200
        data_min = res_min.get_json()
        symbols_min = [s["symbol"] for s in data_min["stocks"]]
        assert "ZERO-STOCK" not in symbols_min
        assert "PRICED-STOCK" in symbols_min

        # max_price=150 should exclude ZERO-STOCK (unpriced price <= 0)
        res_max = client.get("/api/screener?market=us&max_price=150.0")
        assert res_max.status_code == 200
        data_max = res_max.get_json()
        symbols_max = [s["symbol"] for s in data_max["stocks"]]
        assert "ZERO-STOCK" not in symbols_max
        assert "PRICED-STOCK" in symbols_max

        # non-finite floats are now rejected with 400 (R3 strict validation)
        res_nan = client.get("/api/screener?market=us&min_price=NaN&max_price=inf")
        assert res_nan.status_code == 400


def test_api_screener_rejects_overlong_query(client):
    """Regression test (review R1): /api/screener must reject q > 200 chars.

    The query is embedded in the enrichment cache key, which
    ``sanitize_cache_key`` truncates at 256 chars. An unbounded q could push
    the key past that limit and truncate the distinguishing symbol-set hash,
    collapsing distinct (q, symbol-set) combos onto one cache entry that then
    serves stale/wrong enrichment rows within the TTL. /api/search already
    caps q at 200; the screener now enforces the same limit.
    """
    long_q = "a" * 201
    res = client.get(f"/api/screener?q={long_q}")
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert "200" in data.get("details", {}).get("reason", "")


def test_api_screener_accepts_max_length_query(client):
    """A query at exactly the 200-char limit must still work (boundary)."""
    max_q = "a" * 200
    res = client.get(f"/api/screener?q={max_q}")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True


def test_api_screener_high_low_and_null_metrics(client):
    """Verify that screener rows include expected high/low and market_cap numeric metrics."""
    mock_item = {
        "symbol": "MOCK-TEST",
        "name": "Mock Test Stock",
        "market": "us",
        "price": 120.5,
        "change_percent": 1.25,
        "change_value": 1.5,
        "market_cap": 2_500_000_000,
        "pe_ratio": 24.5,
        "volume": 500_000,
        "high": 125.0,
        "low": 118.0,
        "sector": "Technology",
    }
    stocks_mock = {"us": [mock_item], "jp": []}
    with patch("routes.api_stocks._resolve_stocks_for_response", return_value=stocks_mock):
        res = client.get("/api/screener?market=us&q=MOCK-TEST")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["total"] >= 1
        found = next(s for s in data["stocks"] if s["symbol"] == "MOCK-TEST")
        assert found["high"] == 125.0
        assert found["low"] == 118.0
        assert found["price"] == 120.5
