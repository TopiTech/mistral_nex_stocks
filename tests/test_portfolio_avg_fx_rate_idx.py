"""Regression test for avg_fx_rate handling in portfolio updates.

Verifies that avg_fx_rate (FX rate) is NOT applied to non-US markets
(`idx` for indices/ETFs and `jp` for Japanese domestic equities),
since non-US holdings are JPY-denominated and do not support USD/JPY tracking.

Before fix:
- `idx` and `jp` could retain or leak avg_fx_rate across boundaries.
- Test harness asserted against dummy `app_state.market.user_stocks["idx"]`
  which bypassed real `app_state.market.user_idx` and `user_jp`.
After fix:
- avg_fx_rate is strictly stripped for non-US markets on input, persistence,
  and snapshot responses.
- Test harness accurately exercises `app_state.market.user_idx` and `user_jp`.

Issue: Finding R26
"""

import json
from unittest.mock import patch

import pytest

from app import app
from app_state import app_state
from utils.stock_payload import _resolve_stocks_for_response
from utils.storage import _normalize_idx_holding_fields, _normalize_jp_holding_keys


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_portfolio_state():
    """Reset portfolio state before each test."""
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.clear()
        app_state.market.user_jp.clear()
        app_state.market.user_idx.clear()
    app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}


def _add_idx_symbol():
    """Helper to add an idx symbol to the watch list."""
    with app_state.market.user_stocks_lock:
        app_state.market.user_idx["^N225"] = {
            "name": "Nikkei 225", "symbol": "^N225", "market": "idx"
        }
    app_state.market.target_stocks_cache["idx"] = [
        {"symbol": "^N225", "name": "Nikkei 225", "market": "idx"}
    ]
    app_state.market.current_stocks_cache["idx"] = [
        {"symbol": "^N225", "name": "Nikkei 225", "market": "idx"}
    ]


def _add_jp_symbol():
    """Helper to add a jp symbol to the watch list."""
    with app_state.market.user_stocks_lock:
        app_state.market.user_jp["7203.T"] = {
            "name": "トヨタ自動車", "symbol": "7203.T", "market": "jp"
        }
    app_state.market.target_stocks_cache["jp"] = [
        {"symbol": "7203.T", "name": "トヨタ自動車", "market": "jp"}
    ]
    app_state.market.current_stocks_cache["jp"] = [
        {"symbol": "7203.T", "name": "トヨタ自動車", "market": "jp"}
    ]


def _portfolio_post(client, payload):
    """Helper to POST to portfolio endpoint with proper origin headers."""
    return client.post(
        "/api/stocks/portfolio",
        data=json.dumps(payload),
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )


def test_idx_market_rejects_avg_fx_rate(client):
    """avg_fx_rate must NOT be stored for idx market holdings."""
    _add_idx_symbol()

    resp = _portfolio_post(client, {
        "symbol": "^N225",
        "market": "idx",
        "shares": 10.0,
        "avg_price": 38000.0,
        "avg_fx_rate": 150.0,
    })
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["success"] is True

    with app_state.market.user_stocks_lock:
        holding = app_state.market.user_idx.get("^N225")
        assert holding is not None
        assert "avg_fx_rate" not in holding, (
            f"avg_fx_rate should NOT be stored for idx market, got: {holding}"
        )


def test_idx_market_without_avg_fx_rate(client):
    """idx market update without avg_fx_rate works normally."""
    _add_idx_symbol()

    resp = _portfolio_post(client, {
        "symbol": "^N225",
        "market": "idx",
        "shares": 10.0,
        "avg_price": 38000.0,
    })
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["success"] is True

    with app_state.market.user_stocks_lock:
        holding = app_state.market.user_idx.get("^N225")
        assert holding is not None
        assert "avg_fx_rate" not in holding


def test_legacy_idx_avg_fx_rate_is_ignored_in_portfolio_snapshot(monkeypatch):
    """Old idx holdings must not apply a stale USD/JPY rate to P&L."""
    monkeypatch.setattr(
        app_state.market,
        "user_idx",
        {
            "^GSPC": {
                "name": "S&P 500",
                "symbol": "^GSPC",
                "market": "idx",
                "shares": 10.0,
                "avg_price": 100.0,
                "avg_fx_rate": 155.0,
            }
        },
    )
    app_state.market.current_stocks_cache["idx"] = [
        {
            "symbol": "^GSPC",
            "name": "S&P 500",
            "market": "idx",
            "currency": "USD",
            "price": 110.0,
        }
    ]

    with patch("utils.stock_payload.get_current_usdjpy_rate", return_value=(150.0, False)):
        result = _resolve_stocks_for_response(include_portfolio=True)

    row = result["idx"][0]
    assert "avg_fx_rate" not in row
    assert row["portfolio_value"] == 165000.0
    assert row["portfolio_pl"] == 15000.0


def test_idx_holding_normalization_removes_legacy_avg_fx_rate():
    raw = {
        "^GSPC": {"name": "S&P 500", "avg_price": 100.0, "avg_fx_rate": 155.0},
        "^N225": {"name": "Nikkei 225", "avg_price": 38_000.0},
    }

    normalized = _normalize_idx_holding_fields(raw)

    assert "avg_fx_rate" not in normalized["^GSPC"]
    assert "avg_fx_rate" in raw["^GSPC"]
    assert normalized["^N225"]["avg_price"] == 38_000.0


def test_jp_market_rejects_avg_fx_rate(client):
    """avg_fx_rate must NOT be stored for jp market holdings."""
    _add_jp_symbol()

    resp = _portfolio_post(client, {
        "symbol": "7203.T",
        "market": "jp",
        "shares": 100.0,
        "avg_price": 2000.0,
        "avg_fx_rate": 150.0,
    })
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["success"] is True

    with app_state.market.user_stocks_lock:
        holding = app_state.market.user_jp.get("7203.T")
        assert holding is not None
        assert "avg_fx_rate" not in holding, (
            f"avg_fx_rate should NOT be stored for jp market, got: {holding}"
        )


def test_jp_market_without_avg_fx_rate(client):
    """jp market update without avg_fx_rate works normally."""
    _add_jp_symbol()

    resp = _portfolio_post(client, {
        "symbol": "7203.T",
        "market": "jp",
        "shares": 100.0,
        "avg_price": 2000.0,
    })
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["success"] is True

    with app_state.market.user_stocks_lock:
        holding = app_state.market.user_jp.get("7203.T")
        assert holding is not None
        assert "avg_fx_rate" not in holding


def test_legacy_jp_avg_fx_rate_is_ignored_in_portfolio_snapshot(monkeypatch):
    """Stale/corrupted avg_fx_rate on jp holdings must be stripped from snapshot."""
    monkeypatch.setattr(
        app_state.market,
        "user_jp",
        {
            "7203.T": {
                "name": "トヨタ自動車",
                "symbol": "7203.T",
                "market": "jp",
                "shares": 100.0,
                "avg_price": 2000.0,
                "avg_fx_rate": 155.0,
            }
        },
    )
    app_state.market.current_stocks_cache["jp"] = [
        {
            "symbol": "7203.T",
            "name": "トヨタ自動車",
            "market": "jp",
            "currency": "JPY",
            "price": 2200.0,
        }
    ]

    result = _resolve_stocks_for_response(include_portfolio=True)

    row = result["jp"][0]
    assert "avg_fx_rate" not in row
    assert row["portfolio_value"] == 220000.0
    assert row["portfolio_pl"] == 20000.0


def test_jp_holding_normalization_removes_legacy_avg_fx_rate():
    raw = {
        "7203": {"name": "トヨタ自動車", "avg_price": 2000.0, "avg_fx_rate": 155.0},
        "6758.T": {"name": "ソニーグループ", "avg_price": 12000.0, "avg_fx_rate": 150.0},
    }

    normalized = _normalize_jp_holding_keys(raw)

    assert "7203.T" in normalized
    assert "avg_fx_rate" not in normalized["7203.T"]
    assert "avg_fx_rate" not in normalized["6758.T"]
    assert "avg_fx_rate" in raw["7203"]
    assert "avg_fx_rate" in raw["6758.T"]
