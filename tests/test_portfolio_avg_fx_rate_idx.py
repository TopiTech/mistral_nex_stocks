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
from route_helpers import ensure_stock_placeholder_in_caches, remove_stock_from_caches
from utils.stock_payload import (
    _default_stock_names,
    _extract_portfolio_fields,
    _get_stock_container,
    _resolve_stocks_for_response,
)
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
    assert "warnings" in data
    assert any("為替レート" in w and "無視" in w for w in data["warnings"])

    with app_state.market.user_stocks_lock:
        holding = app_state.market.user_idx.get("^N225")
        assert holding is not None
        assert "avg_fx_rate" not in holding, (
            f"avg_fx_rate should NOT be stored for idx market, got: {holding}"
        )

    # Verify placeholder in caches does NOT retain avg_fx_rate
    with app_state.cache.sse_data_lock:
        for store in (
            app_state.market.target_stocks_cache,
            app_state.market.current_stocks_cache,
        ):
            entries = store.get("idx", [])
            matched = next((s for s in entries if s.get("symbol") == "^N225"), None)
            if matched:
                assert "avg_fx_rate" not in matched


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
    assert "warnings" in data
    assert any("為替レート" in w and "無視" in w for w in data["warnings"])

    with app_state.market.user_stocks_lock:
        holding = app_state.market.user_jp.get("7203.T")
        assert holding is not None
        assert "avg_fx_rate" not in holding, (
            f"avg_fx_rate should NOT be stored for jp market, got: {holding}"
        )

    # Verify placeholder in caches does NOT retain avg_fx_rate
    with app_state.cache.sse_data_lock:
        for store in (
            app_state.market.target_stocks_cache,
            app_state.market.current_stocks_cache,
        ):
            entries = store.get("jp", [])
            matched = next((s for s in entries if s.get("symbol") == "7203.T"), None)
            if matched:
                assert "avg_fx_rate" not in matched


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


def test_ensure_and_remove_placeholder_market_case_insensitive():
    """ensure_stock_placeholder_in_caches and remove_stock_from_caches handle uppercase markets."""
    # 1. Uppercase JP market
    ensure_stock_placeholder_in_caches(
        "6758.T",
        "ソニーグループ",
        "JP",
        shares=50.0,
        avg_price=12000.0,
        avg_fx_rate=150.0,
    )
    with app_state.cache.sse_data_lock:
        assert "JP" not in app_state.market.target_stocks_cache
        jp_entries = app_state.market.target_stocks_cache.get("jp", [])
        entry = next((s for s in jp_entries if s.get("symbol") == "6758.T"), None)
        assert entry is not None
        assert entry["shares"] == 50.0
        assert entry["avg_price"] == 12000.0
        assert "avg_fx_rate" not in entry

    # Remove with uppercase JP market
    remove_stock_from_caches("6758.T", "JP")
    with app_state.cache.sse_data_lock:
        jp_entries = app_state.market.target_stocks_cache.get("jp", [])
        assert not any(s.get("symbol") == "6758.T" for s in jp_entries)

    # 2. Uppercase US market retains avg_fx_rate
    ensure_stock_placeholder_in_caches(
        "AAPL",
        "Apple Inc.",
        "US",
        shares=10.0,
        avg_price=200.0,
        avg_fx_rate=152.5,
    )
    with app_state.cache.sse_data_lock:
        assert "US" not in app_state.market.target_stocks_cache
        us_entries = app_state.market.target_stocks_cache.get("us", [])
        entry = next((s for s in us_entries if s.get("symbol") == "AAPL"), None)
        assert entry is not None
        assert entry["shares"] == 10.0
        assert entry["avg_price"] == 200.0
        assert entry.get("avg_fx_rate") == 152.5

    remove_stock_from_caches("AAPL", "US")
    with app_state.cache.sse_data_lock:
        us_entries = app_state.market.target_stocks_cache.get("us", [])
        assert not any(s.get("symbol") == "AAPL" for s in us_entries)


def test_market_case_normalization_in_payload_helpers():
    """_get_stock_container, _default_stock_names, and _extract_portfolio_fields handle case gracefully."""
    # _get_stock_container
    assert _get_stock_container("US") is app_state.market.user_us
    assert _get_stock_container("jp") is app_state.market.user_jp
    assert _get_stock_container("JP") is app_state.market.user_jp
    assert _get_stock_container("IDX") is app_state.market.user_idx
    assert _get_stock_container("invalid") is None

    # _default_stock_names
    assert len(_default_stock_names("US")) > 0
    assert len(_default_stock_names("JP")) > 0
    assert len(_default_stock_names("IDX")) > 0
    assert _default_stock_names("invalid") == {}

    # _extract_portfolio_fields
    holding = {"name": "Test", "shares": 10.0, "avg_price": 100.0, "avg_fx_rate": 150.0}
    # US market retains avg_fx_rate even if uppercase
    _, _, _, fx_us = _extract_portfolio_fields(holding, market="US")
    assert fx_us == 150.0

    # Non-US market drops avg_fx_rate even if uppercase
    _, _, _, fx_jp = _extract_portfolio_fields(holding, market="JP")
    assert fx_jp is None

    _, _, _, fx_idx = _extract_portfolio_fields(holding, market="IDX")
    assert fx_idx is None
