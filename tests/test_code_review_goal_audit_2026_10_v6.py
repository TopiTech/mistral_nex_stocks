"""
tests/test_code_review_goal_audit_2026_10_v6.py

Regression tests for comprehensive code review audit 2026-10 v6:
1. ConstellationController.waitForAiPoll: immediate abort rejection & listener cleanup on timeout.
2. AiDiveController.waitForNewsRetry: immediate abort rejection & listener cleanup on timeout.
3. ai_portfolio.js delayWithAbort: listener cleanup on completion & immediate abort rejection.
4. ai_portfolio.js copyAiPortfolioToMy: items with non-positive / missing target_price sanitized to 100.0.
5. GestureController: blur event handling and timer / drag cleanup on window blur.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for controller JavaScript regression tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Node test failed: {result.stderr}\nStdout: {result.stdout}"
    return result.stdout


def test_constellation_controller_wait_for_ai_poll_abort_and_listener_cleanup():
    """Verify waitForAiPoll cleans up abort listeners and rejects on pre-aborted signal."""
    script = """
const vm = require("vm");
const fs = require("fs");

class MockSignal {
  constructor(aborted = false) {
    this.aborted = aborted;
    this.listeners = new Map();
  }
  addEventListener(event, fn, opts) {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set());
    this.listeners.get(event).add(fn);
  }
  removeEventListener(event, fn) {
    if (this.listeners.has(event)) {
      this.listeners.get(event).delete(fn);
    }
  }
  abort() {
    this.aborted = true;
    if (this.listeners.has("abort")) {
      for (const fn of Array.from(this.listeners.get("abort"))) {
        fn();
      }
    }
  }
}

const context = {
  DOMException: class DOMException extends Error {
    constructor(msg, name) {
      super(msg);
      this.name = name;
    }
  },
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  document: {
    createElement: () => ({ appendChild: () => {}, classList: { add: () => {} } }),
  },
};
context.window = context;
context.global = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync("static/js/experimental/constellation-controller.js", "utf8"), context);

const controller = new context.ConstellationController(
  { state: { connectedSymbols: [] }, subscribe: () => {} },
  {}
);

async function runTests() {
  // 1. Pre-aborted signal rejects immediately without setting timeout
  const preAbortedSignal = new MockSignal(true);
  try {
    await controller.waitForAiPoll(100, preAbortedSignal);
    throw new Error("Expected pre-aborted signal to reject immediately");
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  }

  // 2. Normal timeout resolution detaches the abort listener
  const normalSignal = new MockSignal(false);
  await controller.waitForAiPoll(10, normalSignal);
  const remainingListeners = normalSignal.listeners.get("abort") ? normalSignal.listeners.get("abort").size : 0;
  if (remainingListeners !== 0) {
    throw new Error(`Expected 0 abort listeners after timeout completion, found ${remainingListeners}`);
  }

  // 3. Abort during delay cancels timer and rejects with AbortError
  const abortDuringSignal = new MockSignal(false);
  const waitPromise = controller.waitForAiPoll(500, abortDuringSignal);
  setTimeout(() => abortDuringSignal.abort(), 10);
  try {
    await waitPromise;
    throw new Error("Expected wait to be aborted");
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  }
  const remainingAfterAbort = abortDuringSignal.listeners.get("abort") ? abortDuringSignal.listeners.get("abort").size : 0;
  if (remainingAfterAbort !== 0) {
    throw new Error(`Expected 0 abort listeners after abort rejection, found ${remainingAfterAbort}`);
  }

  process.stdout.write("constellation_waitForAiPoll_ok");
}

runTests().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    out = _run_node(script)
    assert "constellation_waitForAiPoll_ok" in out


def test_ai_dive_controller_wait_for_news_retry_listener_cleanup():
    """Verify waitForNewsRetry removes abort listener on both timeout and abort."""
    script = """
const vm = require("vm");
const fs = require("fs");

class MockSignal {
  constructor(aborted = false) {
    this.aborted = aborted;
    this.listeners = new Map();
  }
  addEventListener(event, fn) {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set());
    this.listeners.get(event).add(fn);
  }
  removeEventListener(event, fn) {
    if (this.listeners.has(event)) {
      this.listeners.get(event).delete(fn);
    }
  }
  abort() {
    this.aborted = true;
    if (this.listeners.has("abort")) {
      for (const fn of Array.from(this.listeners.get("abort"))) {
        fn();
      }
    }
  }
}

const context = {
  DOMException: class DOMException extends Error {
    constructor(msg, name) {
      super(msg);
      this.name = name;
    }
  },
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  document: {
    createElement: () => ({ appendChild: () => {}, classList: { add: () => {} } }),
  },
};
context.window = context;
context.global = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync("static/js/experimental/ai-dive-controller.js", "utf8"), context);

const controller = new context.AiDiveController(
  { state: { aiDiveOpen: false }, subscribe: () => {} },
  {}
);

async function runTests() {
  // 1. Pre-aborted signal
  const preAbortedSignal = new MockSignal(true);
  try {
    await controller.waitForNewsRetry(100, preAbortedSignal);
    throw new Error("Expected pre-aborted signal to reject immediately");
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  }

  // 2. Normal completion removes listener
  const normalSignal = new MockSignal(false);
  await controller.waitForNewsRetry(10, normalSignal);
  const remaining = normalSignal.listeners.get("abort") ? normalSignal.listeners.get("abort").size : 0;
  if (remaining !== 0) {
    throw new Error(`Expected 0 abort listeners after normal completion, found ${remaining}`);
  }

  // 3. Abort during wait
  const abortDuringSignal = new MockSignal(false);
  const waitPromise = controller.waitForNewsRetry(500, abortDuringSignal);
  setTimeout(() => abortDuringSignal.abort(), 10);
  try {
    await waitPromise;
    throw new Error("Expected wait to abort");
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  }
  const remainingAfterAbort = abortDuringSignal.listeners.get("abort") ? abortDuringSignal.listeners.get("abort").size : 0;
  if (remainingAfterAbort !== 0) {
    throw new Error(`Expected 0 abort listeners after abort rejection, found ${remainingAfterAbort}`);
  }

  process.stdout.write("ai_dive_waitForNewsRetry_ok");
}

runTests().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    out = _run_node(script)
    assert "ai_dive_waitForNewsRetry_ok" in out


def test_gesture_controller_blur_event_cleanup():
    """Verify GestureController attaches and removes window blur event listener."""
    script = """
const vm = require("vm");
const fs = require("fs");

const windowListeners = new Map();
const context = {
  window: {
    addEventListener: (ev, fn) => {
      if (!windowListeners.has(ev)) windowListeners.set(ev, new Set());
      windowListeners.get(ev).add(fn);
    },
    removeEventListener: (ev, fn) => {
      if (windowListeners.has(ev)) windowListeners.get(ev).delete(fn);
    },
  },
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
};
context.global = context;
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(fs.readFileSync("static/js/experimental/gesture-controller.js", "utf8"), context);

const mockCanvas = {
  addEventListener: () => {},
  removeEventListener: () => {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
  style: {},
};
const mockState = {
  state: { isConnectMode: false },
  setDraggedSymbol: () => {},
  setHoveredSymbol: () => {},
};
const mockRenderer = {
  hitTest: () => null,
  centerX: 400,
  centerY: 300,
};

const GestureController = context.GestureController || context.window.GestureController;
const controller = new GestureController(mockCanvas, mockState, mockRenderer);

if (!windowListeners.has("blur") || windowListeners.get("blur").size === 0) {
  throw new Error("Expected window blur listener to be registered in bindEvents");
}

controller.destroy();

if (windowListeners.has("blur") && windowListeners.get("blur").size !== 0) {
  throw new Error("Expected window blur listener to be removed on destroy");
}

process.stdout.write("gesture_controller_blur_ok");
"""
    out = _run_node(script)
    assert "gesture_controller_blur_ok" in out


def test_ai_portfolio_delay_with_abort_cleanup_and_rejection():
    """Verify ai_portfolio.js delayWithAbort removes abort listener upon timeout completion."""
    script = """
const vm = require("vm");
const fs = require("fs");

class MockSignal {
  constructor(aborted = false) {
    this.aborted = aborted;
    this.listeners = new Map();
  }
  addEventListener(event, fn) {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set());
    this.listeners.get(event).add(fn);
  }
  removeEventListener(event, fn) {
    if (this.listeners.has(event)) {
      this.listeners.get(event).delete(fn);
    }
  }
  abort() {
    this.aborted = true;
    if (this.listeners.has("abort")) {
      for (const fn of Array.from(this.listeners.get("abort"))) {
        fn();
      }
    }
  }
}

const context = {
  DOMException: class DOMException extends Error {
    constructor(msg, name) {
      super(msg);
      this.name = name;
    }
  },
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
};
context.global = context;
context.window = context;
vm.createContext(context);

const source = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
const start = source.indexOf("function delayWithAbort");
const end = source.indexOf("function beginAiPortfolioRequest", start);
const funcCode = source.slice(start, end);
vm.runInContext(funcCode + "\\nthis.delayWithAbort = delayWithAbort;", context);

async function run() {
  // 1. Immediate rejection on pre-aborted signal
  const preAborted = new MockSignal(true);
  try {
    await context.delayWithAbort(100, preAborted);
    throw new Error("Expected preAborted to reject");
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  }

  // 2. Normal completion detaches listener
  const normal = new MockSignal(false);
  await context.delayWithAbort(10, normal);
  const rem = normal.listeners.get("abort") ? normal.listeners.get("abort").size : 0;
  if (rem !== 0) {
    throw new Error(`Expected 0 remaining listeners, found ${rem}`);
  }

  // 3. Abort during timer
  const during = new MockSignal(false);
  const p = context.delayWithAbort(500, during);
  setTimeout(() => during.abort(), 10);
  try {
    await p;
    throw new Error("Expected delay to abort");
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  }
  const remAbort = during.listeners.get("abort") ? during.listeners.get("abort").size : 0;
  if (remAbort !== 0) {
    throw new Error(`Expected 0 remaining listeners after abort, found ${remAbort}`);
  }

  process.stdout.write("ai_portfolio_delayWithAbort_ok");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    out = _run_node(script)
    assert "ai_portfolio_delayWithAbort_ok" in out


def test_ai_portfolio_copy_sanitizes_zero_target_price():
    """Verify copyAiPortfolioToMy sanitizes target_price <= 0 to default 100.0."""
    script = """
const vm = require("vm");
const fs = require("fs");

let postedBody = null;
const context = {
  csrfFetch: async (url, opts) => {
    postedBody = JSON.parse(opts.body);
    return {
      ok: true,
      json: async () => ({ ok: true, message: "OK", added_count: 2 }),
    };
  },
  showToast: () => {},
  fetchInitialStocks: async () => {},
  loadPortfolioSnapshot: async () => {},
};
context.global = context;
context.window = context;
vm.createContext(context);

const source = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
const start = source.indexOf("async function copyAiPortfolioToMy");
const end = source.indexOf("function renderAiPortfolio", start);
const funcCode = source.slice(start, end);
vm.runInContext(funcCode + "\\nthis.copyAiPortfolioToMy = copyAiPortfolioToMy;", context);

async function run() {
  const items = [
    { symbol: "AAPL", market: "us", weight_pct: 50.0, target_price: 0 },
    { symbol: "MSFT", market: "us", weight_pct: 50.0, target_price: null },
  ];
  await context.copyAiPortfolioToMy(items);

  if (!postedBody || !postedBody.items || postedBody.items.length !== 2) {
    throw new Error("Invalid posted body");
  }
  if (postedBody.items[0].target_price !== 100.0) {
    throw new Error(`Expected item 0 target_price to be 100.0, got ${postedBody.items[0].target_price}`);
  }
  if (postedBody.items[1].target_price !== 100.0) {
    throw new Error(`Expected item 1 target_price to be 100.0, got ${postedBody.items[1].target_price}`);
  }

  process.stdout.write("copy_sanitizes_target_price_ok");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    out = _run_node(script)
    assert "copy_sanitizes_target_price_ok" in out

