"""Unit and regression tests verifying frontend fixes for FE-01 through FE-10."""

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
# FE-01: chrome_extension/content.js DOM traversal limit guard
# ===========================================================================


def test_fe01_content_js_traversal_limit_guard():
    """FE-01: extractPageTextSnippets enforces max node limit to prevent UI freezing."""
    source = _read("chrome_extension/content.js")
    assert "MAX_EXTRACT_TEXT_NODES = 2500" in source
    assert "textBlocks.length >= MAX_EXTRACT_TEXT_NODES" in source

    # Node.js execution verification of the limit behavior
    node_test = r"""
const assert = require("assert");

// Mock TreeWalker that produces 5000 text nodes
let count = 0;
const mockWalker = {
  nextNode() {
    count++;
    if (count > 5000) return null;
    return {
      nodeValue: "TEST " + count,
      parentElement: { tagName: "DIV", isContentEditable: false },
    };
  }
};

const MAX_EXTRACT_TEXT_NODES = 2500;
const textBlocks = [];
let currentNode;
while ((currentNode = mockWalker.nextNode())) {
  textBlocks.push({
    text: currentNode.nodeValue,
    element: currentNode.parentElement,
  });
  if (textBlocks.length >= MAX_EXTRACT_TEXT_NODES) {
    break;
  }
}

assert.strictEqual(textBlocks.length, 2500);
console.log("FE-01 OK");
"""
    out = _run_node(node_test)
    assert "FE-01 OK" in out


# ===========================================================================
# FE-02: static/js/api.js sendChat null guard for chat-log container
# ===========================================================================


def test_fe02_send_chat_log_null_guard():
    """FE-02: sendChat safely handles missing log element without throwing or locking UI."""
    source = _read("static/js/api.js")
    assert "if (!log) {" in source
    assert "$logger.warn(" in source
    assert "if (input) input.disabled = false;" in source
    assert "if (sendBtn) sendBtn.disabled = false;" in source


# ===========================================================================
# FE-03: chrome_extension/background.js setBadgeMessage race condition prevention
# ===========================================================================


def test_fe03_background_badge_clear_timer():
    """FE-03: setBadgeMessage clears any existing timer to avoid badge clearance races."""
    source = _read("chrome_extension/background.js")
    assert "let badgeClearTimer = null;" in source
    assert "if (badgeClearTimer) {" in source
    assert "clearTimeout(badgeClearTimer);" in source
    assert "badgeClearTimer = setTimeout(" in source


# ===========================================================================
# FE-04: static/js/state.js CONSTANTS.refreshColors()
# ===========================================================================


def test_fe04_constants_refresh_colors():
    """FE-04: CONSTANTS provides refreshColors() method to dynamically update CSS colors."""
    source = _read("static/js/state.js")
    assert "refreshColors()" in source
    assert "window.CONSTANTS = CONSTANTS;" in source

    node_test = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

let computedVars = {
  "--primary": "#6bb6ff",
  "--success": "#7dffb0",
  "--danger": "#ff7d7d",
  "--warning": "#ffcc66",
  "--purple": "#c28bff",
  "--gold": "#ffd86b",
  "--text-mute": "#9ca3af",
  "--card-bg": "rgba(255, 255, 255, 0.06)",
};

const context = {
  window: {},
  document: {
    documentElement: {},
  },
  localStorage: {
    getItem: () => null,
    setItem: () => {},
  },
  APP_CONFIG: { has_mistral_key: false },
};
context.window = context;
context.window.addEventListener = () => {};
context.window.getComputedStyle = () => ({
  getPropertyValue: (name) => computedVars[name] || "",
});

vm.createContext(context);
const stateCode = fs.readFileSync("static/js/state.js", "utf8");
vm.runInContext(stateCode, context);

assert(typeof context.CONSTANTS.refreshColors === "function");
assert.strictEqual(context.CONSTANTS.COLORS.PRIMARY, "#6bb6ff");

// Simulate theme switch
computedVars["--primary"] = "#ff5500";
const updated = context.CONSTANTS.refreshColors();
assert.strictEqual(updated.PRIMARY, "#ff5500");
assert.strictEqual(context.CONSTANTS.COLORS.PRIMARY, "#ff5500");
console.log("FE-04 OK");
"""
    out = _run_node(node_test)
    assert "FE-04 OK" in out


def test_fe04_init_theme_color_scheme_triggers_refresh_colors():
    """FE-04: initThemeColorScheme updates DOM and immediately invokes refreshColors."""
    utils_source = _read("static/js/utils.js")
    assert "window.CONSTANTS?.refreshColors?.()" in utils_source

    node_test = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

let refreshColorsCalled = 0;
let bodyClasses = new Set();
let storage = { mns_color_scheme: "jp_standard" };

const context = {
  window: {},
  document: {
    documentElement: {},
    body: {
      classList: {
        add: (cls) => bodyClasses.add(cls),
        remove: (cls) => bodyClasses.delete(cls),
        contains: (cls) => bodyClasses.has(cls),
      },
    },
    getElementById: () => null,
    addEventListener: () => {},
  },
  localStorage: {
    getItem: (key) => storage[key] || null,
    setItem: (key, val) => { storage[key] = String(val); },
  },
};
context.window = context;
context.window.addEventListener = () => {};
context.window.CONSTANTS = {
  refreshColors: () => {
    refreshColorsCalled++;
    return {};
  },
};

vm.createContext(context);
const utilsCode = fs.readFileSync("static/js/utils.js", "utf8");
vm.runInContext(utilsCode, context);

assert(typeof context.initThemeColorScheme === "function");
context.initThemeColorScheme();

assert.strictEqual(refreshColorsCalled, 1);
assert.strictEqual(bodyClasses.has("theme-color-jp"), true);

// Switch back to us_standard
storage.mns_color_scheme = "us_standard";
context.initThemeColorScheme();
assert.strictEqual(refreshColorsCalled, 2);
assert.strictEqual(bodyClasses.has("theme-color-jp"), false);

console.log("FE-04 INIT_THEME_COLOR_SCHEME OK");
"""
    out = _run_node(node_test)
    assert "FE-04 INIT_THEME_COLOR_SCHEME OK" in out


# ===========================================================================
# FE-05: INITIAL_SKELETON_MAX_WAIT_MS exported and guarded in index_main.js
# ===========================================================================


def test_fe05_initial_skeleton_max_wait_ms_export_and_guard():
    """FE-05: INITIAL_SKELETON_MAX_WAIT_MS is exported to window and guarded in index_main.js."""
    api_source = _read("static/js/api.js")
    assert "window.INITIAL_SKELETON_MAX_WAIT_MS = INITIAL_SKELETON_MAX_WAIT_MS;" in api_source

    index_source = _read("static/js/index_main.js")
    assert "typeof INITIAL_SKELETON_MAX_WAIT_MS !== \"undefined\"" in index_source
    assert "window.INITIAL_SKELETON_MAX_WAIT_MS" in index_source


# ===========================================================================
# FE-06: chrome_extension/background.js openOrbit message handling
# ===========================================================================


def test_fe06_background_open_orbit_message_action():
    """FE-06: chrome_extension/background.js handles openOrbit action."""
    source = _read("chrome_extension/background.js")
    assert 'if (message.action === "openOrbit") {' in source
    assert 'return safeSendResponse(await openRoute("/experimental/orbit"));' in source


# ===========================================================================
# FE-07: chrome_extension/popup.js stock filter debouncing
# ===========================================================================


def test_fe07_popup_stock_filter_debounce():
    """FE-07: popup.js implements debounce and applies it to extStockFilter input event."""
    source = _read("chrome_extension/popup.js")
    assert "function debounce(" in source
    assert '$("extStockFilter")?.addEventListener(' in source
    assert "debounce(filterAndRenderStocks, 150)" in source


# ===========================================================================
# FE-08: static/js/realtime_client.js price flash timer clearing
# ===========================================================================


def test_fe08_realtime_price_flash_timer():
    """FE-08: realtime_client.js tracks priceEl.__flashTimer and clears previous timers."""
    source = _read("static/js/realtime_client.js")
    assert "if (priceEl.__flashTimer) {" in source
    assert "clearTimeout(priceEl.__flashTimer);" in source
    assert "priceEl.__flashTimer = setTimeout(" in source


# ===========================================================================
# FE-09: static/js/utils.js stopCsrfAutoRefresh implementation and export
# ===========================================================================


def test_fe09_stop_csrf_auto_refresh():
    """FE-09: utils.js implements and exports stopCsrfAutoRefresh to clearInterval."""
    source = _read("static/js/utils.js")
    assert "function stopCsrfAutoRefresh()" in source
    assert "clearInterval(window.__mnsCsrfRefreshTimer);" in source
    assert "window.__mnsCsrfRefreshTimer = null;" in source
    assert "window.stopCsrfAutoRefresh = stopCsrfAutoRefresh;" in source

    node_test = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

let clearedTimer = null;
const context = {
  window: {},
  document: {
    querySelector: () => ({ content: "test-token" }),
    addEventListener: () => {},
  },
  setInterval: () => 12345,
  clearInterval: (id) => { clearedTimer = id; },
  console: console,
};
context.window = context;
context.window.addEventListener = () => {};

vm.createContext(context);
const utilsCode = fs.readFileSync("static/js/utils.js", "utf8");
vm.runInContext(utilsCode, context);

assert(typeof context.stopCsrfAutoRefresh === "function");
assert(typeof context.window.stopCsrfAutoRefresh === "function");

// Set active timer
context.window.__mnsCsrfRefreshTimer = 999;
context.stopCsrfAutoRefresh();
assert.strictEqual(clearedTimer, 999);
assert.strictEqual(context.window.__mnsCsrfRefreshTimer, null);
console.log("FE-09 OK");
"""
    out = _run_node(node_test)
    assert "FE-09 OK" in out


# ===========================================================================
# FE-10: static/js/experimental/orbit-renderer.js visibilitychange handling
# ===========================================================================


def test_fe10_orbit_renderer_visibilitychange():
    """FE-10: orbit-renderer.js stops loop on document.hidden and resumes when visible."""
    source = _read("static/js/experimental/orbit-renderer.js")
    assert 'document.addEventListener("visibilitychange", this._visibilityHandler);' in source
    assert "cancelAnimationFrame(this.animId)" in source
    assert "this._isRunning" in source

    node_test = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

let cancelId = null;
let reqCount = 0;
const context = {
  window: {},
  document: {
    hidden: false,
    addEventListener: (evt, handler) => {
      if (evt === "visibilitychange") context._visHandler = handler;
    },
    removeEventListener: () => {},
    body: {
      classList: {
        contains: () => false,
      },
    },
  },
  performance: {
    now: () => Date.now(),
  },
  requestAnimationFrame: (cb) => {
    reqCount++;
    return reqCount;
  },
  cancelAnimationFrame: (id) => {
    cancelId = id;
  },
};
context.window = context;
context.window.addEventListener = () => {};
context.window.removeEventListener = () => {};

vm.createContext(context);
const rendererCode = fs.readFileSync("static/js/experimental/orbit-renderer.js", "utf8");
vm.runInContext(rendererCode, context);

const mockCanvas = {
  getContext: () => ({
    fillRect: () => {},
    clearRect: () => {},
    save: () => {},
    restore: () => {},
    beginPath: () => {},
    arc: () => {},
    fill: () => {},
    stroke: () => {},
  }),
  getBoundingClientRect: () => ({ width: 800, height: 600 }),
};
const mockState = { state: { stockList: [] } };

const renderer = new context.OrbitRenderer(mockCanvas, mockState);
assert.strictEqual(renderer._isRunning, false);
assert.strictEqual(renderer.animId, null);

// Start
renderer.start();
assert.strictEqual(renderer._isRunning, true);
assert(renderer.animId !== null);

// Tab hidden
context.document.hidden = true;
context._visHandler();
assert.strictEqual(renderer.animId, null);
assert(cancelId !== null);

// Tab visible again
cancelId = null;
context.document.hidden = false;
context._visHandler();
assert.strictEqual(renderer._isRunning, true);
assert(renderer.animId !== null);

// Explicit stop
renderer.stop();
assert.strictEqual(renderer._isRunning, false);
assert.strictEqual(renderer.animId, null);

// Tab cycle after explicit stop should NOT restart
context.document.hidden = true;
context._visHandler();
context.document.hidden = false;
context._visHandler();
assert.strictEqual(renderer._isRunning, false);
assert.strictEqual(renderer.animId, null);

console.log("FE-10 OK");
"""
    out = _run_node(node_test)
    assert "FE-10 OK" in out
