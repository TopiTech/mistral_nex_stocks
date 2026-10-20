"""Regression tests for market/symbol consistency at watchlist write boundaries.

Root cause: the UI derives a market from the ticker plus the active tab and the
API persisted that pair blindly. A US ticker stored under the ``jp`` market is
valued in JPY because ``build_stock_payload`` derives the currency from the
market, and ``/api/stocks/portfolio`` rejects non ``*.T`` JP holdings, so such a
row can never be valued or given holdings. The write boundaries (main add,
extension add, AI copy-to-my) now reject the mismatch while still normalizing
bare JP securities codes (``7203`` / ``130A`` -> ``*.T``).

The UI inference is covered too: a plain alphabetic ticker must not inherit the
JP tab (which is what produced the mismatched add in the first place), while
4-character JP codes and index symbols keep their existing routing.
"""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app
from app_state import app_state
from error_codes import ErrorCode
from utils.normalization import (
    is_jp_market_symbol,
    market_symbol_mismatch,
    normalize_symbol_for_market,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_app():
    app = create_app(skip_bootstrap=True)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app


def _patch_watchlist_mutations():
    """Patch the side effects of a successful watchlist mutation."""
    return [
        patch("routes.api_stocks.save_user_stocks", return_value=None),
        patch("routes.api_stocks.schedule_sync_all_stocks_now", return_value=None),
        patch("routes.api_stocks._announce_watchlist_state", return_value=None),
        patch("routes.api_stocks._sync_realtime_symbol", return_value=None),
        patch("routes.api_stocks.invalidate_stock_caches", return_value=None),
        patch("routes.api_stocks.ensure_stock_placeholder_in_caches", return_value=None),
    ]


def _reset_containers():
    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {}
        app_state.market.user_jp = {}
        app_state.market.user_idx = {}


# ---------------------------------------------------------------------------
# Shared validation helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "market", "expected"),
    [
        ("AAPL", "jp", "mismatch"),
        ("NVDA", "jp", "mismatch"),
        ("7203.T", "us", "mismatch"),
        ("130A.T", "jp", None),
        ("7203.T", "jp", None),
        ("AAPL", "us", None),
        ("BRK.B", "us", None),
        ("SPY", "idx", None),
        ("7203.T", "idx", None),
    ],
)
def test_market_symbol_mismatch(symbol, market, expected):
    reason = market_symbol_mismatch(symbol, market)
    if expected is None:
        assert reason is None
    else:
        assert isinstance(reason, str) and "mismatch" in reason


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("7203.T", True), ("130A.T", True), ("7203_TEST.T", True), ("7203", False), (None, False)],
)
def test_is_jp_market_symbol(symbol, expected):
    assert is_jp_market_symbol(symbol) is expected


def test_market_symbol_mismatch_ignores_blank_and_non_string():
    assert market_symbol_mismatch(None, "jp") is None
    assert market_symbol_mismatch("", "jp") is None
    assert market_symbol_mismatch(7203, "jp") is None


def test_jp_codes_are_normalized_before_validation():
    # Bare 4-character JP codes are rewritten to *.T, which the validator
    # accepts; this is the shape both add endpoints persist.
    assert normalize_symbol_for_market("130a", "jp") == "130A.T"
    assert market_symbol_mismatch(normalize_symbol_for_market("130a", "jp"), "jp") is None


# ---------------------------------------------------------------------------
# /api/stocks/add
# ---------------------------------------------------------------------------


def test_add_stock_rejects_us_symbol_in_jp_market():
    _reset_containers()
    app = _make_app()
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)

            res = client.post(
                "/api/stocks/add",
                json={"symbol": "AAPL", "market": "jp", "name": "Apple"},
            )
            assert res.status_code == 400
            data = res.get_json()
            assert data["ok"] is False
            assert data["error_code"] == int(ErrorCode.INVALID_MARKET)
            assert "mismatch" in data["details"]["reason"]
            with app_state.market.user_stocks_lock:
                assert "AAPL" not in app_state.market.user_jp
                assert "AAPL.T" not in app_state.market.user_jp

    _reset_containers()


def test_add_stock_rejects_jp_symbol_in_us_market():
    _reset_containers()
    app = _make_app()
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)

            res = client.post(
                "/api/stocks/add",
                json={"symbol": "7203.T", "market": "us", "name": "Toyota"},
            )
            assert res.status_code == 400
            assert res.get_json()["error_code"] == int(ErrorCode.INVALID_MARKET)
            with app_state.market.user_stocks_lock:
                assert "7203.T" not in app_state.market.user_us

    _reset_containers()


@pytest.mark.parametrize(
    ("symbol", "stored"),
    [("6501", "6501.T"), ("130A", "130A.T")],
)
def test_add_stock_accepts_and_normalizes_jp_codes(symbol, stored):
    _reset_containers()
    app = _make_app()
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)

            res = client.post(
                "/api/stocks/add",
                json={"symbol": symbol, "market": "jp", "name": "Test JP"},
            )
            assert res.status_code == 200
            with app_state.market.user_stocks_lock:
                assert stored in app_state.market.user_jp

    _reset_containers()


def test_add_stock_accepts_us_symbol_in_us_market():
    _reset_containers()
    app = _make_app()
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)

            res = client.post(
                "/api/stocks/add",
                json={"symbol": "ZQXUS", "market": "us", "name": "Test US"},
            )
            assert res.status_code == 200
            with app_state.market.user_stocks_lock:
                assert "ZQXUS" in app_state.market.user_us

    _reset_containers()


# ---------------------------------------------------------------------------
# /api/stocks/add_ext
# ---------------------------------------------------------------------------


def test_extension_add_rejects_us_symbol_in_jp_market():
    _reset_containers()
    app = _make_app()
    with (
        app.test_client() as client,
        patch(
            "routes.api_stocks.get_or_create_extension_api_token",
            return_value="extension-test-token",
        ),
        patch("utils.networking._is_allowed_shutdown_origin", return_value=True),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)

            res = client.post(
                "/api/stocks/add_ext",
                json={"symbol": "AAPL", "market": "jp", "name": "Apple"},
                headers={
                    "Authorization": "Bearer extension-test-token",
                    "X-MNS-Extension-Request": "true",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert res.status_code == 400
            assert res.get_json()["error_code"] == int(ErrorCode.INVALID_MARKET)
            with app_state.market.user_stocks_lock:
                assert app_state.market.user_jp == {}

    _reset_containers()


# ---------------------------------------------------------------------------
# /api/ai-portfolio/copy-to-my
# ---------------------------------------------------------------------------


def test_ai_copy_to_my_rejects_market_symbol_mismatch_without_mutation():
    _reset_containers()
    app = _make_app()
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)

            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {
                            "symbol": "NVDA",
                            "market": "jp",
                            "weight_pct": 50.0,
                            "target_price": 150.0,
                        }
                    ]
                },
            )
            assert res.status_code == 400
            data = res.get_json()
            assert data["error_code"] == int(ErrorCode.INVALID_MARKET)
            assert "items[0]" in data["details"]["reason"]
            with app_state.market.user_stocks_lock:
                assert "NVDA" not in app_state.market.user_jp
                assert app_state.market.user_jp == {}

    _reset_containers()


# ---------------------------------------------------------------------------
# Frontend market inference
# ---------------------------------------------------------------------------


def test_infer_market_from_symbol_logic_via_node():
    """A plain alphabetic ticker must never inherit the JP tab."""
    node_script = """
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');
    const assert = require('assert');

    const apiCode = fs.readFileSync(path.join(process.cwd(), 'static/js/api.js'), 'utf-8');
    const match = apiCode.match(/function inferMarketFromSymbol\\([\\s\\S]*?\\n\\}/);
    assert(match, "inferMarketFromSymbol function not found in static/js/api.js");

    const context = {};
    vm.createContext(context);
    vm.runInContext(match[0], context);
    const { inferMarketFromSymbol } = context;

    // JP 4-character securities codes (digits and newer alphanumeric codes)
    assert.strictEqual(inferMarketFromSymbol("7203", "us"), "jp");
    assert.strictEqual(inferMarketFromSymbol("6758", "idx"), "jp");
    assert.strictEqual(inferMarketFromSymbol("130A", "us"), "jp");
    assert.strictEqual(inferMarketFromSymbol("7203.T", "us"), "jp");

    // Index / ETF tickers
    assert.strictEqual(inferMarketFromSymbol("^N225", "jp"), "idx");
    assert.strictEqual(inferMarketFromSymbol("SPY", "idx"), "idx");

    // Plain alphabetic tickers are US tickers, even on the JP tab
    assert.strictEqual(inferMarketFromSymbol("AAPL", "us"), "us");
    assert.strictEqual(inferMarketFromSymbol("AAPL", "jp"), "us");
    assert.strictEqual(inferMarketFromSymbol("BRK.B", "jp"), "us");
    assert.strictEqual(inferMarketFromSymbol("AAPL", "portfolio"), "us");
    assert.strictEqual(inferMarketFromSymbol("", "jp"), "jp");
    assert.strictEqual(inferMarketFromSymbol("", "us"), "us");

    // Client-side JP code normalization matches what the backend persists
    const normMatch = apiCode.match(
      /const normalizeSymbolForMarketClient = \\(symbol, market\\) => \\{[\\s\\S]*?\\n\\};/
    );
    assert(normMatch, "normalizeSymbolForMarketClient not found");
    const normalizeSymbolForMarketClient = vm.runInNewContext(
      normMatch[0] + "; normalizeSymbolForMarketClient"
    );
    assert.strictEqual(normalizeSymbolForMarketClient("7203", "jp"), "7203.T");
    assert.strictEqual(normalizeSymbolForMarketClient("130a", "jp"), "130A.T");
    assert.strictEqual(normalizeSymbolForMarketClient("AAPL", "us"), "AAPL");
    assert.strictEqual(normalizeSymbolForMarketClient("7203", "us"), "7203");

    console.log("INFER_MARKET_CONSISTENCY_SUCCESS");
    """
    proc = subprocess.run(
        ["node", "-e", node_script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "INFER_MARKET_CONSISTENCY_SUCCESS" in proc.stdout
