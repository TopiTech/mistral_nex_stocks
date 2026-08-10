import pytest

from app import create_app
from constants import VALID_HISTORY_INTERVALS
from services.stock_service import _history_payload_short_cache_key


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_valid_history_intervals_constant():
    assert "auto" in VALID_HISTORY_INTERVALS
    assert "1m" in VALID_HISTORY_INTERVALS
    assert "5m" in VALID_HISTORY_INTERVALS
    assert "15m" in VALID_HISTORY_INTERVALS
    assert "1h" in VALID_HISTORY_INTERVALS
    assert "1d" in VALID_HISTORY_INTERVALS
    assert "1wk" in VALID_HISTORY_INTERVALS
    assert "1mo" in VALID_HISTORY_INTERVALS


def test_history_payload_cache_key_with_interval():
    key_default = _history_payload_short_cache_key("AAPL", "1mo")
    assert key_default == "history_short_payload_AAPL_1mo_auto"

    key_15m = _history_payload_short_cache_key("AAPL", "1mo", "15m")
    assert key_15m == "history_short_payload_AAPL_1mo_15m"


def test_api_stock_history_accepts_interval(client):
    from utils.caching import _set_cached_value

    cache_key = "hist_AAPL_1mo_5m"
    _set_cached_value(cache_key, {
        "symbol": "AAPL",
        "history": [{"x": 1000, "o": 10, "h": 12, "l": 9, "c": 11, "v": 100}],
        "interval_used": "5m",
    }, 3600)

    response = client.get("/api/stock-history?symbol=AAPL&market=us&period=1mo&interval=5m")
    assert response.status_code == 200
    data = response.get_json()
    assert data["symbol"] == "AAPL"
    assert data["interval_used"] == "5m"


def test_api_stock_history_accepts_tv_symbol(client):
    """Verify /api/stock-history normalizes NASDAQ:NVDA to NVDA and returns 200 instead of 400."""
    from utils.caching import _set_cached_value

    cache_key = "hist_NVDA_3mo"
    _set_cached_value(cache_key, {
        "symbol": "NVDA",
        "history": [{"x": 1000, "o": 100, "h": 105, "l": 98, "c": 102, "v": 5000}],
        "interval_used": "1d",
    }, 3600)

    response = client.get("/api/stock-history?symbol=NASDAQ%3ANVDA&market=us&period=3mo&interval=auto")
    assert response.status_code == 200
    data = response.get_json()
    assert data["symbol"] == "NVDA"

