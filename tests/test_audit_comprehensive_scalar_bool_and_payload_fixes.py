# tests/test_audit_comprehensive_scalar_bool_and_payload_fixes.py
"""Comprehensive verification for scalar boolean guards, chart OHLC data integrity,
portfolio field extraction, and USD/JPY rate isolation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app_state import app_state
from bg.sync.process import _update_indices_data
from schemas.stocks import PortfolioUpdateRequest, ScreenerQueryRequest
from services.realtime.engine import RealtimeMarketEngine
from services.stock.sanitize import _finite_float, sanitize_fundamental_dict
from utils.normalization import is_bool_like
from utils.stock_payload import (
    _build_chart_ohlc_data,
    _extract_portfolio_fields,
    _finite_or_none,
    get_current_usdjpy_rate,
    get_stock_previous_close,
    get_stock_volume,
)
from utils.text_utils import parse_non_negative_float
from utils.worker_validation import _positive_int


def test_is_bool_like_classification():
    """Verify is_bool_like identifies both Python and NumPy booleans while preserving numbers."""
    assert is_bool_like(True) is True
    assert is_bool_like(False) is True
    assert is_bool_like(np.bool_(True)) is True
    assert is_bool_like(np.bool_(False)) is True

    # Numbers and strings must NEVER be classified as bool-like
    assert is_bool_like(0) is False
    assert is_bool_like(1) is False
    assert is_bool_like(0.0) is False
    assert is_bool_like(1.0) is False
    assert is_bool_like(np.int64(1)) is False
    assert is_bool_like(np.float64(1.0)) is False
    assert is_bool_like("true") is False
    assert is_bool_like("false") is False
    assert is_bool_like(None) is False
    assert is_bool_like([]) is False
    assert is_bool_like({}) is False


def test_finite_or_none_rejects_bools():
    """Verify _finite_or_none returns None for Python and NumPy booleans instead of 1.0/0.0."""
    assert _finite_or_none(True) is None
    assert _finite_or_none(False) is None
    assert _finite_or_none(np.bool_(True)) is None
    assert _finite_or_none(np.bool_(False)) is None

    # Finite numbers must be preserved
    assert _finite_or_none(123.45) == 123.45
    assert _finite_or_none(0) == 0.0
    assert _finite_or_none(-50.0, allow_negative=True) == -50.0
    assert _finite_or_none(-50.0, allow_negative=False) is None
    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(float("inf")) is None


def test_build_chart_ohlc_data_rejects_bools():
    """Verify _build_chart_ohlc_data rejects boolean values and does not fabricate 1.0 candles."""
    dates = pd.date_range("2026-01-01", periods=3, freq="D")
    df = pd.DataFrame(
        {
            "Open": [100.0, np.bool_(True), 102.0],
            "High": [105.0, 105.0, 107.0],
            "Low": [95.0, 95.0, 97.0],
            "Close": [102.0, np.bool_(True), 104.0],
            "Volume": [1000, np.bool_(True), 1500],
            "MA5": [101.0, np.bool_(True), 103.0],
            "MA25": [101.0, 101.0, 101.0],
        },
        index=dates,
    )

    chart, ohlc = _build_chart_ohlc_data(df)
    # The middle row had Close=np.bool_(True). Since boolean Close is rejected (None),
    # it must NOT appear as 1.0 in chart data or produce a valid 1.0 candle.
    for pt in chart:
        assert pt["price"] != 1.0
        assert pt["price"] in (102.0, 104.0)

    # For OHLC, verify no candle has close or volume of 1 from a coerced boolean
    for candle in ohlc:
        assert candle["c"] != 1.0
        if candle["c"] == 102.0:
            assert candle["v"] == 1000
        elif candle["c"] == 104.0:
            assert candle["v"] == 1500


def test_extract_portfolio_fields_rejects_bools():
    """Verify _extract_portfolio_fields ignores boolean inputs and does not coerce to 1.0."""
    # Test with Python bool
    name, shares, avg_price, avg_fx = _extract_portfolio_fields(
        {"name": "Apple", "shares": True, "avg_price": True, "avg_fx_rate": True},
        market="us",
    )
    assert name == "Apple"
    assert shares == 0.0
    assert avg_price == 0.0
    assert avg_fx is None

    # Test with NumPy bool_
    name, shares, avg_price, avg_fx = _extract_portfolio_fields(
        {
            "name": "Toyota",
            "shares": np.bool_(True),
            "avg_price": np.bool_(True),
            "avg_fx_rate": np.bool_(True),
        },
        market="jp",
    )
    assert name == "Toyota"
    assert shares == 0.0
    assert avg_price == 0.0
    assert avg_fx is None

    # Test with valid numbers
    name, shares, avg_price, avg_fx = _extract_portfolio_fields(
        {"name": "Microsoft", "shares": 10.5, "avg_price": 420.0, "avg_fx_rate": 155.0},
        market="us",
    )
    assert name == "Microsoft"
    assert shares == 10.5
    assert avg_price == 420.0
    assert avg_fx == 155.0


def test_get_current_usdjpy_rate_bool_immunity():
    """Verify get_current_usdjpy_rate ignores boolean rates in state or cache."""
    orig_rate = getattr(app_state.market, "last_usdjpy_rate", 150.0)
    orig_ts = getattr(app_state.market, "last_usdjpy_rate_ts", 0.0)
    try:
        # Inject np.bool_(True) into market state
        app_state.market.last_usdjpy_rate = np.bool_(True)
        app_state.market.last_usdjpy_rate_ts = 1700000000.0

        rate, _ = get_current_usdjpy_rate(default_rate=152.0)
        # Must not return 1.0! Must fall back safely.
        assert rate != 1.0
        assert rate > 50.0
    finally:
        app_state.market.last_usdjpy_rate = orig_rate
        app_state.market.last_usdjpy_rate_ts = orig_ts


def test_get_stock_volume_bool_immunity():
    """Verify get_stock_volume ignores boolean values and does not return 1."""
    sym = "TEST_VOL_SYM"
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache[f"info_short_{sym}"] = {
            "regularMarketVolume": np.bool_(True),
            "volume": True,
        }
    try:
        vol = get_stock_volume(sym)
        assert vol != 1
        assert vol is None

        # Test volume in stocks_cache
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache["us"] = [{"symbol": sym, "volume": np.bool_(True)}]
        vol2 = get_stock_volume(sym)
        assert vol2 != 1
        assert vol2 is None
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(f"info_short_{sym}", None)
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache["us"] = []


def test_get_stock_previous_close_bool_immunity():
    """Verify get_stock_previous_close ignores boolean values and does not return 1.0."""
    sym = "TEST_PREV_SYM"
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache[f"info_short_{sym}"] = {
            "previousClose": np.bool_(True),
        }
    try:
        prev = get_stock_previous_close(sym)
        assert prev != 1.0
        assert prev is None

        # Test previous_close and price/change in stocks_cache
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache["us"] = [
                {"symbol": sym, "previous_close": np.bool_(True), "price": True, "change": False}
            ]
        prev2 = get_stock_previous_close(sym)
        assert prev2 != 1.0
        assert prev2 is None
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(f"info_short_{sym}", None)
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache["us"] = []


def test_update_indices_data_ignores_bool_usdjpy():
    """Verify _update_indices_data does not overwrite last_usdjpy_rate with 1.0 on boolean price."""
    orig_rate = getattr(app_state.market, "last_usdjpy_rate", 150.0)
    try:
        app_state.market.last_usdjpy_rate = 153.5
        bogus_idx = [
            {"symbol": "USDJPY=X", "price": np.bool_(True)},
        ]
        _update_indices_data(bogus_idx, [], [])
        # Rate must remain intact, NOT overwritten with 1.0
        assert app_state.market.last_usdjpy_rate == 153.5
    finally:
        app_state.market.last_usdjpy_rate = orig_rate


def test_realtime_engine_rejects_bool_price_in_producer_update():
    """Verify realtime engine does not calculate change = 1 - prev_close when price is bool."""
    engine = RealtimeMarketEngine()
    payload = {
        "symbol": "7203.T",
        "price": np.bool_(True),
    }
    engine._handle_producer_update(payload)
    # The boolean price must not have triggered change / change_percent calculation
    assert "change" not in payload
    assert "change_percent" not in payload


def test_fundamental_sanitize_bool_rejection():
    """Verify sanitize_fundamental_dict drops boolean fields while keeping valid numbers."""
    raw = {
        "marketCap": np.bool_(True),
        "pe": True,
        "trailingPE": 15.2,
        "nested": {"bool_val": np.bool_(False), "num": 100},
    }
    clean = sanitize_fundamental_dict(raw)
    assert "marketCap" not in clean
    assert "pe" not in clean
    assert clean["trailingPE"] == 15.2
    assert "bool_val" not in clean["nested"]
    assert clean["nested"]["num"] == 100

    assert _finite_float(np.bool_(True), default=None) is None
    assert _finite_float(True, default=None) is None
    assert _finite_float(42.5) == 42.5


def test_schema_rejects_scalar_booleans():
    """Verify schemas reject boolean values on numeric fields."""
    with pytest.raises(ValueError):
        PortfolioUpdateRequest(
            market="us",
            symbol="AAPL",
            shares=np.bool_(True),
            avg_price=150.0,
        )

    with pytest.raises(ValueError):
        PortfolioUpdateRequest(
            market="us",
            symbol="AAPL",
            shares=10.0,
            avg_price=np.bool_(True),
        )

    with pytest.raises(ValueError):
        ScreenerQueryRequest(limit=np.bool_(True))

    with pytest.raises(ValueError):
        ScreenerQueryRequest(min_price=np.bool_(True))


def test_worker_and_text_utils_reject_scalar_booleans():
    """Verify worker validation and text utils reject numpy booleans."""
    assert _positive_int(np.bool_(True)) is None
    assert _positive_int(True) is None
    assert _positive_int(4) == 4

    with pytest.raises(ValueError, match="must be a number"):
        parse_non_negative_float(np.bool_(True), "test_field")

    with pytest.raises(ValueError, match="must be a number"):
        parse_non_negative_float(True, "test_field")

    assert parse_non_negative_float(12.34, "test_field") == 12.34
