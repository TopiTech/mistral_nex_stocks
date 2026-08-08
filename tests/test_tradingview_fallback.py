"""tests/test_tradingview_fallback.py - Verification tests for TradingView exchange auto-detection and fallback logic."""

from unittest.mock import patch

import pandas as pd

from utils.tradingview_mapper import (
    _TICKER_EXCHANGE_CACHE,
    get_tradingview_symbol_meta,
    resolve_exchange_prefix,
)


def test_enhanced_resolve_exchange_prefix():
    """Verify new exchange aliases resolve correctly."""
    assert resolve_exchange_prefix("PCX") == "NYSE"
    assert resolve_exchange_prefix("NYE") == "NYSE"
    assert resolve_exchange_prefix("NYSE ARCA") == "NYSE"
    assert resolve_exchange_prefix("NYSE MKT") == "NYSE"
    assert resolve_exchange_prefix("NAS") == "NASDAQ"
    assert resolve_exchange_prefix("NASDAQ STOCK MARKET") == "NASDAQ"
    assert resolve_exchange_prefix("PNK") == "OTC"
    assert resolve_exchange_prefix("BATS") == "BATS"


def test_ticker_heuristics_for_us_symbols():
    """Verify 1-2 character US symbols and class shares default to NYSE."""
    # Class shares with dot or dash
    symbol, is_fallback, prefix = get_tradingview_symbol_meta("CW.A")
    assert symbol == "NYSE:CW.A"
    assert is_fallback is False
    assert prefix == "NYSE"

    symbol_b, is_fallback_b, prefix_b = get_tradingview_symbol_meta("BRK-B")
    assert symbol_b == "NYSE:BRK-B"
    assert is_fallback_b is False
    assert prefix_b == "NYSE"

    # 1 or 2 letter tickers
    symbol_c, is_fallback_c, _ = get_tradingview_symbol_meta("T")
    assert symbol_c == "NYSE:T"
    assert is_fallback_c is False

    symbol_ge, is_fallback_ge, _ = get_tradingview_symbol_meta("GE")
    assert symbol_ge == "NYSE:GE"
    assert is_fallback_ge is False


def test_get_tradingview_symbol_meta_fallback():
    """Verify fallback detection flag when symbol is unmapped US stock with no exchange."""
    from utils.tradingview_mapper import _CACHE_LOCK

    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("TESTXYZSTOCK", None)

    tv_sym, is_fallback, prefix = get_tradingview_symbol_meta("TESTXYZSTOCK")
    assert tv_sym == "NASDAQ:TESTXYZSTOCK"
    assert is_fallback is True
    assert prefix == "NASDAQ"


def test_stock_payload_integration():
    """Verify build_stock_payload populates tv_symbol and exchange properly."""
    from utils.stock_payload import build_stock_payload

    hist = {
        "Close": [100.0, 105.0],
        "High": [102.0, 106.0],
        "Low": [99.0, 104.0],
        "Open": [100.0, 104.5],
        "Volume": [10000, 12000],
    }
    df_hist = pd.DataFrame(hist)

    with patch("utils.stock_payload.get_stock_info_cached", return_value={"exchange": "NYQ"}):
        payload = build_stock_payload(
            symbol="IONQ",
            name_or_dict="IonQ Inc",
            market="us",
            hist=df_hist,
        )
    assert payload is not None
    assert payload["symbol"] == "IONQ"
    assert payload["exchange"] == "NYSE"
    assert payload["tv_symbol"] == "NYSE:IONQ"
