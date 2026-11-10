"""Regression tests for Phase 2 frontend fixes: ISSUE-FE-01, ISSUE-FE-02, ISSUE-FE-03."""

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
    assert result.returncode == 0, f"Node script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result.stdout


# ===========================================================================
# ISSUE-FE-01: orbit-state.js NaN pollution resilience
# ===========================================================================


def test_issue_fe01_nan_pollution_resilience():
    """ISSUE-FE-01: setTimeCursor guards against NaN/non-finite values and falls back to 0."""
    source = _read("static/js/experimental/orbit-state.js")
    assert "Number.isFinite(num)" in source
    assert "setTimeCursor(cursor)" in source

    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const stateCode = fs.readFileSync('static/js/experimental/orbit-state.js', 'utf8');
const window = {
  location: { search: '' },
  matchMedia: () => ({ matches: false }),
  history: { replaceState: () => {} }
};
const ctx = { window, console, URLSearchParams, URL, Set, Map, Math, Number, String, Object };
vm.createContext(ctx);
vm.runInContext(stateCode, ctx);

const state = new window.ObservatoryState();
assert.strictEqual(state.state.timeCursor, 0);

// NaN input
state.setTimeCursor(NaN);
assert.strictEqual(state.state.timeCursor, 0);

// Invalid string input
state.setTimeCursor("not-a-number");
assert.strictEqual(state.state.timeCursor, 0);

// Undefined input
state.setTimeCursor(undefined);
assert.strictEqual(state.state.timeCursor, 0);

// Valid positive integer
state.setTimeCursor(5);
assert.strictEqual(state.state.timeCursor, 5);

// Negative input -> clamped to 0
state.setTimeCursor(-10);
assert.strictEqual(state.state.timeCursor, 0);

console.log("ISSUE-FE-01 PASS");
"""
    output = _run_node(node_script)
    assert "ISSUE-FE-01 PASS" in output


# ===========================================================================
# ISSUE-FE-02: Controller unsubscribe on destroy
# ===========================================================================


def test_issue_fe02_controller_unsubscribe_on_destroy():
    """ISSUE-FE-02: Controllers unsubscribe from state on destroy to prevent memory leaks."""
    for path in [
        "static/js/experimental/accessibility-controller.js",
        "static/js/experimental/ai-dive-controller.js",
        "static/js/experimental/constellation-controller.js",
        "static/js/experimental/temporal-controller.js",
    ]:
        src = _read(path)
        assert "this._unsubState = this.state.subscribe" in src or "this._unsubState =" in src
        assert "this._unsubState()" in src

    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

function createEnv() {
  const window = {
    location: { search: '', href: 'http://localhost/' },
    matchMedia: () => ({ matches: false, addListener: () => {}, removeListener: () => {} }),
    history: { replaceState: () => {} },
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  const document = {
    createElement: () => ({
      classList: { add: () => {}, remove: () => {} },
      appendChild: () => {},
      addEventListener: () => {},
      style: {}
    }),
    body: { classList: { contains: () => false } }
  };
  const ctx = {
    window,
    global: window,
    document,
    console,
    URLSearchParams,
    URL,
    Set,
    Map,
    Math,
    Number,
    String,
    Object,
    parseInt,
    parseFloat,
    clearTimeout: () => {},
    setTimeout: () => 1,
    AbortController: class { abort() {} }
  };
  vm.createContext(ctx);
  return { ctx, window };
}

const stateCode = fs.readFileSync('static/js/experimental/orbit-state.js', 'utf8');
const accessCode = fs.readFileSync('static/js/experimental/accessibility-controller.js', 'utf8');
const aiCode = fs.readFileSync('static/js/experimental/ai-dive-controller.js', 'utf8');
const constCode = fs.readFileSync('static/js/experimental/constellation-controller.js', 'utf8');
const tempCode = fs.readFileSync('static/js/experimental/temporal-controller.js', 'utf8');

// 1. AccessibilityController
{
  const { ctx, window } = createEnv();
  vm.runInContext(stateCode, ctx);
  vm.runInContext(accessCode, ctx);
  const state = new window.ObservatoryState();
  const ctrl = new window.AccessibilityController(state, {});
  assert.strictEqual(state._listeners.size, 1);
  ctrl.destroy();
  assert.strictEqual(state._listeners.size, 0);
  assert.strictEqual(ctrl._unsubState, null);
}

// 2. AiDiveController
{
  const { ctx, window } = createEnv();
  vm.runInContext(stateCode, ctx);
  vm.runInContext(aiCode, ctx);
  const state = new window.ObservatoryState();
  const ctrl = new window.AiDiveController(state, {});
  assert.strictEqual(state._listeners.size, 1);
  ctrl.destroy();
  assert.strictEqual(state._listeners.size, 0);
  assert.strictEqual(ctrl._unsubState, null);
}

// 3. ConstellationController
{
  const { ctx, window } = createEnv();
  vm.runInContext(stateCode, ctx);
  vm.runInContext(constCode, ctx);
  const state = new window.ObservatoryState();
  const ctrl = new window.ConstellationController(state, {});
  assert.strictEqual(state._listeners.size, 1);
  ctrl.destroy();
  assert.strictEqual(state._listeners.size, 0);
  assert.strictEqual(ctrl._unsubState, null);
}

// 4. TemporalController
{
  const { ctx, window } = createEnv();
  vm.runInContext(stateCode, ctx);
  vm.runInContext(tempCode, ctx);
  const state = new window.ObservatoryState();
  const ctrl = new window.TemporalController(state, {});
  assert.strictEqual(state._listeners.size, 1);
  ctrl.destroy();
  assert.strictEqual(state._listeners.size, 0);
  assert.strictEqual(ctrl._unsubState, null);
}

console.log("ISSUE-FE-02 PASS");
"""
    output = _run_node(node_script)
    assert "ISSUE-FE-02 PASS" in output


# ===========================================================================
# ISSUE-FE-03: changePercent toFixed safety guards
# ===========================================================================


def test_issue_fe03_tofixed_safety_guards():
    """ISSUE-FE-03: changePercent guards against null/undefined/NaN before calling toFixed."""
    files_to_check = [
        "static/js/experimental/accessibility-controller.js",
        "static/js/experimental/orbit-renderer.js",
        "static/js/experimental/orbit-entry.js",
        "static/js/experimental/constellation-controller.js",
        "static/js/experimental/ai-dive-controller.js",
    ]
    for path in files_to_check:
        src = _read(path)
        assert "Number.isFinite" in src

    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

function createEnv() {
  const window = {
    location: { search: '', href: 'http://localhost/' },
    matchMedia: () => ({ matches: false, addListener: () => {}, removeListener: () => {} }),
    history: { replaceState: () => {} },
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  const createdElements = [];
  const document = {
    createElement: (tag) => {
      const el = {
        tag,
        classList: { add: () => {}, remove: () => {} },
        appendChild: (child) => el.children.push(child),
        children: [],
        addEventListener: () => {},
        style: {},
        textContent: '',
        className: '',
        setAttribute: () => {},
        getAttribute: () => null,
        removeAttribute: () => {},
      };
      createdElements.push(el);
      return el;
    },
    body: { classList: { contains: () => false } }
  };
  const ctx = {
    window,
    global: window,
    document,
    console,
    URLSearchParams,
    URL,
    Set,
    Map,
    Math,
    Number,
    String,
    Object,
    parseInt,
    parseFloat,
    clearTimeout: () => {},
    setTimeout: () => 1,
    AbortController: class { abort() {} }
  };
  vm.createContext(ctx);
  return { ctx, window, document };
}

const adapterCode = fs.readFileSync('static/js/experimental/data-adapter.js', 'utf8');
const stateCode = fs.readFileSync('static/js/experimental/orbit-state.js', 'utf8');
const accessCode = fs.readFileSync('static/js/experimental/accessibility-controller.js', 'utf8');
const aiCode = fs.readFileSync('static/js/experimental/ai-dive-controller.js', 'utf8');
const constCode = fs.readFileSync('static/js/experimental/constellation-controller.js', 'utf8');

// 1. AccessibilityController table
{
  const { ctx, window } = createEnv();
  vm.runInContext(adapterCode, ctx);
  vm.runInContext(stateCode, ctx);
  vm.runInContext(accessCode, ctx);
  const state = new window.ObservatoryState();
  const tableBody = { innerHTML: '', appendChild: () => {} };
  const ctrl = new window.AccessibilityController(state, { screenReaderTableBody: tableBody });

  ctrl.updateScreenReaderTable({
    selectedSymbol: 'TEST',
    stocks: new Map([
      ['S1', { symbol: 'S1', price: 100, changePercent: null }],
      ['S2', { symbol: 'S2', price: 100, changePercent: undefined }],
      ['S3', { symbol: 'S3', price: 100, changePercent: NaN }],
      ['S4', { symbol: 'S4', price: 100, changePercent: 5.678 }]
    ])
  });
}

// 2. AiDiveController Tier 1
{
  const { ctx, window } = createEnv();
  vm.runInContext(adapterCode, ctx);
  vm.runInContext(stateCode, ctx);
  vm.runInContext(aiCode, ctx);
  const state = new window.ObservatoryState();
  const ctrl = new window.AiDiveController(state, {});

  for (const cp of [null, undefined, NaN, -2.5, 0, 3.14]) {
    ctrl.renderTier1Financials({ symbol: 'TEST', price: 100, changePercent: cp });
  }
}

// 3. ConstellationController HUD metric getter
{
  const { ctx, window, document } = createEnv();
  vm.runInContext(adapterCode, ctx);
  vm.runInContext(stateCode, ctx);
  vm.runInContext(constCode, ctx);
  const state = new window.ObservatoryState();
  const drawer = document.createElement('div');
  const ctrl = new window.ConstellationController(state, { drawer });

  state.setStocks([
    { symbol: 'A', price: 10, changePercent: null },
    { symbol: 'B', price: 20, changePercent: undefined },
    { symbol: 'C', price: 30, changePercent: NaN }
  ]);
  state.set({ isConnectMode: true, connectedSymbols: ['A', 'B', 'C'] });
  ctrl.renderHUD(state.state);
}

console.log("ISSUE-FE-03 PASS");
"""
    output = _run_node(node_script)
    assert "ISSUE-FE-03 PASS" in output
