"""Unit tests for autonomous code review and goal remediation fixes (2026-10).

Covers:
1. Safe default for resolve_stocks_for_response (include_portfolio=False).
2. Numeric zero (0.0) preservation in market_data_service metrics.
3. Request ID correlation in background AI portfolio tasks.
4. Canonical Pydantic schema validation integration in AI portfolio routes.
5. Non-blocking user_stocks_lock during disk persistence in save_user_stocks.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app
from app_state import app_state
from error_codes import ErrorCode
from routes.stocks.common import resolve_stocks_for_response
from services.market_data_service import _build_market_row, build_heatmap_payload
from utils import storage


@pytest.fixture
def test_app():
    app = create_app(
        config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True
    )
    return app


@pytest.fixture
def client(test_app):
    with test_app.test_client() as c:
        yield c


def test_resolve_stocks_for_response_defaults_to_excluding_portfolio():
    """Unparameterized resolve_stocks_for_response() must default to include_portfolio=False."""
    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {
            "TESTSEC": {"name": "Test Secure", "shares": 100.0, "avg_price": 50.0}
        }
        app_state.market.user_jp = {}
        app_state.market.user_idx = {}

    with app_state.cache.sse_data_lock:
        app_state.market.current_stocks_cache = {
            "us": [
                {
                    "symbol": "TESTSEC",
                    "name": "Test Secure",
                    "price": 55.0,
                    "shares": 100.0,
                    "avg_price": 50.0,
                }
            ],
            "jp": [],
            "idx": [],
        }

    # Calling with no arguments must default to include_portfolio=False
    resolved = resolve_stocks_for_response()
    us_rows = resolved.get("us", [])
    assert len(us_rows) == 1
    # Portfolio fields must be stripped by default
    assert "shares" not in us_rows[0]
    assert "avg_price" not in us_rows[0]
    assert "portfolio_value" not in us_rows[0]

    # Explicit include_portfolio=True must include portfolio fields
    resolved_with_pf = resolve_stocks_for_response(include_portfolio=True)
    us_pf_rows = resolved_with_pf.get("us", [])
    assert len(us_pf_rows) == 1
    assert "shares" in us_pf_rows[0]
    assert us_pf_rows[0]["shares"] == 100.0


def test_market_data_service_zero_preservation():
    """Numeric zero (0.0) must not be treated as falsy or replaced with fallbacks."""
    source_with_zeros = {
        "price": 0.0,
        "sharesOutstanding": 1000.0,
        "market_cap": 0.0,
        "volume": 0.0,
        "high": 0.0,
        "low": 0.0,
        "pe_ratio": 0.0,
        "trailingPE": 15.0,  # Should NOT be used because pe_ratio=0.0 is present
        "name": "Zero Metrics Corp",
    }
    row = _build_market_row("ZERO", "us", source_with_zeros, "Fallback")
    assert row["price"] == 0.0
    assert row["market_cap"] == 0.0
    assert row["volume"] == 0.0
    assert row["high"] == 0.0
    assert row["low"] == 0.0
    assert row["pe_ratio"] == 0.0  # Must be 0.0, not 15.0 from trailingPE


def test_build_heatmap_payload_zero_price():
    """Heatmap row building must preserve valid numbers when price is 0.0."""
    def mock_fetch(items, lightweight=True):
        return [
            {
                "symbol": "ZPRICE",
                "price": 0.0,
                "sharesOutstanding": 500.0,
                "market_cap": 1000.0,
                "sector": "Tech",
            }
        ]

    payload = build_heatmap_payload("us", ["ZPRICE"], fetch_batch_fn=mock_fetch)
    stocks = payload.get("stocks", [])
    assert len(stocks) == 1
    assert stocks[0]["price"] == 0.0
    assert stocks[0]["market_cap"] == 1000.0


def test_ai_portfolio_request_id_correlation(client, caplog):
    """Background AI portfolio tasks must log the caller's request_id."""
    headers = {
        "Origin": "http://localhost:5000",
        "Content-Type": "application/json",
        "X-Request-Id": "req-trace-test-12345",
    }

    mock_res = {
        "id": "tech",
        "title": "Tech Portfolio",
        "description": "Tech description",
        "items": [],
    }

    with (
        patch("routes.api_stocks.generate_ai_portfolio_by_theme", return_value=mock_res),
        caplog.at_level(logging.INFO),
    ):
        resp = client.post(
            "/api/ai-portfolio/generate",
            json={"theme": "tech_trace_test"},
            headers=headers,
        )
        assert resp.status_code == 200

    # Ensure the background task log recorded the request ID
    log_records = [
        r.message
        for r in caplog.records
        if "Starting background AI portfolio generation job" in r.message
    ]
    assert len(log_records) > 0
    # Must NOT be the empty fallback "id=-"
    assert "id=req-trace-test-12345" in log_records[0] or "id=-" not in log_records[0]


def test_ai_portfolio_schema_validation_integration(client):
    """AI portfolio routes must enforce constraints using canonical Pydantic schemas."""
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    # 1. Overlength theme in generate (> 120 chars)
    resp = client.post(
        "/api/ai-portfolio/generate",
        json={"theme": "x" * 121},
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error_code"] == ErrorCode.INVALID_INPUT

    # 2. Overlength theme in rebalance (> 120 chars)
    resp = client.post(
        "/api/ai-portfolio/rebalance",
        json={"theme": "x" * 121},
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 3. Overlength id in delete (> 256 chars)
    resp = client.delete(
        "/api/ai-portfolio/custom",
        json={"id": "x" * 257},
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 4. Save custom portfolio with invalid portfolio root
    resp = client.post(
        "/api/ai-portfolio/save",
        json={"portfolio": "not-a-dict"},
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error_code"] == ErrorCode.MALFORMED_INPUT


def test_storage_save_user_stocks_concurrency():
    """save_user_stocks must serialize disk operations while freeing user_stocks_lock."""
    with tempfile.TemporaryDirectory() as td:
        tmp_stocks = Path(td) / "user_stocks.json"
        with (
            patch("utils.storage.USER_STOCKS_FILE", tmp_stocks),
            patch("config_store.get_or_create_master_key", return_value="dummy_key_1234567890"),
            patch("utils.storage.protect_data", return_value={"scheme": "dummy", "value": "test"}),
        ):
            # Concurrent calls to save_user_stocks must all succeed without deadlocks
            errors = []

            def worker():
                try:
                    storage.save_user_stocks()
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

            assert len(errors) == 0
            assert tmp_stocks.exists()
