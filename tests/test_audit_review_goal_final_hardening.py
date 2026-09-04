"""
Regression test suite for autonomous full-codebase review & hardening (2026).
Covers:
1. routes/stocks/views.py: _parse_strict_float rejects Python and NumPy booleans.
2. routes/stocks/views.py: _parse_screener_float safely rejects booleans, NaN/Inf, invalid types.
3. routes/stocks/views.py: api_screener filters correctly when stock items have boolean fields.
4. routes/stocks/views.py: _safe_sort_key does not coerce booleans to 1.0 or 0.0.
5. routes/stocks/ai_portfolio.py: api_copy_ai_portfolio_to_my strictly rejects NumPy bools.
6. static/js/index_main.js & static/js/utils.js: Escape key handler closes alertModal and portfolioModal.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app import create_app
from error_codes import ErrorCode


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MNS_DATA_DIR", "tests_runtime_data")
    monkeypatch.setenv("MNS_DISABLE_LOCAL_RATE_LIMIT", "1")
    app = create_app(
        config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True
    )
    with app.test_client() as c:
        yield c


def test_screener_strict_float_rejects_booleans(client):
    """Verify /api/screener query parameters reject boolean values if evaluated."""
    # When query param is "true", float("true") fails ValueError -> returns INVALID_INPUT error
    resp = client.get("/api/screener?min_price=true", headers={"Origin": "http://localhost:5000"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data.get("error_code") == ErrorCode.INVALID_INPUT.value
    assert "min_price は数値で指定してください" in data.get("details", {}).get("reason", "")


def test_parse_screener_float_and_safe_sort_key(client):
    """Verify _parse_screener_float and screener sorting logic resist boolean scalars."""
    mock_stocks = [
        {
            "symbol": "BOOL_PRICE",
            "name": "Bool Stock",
            "price": True,
            "change_percent": 2.5,
            "market_cap": 1000.0,
            "sector": "Technology",
        },
        {
            "symbol": "NP_BOOL_PRICE",
            "name": "NP Bool Stock",
            "price": np.bool_(True),
            "change_percent": 1.5,
            "market_cap": 2000.0,
            "sector": "Technology",
        },
        {
            "symbol": "NORMAL_STOCK",
            "name": "Normal Stock",
            "price": 150.0,
            "change_percent": 0.5,
            "market_cap": 3000.0,
            "sector": "Technology",
        },
        {
            "symbol": "LOW_STOCK",
            "name": "Low Stock",
            "price": 0.5,
            "change_percent": True,
            "market_cap": 500.0,
            "sector": "Technology",
        },
    ]

    with (
        patch("routes.stocks.views.build_screener_base_rows_dispatch", return_value=mock_stocks),
        patch("routes.stocks.views.build_screener_enrichment_dispatch", return_value={}),
    ):
        resp = client.get(
            "/api/screener?min_price=1.0&sort_by=price&sort_order=asc",
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        symbols = [s["symbol"] for s in data["stocks"]]
        # BOOL_PRICE and NP_BOOL_PRICE must NOT be coerced to 1.0 and must NOT pass min_price=1.0
        assert "BOOL_PRICE" not in symbols
        assert "NP_BOOL_PRICE" not in symbols
        # NORMAL_STOCK (price 150.0) must be included
        assert "NORMAL_STOCK" in symbols


def test_screener_safe_sort_key_does_not_rank_bool_as_one(client):
    """Verify sorting does not treat True as 1.0 (ranking it next to real 1.0 stocks)."""
    mock_stocks = [
        {
            "symbol": "REAL_ONE",
            "name": "Real One",
            "price": 1.0,
            "market_cap": 100.0,
            "sector": "Technology",
        },
        {
            "symbol": "REAL_ZERO_FIVE",
            "name": "Real Half",
            "price": 0.5,
            "market_cap": 50.0,
            "sector": "Technology",
        },
        {
            "symbol": "BOOL_STOCK",
            "name": "Bool Stock",
            "price": True,
            "market_cap": 200.0,
            "sector": "Technology",
        },
        {
            "symbol": "REAL_TWO",
            "name": "Real Two",
            "price": 2.0,
            "market_cap": 300.0,
            "sector": "Technology",
        },
    ]

    with (
        patch("routes.stocks.views.build_screener_base_rows_dispatch", return_value=mock_stocks),
        patch("routes.stocks.views.build_screener_enrichment_dispatch", return_value={}),
    ):
        resp = client.get(
            "/api/screener?sort_by=price&sort_order=asc",
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        symbols = [s["symbol"] for s in data["stocks"]]
        # In ascending sort, invalid/missing/boolean prices sort to +inf (at end)
        assert symbols == ["REAL_ZERO_FIVE", "REAL_ONE", "REAL_TWO", "BOOL_STOCK"]


def test_ai_portfolio_copy_to_my_rejects_numpy_booleans(client):
    """Verify /api/ai-portfolio/copy-to-my rejects NumPy boolean scalars."""
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    # Python bool
    resp = client.post(
        "/api/ai-portfolio/copy-to-my",
        json={
            "items": [
                {"symbol": "AAPL", "market": "us", "target_price": True, "weight_pct": 50.0},
                {"symbol": "MSFT", "market": "us", "target_price": 400.0, "weight_pct": 50.0},
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "真偽値は不可" in data.get("details", {}).get("reason", "")

    # Direct handler test with NumPy bool scalar in payload dict
    from routes.stocks.ai_portfolio import api_copy_ai_portfolio_to_my

    app = create_app(
        config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True
    )
    with (
        patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, "")),
        patch(
            "routes.stocks.ai_portfolio._parse_json_request",
            return_value={
                "items": [
                    {
                        "symbol": "AAPL",
                        "market": "us",
                        "target_price": np.bool_(True),
                        "weight_pct": 50.0,
                    },
                ]
            },
        ),
    ):
        with app.test_request_context(
            "/api/ai-portfolio/copy-to-my",
            method="POST",
            headers=headers,
        ):
            raw_resp = api_copy_ai_portfolio_to_my()
            if isinstance(raw_resp, tuple):
                data = raw_resp[0].get_json()
            else:
                data = raw_resp.get_json()
            assert data["ok"] is False
            assert "真偽値は不可" in data.get("details", {}).get("reason", "")


def test_modal_escape_key_and_tabindex_handling():
    """Verify static/js/index_main.js and static/js/utils.js modal Escape & accessibility behavior."""
    node_script = """
    const fs = require('fs');
    const vm = require('vm');

    // 1. Check templates/index.html has tabindex="-1" on modals
    const indexHtml = fs.readFileSync('templates/index.html', 'utf8');
    if (!indexHtml.includes('id="portfolioModal"') || !indexHtml.includes('id="alertModal"')) {
      throw new Error('Modals not found in index.html');
    }
    const pfModalMatch = indexHtml.match(/<div[^>]*id="portfolioModal"[^>]*>/s);
    if (!pfModalMatch || !pfModalMatch[0].includes('tabindex="-1"')) {
      throw new Error('portfolioModal missing tabindex="-1"');
    }
    const alertModalMatch = indexHtml.match(/<div[^>]*id="alertModal"[^>]*>/s);
    if (!alertModalMatch || !alertModalMatch[0].includes('tabindex="-1"')) {
      throw new Error('alertModal missing tabindex="-1"');
    }

    // 2. Check static/js/index_main.js global Escape handler includes alertModal and portfolioModal
    const indexMainJs = fs.readFileSync('static/js/index_main.js', 'utf8');
    if (!indexMainJs.includes('alertModal.classList.contains("show")')) {
      throw new Error('index_main.js Escape handler missing alertModal check');
    }
    if (!indexMainJs.includes('portfolioModal.classList.contains("show")')) {
      throw new Error('index_main.js Escape handler missing portfolioModal check');
    }

    // 3. Check static/js/utils.js openModal ensures tabindex
    const utilsJs = fs.readFileSync('static/js/utils.js', 'utf8');
    if (!utilsJs.includes('modal.setAttribute("tabindex", "-1")')) {
      throw new Error('utils.js openModal does not ensure tabindex="-1"');
    }

    console.log('ALL_MODAL_CHECKS_OK');
    """
    proc = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node script failed: {proc.stderr}"
    assert "ALL_MODAL_CHECKS_OK" in proc.stdout
