"""
Regression test suite for autonomous review findings remediation (2026).
Covers:
1. market_state.py: update_previous_close_cache boolean & non-positive rejection.
2. bg/sse_interpolator.py: _interpolate_and_fluctuate_market boolean price & non-positive rejection.
3. utils/text_utils.py: parse_non_negative_float NumPy boolean scalar rejection.
4. static/js/chart.js: calculateHeikinAshi null/non-object safety.
5. routes/stocks/ai_portfolio.py: api_save_ai_portfolio malformed JSON error handling.
"""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from app import create_app
from app_state import app_state
from bg.sse_interpolator import _interpolate_and_fluctuate_market
from error_codes import ErrorCode
from utils.text_utils import parse_non_negative_float


def test_update_previous_close_cache_rejects_booleans_and_invalid_values():
    """Verify update_previous_close_cache strictly rejects booleans, NaN, Inf, and non-positive numbers."""
    sym = "TEST_BOOL_COERCION"

    # Clean initial state
    app_state.market.clear_previous_close_cache(sym)
    assert app_state.market.get_previous_close_cached(sym) is None

    # Python bools MUST be rejected
    app_state.market.update_previous_close_cache(sym, True)
    assert app_state.market.get_previous_close_cached(sym) is None
    app_state.market.update_previous_close_cache(sym, False)
    assert app_state.market.get_previous_close_cached(sym) is None

    # NumPy bool scalars MUST be rejected
    app_state.market.update_previous_close_cache(sym, np.bool_(True))
    assert app_state.market.get_previous_close_cached(sym) is None
    app_state.market.update_previous_close_cache(sym, np.bool_(False))
    assert app_state.market.get_previous_close_cached(sym) is None

    # Non-positive numbers MUST be rejected
    app_state.market.update_previous_close_cache(sym, 0)
    assert app_state.market.get_previous_close_cached(sym) is None
    app_state.market.update_previous_close_cache(sym, 0.0)
    assert app_state.market.get_previous_close_cached(sym) is None
    app_state.market.update_previous_close_cache(sym, -15.5)
    assert app_state.market.get_previous_close_cached(sym) is None

    # Non-finite values MUST be rejected
    app_state.market.update_previous_close_cache(sym, float("nan"))
    assert app_state.market.get_previous_close_cached(sym) is None
    app_state.market.update_previous_close_cache(sym, float("inf"))
    assert app_state.market.get_previous_close_cached(sym) is None
    app_state.market.update_previous_close_cache(sym, "not_a_number")
    assert app_state.market.get_previous_close_cached(sym) is None

    # Valid positive numbers MUST be cached as floats
    app_state.market.update_previous_close_cache(sym, 185.5)
    assert app_state.market.get_previous_close_cached(sym) == 185.5
    app_state.market.update_previous_close_cache(sym, "220.75")
    assert app_state.market.get_previous_close_cached(sym) == 220.75

    # None clears the cache
    app_state.market.update_previous_close_cache(sym, None)
    assert app_state.market.get_previous_close_cached(sym) is None


def test_sse_interpolator_rejects_booleans_and_non_positive():
    """Verify _interpolate_and_fluctuate_market does not coerce boolean prices and ignores <=0 prices."""
    # Target with boolean price must not interpolate current price to 1.0
    targets = [{"symbol": "AAPL", "price": True, "change": False}]
    currents = [{"symbol": "AAPL", "price": 175.0}]
    result = _interpolate_and_fluctuate_market(targets, currents, is_open=False, market="us")
    assert len(result) == 1
    assert result[0]["price"] == 175.0

    # Target with np.bool_ price must not interpolate
    targets_np = [{"symbol": "AAPL", "price": np.bool_(True), "change": np.bool_(False)}]
    result_np = _interpolate_and_fluctuate_market(targets_np, currents, is_open=False, market="us")
    assert result_np[0]["price"] == 175.0

    # Target with <= 0 price must not interpolate
    targets_zero = [{"symbol": "AAPL", "price": 0.0, "change": 0.0}]
    result_zero = _interpolate_and_fluctuate_market(
        targets_zero, currents, is_open=False, market="us"
    )
    assert result_zero[0]["price"] == 175.0

    targets_neg = [{"symbol": "AAPL", "price": -50.0, "change": 0.0}]
    result_neg = _interpolate_and_fluctuate_market(
        targets_neg, currents, is_open=False, market="us"
    )
    assert result_neg[0]["price"] == 175.0

    # Target with valid price interpolates properly
    targets_valid = [{"symbol": "AAPL", "price": 180.0, "change": 2.0}]
    result_valid = _interpolate_and_fluctuate_market(
        targets_valid, currents, is_open=False, market="us"
    )
    assert result_valid[0]["price"] > 175.0
    assert result_valid[0]["price"] <= 180.0 * 1.01


def test_parse_non_negative_float_rejects_numpy_booleans():
    """Verify parse_non_negative_float strictly rejects both Python bool and np.bool_."""
    # Standard valid
    assert parse_non_negative_float(10.5, "shares") == 10.5
    assert parse_non_negative_float("150.0", "avg_price") == 150.0
    assert parse_non_negative_float(0, "shares") == 0.0

    # Python bool rejected
    with pytest.raises(ValueError, match="shares must be a number"):
        parse_non_negative_float(True, "shares")
    with pytest.raises(ValueError, match="shares must be a number"):
        parse_non_negative_float(False, "shares")

    # NumPy bool rejected
    with pytest.raises(ValueError, match="shares must be a number"):
        parse_non_negative_float(np.bool_(True), "shares")
    with pytest.raises(ValueError, match="shares must be a number"):
        parse_non_negative_float(np.bool_(False), "shares")


def test_calculate_heikin_ashi_null_and_non_object_safety():
    """Verify calculateHeikinAshi in static/js/chart.js safely handles null, undefined, and primitives."""
    node_script = """
    const fs = require('fs');
    const vm = require('vm');
    const ctx = { APP_CONFIG: {}, addEventListener: () => {} };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync('static/js/chart.js', 'utf8'), ctx);

    // Array containing null, undefined, primitives, and valid candle
    const res = ctx.calculateHeikinAshi([
      null,
      undefined,
      123,
      "invalid",
      { x: 1000, o: 100, h: 110, l: 95, c: 105, v: 500 }
    ]);
    if (!Array.isArray(res) || res.length !== 1) {
      throw new Error('Expected 1 candle result, got: ' + JSON.stringify(res));
    }
    if (res[0].x !== 1000 || res[0].o !== 102.5) {
      throw new Error('Unexpected candle output: ' + JSON.stringify(res[0]));
    }
    console.log('OK');
    """
    proc = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node script failed: {proc.stderr}"
    assert "OK" in proc.stdout


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("MNS_DATA_DIR", "tests_runtime_data")
    monkeypatch.setenv("MNS_DISABLE_LOCAL_RATE_LIMIT", "1")
    test_app = create_app(
        config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True
    )
    with test_app.test_client() as c:
        yield c


def test_api_save_ai_portfolio_malformed_json(app_client):
    """Verify /api/ai-portfolio/save strictly rejects malformed JSON with 400 MALFORMED_INPUT."""
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    # Malformed JSON payload
    resp = app_client.post(
        "/api/ai-portfolio/save",
        data="not-valid-json",
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data.get("error_code") == ErrorCode.MALFORMED_INPUT.value
    assert data.get("details", {}).get("reason") == "JSON形式が不正です"

    # Non-dict JSON payload (JSON list)
    resp = app_client.post(
        "/api/ai-portfolio/save",
        data="[1, 2, 3]",
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data.get("error_code") == ErrorCode.MALFORMED_INPUT.value
