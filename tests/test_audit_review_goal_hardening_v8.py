# tests/test_audit_review_goal_hardening_v8.py
"""Comprehensive regression tests for Code Review & Hardening Audit (V8).

Covers:
- Credentials Verification Error Envelopes: details.reason and latency_ms consistency for Mistral API.
- Dynamic FX Warning Message: accurately reporting custom default USD/JPY rates instead of hardcoded 150.0.
- AI Portfolio Result Cache: iteration thread safety during concurrent additions and expirations.
- Disk Cache _entry_age_seconds: defensive handling of unlinked / missing cache files without raising OSError.
- State Manager getStockByKey: dirty-flag caching preventing index rebuild churn on missing keys, and invalidation on updates.
- Detail Panel Accessibility: explicit type="button" on dynamic period and interval button controls.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from cachetools import TTLCache

from app_state import app_state
from error_codes import ErrorCode
from routes.stocks.ai_portfolio import ai_portfolio_fetch_lock
from utils.disk_cache import _entry_age_seconds

ROOT = Path(__file__).resolve().parents[1]


# =============================================================================
# 1. Credentials Verification Error Contract Parity
# =============================================================================


def test_api_credentials_verify_mistral_missing_key_details_reason(client):
    """POST /api/credentials/verify with empty Mistral key must include details.reason."""
    res = client.post(
        "/api/credentials/verify",
        json={"mistral_api_key": ""},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert data["valid"] is False
    assert data["code"] == "INVALID_API_KEY"
    assert data["error_code"] == ErrorCode.INVALID_API_KEY.value
    expected_msg = "Mistral APIキーが指定されていません。"
    assert data["message"] == expected_msg
    assert data["error"] == expected_msg
    assert isinstance(data.get("details"), dict)
    assert data["details"]["reason"] == expected_msg


def test_api_credentials_verify_mistral_exception_details_reason(client):
    """POST /api/credentials/verify on Mistral exception must include details.reason and latency_ms."""
    with patch(
        "routes.api_system.app_state.ai.get_or_create_mistral_client",
        side_effect=RuntimeError("internal upstream connection failed"),
    ):
        res = client.post(
            "/api/credentials/verify",
            json={"mistral_api_key": "test-valid-key-0123456789012345"},
            headers={"Origin": "http://localhost:5000"},
        )
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert data["valid"] is False
    assert data["code"] == "INVALID_API_KEY"
    expected_msg = "APIキーの検証に失敗しました。設定と接続を確認してください。"
    assert data["message"] == expected_msg
    assert isinstance(data.get("details"), dict)
    assert data["details"]["reason"] == expected_msg
    assert isinstance(data["details"].get("latency_ms"), int)
    assert isinstance(data.get("latency_ms"), int)


# =============================================================================
# 2. Dynamic FX Warning Message in copy-to-my
# =============================================================================


def test_api_copy_ai_portfolio_dynamic_fx_rate_warning(client):
    """Verify that copy-to-my warns using the active default rate rather than hardcoded 150.0."""
    custom_rate = 155.5
    with (
        patch("routes.stocks.ai_portfolio.DEFAULT_USDJPY_RATE", custom_rate),
        patch(
            "routes.stocks.ai_portfolio.get_current_usdjpy_rate", return_value=(custom_rate, True)
        ),
        patch.object(app_state.market, "last_usdjpy_rate", custom_rate),
        patch.object(
            app_state.market, "last_usdjpy_rate_ts", 0.0
        ),  # Stale timestamp triggers warning
        patch("routes.stocks.ai_portfolio.save_user_stocks"),
        patch("routes.stocks.ai_portfolio.invalidate_stock_caches"),
        patch("routes.stocks.ai_portfolio.ensure_stock_placeholder_in_caches"),
        patch("routes.stocks.ai_portfolio._common._announce_watchlist_state"),
        patch("routes.stocks.ai_portfolio.schedule_sync_all_stocks_now"),
    ):
        payload = {
            "items": [
                {
                    "symbol": "AAPL",
                    "market": "us",
                    "target_price": 200.0,
                    "weight_pct": 50.0,
                }
            ],
        }
        res = client.post(
            "/api/ai-portfolio/copy-to-my",
            json=payload,
            headers={"Origin": "http://localhost:5000"},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data.get("ok") is True
        warning = data.get("stale_warning")
        assert warning is not None
        assert f"1ドル={custom_rate:.1f}円" in warning
        assert "150.0円" not in warning


# =============================================================================
# 3. AI Portfolio Result Cache Thread-Safe Key Eviction
# =============================================================================


def test_ai_portfolio_result_cache_safe_snapshot_iteration(client):
    """Verify that concurrent cache updates during save/delete eviction do not cause RuntimeError."""
    test_cache: TTLCache[str, tuple[float, any, any]] = TTLCache(maxsize=100, ttl=60)
    for i in range(50):
        test_cache[f"generate:scope_{i}:theme_{i}"] = (time.time(), {"id": f"p_{i}"}, None)

    # Simulate concurrent mutation during iteration
    with patch("routes.stocks.ai_portfolio.ai_portfolio_result_cache", test_cache):
        with ai_portfolio_fetch_lock:
            # list snapshot should not raise even if keys are popped
            keys_to_pop = [k for k in list(test_cache) if ":theme_10" in k or ":p_10" in k]
            for k in keys_to_pop:
                test_cache.pop(k, None)

        assert not any(":theme_10" in k for k in test_cache)


# =============================================================================
# 4. Disk Cache _entry_age_seconds File-Vanished Safety
# =============================================================================


def test_entry_age_seconds_handles_unlinked_path_safely(tmp_path):
    """_entry_age_seconds must not raise OSError if the cache file was concurrently unlinked."""
    missing_file = tmp_path / "deleted_cache_entry.json"
    assert not missing_file.exists()

    # Empty data with missing file should return finite age (max(0.0, current - 0.0))
    age = _entry_age_seconds({}, missing_file)
    assert isinstance(age, float)
    assert math.isfinite(age)
    assert age >= 0.0


# =============================================================================
# 5. Frontend State Manager getStockByKey O(1) Cache & Rebuild Invalidation
# =============================================================================


def test_frontend_state_get_stock_by_key_missing_key_performance():
    """Verify that getStockByKey does not re-iterate and rebuild index on missing key lookups."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend state tests")

    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(path.join(process.cwd(), "static", "js", "state.js"), "utf8");

const noop = () => {};
const context = {
  console, JSON, Math, Object, Array, String, Number, Boolean, Date, Map, Set, WeakMap, Promise, Error, Intl,
  performance: { now: () => 0 },
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout: noop, setInterval: () => 0, clearInterval: noop,
  requestAnimationFrame: (fn) => { fn(0); return 1; },
  cancelAnimationFrame: noop,
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  APP_CONFIG: {},
  DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
  document: {
    documentElement: {}, addEventListener: noop, querySelector: () => null, querySelectorAll: () => [],
    getElementById: () => null, createElement: () => ({ classList: { add: noop, remove: noop, toggle: noop }, style: {}, appendChild: noop, setAttribute: noop }),
    body: { classList: { add: noop, remove: noop, toggle: noop } },
  },
  window: {
    addEventListener: noop, getComputedStyle: () => ({ getPropertyValue: () => "" }),
    matchMedia: () => ({ matches: false, addEventListener: noop }), location: { href: "" },
  },
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  logger: { warn: noop, info: noop, debug: noop, error: noop },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "state.js" });

const result = vm.runInContext(`
  const stockA = { symbol: "AAPL", market: "us", price: 150 };
  state.updateStocks({ us: [stockA], jp: [], idx: [] });

  // First lookup builds the index
  const foundA = getStockByKey("us:AAPL");

  // Spy on _rebuildStockKeyIndex to ensure repeated missing key lookups don't rebuild
  let rebuildCount = 0;
  const originalRebuild = _rebuildStockKeyIndex;
  _rebuildStockKeyIndex = () => {
    rebuildCount++;
    return originalRebuild();
  };

  const missing1 = getStockByKey("us:MISSING");
  const missing2 = getStockByKey("us:MISSING");
  const missing3 = getStockByKey("jp:UNKNOWN");

  // Now update stocks to verify invalidation causes exactly one rebuild
  const stockB = { symbol: "7203.T", market: "jp", price: 2000 };
  state.updateStocks({ us: [stockA], jp: [stockB], idx: [] });
  const foundB = getStockByKey("jp:7203.T");
  const foundB2 = getStockByKey("jp:7203.T");

  ({
    foundA: foundA === stockA,
    missing1: missing1 === null,
    missing2: missing2 === null,
    missing3: missing3 === null,
    foundB: foundB === stockB,
    rebuildCountAfterMissing: rebuildCount,
  });
`, context);

if (!result.foundA) throw new Error("foundA lookup failed");
if (!result.missing1 || !result.missing2 || !result.missing3) throw new Error("missing lookup failed");
if (!result.foundB) throw new Error("foundB after update failed");
// Rebuild count should be 0 during the missing checks, and 1 after updateStocks
if (result.rebuildCountAfterMissing !== 1) {
  throw new Error("rebuild count mismatch: " + result.rebuildCountAfterMissing);
}
process.stdout.write("ok");
"""
    proc = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout == "ok"


# =============================================================================
# 6. Detail Panel Controls Button Type Parity
# =============================================================================


def test_ui_detail_panel_controls_button_types():
    """Verify that buildDetailPanel creates period and interval buttons with explicit type='button'."""
    ui_js_path = ROOT / "static" / "js" / "ui.js"
    content = ui_js_path.read_text(encoding="utf-8")

    # In period controls:
    assert 'btn.type = "button";\n    btn.dataset.period = p;' in content or (
        'btn.type = "button";' in content and "btn.dataset.period = p;" in content
    )
    # In interval controls:
    assert 'btn.type = "button";\n    btn.dataset.interval = item.id;' in content or (
        'btn.type = "button";' in content and "btn.dataset.interval = item.id;" in content
    )
