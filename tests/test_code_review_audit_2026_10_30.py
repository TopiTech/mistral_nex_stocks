"""Comprehensive code review audit and regression tests (2026-10-30).

Verifies:
1. settings.js: renderList guards against null list element without throwing TypeError.
2. settings.js: beforeunload lifecycle cleans up stocksLoadRetryTimer and stocksLoadAbortController.
3. screener.js: beforeunload lifecycle registered and cleans up fetchTimeout and screenerAbortController.
4. ai_portfolio.js: beforeunload lifecycle registered and cancels pending requests and saved controller.
5. index_main.js: beforeunload cancels in-flight initial stocks fetch.
6. realtime_client.js: rapid fallback price updates properly reset __updatingTimer.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend regression tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return result.stdout.strip()


def test_settings_render_list_null_guard() -> None:
    """Verify renderList in settings.js gracefully handles null DOM container."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "settings.js"),
  "utf8",
);

const elements = new Map();
const windowListeners = new Map();

const context = {
  console,
  setTimeout: () => 1,
  clearTimeout: () => {},
  AbortController: class {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; }
  },
  document: {
    getElementById: (id) => elements.get(id) || null,
    createElement: (tag) => ({ tagName: tag, classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: {
    addEventListener: (event, handler) => {
      windowListeners.set(event, handler);
    },
  },
  localStorage: {
    getItem: () => null,
    setItem: () => {},
  },
  DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
  APP_CONFIG: {},
  showToast: () => {},
  logger: { error() {}, info() {}, warn() {} },
};

vm.createContext(context);
vm.runInContext(source, context);

// Test: Calling renderList with non-existent container should not throw
try {
  vm.runInContext("renderList('non_existent_market', [{ symbol: 'TEST', name: 'Test' }]);", context);
  process.stdout.write("renderList_null_guard_ok");
} catch (err) {
  console.error("renderList threw error on null element:", err);
  process.exit(1);
}
"""
    assert _run_node(script) == "renderList_null_guard_ok"


def test_settings_beforeunload_lifecycle() -> None:
    """Verify beforeunload in settings.js clears retry timer and aborts pending controller."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "settings.js"),
  "utf8",
);

let timerCleared = false;
let controllerAborted = false;
const windowListeners = new Map();

class MockAbortController {
  constructor() {
    this.signal = { aborted: false };
  }
  abort() {
    this.signal.aborted = true;
    controllerAborted = true;
  }
}

const context = {
  console,
  setTimeout: (fn, ms) => 42,
  clearTimeout: (id) => {
    if (id === 42) timerCleared = true;
  },
  AbortController: MockAbortController,
  document: {
    getElementById: () => null,
    createElement: () => ({ classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: {
    addEventListener: (event, handler) => {
      windowListeners.set(event, handler);
    },
  },
  localStorage: { getItem: () => null, setItem: () => {} },
  DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
  APP_CONFIG: {},
  showToast: () => {},
  logger: { error() {}, info() {}, warn() {} },
};

vm.createContext(context);
vm.runInContext(source, context);

// Simulate pending timer and controller
vm.runInContext("stocksLoadRetryTimer = 42; stocksLoadAbortController = new AbortController();", context);

// Trigger beforeunload
const beforeUnloadHandler = windowListeners.get("beforeunload");
if (typeof beforeUnloadHandler !== "function") {
  console.error("beforeunload handler not registered in settings.js");
  process.exit(1);
}
beforeUnloadHandler();

if (!timerCleared) {
  console.error("stocksLoadRetryTimer was not cleared on beforeunload");
  process.exit(1);
}
if (!controllerAborted) {
  console.error("stocksLoadAbortController was not aborted on beforeunload");
  process.exit(1);
}

process.stdout.write("settings_beforeunload_ok");
"""
    assert _run_node(script) == "settings_beforeunload_ok"


def test_screener_beforeunload_lifecycle() -> None:
    """Verify beforeunload in screener.js clears fetchTimeout and aborts screenerAbortController."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "screener.js"),
  "utf8",
);

// Source integrity checks
if (!source.includes('window.addEventListener("beforeunload"')) {
  console.error("screener.js must register beforeunload listener");
  process.exit(1);
}
if (!source.includes("if (fetchTimeout) {")) {
  console.error("screener.js must clear fetchTimeout in beforeunload");
  process.exit(1);
}
if (!source.includes("if (screenerAbortController) {")) {
  console.error("screener.js must abort screenerAbortController in beforeunload");
  process.exit(1);
}

// Runtime test in VM
const windowListeners = new Map();
const context = {
  console,
  setTimeout: () => 1,
  clearTimeout: () => {},
  AbortController: class {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; }
  },
  document: {
    getElementById: () => null,
    createElement: () => ({ classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
  },
  window: {
    addEventListener: (event, handler) => {
      windowListeners.set(event, handler);
    },
    getComputedStyle: () => ({ display: "block" }),
  },
  requestAnimationFrame: (cb) => cb(),
};

vm.createContext(context);
vm.runInContext(source, context);

const beforeUnloadHandler = windowListeners.get("beforeunload");
if (typeof beforeUnloadHandler !== "function") {
  console.error("beforeunload handler was not registered in windowListeners");
  process.exit(1);
}

// Invoke the handler safely
beforeUnloadHandler();

process.stdout.write("screener_beforeunload_ok");
"""
    assert _run_node(script) == "screener_beforeunload_ok"


def test_ai_portfolio_beforeunload_lifecycle() -> None:
    """Verify beforeunload in ai_portfolio.js aborts active and saved controllers."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "ai_portfolio.js"),
  "utf8",
);

// Source integrity checks
if (!source.includes('window.addEventListener("beforeunload"')) {
  console.error("ai_portfolio.js must register beforeunload listener");
  process.exit(1);
}
if (!source.includes("cancelAiPortfolioRequest();")) {
  console.error("ai_portfolio.js must call cancelAiPortfolioRequest in beforeunload");
  process.exit(1);
}
if (!source.includes("savedAiPortfoliosAbortController.abort();")) {
  console.error("ai_portfolio.js must abort savedAiPortfoliosAbortController in beforeunload");
  process.exit(1);
}

// Runtime test in VM
const windowListeners = new Map();
const context = {
  console,
  setTimeout: (fn, ms) => 1,
  clearTimeout: () => {},
  AbortController: class {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; }
  },
  DOMException: class extends Error {},
  document: {
    getElementById: () => null,
    createElement: () => ({ classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
  },
  window: {
    addEventListener: (event, handler) => {
      windowListeners.set(event, handler);
    },
  },
};

vm.createContext(context);
vm.runInContext(source, context);

const beforeUnloadHandler = windowListeners.get("beforeunload");
if (typeof beforeUnloadHandler !== "function") {
  console.error("beforeunload handler not registered in ai_portfolio.js");
  process.exit(1);
}

// Invoke handler to ensure clean execution
beforeUnloadHandler();

process.stdout.write("ai_portfolio_beforeunload_ok");
"""
    assert _run_node(script) == "ai_portfolio_beforeunload_ok"


def test_index_main_beforeunload_lifecycle() -> None:
    """Verify beforeunload in index_main.js calls invalidatePendingInitialStocksFetch."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "index_main.js"),
  "utf8",
);

let aborted = false;
const windowListeners = new Map();

class MockAbortController {
  constructor() {
    this.signal = { aborted: false };
  }
  abort() {
    this.signal.aborted = true;
    aborted = true;
  }
}

const context = {
  console,
  setTimeout: () => 1,
  clearTimeout: () => {},
  AbortController: MockAbortController,
  DOM: { get: () => null },
  document: {
    getElementById: () => null,
    createElement: () => ({ classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
  },
  window: {
    addEventListener: (event, handler) => {
      windowListeners.set(event, handler);
    },
    state: { stocks: {} },
  },
  localStorage: { getItem: () => null, setItem: () => {} },
  apiFetch: () => new Promise(() => {}),
  DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
  APP_CONFIG: {},
  showToast: () => {},
  logger: { error() {}, info() {}, warn() {} },
};

vm.createContext(context);
vm.runInContext(source, context);

const beforeUnloadHandler = windowListeners.get("beforeunload");
if (typeof beforeUnloadHandler !== "function") {
  console.error("beforeunload handler not registered in index_main.js");
  process.exit(1);
}

// Set fetchInitialStocksAbortController so it is active
vm.runInContext("fetchInitialStocksAbortController = new AbortController();", context);

beforeUnloadHandler();

if (!aborted) {
  console.error("fetchInitialStocksAbortController was not aborted on beforeunload");
  process.exit(1);
}

process.stdout.write("index_main_beforeunload_ok");
"""
    assert _run_node(script) == "index_main_beforeunload_ok"


def test_realtime_client_updating_timer() -> None:
    """Verify realtime_client resets __updatingTimer on rapid fallback updates."""
    script = r"""
const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "realtime_client.js"),
  "utf8",
);

// Verify __updatingTimer clear and assignment in source code
if (!source.includes("if (priceEl.__updatingTimer)")) {
  console.error("realtime_client.js must check and clear priceEl.__updatingTimer");
  process.exit(1);
}
if (!source.includes("clearTimeout(priceEl.__updatingTimer)")) {
  console.error("realtime_client.js must clearTimeout(priceEl.__updatingTimer)");
  process.exit(1);
}
if (!source.includes("priceEl.__updatingTimer = null")) {
  console.error("realtime_client.js must reset priceEl.__updatingTimer to null");
  process.exit(1);
}

process.stdout.write("realtime_client_updating_timer_ok");
"""
    assert _run_node(script) == "realtime_client_updating_timer_ok"
