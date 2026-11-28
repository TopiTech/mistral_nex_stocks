"""Unit 5 comprehensive review fixes regression tests (2026-11-28)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend regression tests"
    res = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Node check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"


def test_setup_pageshow_bfcache_resets_submit_inflight() -> None:
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("vm");

const listeners = new Map();
const elements = new Map();
function makeEl(id) {
  const el = {
    id,
    disabled: false,
    hidden: false,
    textContent: id === "saveBtn" ? "保存して開始" : "",
    value: "",
    style: {},
    dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(type, fn) {
      listeners.set(`${id}:${type}`, fn);
    },
    focus() {},
  };
  elements.set(id, el);
  return el;
}
["saveBtn", "previewBtn", "errorMsg", "agentIdGroup", "mistralSetupContainer", "openaiSetupContainer"].forEach(makeEl);

let fetchCalls = 0;
const sandbox = {
  window: {
    APP_CONFIG: {},
    location: { href: "" },
    addEventListener(type, fn) {
      if (!listeners.has(`win:${type}`)) listeners.set(`win:${type}`, []);
      listeners.get(`win:${type}`).push(fn);
    },
  },
  document: {
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
  },
  AbortController,
  csrfFetch: async () => {
    fetchCalls += 1;
    return { ok: true, json: async () => ({ ok: true }) };
  },
  console: { log() {}, warn() {}, error() {}, debug() {} },
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(fs.readFileSync("static/js/setup.js", "utf8"), sandbox, { filename: "setup.js" });

(async () => {
  await sandbox.startPreviewMode();
  assert.equal(fetchCalls, 1);
  // While redirecting (before bfcache restore), duplicate submit is blocked
  await sandbox.startPreviewMode();
  assert.equal(fetchCalls, 1);

  // bfcache restore fires pageshow with persisted: true -> resets setupSubmitInFlight
  const pageshowHandlers = listeners.get("win:pageshow") || [];
  assert.ok(pageshowHandlers.length > 0);
  pageshowHandlers.forEach((fn) => fn({ persisted: true }));

  assert.equal(elements.get("previewBtn").disabled, false);
  assert.equal(elements.get("saveBtn").disabled, false);
  await sandbox.startPreviewMode();
  assert.equal(fetchCalls, 2);
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node(script)


def test_open_modal_reopen_preserves_previous_focus_outside_modal() -> None:
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("vm");

function makeFocusable(name) {
  return {
    name,
    focused: false,
    focus() {
      sandbox.document.activeElement = this;
      this.focused = true;
    },
    hasAttribute() { return false; },
    closest() { return null; },
    getBoundingClientRect() { return { width: 10, height: 10 }; },
    offsetParent: {},
  };
}

const triggerBtn = makeFocusable("triggerBtn");
const modalInnerBtn = makeFocusable("modalInnerBtn");
const modalEl = {
  id: "testModal",
  style: { display: "none" },
  attrs: {},
  classes: new Set(["hidden"]),
  classList: {
    contains: (c) => modalEl.classes.has(c),
    add: (c) => modalEl.classes.add(c),
    remove: (c) => modalEl.classes.delete(c),
  },
  setAttribute(k, v) { this.attrs[k] = v; },
  removeAttribute(k) { delete this.attrs[k]; },
  hasAttribute(k) { return k in this.attrs; },
  addEventListener() {},
  removeEventListener() {},
  querySelectorAll: () => [modalInnerBtn],
  contains: (el) => el === modalEl || el === modalInnerBtn,
  focus() {},
};

const sandbox = {
  window: {
    getComputedStyle: () => ({ display: "flex", visibility: "visible" }),
    addEventListener() {},
  },
  document: {
    activeElement: triggerBtn,
    body: { style: {}, _modalOpenCount: 0 },
    getElementById: (id) => (id === "testModal" ? modalEl : null),
    addEventListener() {},
  },
  DOM: { get: (id) => (id === "testModal" ? modalEl : null) },
  URLSearchParams,
  console: { log() {}, warn() {}, error() {}, debug() {} },
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(fs.readFileSync("static/js/utils.js", "utf8"), sandbox, { filename: "utils.js" });

// First open captures external triggerBtn and moves focus inside modal
sandbox.openModal("testModal");
assert.equal(modalEl._previousFocus, triggerBtn);
assert.equal(sandbox.document.activeElement, modalInnerBtn);

// Re-opening while focus is inside modal must NOT overwrite _previousFocus with modalInnerBtn
sandbox.openModal("testModal");
assert.equal(modalEl._previousFocus, triggerBtn);
"""
    _run_node(script)


def test_api_client_direct_open_sse_while_visibility_paused_updates_last_params() -> None:
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("vm");

class FakeEventSource {
  constructor(url) { this.url = url; this.readyState = 0; }
  addEventListener() {}
  removeEventListener() {}
  close() { this.readyState = 2; }
}

const sandbox = {
  window: { addEventListener() {}, removeEventListener() {} },
  document: { hidden: false, addEventListener() {}, removeEventListener() {} },
  EventSource: FakeEventSource,
  console: { debug() {}, info() {}, warn() {}, error() {} },
  setTimeout: () => 1,
  clearTimeout() {},
  setInterval: () => 1,
  clearInterval() {},
};
vm.runInNewContext(fs.readFileSync("static/js/api_client.js", "utf8"), sandbox, { filename: "api_client.js" });

const client = new sandbox.window.APIClient("/api", {
  sseHeartbeatTimeout: 60000,
  sseReconnectBaseDelay: 1000,
  sseReconnectMaxDelay: 1000,
  watchdogInterval: 60000,
  visibilityTimeout: 60000,
});
client.openSSE("/stocks/stream?mode=old", () => {}, () => {});
client.isVisibilityPaused = true;

client.openSSE("/stocks/stream?mode=new", () => {}, () => {});
assert.equal(client.isVisibilityPaused, false);
assert.equal(client._lastSSEParams.url, "/stocks/stream?mode=new");
client.destroy();
"""
    _run_node(script)


def test_search_results_dismiss_clears_combobox_aria_and_watchdog_cleared_on_unload() -> None:
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("vm");

const searchInputEl = {
  id: "searchInput",
  attrs: { "aria-expanded": "true", "aria-activedescendant": "search-result-option-0" },
  events: [],
  setAttribute(k, v) { this.attrs[k] = v; },
  removeAttribute(k) { delete this.attrs[k]; },
  hasAttribute(k) { return k in this.attrs; },
  dispatchEvent(ev) { this.events.push(ev.type); },
  contains: () => false,
};
const searchResultsEl = {
  id: "search-results",
  style: { display: "block" },
  contains: () => false,
};

const winListeners = new Map();
const docListeners = new Map();
const clearedTimeouts = [];
const sandbox = {
  window: {
    getComputedStyle: (el) => el.style,
    addEventListener(type, fn) {
      if (!winListeners.has(type)) winListeners.set(type, []);
      winListeners.get(type).push(fn);
    },
  },
  document: {
    getElementById: (id) => (id === "searchResults" || id === "search-results" ? searchResultsEl : id === "searchInput" ? searchInputEl : null),
    querySelector: () => null,
    querySelectorAll: () => [],
    body: { style: {} },
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push(fn);
    },
  },
  DOM: {
    get: (id) => (id === "search-results" ? searchResultsEl : id === "searchInput" ? searchInputEl : null),
  },
  CustomEvent: class { constructor(type) { this.type = type; } },
  URLSearchParams,
  state: { stocks: { us: [], jp: [], idx: [] }, activeTab: "us" },
  INITIAL_SKELETON_MAX_WAIT_MS: 8000,
  console: { log() {}, warn() {}, error() {}, debug() {} },
  setTimeout: () => 999,
  clearTimeout: (id) => clearedTimeouts.push(id),
};

vm.runInNewContext(fs.readFileSync("static/js/utils.js", "utf8"), sandbox, { filename: "utils.js" });
const escapeEvt = { key: "Escape", preventDefault() {}, stopPropagation() {} };
assert.equal(sandbox.closeTopmostOverlayOnEscape(escapeEvt), true);
assert.equal(searchResultsEl.style.display, "none");
assert.equal(searchInputEl.attrs["aria-expanded"], "false");
assert.equal("aria-activedescendant" in searchInputEl.attrs, false);
assert.ok(searchInputEl.events.includes("searchresultschange"));

// Outside click in index_main.js (registered on window) also clears ARIA attributes on #searchInput
vm.runInNewContext(fs.readFileSync("static/js/index_main.js", "utf8"), sandbox, { filename: "index_main.js" });
searchResultsEl.style.display = "block";
searchInputEl.attrs["aria-expanded"] = "true";
searchInputEl.attrs["aria-activedescendant"] = "search-result-option-1";
const clickHandlers = winListeners.get("click") || [];
assert.ok(clickHandlers.length > 0);
clickHandlers.forEach((fn) => fn({ target: { contains: () => false } }));
assert.equal(searchResultsEl.style.display, "none");
assert.equal(searchInputEl.attrs["aria-expanded"], "false");
assert.equal("aria-activedescendant" in searchInputEl.attrs, false);

// Watchdog armed and then cleared on invalidatePendingInitialStocksFetch / pagehide
sandbox.armInitialSkeletonWatchdog();
sandbox.invalidatePendingInitialStocksFetch();
assert.ok(clearedTimeouts.includes(999));

clearedTimeouts.length = 0;
sandbox.armInitialSkeletonWatchdog();
(winListeners.get("pagehide") || []).forEach((fn) => fn());
assert.ok(clearedTimeouts.includes(999));
"""
    _run_node(script)
