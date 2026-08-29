"""
tests/test_yfinance_best_practices.py

Tests for yfinance best practice enhancements:
- Negative symbol caching (Fast-fail on invalid/missing symbols to prevent 429)
- Observer listener registration between session_manager and ticker cache
- Pandas 2.x safe history frame merging and records conversion
- Fundamental data sanitization (NaN/Inf cleanup)
"""

from unittest.mock import patch

import pandas as pd

from market_state import MarketDataState
from services.stock_provider import (
    YFinanceProvider,
    sanitize_fundamental_dict,
)
from session_manager import (
    register_auth_reset_listener,
    reset_yfinance_auth,
    unregister_auth_reset_listener,
)


def test_market_state_negative_symbol_cache():
    """Negative cache should record and query invalid symbols correctly."""
    m_state = MarketDataState()
    assert not m_state.is_negative_cached_symbol("INVALID_TICKER")

    m_state.mark_negative_cached_symbol("invalid_ticker", reason="404 Not Found")
    assert m_state.is_negative_cached_symbol("INVALID_TICKER")
    assert m_state.is_negative_cached_symbol("invalid_ticker")

    m_state.clear_negative_symbol_cache("INVALID_TICKER")
    assert not m_state.is_negative_cached_symbol("INVALID_TICKER")


def test_yfinance_provider_negative_cache_fast_fail():
    """YFinanceProvider should immediately fast-fail for negative-cached symbols without hitting network."""
    m_state = MarketDataState()
    m_state.mark_negative_cached_symbol("DELISTED_CO", reason="delisted")
    provider = YFinanceProvider(market_state=m_state)

    with patch("yfinance.Ticker") as mock_yf_ticker:
        # get_ticker
        ticker = provider.get_ticker("DELISTED_CO")
        assert ticker is None
        assert mock_yf_ticker.call_count == 0

        # get_info
        info = provider.get_info("DELISTED_CO")
        assert info == {}
        assert mock_yf_ticker.call_count == 0


def test_session_manager_auth_reset_observer():
    """reset_yfinance_auth should invoke registered listeners like clear_ticker_cache."""
    called = []

    def mock_listener():
        called.append(True)

    register_auth_reset_listener(mock_listener)
    try:
        reset_yfinance_auth()
        assert len(called) >= 1
    finally:
        unregister_auth_reset_listener(mock_listener)


def test_sanitize_fundamental_dict():
    """sanitize_fundamental_dict should prune NaN, Inf, and None safely."""
    raw = {
        "trailingPE": 15.5,
        "forwardPE": float("nan"),
        "pegRatio": float("inf"),
        "negativeInf": float("-inf"),
        "shortName": "Apple Inc.",
        "noneVal": None,
        "marketCap": 2500000000,
    }
    clean = sanitize_fundamental_dict(raw)
    assert clean["trailingPE"] == 15.5
    assert clean["shortName"] == "Apple Inc."
    assert clean["marketCap"] == 2500000000
    assert "forwardPE" not in clean
    assert "pegRatio" not in clean
    assert "negativeInf" not in clean
    assert "noneVal" not in clean


def test_merge_quote_into_history_timezone_handling():
    """_merge_quote_into_history should merge quote safely without timezone mismatch errors."""
    provider = YFinanceProvider()

    # Create tz-aware DataFrame (Asia/Tokyo)
    idx = pd.date_range("2026-08-20", periods=5, freq="D", tz="Asia/Tokyo")
    df = pd.DataFrame(
        {
            "Open": [100.0, 102.0, 101.0, 103.0, 104.0],
            "High": [105.0, 106.0, 104.0, 105.0, 107.0],
            "Low": [99.0, 101.0, 100.0, 102.0, 103.0],
            "Close": [102.0, 101.0, 103.0, 104.0, 106.0],
            "Volume": [1000, 1200, 1100, 1300, 1500],
        },
        index=idx,
    )

    quote = {
        "regularMarketPrice": 108.0,
        "regularMarketOpen": 106.0,
        "regularMarketDayHigh": 109.0,
        "regularMarketDayLow": 105.5,
        "regularMarketVolume": 2000,
        "regularMarketTime": 1787616000,  # some future timestamp
    }

    merged = provider._merge_quote_into_history(df, quote, "7203.T")
    assert not merged.empty
    assert len(merged) >= 5
    assert merged["Close"].iloc[-1] == 108.0
    assert merged["Volume"].iloc[-1] == 2000


def test_df_to_records_nan_inf_safety():
    """_df_to_records should convert NaNs/Infs to None without Pandas FutureWarning."""
    provider = YFinanceProvider()
    df = pd.DataFrame(
        {
            "symbol": ["AAPL", "MSFT", "GOOG"],
            "pe": [25.0, float("nan"), float("inf")],
            "date": pd.date_range("2026-08-20", periods=3),
        }
    )
    records = provider._df_to_records(df)
    assert len(records) == 3
    assert records[0]["pe"] == 25.0
    assert records[1]["pe"] is None
    assert records[2]["pe"] is None
