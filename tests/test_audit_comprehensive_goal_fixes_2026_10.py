"""
Comprehensive regression and edge-case tests for 2026-10 Goal Audit Fixes:
1. SSE stream dual-mount routing (/api/stocks/stream and /stocks/stream).
2. Stock details failure envelope with localized error message.
3. NumPy / scalar boolean rejection across fundamental, portfolio, and validation helpers.
4. Positive int and PTS extraction scalar boolean resilience.
"""

from __future__ import annotations

import numpy as np

from app_state import app_state
from services.ai_portfolio_service import sanitize_ai_portfolio
from services.realtime.utils import _extract_pts_fields
from services.stock.sanitize import (
    _DROP_FUNDAMENTAL_VALUE,
    _sanitize_fundamental_value,
    sanitize_fundamental_dict,
)
from utils.validators import _clean_numeric_price, validate_analysis_result
from utils.worker_validation import _positive_int


def test_sse_stream_dual_routing(client):
    """GET /api/stocks/stream and /stocks/stream with mode=0 both route to api_stocks_stream."""
    # Test primary endpoint
    resp1 = client.get("/api/stocks/stream?mode=0")
    assert resp1.status_code == 200
    data1 = resp1.get_json()
    assert isinstance(data1, dict)
    assert data1.get("ok") is True
    assert data1.get("status") == "disabled"

    # Test alias endpoint
    resp2 = client.get("/stocks/stream?mode=0")
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert isinstance(data2, dict)
    assert data2.get("ok") is True
    assert data2.get("status") == "disabled"


def test_stock_details_failure_envelope_has_error(client):
    """GET /api/stock-details returns error field on cache failure so UI error handling activates."""
    symbol = "TESTFAIL_AUDIT"
    cache_key = f"info_short_{symbol}"
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache[cache_key] = {"failed": True}

    try:
        resp = client.get(f"/api/stock-details?symbol={symbol}&market=us")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data.get("ok") is False
        assert data.get("success") is False
        assert data.get("failed") is True
        assert "error" in data
        assert isinstance(data.get("error"), str)
        assert len(data.get("error")) > 0
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(cache_key, None)


def test_sanitize_fundamental_numpy_bool_scalar():
    """_sanitize_fundamental_value and sanitize_fundamental_dict drop np.bool_ scalars."""
    np_true = np.bool_(True)
    np_false = np.bool_(False)

    assert _sanitize_fundamental_value(np_true) is _DROP_FUNDAMENTAL_VALUE
    assert _sanitize_fundamental_value(np_false) is _DROP_FUNDAMENTAL_VALUE

    raw_dict = {
        "is_active": np_true,
        "is_suspended": np_false,
        "valid_metric": 42.5,
        "nested": {"flag": np_true, "score": 10},
    }
    cleaned = sanitize_fundamental_dict(raw_dict)
    assert "is_active" not in cleaned
    assert "is_suspended" not in cleaned
    assert cleaned["valid_metric"] == 42.5
    assert cleaned["nested"] == {"score": 10}


def test_sanitize_ai_portfolio_numpy_bool_scalar():
    """sanitize_ai_portfolio rejects items with numpy bool scalars for weight or target price."""
    # Weight as np.bool_
    portfolio_bad_weight = {
        "name": "Test Portfolio",
        "market": "us",
        "items": [
            {"symbol": "AAPL", "market": "us", "weight_pct": np.bool_(True)},
        ],
    }
    sanitized = sanitize_ai_portfolio(portfolio_bad_weight)
    assert len(sanitized["items"]) == 0

    # Target price as np.bool_
    portfolio_bad_target = {
        "name": "Test Portfolio",
        "market": "us",
        "items": [
            {"symbol": "AAPL", "market": "us", "weight_pct": 20.0, "target_price": np.bool_(True)},
        ],
    }
    sanitized2 = sanitize_ai_portfolio(portfolio_bad_target)
    assert len(sanitized2["items"]) == 0


def test_clean_numeric_price_numpy_bool_scalar():
    """_clean_numeric_price must return None when passed np.bool_ scalars."""
    assert _clean_numeric_price(np.bool_(True)) is None
    assert _clean_numeric_price(np.bool_(False)) is None
    assert _clean_numeric_price(150.25) == 150.25


def test_validate_analysis_result_numpy_bool():
    """validate_analysis_result rejects target_price_3m containing np.bool_."""
    valid_payload = {
        "analysis_summary": "Strong momentum",
        "recommendation": "BUY",
        "sentiment": "bullish",
        "target_price_3m": np.bool_(True),
    }
    ok, reason = validate_analysis_result(valid_payload)
    assert ok is False
    assert "target_price_3m must be numeric" in reason


def test_positive_int_worker_validation_numpy_bool():
    """_positive_int rejects numpy bool scalars."""
    assert _positive_int(np.bool_(True)) is None
    assert _positive_int(np.bool_(False)) is None
    assert _positive_int(4) == 4


def test_extract_pts_fields_numpy_bool():
    """_extract_pts_fields formats numpy bool scalars as 'true' or 'false'."""
    segment = '{"price": {"raw": 100}, "active": {"raw": true}}'
    fields = _extract_pts_fields(segment)
    assert fields.get("active") == "true"
