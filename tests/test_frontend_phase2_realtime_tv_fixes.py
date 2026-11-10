"""Regression tests for Phase 2 frontend fixes: ISSUE-FE-04, ISSUE-FE-05."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executing frontend JavaScript test scripts")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, (
        f"Node script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


# ===========================================================================
# ISSUE-FE-04: static/js/tradingview_manager.js fallback flag reset on symbol load
# ===========================================================================


def test_issue_fe04_tradingview_fallback_flag_reset():
    """ISSUE-FE-04: TradingViewManager resets hasSwitched and fallbackState on symbol switches."""
    source = _read("static/js/tradingview_manager.js")
    assert "resetFallbackFlag" in source
    assert "loadSymbol" in source
    assert "hasSwitched" in source

    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const tvCode = fs.readFileSync('static/js/tradingview_manager.js', 'utf8');
const fakeDoc = {
  getElementById: (id) => {
    if (id === 'tv-script-js') return null;
    return {
      replaceChildren: () => {},
      appendChild: () => {},
      isConnected: true,
      querySelector: () => null
    };
  },
  body: { classList: { contains: () => false } },
  createElement: () => ({ style: {}, setAttribute: () => {}, appendChild: () => {} }),
  querySelector: () => null,
  head: { appendChild: () => {} }
};
const tvContext = {
  window: {
    addEventListener: () => {},
    removeEventListener: () => {}
  },
  document: fakeDoc,
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  Math: Math,
  String: String,
  Number: Number,
  Map: Map,
  Set: Set,
  JSON: JSON
};
tvContext.window.window = tvContext.window;
vm.createContext(tvContext);
vm.runInContext(tvCode, tvContext);

const TVM = tvContext.window.TradingViewManager;
assert(TVM, 'TradingViewManager should exist on window');
assert.strictEqual(typeof TVM.loadSymbol, 'function', 'loadSymbol should be a function');
assert.strictEqual(typeof TVM.resetFallbackFlag, 'function', 'resetFallbackFlag should be a function');
assert.strictEqual(typeof TVM.isFallbackActive, 'function', 'isFallbackActive should be a function');

// Initial state
assert.strictEqual(TVM.hasSwitched, false);

// Simulate fallback on stock A
TVM.hasSwitched = true;
TVM.fallbackState.set('AAPL', true);
assert.strictEqual(TVM.isFallbackActive('AAPL'), true);

// Switch to stock B via loadSymbol
TVM.loadSymbol('MSFT');
assert.strictEqual(TVM.hasSwitched, false, 'hasSwitched should reset to false on loadSymbol');
assert.strictEqual(TVM.isFallbackActive('MSFT'), false, 'MSFT should not be in fallback state');
assert.strictEqual(TVM.currentSymbol, 'MSFT');

// Explicit resetFallbackFlag
TVM.hasSwitched = true;
TVM.resetFallbackFlag('AAPL');
assert.strictEqual(TVM.hasSwitched, false);
assert.strictEqual(TVM.isFallbackActive('AAPL'), false);

console.log("ISSUE-FE-04 PASS");
"""
    output = _run_node(node_script)
    assert "ISSUE-FE-04 PASS" in output


# ===========================================================================
# ISSUE-FE-05: static/js/realtime_client.js reconnection backoff with jitter
# ===========================================================================


def test_issue_fe05_realtime_client_reconnect_jitter():
    """ISSUE-FE-05: Reconnection exponential backoff applies full jitter to prevent thundering herd."""
    source = _read("static/js/realtime_client.js")
    assert "calculateReconnectDelay" in source
    assert "Math.random()" in source
    assert "jitter" in source

    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const realtimeCode = fs.readFileSync('static/js/realtime_client.js', 'utf8');
const realtimeContext = {
  window: {},
  document: { readyState: 'complete' },
  CSS: { escape: (s) => s },
  Math: Math,
  Number: Number,
  String: String,
  Map: Map,
  Set: Set,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout
};
vm.createContext(realtimeContext);
vm.runInContext(realtimeCode, realtimeContext);

const { calculateReconnectDelay, RealtimeStockClient } = realtimeContext.window;
assert.strictEqual(typeof calculateReconnectDelay, 'function');
assert.strictEqual(typeof RealtimeStockClient, 'function');

const client = new RealtimeStockClient();
assert.strictEqual(client.reconnectAttempt, 0);

// Verify that backoff delays include random jitter between 0.5 and 1.0 of the calculated exponential delay
for (let attempt = 0; attempt <= 4; attempt++) {
  const delays = [];
  for (let i = 0; i < 200; i++) {
    const delay = calculateReconnectDelay(attempt, 1000, 30000);
    delays.push(delay);
  }
  const min = Math.min(...delays);
  const max = Math.max(...delays);
  const rawExponential = Math.min(1000 * Math.pow(2, attempt), 30000);

  // Full jitter range is [0.5 * raw, 1.0 * raw]
  assert(min >= rawExponential * 0.49, `min ${min} should be >= ${rawExponential * 0.49}`);
  assert(max <= rawExponential * 1.01, `max ${max} should be <= ${rawExponential * 1.01}`);
  // Confirm jitter produces varied values, not a constant
  const uniqueCount = new Set(delays).size;
  assert(uniqueCount > 20, `delays should be distributed with jitter, got ${uniqueCount} unique values`);
}

// Verify client methods
const nextDelay1 = client.getNextReconnectDelay();
assert.strictEqual(client.reconnectAttempt, 1);
assert(nextDelay1 >= 490 && nextDelay1 <= 1010);

const nextDelay2 = client.getNextReconnectDelay();
assert.strictEqual(client.reconnectAttempt, 2);
assert(nextDelay2 >= 990 && nextDelay2 <= 2020);

// Verify resetReconnect
client.resetReconnect();
assert.strictEqual(client.reconnectAttempt, 0);
assert.strictEqual(client.reconnectTimer, null);

console.log("ISSUE-FE-05 PASS");
"""
    output = _run_node(node_script)
    assert "ISSUE-FE-05 PASS" in output
