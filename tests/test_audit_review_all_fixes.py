"""Unit and integration tests verifying all comprehensive code review fixes."""

from unittest.mock import MagicMock, patch

import pytest

import credential_manager
import crypto_utils
from app import create_app
from app_state import app_state
from market_state import MarketDataState
from services.ai_portfolio_service import generate_ai_portfolio_by_theme
from services.fallback_provider import (
    MinkabuProvider,
)
from services.realtime.scrapers import YahooJPRealtimeScraper
from utils.stock_payload import _resolve_stocks_for_response


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ===========================================================================
# 1. Security & Crypto Subsystem
# ===========================================================================

def test_crypto_utils_log_sanitization_on_keyring_sync(caplog):
    """Ensure sensitive messages from custom keyring backends are not logged."""
    import logging
    caplog.set_level(logging.DEBUG)
    with patch("crypto_utils.KEYRING_AVAILABLE", True), \
         patch("crypto_utils.keyring.get_password", return_value=""), \
         patch("crypto_utils._is_windows", return_value=True), \
         patch("crypto_utils._dpapi_unprotect", return_value=b"secret_value"), \
         patch("crypto_utils.keyring.set_password", side_effect=Exception("SUPER_SECRET_VALUE_IN_EXCEPTION")):
        entry = {
            "scheme": "keyring",
            "value": "",
            "dpapi_fallback": "c2VjcmV0X3ZhbHVl",
        }
        val = crypto_utils._decode_secret(entry, "test_key")
        assert val == "secret_value"
        assert "SUPER_SECRET_VALUE_IN_EXCEPTION" not in caplog.text
        assert "Exception" in caplog.text


def test_credential_manager_clear_credentials_safe_on_missing_keyring_keys():
    """Ensure clear_api_credentials does not fail if keys do not exist in keyring."""
    mock_kr = MagicMock()
    mock_kr.get_password.return_value = None  # Key does not exist in keyring
    from keyring.errors import PasswordDeleteError
    mock_kr.delete_password.side_effect = PasswordDeleteError("not found")
    with patch("credential_manager._keyring_available", return_value=True), \
         patch("credential_manager._keyring", return_value=mock_kr), \
         patch("config_store.load_config", return_value={"api_credentials": {}}), \
         patch("config_store.save_config"):
        failed = credential_manager.clear_api_credentials()
        assert failed == []


def test_credential_manager_extension_token_created_safe_float_parse():
    """Corrupt or non-float extension_api_token_created should not crash token generation."""
    with patch("config_store.load_config", return_value={
        "extension_api_token": {"scheme": "fernet", "value": "dummy"},
        "extension_api_token_created": "corrupted_non_float_timestamp",
    }), patch("crypto_utils.unprotect_data", return_value="a" * 32), \
       patch("config_store.save_config"):
        token = credential_manager.get_or_create_extension_api_token()
        assert isinstance(token, str)


# ===========================================================================
# 2. Backend Services & Concurrency
# ===========================================================================

def test_market_state_bounded_ttl_collections():
    """MarketDataState collections should be bounded TTLCache instances."""
    state = MarketDataState()
    assert hasattr(state.previous_close_cache, "maxsize")
    assert state.previous_close_cache.maxsize == 2048
    assert hasattr(state.history_circuit_state, "maxsize")
    assert state.history_circuit_state.maxsize == 2048
    assert hasattr(state.invalid_symbol_streak, "maxsize")
    assert state.invalid_symbol_streak.maxsize == 1024


def test_stock_payload_realtime_portfolio_metric_recomputation():
    """Applying realtime prices should recalculate portfolio_value and portfolio_pl."""
    test_rows = {
        "us": [{
            "symbol": "AAPL",
            "name": "Apple",
            "price": 100.0,
            "currency": "USD",
            "shares": 10.0,
            "avg_price": 80.0,
            "avg_fx_rate": 150.0,
            "portfolio_value": 150000.0,
            "portfolio_pl": 30000.0,
        }],
        "jp": [],
        "idx": [],
    }
    user_holdings = {
        "AAPL": {
            "shares": 10.0,
            "avg_price": 80.0,
            "avg_fx_rate": 150.0,
        }
    }
    with patch("app_state.app_state.cache.sse_data_lock"), \
         patch("app_state.app_state.market.user_stocks_lock"), \
         patch.object(app_state.market, "current_stocks_cache", test_rows), \
         patch.object(app_state.market, "user_us", user_holdings), \
         patch("services.realtime_engine.realtime_market_engine.get_market_snapshot", return_value={
             "AAPL": {
                 "price": 120.0,
                 "change": 20.0,
                 "change_percent": 20.0,
                 "volume": 5000,
                 "source": "tv_realtime",
             }
         }):
        res = _resolve_stocks_for_response(include_portfolio=True)
        aapl = res["us"][0]
        assert aapl["price"] == 120.0
        # New value: 10 * 120 * 150 = 180,000 JPY
        assert aapl["portfolio_value"] == 180000.0
        # New PL: 180,000 - (10 * 80 * 150) = 180,000 - 120,000 = 60,000 JPY
        assert aapl["portfolio_pl"] == 60000.0


def test_fallback_providers_include_previous_close():
    """YahooJP and Minkabu fallback providers must not fake the previous close.

    Faking ``regularMarketPreviousClose`` as the current price forced
    change=0 and made stale fallback data look like a flat market. They must
    report None instead (same policy as ``_parse_live_price_marker``).
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<div class="stock_price">3,500.0</div>'

    minkabu = MinkabuProvider()
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    with patch.object(minkabu, "_get_client", return_value=(mock_client, True)):
        quote = minkabu.get_latest_quote("7203.T")
        assert quote is not None
        assert quote["regularMarketPrice"] == 3500.0
        assert quote["regularMarketPreviousClose"] is None


def test_realtime_scrapers_remove_symbol_clears_aliases():
    """remove_symbol must purge failure counters and tokens for both raw and .T aliases."""
    mgr = YahooJPRealtimeScraper()
    mgr.symbols.add("7203")
    mgr.symbols.add("7203.T")
    mgr._symbol_tokens["7203"] = "token1"
    mgr._symbol_tokens["7203.T"] = "token2"
    mgr._consecutive_failures[("7203", "regular")] = 3
    mgr._consecutive_failures[("7203.T", "regular")] = 3

    mgr.remove_symbol("7203")

    assert "7203" not in mgr.symbols
    assert "7203.T" not in mgr.symbols
    assert ("7203", "regular") not in mgr._consecutive_failures
    assert ("7203.T", "regular") not in mgr._consecutive_failures


def test_ai_portfolio_concurrent_generation_failure_raises_storage_error():
    """When a concurrent generation request fails to save, waiting threads should raise PortfolioStorageError."""
    from services.ai_portfolio_service import PortfolioStorageError
    with patch("services.ai_portfolio_service._acquire_ai_generation_slot", return_value=False), \
         patch("services.ai_portfolio_service._wait_ai_generation_slot"), \
         patch("services.ai_portfolio_service._find_saved_ai_portfolio", return_value=None):
        with pytest.raises(PortfolioStorageError):
            generate_ai_portfolio_by_theme(theme_or_preset_id="custom_theme_high_dividend")


# ===========================================================================
# 3. API & Routes
# ===========================================================================

def test_api_screener_fundamental_filters(client):
    """Test screener filtering by min_market_cap, max_market_cap, min_pe, max_pe, limit."""
    mock_stocks = {
        "us": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "market": "us",
                "price": 150.0,
                "change_percent": 1.5,
                "market_cap": 2_500_000_000_000,
                "pe_ratio": 28.5,
                "sector": "Technology",
            },
            {
                "symbol": "SMALL",
                "name": "Small Tech",
                "market": "us",
                "price": 10.0,
                "change_percent": -2.0,
                "market_cap": 500_000_000,
                "pe_ratio": 12.0,
                "sector": "Technology",
            },
        ],
        "jp": [],
        "idx": [],
    }
    with patch("routes.stocks.views.resolve_stocks_for_response", return_value=mock_stocks), \
         patch("routes.stocks.views.build_popular_symbol_items_dispatch", return_value=[]):
        
        # Test min_market_cap filter
        res = client.get("/api/screener?market=us&min_market_cap=1000000000")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert len(data["stocks"]) == 1
        assert data["stocks"][0]["symbol"] == "AAPL"

        # Test max_pe filter
        res = client.get("/api/screener?market=us&max_pe=20")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["stocks"]) == 1
        assert data["stocks"][0]["symbol"] == "SMALL"

        # Test limit
        res = client.get("/api/screener?market=us&limit=1")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["stocks"]) == 1

        # Test sort_by alias change_pct
        res = client.get("/api/screener?market=us&sort_by=change_pct&sort_order=asc")
        assert res.status_code == 200
        data = res.get_json()
        assert data["stocks"][0]["symbol"] == "SMALL"


def test_api_stock_details_explicit_failed_state(client):
    """When short cache contains failed flag, api_stock_details returns failed: True."""
    with patch.object(app_state.yfinance_short_cache, "get", return_value={"failed": True}):
        res = client.get("/api/stock-details?symbol=INVALID&market=us")
        assert res.status_code == 200
        data = res.get_json()
        assert data.get("failed") is True
        assert data.get("symbol") == "INVALID"
