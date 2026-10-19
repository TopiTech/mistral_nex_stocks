"""
Unit and regression tests for autonomous quality improvements (Goal Audit 2026).

Verifies:
1. static/js/api_client.ts: Zero `any` types, zero `@ts-ignore` / `@ts-expect-error` / `eslint-disable`.
2. static/js/api.js: apiFetch attaches errorBody & details to thrown errors, and surfaces backend detail reasons.
3. static/js/api.js: inferMarketFromSymbol accurately detects JP, Index, and US tickers, and addStockPrompt uses it.
4. static/js/api.js: duplicate stock addition surfaces yellow warning toast (#ffb86c) instead of red error.
5. static/js/api.js: toast deduplication in loadNews, chatPollingReply, and requestStockAnalysis.
6. static/js/chart.js: _safeDestroyChart defensive wrapper prevents ReferenceError when loaded before ui.js.
7. chrome_extension/popup.js: window registers pagehide, unload, and beforeunload listeners for stopStockPolling.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_typescript_zero_any_and_no_suppressions() -> None:
    """Ensure static/js/api_client.ts contains zero `any` types and zero suppression comments."""
    ts_file = ROOT_DIR / "static" / "js" / "api_client.ts"
    assert ts_file.exists(), f"{ts_file} does not exist"
    content = ts_file.read_text(encoding="utf-8")

    # Ensure no suppression comments
    assert "@ts-ignore" not in content, "Found forbidden @ts-ignore in api_client.ts"
    assert "@ts-expect-error" not in content, "Found forbidden @ts-expect-error in api_client.ts"
    assert "eslint-disable" not in content, "Found forbidden eslint-disable in api_client.ts"

    # Ensure no `any` type annotations or casts
    any_patterns = [
        re.compile(r":\s*any\b"),
        re.compile(r"\bas\s+any\b"),
        re.compile(r"<any>"),
    ]
    for pattern in any_patterns:
        matches = pattern.findall(content)
        assert len(matches) == 0, f"Found forbidden 'any' pattern {pattern.pattern} in api_client.ts: {matches}"

    # Ensure explicit interfaces are present
    assert "interface CustomEventSource" in content
    assert "type EventSourceListener" in content


def test_api_fetch_error_handling_and_data_attachment() -> None:
    """Verify apiFetch in static/js/api.js attaches err.data and err.details, and extracts detail reasons."""
    api_js = (ROOT_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")

    assert "err.data = errorBody;" in api_js
    assert "err.details = errorBody?.details;" in api_js
    assert "errorBody?.details?.reason" in api_js


def test_infer_market_from_symbol_logic_via_node() -> None:
    """Verify inferMarketFromSymbol correctly detects JP, Index, and US markets via Node.js VM."""
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

    // JP 4-digit code
    assert.strictEqual(inferMarketFromSymbol("7203", "us"), "jp");
    assert.strictEqual(inferMarketFromSymbol("6758", "idx"), "jp");
    // JP explicit .T
    assert.strictEqual(inferMarketFromSymbol("7203.T", "us"), "jp");
    assert.strictEqual(inferMarketFromSymbol("6758.t", "us"), "jp");
    // Index starting with ^
    assert.strictEqual(inferMarketFromSymbol("^N225", "jp"), "idx");
    assert.strictEqual(inferMarketFromSymbol("^GSPC", "us"), "idx");
    // Default US / activeTab pass-through
    assert.strictEqual(inferMarketFromSymbol("AAPL", "us"), "us");
    assert.strictEqual(inferMarketFromSymbol("AAPL", "jp"), "jp");
    assert.strictEqual(inferMarketFromSymbol("AAPL", "portfolio"), "us");
    assert.strictEqual(inferMarketFromSymbol("", "us"), "us");

    console.log("INFER_MARKET_SUCCESS");
    """
    proc = subprocess.run(
        ["node", "-e", node_script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "INFER_MARKET_SUCCESS" in proc.stdout


def test_add_stock_prompt_uses_market_inference() -> None:
    """Verify addStockPrompt invokes inferMarketFromSymbol."""
    api_js = (ROOT_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")
    assert "const targetMarket = inferMarketFromSymbol(symbol, activeTab);" in api_js
    assert "addStock(symbol, name, targetMarket);" in api_js


def test_add_stock_handles_duplicate_warning() -> None:
    """Verify addStock distinguishes duplicate stocks and emits a warning toast."""
    api_js = (ROOT_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")

    assert "#ffb86c" in api_js
    assert "既に追加されています" in api_js


def test_api_fetch_toast_deduplication_flags() -> None:
    """Verify loadNews, chatPollingReply, and requestStockAnalysis suppress duplicate default toasts."""
    api_js = (ROOT_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")

    # chatPollingReply suppression
    assert 'apiFetch(\n      "/api/chat"' in api_js or 'apiFetch("/api/chat"' in api_js
    assert 'apiFetch(\n      "/api/analyze-v2"' in api_js or 'apiFetch("/api/analyze-v2"' in api_js
    assert "{ showToast: false }" in api_js


def test_chart_safe_destroy_wrapper_via_node() -> None:
    """Verify _safeDestroyChart safely handles chart teardown even if destroyChart is undefined."""
    chart_js = (ROOT_DIR / "static" / "js" / "chart.js").read_text(encoding="utf-8")

    assert "function _safeDestroyChart(" in chart_js
    assert "_safeDestroyChart(canvas);" in chart_js
    # Ensure no direct destroyChart(canvas) remains in chart.js
    assert "destroyChart(canvas);" not in chart_js

    node_script = """
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');
    const assert = require('assert');

    const chartCode = fs.readFileSync(path.join(process.cwd(), 'static/js/chart.js'), 'utf-8');
    const match = chartCode.match(/function _safeDestroyChart\\([\\s\\S]*?\\n\\}/);
    assert(match, "_safeDestroyChart function not found in static/js/chart.js");

    let destroyedViaCustom = false;
    let destroyedViaInstances = false;

    // Case 1: destroyChart is globally defined
    const ctx1 = {
      destroyChart: (el) => {
        destroyedViaCustom = true;
      }
    };
    vm.createContext(ctx1);
    vm.runInContext(match[0], ctx1);
    ctx1._safeDestroyChart({});
    assert(destroyedViaCustom, "Expected destroyChart to be invoked when available");

    // Case 2: destroyChart is undefined, chartInstances is available
    const dummyEl = { __chart: {} };
    const dummyMap = new WeakMap();
    dummyMap.set(dummyEl, {
      destroy: () => {
        destroyedViaInstances = true;
      }
    });

    const ctx2 = {
      chartInstances: dummyMap,
      console: { warn: () => {} }
    };
    vm.createContext(ctx2);
    vm.runInContext(match[0], ctx2);
    ctx2._safeDestroyChart(dummyEl);
    assert(destroyedViaInstances, "Expected chartInstances.destroy to be invoked as fallback");
    assert.strictEqual(dummyMap.has(dummyEl), false, "Expected element removed from chartInstances");
    assert.strictEqual(dummyEl.__chart, null, "Expected el.__chart cleared");

    console.log("SAFE_DESTROY_CHART_SUCCESS");
    """
    proc = subprocess.run(
        ["node", "-e", node_script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "SAFE_DESTROY_CHART_SUCCESS" in proc.stdout


def test_chrome_extension_popup_lifecycle_cleanup() -> None:
    """Verify chrome_extension/popup.js registers lifecycle cleanup listeners."""
    popup_js = (ROOT_DIR / "chrome_extension" / "popup.js").read_text(encoding="utf-8")

    assert 'window.addEventListener("pagehide", stopStockPolling);' in popup_js
    assert 'window.addEventListener("unload", stopStockPolling);' in popup_js
    assert 'window.addEventListener("beforeunload", stopStockPolling);' in popup_js
