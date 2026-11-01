# tests/test_audit_review_goal_hardening_v7.py
"""Comprehensive regression tests for Code Review & Hardening Audit (V7).

Covers:
- Concurrency & Thread Joining in RealtimeMarketEngine.stop() during in-flight PTS fetch drain.
- Market Logic & Symbol Normalization in RealtimeMarketEngine._handle_pts_update.
- Safe serialization of NumPy / Pandas numeric & boolean types in AI tool calling loops.
- Unified response envelope compliance (ok_response) for /api/heatmap endpoint.
"""

from __future__ import annotations

import json
import math
import threading
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from flask import Flask

from routes.api_stocks import api_stocks_bp
from services.ai_service import _json_safe_tool_value as mistral_json_safe
from services.openai_service import _json_safe_tool_value as openai_json_safe
from services.realtime.engine import RealtimeMarketEngine

# =============================================================================
# 1. RealtimeMarketEngine.stop() PTS Thread Joining
# =============================================================================


def test_realtime_engine_stop_joins_thread_after_fetch_drain():
    """Verify that stop() joins the PTS thread after an in-flight fetch drains,

    ensuring pts_thread is cleanly set to None.
    """
    engine = RealtimeMarketEngine()
    entered = threading.Event()
    release = threading.Event()

    def fake_fetch(symbol, use_primary=True):
        entered.set()
        assert release.wait(timeout=10.0), "Fetch release signal timed out"
        return {"symbol": symbol, "price": 1000.0}

    patches = [
        patch.object(engine.yahoojp_scraper, "_is_startup_ready", return_value=True),
        patch("services.realtime.engine._is_scraper_blocked", return_value=False),
        patch("services.realtime.engine._is_yf_rate_limited", return_value=False),
        patch("services.realtime.engine.is_pts_session", return_value=True),
        patch("services.realtime.engine._dedupe_pts_symbols", return_value=["7203.T"]),
        patch.object(engine, "_pts_cached_payload", return_value=None),
        patch.object(engine, "_fetch_pts_with_fallback", side_effect=fake_fetch),
        patch.object(engine.tv_client, "start"),
        patch.object(engine.yahoojp_scraper, "start"),
    ]

    for p in patches:
        p.start()

    try:
        engine.start()
        assert entered.wait(timeout=5.0), "PTS loop did not enter fetch"

        # Worker is in-flight: tracker is not set
        tracker = engine._pts_fetch_tracker()
        assert tracker is not None
        assert not tracker.is_set()

        # Release fetch shortly after stop() is triggered
        threading.Timer(0.2, release.set).start()

        stopped = engine.stop()
        assert stopped is True
        assert engine.pts_thread is None
    finally:
        release.set()
        try:
            engine.stop()
        finally:
            for p in patches:
                p.stop()


# =============================================================================
# 2. RealtimeMarketEngine._handle_pts_update Market & Registration Logic
# =============================================================================


def test_realtime_engine_pts_update_stale_registration_early_return():
    """Verify that an outdated registration aborts _handle_pts_update early

    without calling _get_yfinance_previous_close or mutating pts_store.
    """
    engine = RealtimeMarketEngine()
    token = object()
    stale_epoch = engine._engine_epoch - 1
    registration = ("jp", "7203.T", token, stale_epoch)

    payload = {"symbol": "7203.T", "price": 2500.0}

    with patch("services.realtime.engine._get_yfinance_previous_close") as mock_prev_close:
        engine._handle_pts_update(payload, registration=registration)
        mock_prev_close.assert_not_called()

    assert "7203.T" not in engine.pts_store


def test_realtime_engine_pts_update_jp_alphanumeric_precision():
    """Verify that Japanese tickers including alphanumeric TSE codes (e.g. 130A)

    are correctly recognized as JP and rounded to 2 decimal places.
    """
    engine = RealtimeMarketEngine()
    token = object()
    current_epoch = engine._engine_epoch
    engine._registration_tokens[("jp", "130A")] = token
    registration = ("jp", "130A", token, current_epoch)

    payload = {
        "symbol": "130A",
        "price": 1234.5678,
        "change": 12.3456,
        "change_percent": 1.2345,
    }

    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=1200.0):
        engine._handle_pts_update(payload, registration=registration)

    stored = engine.pts_store.get("130A")
    assert stored is not None
    assert stored["price"] == 1234.57
    assert stored["change"] == 12.35
    assert stored["change_percent"] == 1.23


def test_realtime_engine_pts_update_us_precision():
    """Verify that non-Japanese tickers (US stocks) retain 4 decimal places."""
    engine = RealtimeMarketEngine()
    token = object()
    current_epoch = engine._engine_epoch
    engine._registration_tokens[("us", "AAPL")] = token
    registration = ("us", "AAPL", token, current_epoch)

    payload = {
        "symbol": "AAPL",
        "price": 234.5678,
        "change": 2.3456,
        "change_percent": 1.0123,
    }

    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=230.0):
        engine._handle_pts_update(payload, registration=registration)

    stored = engine.pts_store.get("AAPL")
    assert stored is not None
    assert stored["price"] == 234.5678
    assert stored["change"] == 2.3456
    assert stored["change_percent"] == 1.01


# =============================================================================
# 3. AI Service Tool Call Serialization with NumPy / Pandas Types
# =============================================================================


def test_ai_service_json_safe_tool_value_numpy_pandas():
    """Verify that _json_safe_tool_value in both Mistral and OpenAI agent loops

    safely serializes NumPy integers, floats, booleans, and Pandas structures
    without coercing them to strings or raising serialization errors.
    """
    for helper in (mistral_json_safe, openai_json_safe):
        test_data = {
            "int_val": np.int64(42),
            "int32_val": np.int32(100),
            "float_val": np.float64(3.14159),
            "bool_val": np.bool_(True),
            "bool_false": np.bool_(False),
            "nan_val": float("nan"),
            "inf_val": float("inf"),
            "ndarray": np.array([np.int64(1), np.int64(2), np.int64(3)]),
            "series": pd.Series([10.5, np.nan, 30.5]),
            "nested": {
                "sub_int": np.int64(99),
                "sub_bool": np.bool_(True),
            },
        }

        sanitized = helper(test_data)

        # Assert types are native Python JSON-serializable types, NOT strings
        assert isinstance(sanitized["int_val"], int) and not isinstance(sanitized["int_val"], bool)
        assert sanitized["int_val"] == 42

        assert isinstance(sanitized["int32_val"], int) and not isinstance(sanitized["int32_val"], bool)
        assert sanitized["int32_val"] == 100

        assert isinstance(sanitized["float_val"], float)
        assert math.isclose(sanitized["float_val"], 3.14159, rel_tol=1e-5)

        assert isinstance(sanitized["bool_val"], bool)
        assert sanitized["bool_val"] is True

        assert isinstance(sanitized["bool_false"], bool)
        assert sanitized["bool_false"] is False

        assert sanitized["nan_val"] is None
        assert sanitized["inf_val"] is None

        assert sanitized["ndarray"] == [1, 2, 3]
        assert sanitized["series"] == [10.5, None, 30.5]
        assert sanitized["nested"] == {"sub_int": 99, "sub_bool": True}

        # Verify strict JSON dumping succeeds without allow_nan
        dumped = json.dumps(sanitized, ensure_ascii=False, allow_nan=False)
        assert '"int_val": 42' in dumped
        assert '"bool_val": true' in dumped
        assert '"bool_false": false' in dumped


# =============================================================================
# 4. Heatmap API Unified ok_response Envelope
# =============================================================================


@pytest.fixture
def heatmap_app():
    """Create Flask test app configured with stock routes."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(api_stocks_bp)
    return app


def test_api_heatmap_memory_cached_envelope(heatmap_app):
    """Verify that /api/heatmap returns unified ok_response from memory cache."""
    client = heatmap_app.test_client()
    cached_payload = {
        "stocks": [{"symbol": "AAPL", "change_percent": 1.5}],
        "timestamp": 1234567890.0,
    }

    with (
        patch("routes.stocks.views._get_cached_value_if_present", return_value=(True, cached_payload)),
        patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, None)),
    ):
        res = client.get("/api/heatmap?market=us")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert data["stocks"] == cached_payload["stocks"]


def test_api_heatmap_disk_cached_envelope(heatmap_app):
    """Verify that /api/heatmap returns unified ok_response from disk cache."""
    from app_state import app_state

    client = heatmap_app.test_client()
    disk_payload = {
        "stocks": [{"symbol": "7203.T", "change_percent": -0.8}],
        "timestamp": 1234567890.0,
    }

    with (
        patch("routes.stocks.views._get_cached_value_if_present", return_value=(False, None)),
        patch.object(app_state.payload_disk_cache, "get", return_value=disk_payload),
        patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, None)),
    ):
        res = client.get("/api/heatmap?market=jp")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert data["stocks"] == disk_payload["stocks"]


def test_api_heatmap_fetching_fallback_envelope(heatmap_app):
    """Verify that /api/heatmap returns unified ok_response when fetching in background."""
    from app_state import app_state

    client = heatmap_app.test_client()

    with (
        patch("routes.stocks.views._get_cached_value_if_present", return_value=(False, None)),
        patch.object(app_state.payload_disk_cache, "get", return_value=None),
        patch.object(app_state.execution.data_executor, "submit"),
        patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, None)),
    ):
        res = client.get("/api/heatmap?market=us")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert data["fetching"] is True
        assert data["stocks"] == []
        assert "message" in data
