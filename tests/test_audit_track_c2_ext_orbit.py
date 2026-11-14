"""Regression tests for Track C2 fixes: Experimental Orbit (UIX-001..009) and Chrome Extension (EXT-001..006)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str, timeout: int = 20) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for JavaScript VM regression tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, f"Node script failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout


def test_uix001_ai_dive_stale_request_cannot_clobber_active_symbol():
    """Closing or switching AI Dive mid-flight must prevent stale catch/finally DOM mutations."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeEl(id = "") {
  const classes = new Set();
  const attrs = new Map();
  return {
    id,
    textContent: "",
    disabled: false,
    title: "",
    children: [],
    style: {},
    className: "",
    classList: {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      toggle: (n, force) => (force === undefined ? (classes.has(n) ? classes.delete(n) : classes.add(n)) : force ? classes.add(n) : classes.delete(n)),
      contains: (n) => classes.has(n),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: (k) => (attrs.has(k) ? attrs.get(k) : null),
    removeAttribute: (k) => attrs.delete(k),
    hasAttribute: (k) => attrs.has(k),
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    querySelector: () => null,
    addEventListener: () => {},
    focus: () => {},
    contains: () => false,
  };
}

let deferred = [];
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
  APP_CONFIG: { ai_provider: "mistral" },
  createRequestToken: () => "tok-1",
  apiFetch(url, opts = {}, behaviors = {}) {
    return new Promise((resolve, reject) => {
      deferred.push({ url, opts, behaviors, resolve, reject });
    });
  },
};
window.window = window;

const document = {
  body: { classList: { contains: () => false } },
  activeElement: null,
  contains: () => false,
  getElementById: () => null,
  createElement: (tag) => makeEl(tag),
  createTextNode: (t) => ({ nodeValue: String(t), textContent: String(t) }),
};

const ctx = {
  window,
  global: window,
  document,
  console,
  URLSearchParams,
  URL,
  AbortController,
  DOMException,
  setTimeout,
  clearTimeout,
  Number,
  String,
  Math,
  Array,
  Map,
  Set,
  Promise,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/ai-dive-controller.js", "utf8"), ctx);

(async () => {
  const state = new window.ObservatoryState();
  state.setStocks([
    { symbol: "AAPL", market: "us", name: "Apple", price: 180, changePercent: 1.5 },
    { symbol: "MSFT", market: "us", name: "Microsoft", price: 420, changePercent: 2.0 },
  ]);

  const els = {
    overlay: makeEl("overlay"),
    closeBtn: makeEl("closeBtn"),
    symbolTitle: makeEl("symbolTitle"),
    nameSubtitle: makeEl("nameSubtitle"),
    tier1Container: makeEl("tier1"),
    tier2Container: makeEl("tier2"),
    tier3Container: makeEl("tier3"),
    tier4Container: makeEl("tier4"),
    tier4ProviderLabel: makeEl("providerLabel"),
    startAiBtn: makeEl("startBtn"),
  };

  const ctrl = new window.AiDiveController(state, els);
  state.openAiDive("AAPL");
  // News request for AAPL should have showToast: false
  assert.strictEqual(deferred[0].url, "/api/news");
  assert.strictEqual(deferred[0].behaviors.showToast, false);

  const runAapl = ctrl.runDeepAiAnalysis();
  const analyzeAapl = deferred.find((d) => d.url === "/api/analyze-v2");
  assert.ok(analyzeAapl);
  assert.strictEqual(analyzeAapl.behaviors.showToast, false);
  assert.strictEqual(els.startAiBtn.disabled, true);

  // Switch to MSFT before AAPL completes; then start MSFT AI analysis
  state.openAiDive("MSFT");
  const runMsft = ctrl.runDeepAiAnalysis();
  els.startAiBtn.disabled = true;
  els.startAiBtn.textContent = "MSFT_RUNNING";
  els.tier4Container.textContent = "MSFT_LOADING";

  // Fail old AAPL request with non-AbortError (e.g., delayed network error)
  analyzeAapl.reject(new Error("old failure"));
  await runAapl;

  // Stale AAPL catch/finally must NOT re-enable MSFT button or overwrite MSFT container
  assert.strictEqual(els.startAiBtn.disabled, true);
  assert.strictEqual(els.startAiBtn.textContent, "MSFT_RUNNING");
  assert.strictEqual(els.tier4Container.textContent, "MSFT_LOADING");

  // Also reject old AAPL news cleanly -> Tier 3 must remain MSFT loading, not error
  deferred[0].reject(new Error("old news failure"));
  await new Promise((r) => setTimeout(r, 10));
  assert.notStrictEqual(els.tier3Container.children[0]?.className, "news-empty");

  // Resolve active MSFT request cleanly
  const analyzeMsft = deferred.filter((d) => d.url === "/api/analyze-v2")[1];
  analyzeMsft.resolve({
    response: { ok: true, status: 200 },
    data: { recommendation: "買い", sentiment: "ポジティブ" },
  });
  await runMsft;
  assert.strictEqual(els.startAiBtn.disabled, false);
  process.stdout.write("OK");
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
"""
    )
    assert output == "OK"


def test_uix001_constellation_close_clear_abort_and_stocks_refresh():
    """ConstellationDrawer must abort/clear on close & <2 symbols, guard finally, and refresh on stocks."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeEl(id = "") {
  const classes = new Set();
  const attrs = new Map();
  const listeners = new Map();
  return {
    id,
    textContent: "",
    disabled: false,
    children: [],
    className: "",
    classList: {
      add: (...n) => n.forEach((x) => classes.add(x)),
      remove: (...n) => n.forEach((x) => classes.delete(x)),
      toggle: (x, f) => (f === undefined ? (classes.has(x) ? classes.delete(x) : classes.add(x)) : f ? classes.add(x) : classes.delete(x)),
      contains: (x) => classes.has(x),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: (k) => attrs.get(k) || null,
    removeAttribute: (k) => attrs.delete(k),
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    addEventListener(ev, fn) {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    dispatchEvent(ev) {
      for (const fn of listeners.get(ev) || []) fn();
    },
    contains: () => false,
    focus: () => {},
  };
}

let chatDeferred = [];
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
  createRequestToken: () => "tok-c",
  apiFetch(url, opts = {}, behaviors = {}) {
    return new Promise((resolve, reject) => {
      chatDeferred.push({ url, opts, behaviors, resolve, reject });
    });
  },
};
window.window = window;

const document = {
  body: { classList: { contains: () => false } },
  activeElement: null,
  createElement: (t) => makeEl(t),
  createTextNode: (t) => ({ nodeValue: String(t), textContent: String(t) }),
};

const ctx = {
  window,
  global: window,
  document,
  console,
  URLSearchParams,
  URL,
  AbortController,
  DOMException,
  setTimeout,
  clearTimeout,
  Number,
  String,
  Math,
  Array,
  Map,
  Set,
  Promise,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/constellation-controller.js", "utf8"), ctx);

(async () => {
  const state = new window.ObservatoryState();
  state.setStocks([
    { symbol: "AAPL", market: "us", name: "Apple", price: 180, changePercent: 1.0 },
    { symbol: "MSFT", market: "us", name: "Microsoft", price: 420, changePercent: 2.0 },
    { symbol: "NVDA", market: "us", name: "Nvidia", price: 120, changePercent: 3.0 },
  ]);

  const els = {
    toggleBtn: makeEl("toggle"),
    drawer: makeEl("drawer"),
    closeBtn: makeEl("close"),
    countText: makeEl("count"),
    listContainer: makeEl("list"),
    chartWrapper: makeEl("chartWrapper"),
    chartCanvas: null,
    aiCompareBtn: makeEl("aiCompareBtn"),
    aiResultContainer: makeEl("aiResult"),
  };
  els.drawer.classList.add("hidden");

  const ctrl = new window.ConstellationController(state, els);
  state.toggleConnectMode(true);
  state.toggleConnectedSymbol("AAPL");
  state.toggleConnectedSymbol("MSFT");
  assert.strictEqual(els.aiCompareBtn.disabled, false);

  const run1 = ctrl.runAiComparison();
  assert.strictEqual(chatDeferred.length, 1);
  assert.strictEqual(chatDeferred[0].behaviors.showToast, false);
  const sig1 = chatDeferred[0].opts.signal;
  assert.strictEqual(sig1.aborted, false);

  // Removing a symbol (<2 connected) must abort in-flight request & hide result & disable button
  state.toggleConnectedSymbol("MSFT");
  assert.strictEqual(sig1.aborted, true);
  assert.strictEqual(els.aiResultContainer.classList.contains("hidden"), true);
  chatDeferred[0].reject(new Error("late failure"));
  await run1;
  assert.strictEqual(els.aiCompareBtn.disabled, true);

  // Reconnect 2 stocks and update stocks -> HUD must refresh prices
  state.toggleConnectedSymbol("MSFT");
  state.setStocks([
    { symbol: "AAPL", market: "us", name: "Apple", price: 199.99, changePercent: 5.0 },
    { symbol: "MSFT", market: "us", name: "Microsoft", price: 450.0, changePercent: 2.5 },
  ]);
  const serializeText = (node) =>
    (node.textContent || "") + (node.children || []).map(serializeText).join(" ");
  assert.ok(serializeText(els.listContainer).includes("$199.99"));

  // Close button click aborts any in-flight comparison and clears/hides aiResultContainer
  const run2 = ctrl.runAiComparison();
  const sig2 = chatDeferred[1].opts.signal;
  els.closeBtn.dispatchEvent("click");
  assert.strictEqual(sig2.aborted, true);
  assert.strictEqual(els.aiResultContainer.classList.contains("hidden"), true);
  chatDeferred[1].reject(new DOMException("aborted", "AbortError"));
  await run2;
  assert.strictEqual(els.aiCompareBtn.disabled, true);
  process.stdout.write("OK");
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
"""
    )
    assert output == "OK"


def test_uix002_ai_dive_japanese_recommendation_classes():
    """Japanese recommendation strings map to matching CSS classes for .ai-rec-badge."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeEl(tag = "") {
  let _text = "";
  return {
    tag,
    get textContent() {
      return _text;
    },
    set textContent(v) {
      _text = String(v);
      if (_text === "") this.children = [];
    },
    className: "",
    children: [],
    classList: { add: () => {}, remove: () => {}, toggle: () => {} },
    setAttribute: () => {},
    removeAttribute: () => {},
    addEventListener: () => {},
    appendChild(c) {
      this.children.push(c);
      return c;
    },
  };
}
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
  ObservatoryDataAdapter: { formatPrice: (p) => String(p) },
};
window.window = window;
const document = {
  body: { classList: { contains: () => false } },
  createElement: (t) => makeEl(t),
  createTextNode: (t) => ({ nodeValue: t, textContent: t }),
};
const ctx = {
  window,
  global: window,
  document,
  console,
  URLSearchParams,
  URL,
  Number,
  String,
  Array,
  Map,
  Set,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/ai-dive-controller.js", "utf8"), ctx);

const state = new window.ObservatoryState();
const tier4 = makeEl("div");
const ctrl = new window.AiDiveController(state, { tier4Container: tier4 });

const expected = [
  ["強い買い", "rec-strong-buy"],
  ["買い", "rec-buy"],
  ["中立", "rec-hold"],
  ["売り", "rec-sell"],
  ["強い売り", "rec-sell"],
  ["STRONG_BUY", "rec-strong-buy"],
  ["BUY", "rec-buy"],
  ["HOLD", "rec-hold"],
  ["SELL", "rec-sell"],
];

for (const [rec, expectedClass] of expected) {
  ctrl.renderAiAnalysisResult({ recommendation: rec });
  const badge = tier4.children[0].children[0].children[0];
  assert.strictEqual(
    badge.className,
    `ai-rec-badge ${expectedClass}`,
    `Expected ${rec} -> ${expectedClass}, got ${badge.className}`,
  );
}
process.stdout.write("OK");
"""
    )
    assert output == "OK"


def test_uix003_temporal_retry_timers_granularity_sync_and_slider_stop():
    """Switching symbol/granularity clears retryTimers, granularity stays synced, slider stops timelapse."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeBtn(gran) {
  const classes = new Set();
  const attrs = new Map([["data-granularity", gran]]);
  return {
    classList: {
      toggle: (n, f) => (f ? classes.add(n) : classes.delete(n)),
      contains: (n) => classes.has(n),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: (k) => attrs.get(k) || null,
  };
}

let nextTimerId = 1;
const activeTimers = new Map();
const fetchQueue = [];
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
  apiFetch(url, opts = {}, behaviors = {}) {
    return new Promise((resolve, reject) => {
      fetchQueue.push({ url, opts, behaviors, resolve, reject });
    });
  },
};
window.window = window;

const ctx = {
  window,
  global: window,
  document: { body: { classList: { contains: () => false } } },
  console,
  URLSearchParams,
  URL,
  AbortController,
  Number,
  String,
  Array,
  Map,
  Set,
  Promise,
  parseInt,
  setTimeout(fn) {
    const id = nextTimerId++;
    activeTimers.set(id, fn);
    return id;
  },
  clearTimeout(id) {
    activeTimers.delete(id);
  },
  setInterval: () => 999,
  clearInterval: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/temporal-controller.js", "utf8"), ctx);

(async () => {
  const state = new window.ObservatoryState();
  state.set({ market: "all" });
  state.setStocks([
    { symbol: "^N225", market: "idx", price: 38000, changePercent: 0.5 },
    { symbol: "AAPL", market: "us", price: 180, changePercent: 1.0 },
  ]);

  const granBtns = ["1d", "5d", "1mo", "3mo", "1y"].map(makeBtn);
  const granGroup = {
    querySelectorAll: () => granBtns,
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  const sliderListeners = {};
  const timeSlider = {
    disabled: false,
    min: "0",
    max: "0",
    value: "0",
    setAttribute: () => {},
    addEventListener: (ev, fn) => {
      sliderListeners[ev] = fn;
    },
    removeEventListener: () => {},
  };
  const timeDisplay = { textContent: "" };
  const timeBadge = { textContent: "", className: "" };

  const ctrl = new window.TemporalController(state, {
    granularityGroup: granGroup,
    timeSlider,
    timeDisplay,
    timeBadge,
  });

  // 1. Initial fetch for ^N225 must resolve market=idx (never market=all) & pass showToast: false
  assert.strictEqual(fetchQueue.length, 1);
  assert.ok(fetchQueue[0].url.includes("market=idx"));
  assert.strictEqual(fetchQueue[0].behaviors.showToast, false);

  // Return fetching: true -> schedules retry timer
  fetchQueue[0].resolve({ fetching: true });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(ctrl.retryTimers.size, 1);

  // Switch symbol to AAPL -> pending retryTimers from ^N225:3mo MUST be cleared!
  state.setSelectedSymbol("AAPL");
  assert.strictEqual(ctrl.retryTimers.size, 0);
  assert.strictEqual(activeTimers.size, 0);

  // Granularity sync before early return in showHistoryError and error catch
  state.setTimeGranularity("1y");
  const aapl1y = fetchQueue[fetchQueue.length - 1];
  aapl1y.reject(new Error("boom"));
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(timeBadge.textContent, "ERROR");
  assert.strictEqual(granBtns.find((b) => b.getAttribute("data-granularity") === "1y").classList.contains("active"), true);
  assert.strictEqual(granBtns.find((b) => b.getAttribute("data-granularity") === "3mo").classList.contains("active"), false);

  // Slider input stops active timelapse
  ctrl.isPlaying = true;
  ctrl.playTimer = 999;
  sliderListeners.input({ target: { value: "0" } });
  assert.strictEqual(ctrl.isPlaying, false);
  process.stdout.write("OK");
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
"""
    )
    assert output == "OK"


def test_uix004_uix007_orbit_entry_silent_idx_toast_and_combobox_backdrops():
    """orbit-entry handles idx in all-market, preserves silent retry & error banner, combobox nav, and backdrop closes."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeElement(id, tag = "div") {
  const classes = new Set();
  const attrs = new Map([["id", id]]);
  const listeners = new Map();
  const el = {
    id,
    tagName: tag.toUpperCase(),
    textContent: "",
    value: "",
    disabled: false,
    children: [],
    parentElement: null,
    style: {},
    classList: {
      add: (...n) => n.forEach((x) => classes.add(x)),
      remove: (...n) => n.forEach((x) => classes.delete(x)),
      toggle: (x, f) => (f === undefined ? (classes.has(x) ? classes.delete(x) : classes.add(x)) : f ? classes.add(x) : classes.delete(x)),
      contains: (x) => classes.has(x),
    },
    setAttribute(k, v) {
      attrs.set(k, String(v));
      if (k === "id") el.id = String(v);
    },
    getAttribute: (k) => (attrs.has(k) ? attrs.get(k) : null),
    hasAttribute: (k) => attrs.has(k),
    removeAttribute: (k) => attrs.delete(k),
    appendChild(child) {
      child.parentElement = el;
      el.children.push(child);
      return child;
    },
    querySelectorAll(sel) {
      const out = [];
      const walk = (node) => {
        for (const ch of node.children || []) {
          if (sel === '[role="option"]' && ch.getAttribute && ch.getAttribute("role") === "option") {
            out.push(ch);
          }
          walk(ch);
        }
      };
      walk(el);
      return out;
    },
    querySelector: () => null,
    addEventListener(ev, fn) {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    removeEventListener: () => {},
    dispatchEvent(evObj) {
      const ev = typeof evObj === "string" ? { type: evObj, target: el, preventDefault() {} } : evObj;
      if (!ev.preventDefault) ev.preventDefault = () => {};
      for (const fn of listeners.get(ev.type) || []) fn(ev);
    },
    click() {
      this.dispatchEvent({ type: "click", target: el });
    },
    focus() {
      document.activeElement = el;
    },
    blur() {},
    contains(other) {
      let c = other;
      while (c) {
        if (c === el) return true;
        c = c.parentElement;
      }
      return false;
    },
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    scrollIntoView: () => {},
    closest: () => null,
  };
  return el;
}

const byId = new Map();
for (const [id, tag] of [
  ["orbit-canvas", "canvas"],
  ["observatory-loading", "div"],
  ["observatory-error", "div"],
  ["observatory-error-msg", "p"],
  ["observatory-retry-btn", "button"],
  ["market-toggle-us", "button"],
  ["market-toggle-jp", "button"],
  ["market-toggle-all", "button"],
  ["btn-orbit-search", "button"],
  ["orbit-search-modal", "div"],
  ["orbit-search-input", "input"],
  ["orbit-search-results", "div"],
  ["orbit-search-close", "button"],
  ["btn-shortcuts-help", "button"],
  ["shortcuts-help-modal", "div"],
  ["shortcuts-help-close", "button"],
  ["center-stock-symbol", "div"],
  ["center-stock-name", "div"],
  ["center-stock-price", "span"],
  ["center-stock-change", "span"],
  ["observatory-live-region", "div"],
  ["observatory-sr-table-container", "div"],
]) {
  byId.set(id, makeElement(id, tag));
}
byId.get("orbit-search-modal").classList.add("hidden");
byId.get("orbit-search-modal").setAttribute("aria-hidden", "true");
byId.get("shortcuts-help-modal").classList.add("hidden");
byId.get("shortcuts-help-modal").setAttribute("aria-hidden", "true");
byId.get("observatory-error").classList.add("hidden");

const canvas = byId.get("orbit-canvas");
canvas.getContext = () => ({
  save() {},
  restore() {},
  scale() {},
  createRadialGradient: () => ({ addColorStop() {} }),
  fillRect() {},
  beginPath() {},
  arc() {},
  ellipse() {},
  fill() {},
  stroke() {},
  setLineDash() {},
  fillText() {},
  measureText: (t) => ({ width: String(t).length * 7 }),
});

let domReadyCb = null;
const apiCalls = [];
let stockResponseQueue = [];
const timeouts = [];

const document = {
  body: { classList: { contains: () => false } },
  hidden: false,
  activeElement: null,
  contains: () => true,
  getElementById: (id) => byId.get(id) || null,
  createElement: (tag) => makeElement("", tag),
  createTextNode: (t) => ({ nodeValue: t, textContent: t }),
  addEventListener(ev, fn) {
    if (ev === "DOMContentLoaded") domReadyCb = fn;
  },
  removeEventListener: () => {},
};

const window = {
  location: { search: "?market=all", href: "http://localhost/experimental/orbit?market=all" },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  history: { replaceState: () => {} },
  addEventListener: () => {},
  removeEventListener: () => {},
  devicePixelRatio: 1,
  innerWidth: 1200,
  innerHeight: 800,
  apiFetch(url, opts = {}, behaviors = {}) {
    apiCalls.push({ url, opts, behaviors });
    const next = stockResponseQueue.shift();
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve(next);
  },
};
window.window = window;

const ctx = {
  window,
  global: window,
  document,
  console,
  URLSearchParams,
  URL,
  AbortController,
  performance: { now: () => 1000 },
  requestAnimationFrame: () => 1,
  cancelAnimationFrame: () => {},
  setTimeout(fn, delay) {
    const entry = { fn, delay };
    timeouts.push(entry);
    return entry;
  },
  clearTimeout: () => {},
  setInterval: () => 1,
  clearInterval: () => {},
  Number,
  String,
  Math,
  Array,
  Map,
  Set,
  Promise,
  Object,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
for (const file of [
  "data-adapter.js",
  "orbit-state.js",
  "orbit-renderer.js",
  "gesture-controller.js",
  "temporal-controller.js",
  "constellation-controller.js",
  "ai-dive-controller.js",
  "accessibility-controller.js",
  "orbit-entry.js",
]) {
  vm.runInContext(fs.readFileSync(`static/js/experimental/${file}`, "utf8"), ctx);
}

(async () => {
  // Initial load fails -> error overlay shown; then a silent refresh runs and returns fetching: true
  stockResponseQueue.push(new Error("initial fail"));
  domReadyCb();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const errOverlay = byId.get("observatory-error");
  assert.strictEqual(errOverlay.classList.contains("hidden"), false, "error overlay should be visible");
  assert.strictEqual(apiCalls[0].behaviors.showToast, false);

  // Simulate silent interval callback when backend says fetching: true -> must keep error visible & pass silent on retry
  stockResponseQueue.push({ data: { fetching: true } });
  stockResponseQueue.push({
    data: {
      stocks: {
        us: [{ symbol: "AAPL", name: "Apple", price: 185, change_percent: 1.2 }],
        jp: [{ symbol: "7203.T", name: "Toyota", price: 2500, change_percent: -0.5 }],
        idx: [{ symbol: "^N225", name: "Nikkei", price: 39000, change_percent: 0.8 }],
      },
    },
  });

  // Trigger silent load via visibility/retry? Better: trigger retry button vs silent refresh
  // Let's find the retry timeout handler or visibility? Trigger retryBtn? RetryBtn is non-silent!
  // Let's also verify setInterval handler by wrapping setInterval cleanly:
  // Actually reload with captured setInterval callback:
  process.exitCode = 0;
  // Test search combobox keyboard & backdrop clicks on current page
  // First populate stocks via retryBtn
  stockResponseQueue.length = 0;
  stockResponseQueue.push({
    data: {
      stocks: {
        us: [{ symbol: "AAPL", name: "Apple", price: 185, change_percent: 1.2 }],
        jp: [{ symbol: "7203.T", name: "Toyota", price: 2500, change_percent: -0.5 }],
        idx: [{ symbol: "^N225", name: "Nikkei", price: 39000, change_percent: 0.8 }],
      },
    },
  });
  byId.get("observatory-retry-btn").click();
  await new Promise((r) => setImmediate(r));

  assert.strictEqual(errOverlay.classList.contains("hidden"), true);
  // Check idx (^N225) was loaded when market === "all"
  const textOf = (el) =>
    (el.textContent || "") + (el.children || []).map(textOf).join(" ");
  const searchResults = byId.get("orbit-search-results");
  byId.get("btn-orbit-search").click();
  const options = searchResults.querySelectorAll('[role="option"]');
  assert.strictEqual(options.length, 3);
  assert.ok(options.some((o) => textOf(o).includes("^N225")));
  // Unique option IDs + keyboard navigation
  const input = byId.get("orbit-search-input");
  input.dispatchEvent({ type: "keydown", key: "ArrowDown" });
  assert.strictEqual(input.getAttribute("aria-activedescendant"), options[0].id);
  assert.strictEqual(options[0].classList.contains("active"), true);
  input.dispatchEvent({ type: "keydown", key: "End" });
  assert.strictEqual(input.getAttribute("aria-activedescendant"), options[2].id);
  input.dispatchEvent({ type: "keydown", key: "Home" });
  assert.strictEqual(input.getAttribute("aria-activedescendant"), options[0].id);
  input.dispatchEvent({ type: "keydown", key: "ArrowDown" });
  assert.strictEqual(input.getAttribute("aria-activedescendant"), options[1].id);
  input.dispatchEvent({ type: "keydown", key: "Enter" });
  // Search modal closed on Enter & active selection became option 1 (7203.T)
  assert.strictEqual(byId.get("orbit-search-modal").classList.contains("hidden"), true);
  assert.strictEqual(byId.get("center-stock-symbol").textContent, "7203.T");

  // Backdrop click closes search modal & help modal
  byId.get("btn-orbit-search").click();
  assert.strictEqual(byId.get("orbit-search-modal").classList.contains("hidden"), false);
  byId.get("orbit-search-modal").dispatchEvent({
    type: "click",
    target: byId.get("orbit-search-modal"),
  });
  assert.strictEqual(byId.get("orbit-search-modal").classList.contains("hidden"), true);

  byId.get("btn-shortcuts-help").click();
  assert.strictEqual(byId.get("shortcuts-help-modal").classList.contains("hidden"), false);
  byId.get("shortcuts-help-modal").dispatchEvent({
    type: "click",
    target: byId.get("shortcuts-help-modal"),
  });
  assert.strictEqual(byId.get("shortcuts-help-modal").classList.contains("hidden"), true);

  process.stdout.write("OK");
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
"""
    )
    assert output == "OK"


def test_uix004_silent_poll_fetching_retry_preserves_error_overlay():
    """Silent poll hitting fetching: true forwards silent on retry and preserves visible error overlay."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeElement(id, tag = "div") {
  const classes = new Set();
  const attrs = new Map([["id", id]]);
  const listeners = new Map();
  const el = {
    id,
    tagName: tag.toUpperCase(),
    textContent: "",
    children: [],
    style: {},
    classList: {
      add: (...n) => n.forEach((x) => classes.add(x)),
      remove: (...n) => n.forEach((x) => classes.delete(x)),
      toggle: (x, f) => (f ? classes.add(x) : classes.delete(x)),
      contains: (x) => classes.has(x),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: (k) => attrs.get(k) || null,
    removeAttribute: (k) => attrs.delete(k),
    appendChild: (c) => el.children.push(c),
    querySelectorAll: () => [],
    addEventListener: (ev, fn) => {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    contains: () => false,
  };
  return el;
}

const byId = new Map(
  ["orbit-canvas", "observatory-loading", "observatory-error", "observatory-error-msg", "observatory-retry-btn"].map((id) => [id, makeElement(id)]),
);
byId.get("orbit-canvas").getContext = () => ({
  save() {}, restore() {}, scale() {}, createRadialGradient: () => ({ addColorStop() {} }),
  fillRect() {}, beginPath() {}, arc() {}, ellipse() {}, fill() {}, stroke() {},
  setLineDash() {}, fillText() {}, measureText: () => ({ width: 20 }),
});

let domReady = null;
let intervalCb = null;
const timeouts = [];
const queue = [
  new Error("initial down"),
  { data: { fetching: true } },
];

const document = {
  body: { classList: { contains: () => false } },
  hidden: false,
  activeElement: null,
  contains: () => true,
  getElementById: (id) => byId.get(id) || null,
  createElement: (t) => makeElement("", t),
  addEventListener: (ev, fn) => { if (ev === "DOMContentLoaded") domReady = fn; },
  removeEventListener: () => {},
};
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  history: { replaceState: () => {} },
  addEventListener: () => {},
  removeEventListener: () => {},
  setTimeout: (fn, ms) => { const t = { fn, ms }; timeouts.push(t); return t; },
  clearTimeout: () => {},
  setInterval: (fn) => { intervalCb = fn; return 1; },
  clearInterval: () => {},
  devicePixelRatio: 1,
  apiFetch: () => {
    const n = queue.shift();
    return n instanceof Error ? Promise.reject(n) : Promise.resolve(n);
  },
};
window.window = window;

const ctx = {
  window, global: window, document, console, URLSearchParams, URL, AbortController,
  performance: { now: () => 0 },
  requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
  setTimeout: window.setTimeout,
  clearTimeout: window.clearTimeout,
  setInterval: window.setInterval,
  clearInterval: window.clearInterval,
  Number, String, Math, Array, Map, Set, Promise, Object,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
for (const file of [
  "data-adapter.js", "orbit-state.js", "orbit-renderer.js", "gesture-controller.js",
  "temporal-controller.js", "constellation-controller.js", "ai-dive-controller.js",
  "accessibility-controller.js", "orbit-entry.js",
]) {
  vm.runInContext(fs.readFileSync(`static/js/experimental/${file}`, "utf8"), ctx);
}

(async () => {
  domReady();
  await new Promise((r) => setImmediate(r));
  const errOverlay = byId.get("observatory-error");
  assert.strictEqual(errOverlay.classList.contains("hidden"), false);

  // Trigger silent interval refresh -> returns fetching: true -> retry fires -> still silent, error remains visible!
  intervalCb();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(errOverlay.classList.contains("hidden"), false, "silent poll fetching must not hide error overlay");
  assert.strictEqual(timeouts.length, 1);

  queue.push(new Error("silent retry failed"));
  timeouts[0].fn();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(errOverlay.classList.contains("hidden"), false, "silent retry failure must preserve visible error overlay");

  // Silent retry succeeds with real data -> NOW error overlay hides!
  queue.push({ data: { stocks: { us: [{ symbol: "AAPL", price: 180, change_percent: 1 }] } } });
  intervalCb();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(errOverlay.classList.contains("hidden"), true);
  process.stdout.write("OK");
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
"""
    )
    assert output == "OK"


def test_uix005_renderer_idle_pause_hit_order_theme_cache_tooltip_and_rings():
    """Renderer gates ambient animations & pauses rAF when idle, matches hitTest draw order, caches theme, measures tooltips, and aligns ring labels."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

let rafQueue = [];
let nextRafId = 1;
let themeQueries = 0;
let isJpTheme = false;
const fillTextCalls = [];
const labelsDrawn = [];

const ctx2d = {
  font: "",
  textAlign: "left",
  textBaseline: "top",
  fillStyle: "",
  strokeStyle: "",
  lineWidth: 1,
  shadowBlur: 0,
  shadowColor: "",
  save() {},
  restore() {},
  scale() {},
  createRadialGradient: () => ({ addColorStop() {} }),
  fillRect() {},
  beginPath() {},
  arc() {},
  ellipse() {},
  rect() {},
  roundRect() {},
  fill() {},
  stroke() {},
  setLineDash() {},
  moveTo() {},
  lineTo() {},
  fillText(text, x, y) {
    fillTextCalls.push({ text: String(text), x, y, align: this.textAlign, font: this.font });
    if (["PORTFOLIO", "CORE", "WATCHLIST", "MARKET"].includes(String(text))) {
      labelsDrawn.push(String(text));
    }
  },
  measureText: (t) => ({ width: String(t || "").length * 10 }),
};

const canvas = {
  width: 0,
  height: 0,
  style: {},
  getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
  getContext: () => ctx2d,
};

let observerCallback = null;
class MockMutationObserver {
  constructor(cb) { observerCallback = cb; }
  observe() {}
  disconnect() {}
}

const document = {
  hidden: false,
  body: {
    classList: {
      contains(cls) {
        if (cls === "theme-color-jp") themeQueries += 1;
        return isJpTheme;
      },
    },
  },
  addEventListener: () => {},
  removeEventListener: () => {},
};
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
  addEventListener: () => {},
  removeEventListener: () => {},
  devicePixelRatio: 1,
  MutationObserver: MockMutationObserver,
};
window.window = window;

const ctx = {
  window, global: window, document, console, URLSearchParams, URL,
  performance: { now: () => 5000 },
  requestAnimationFrame(fn) {
    const id = nextRafId++;
    rafQueue.push({ id, fn });
    return id;
  },
  cancelAnimationFrame(id) {
    rafQueue = rafQueue.filter((r) => r.id !== id);
  },
  MutationObserver: MockMutationObserver,
  Number, String, Math, Array, Map, Set, Object,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-renderer.js", "utf8"), ctx);

const state = new window.ObservatoryState();
// 1. Test ring assignment with no holdings -> label is CORE, not PORTFOLIO
state.setStocks([
  { symbol: "AAPL", market: "us", price: 180, changePercent: 1.5 },
  { symbol: "MSFT", market: "us", price: 420, changePercent: -1.5 },
  { symbol: "^N225", market: "idx", price: 39000, changePercent: 0.5 },
]);
const renderer = new window.OrbitRenderer(canvas, state);
const queriesAfterInit = themeQueries;
assert.strictEqual(queriesAfterInit, 1, "theme queried once in constructor");

renderer.start();
assert.strictEqual(rafQueue.length, 1);
rafQueue.shift().fn(1000);
assert.ok(labelsDrawn.includes("CORE"), "non-portfolio mode should label inner ring CORE");
assert.strictEqual(labelsDrawn.includes("PORTFOLIO"), false);
// Theme queries did NOT repeat per node during render!
assert.strictEqual(themeQueries, 1);

// 2. MutationObserver updates cached theme without per-frame DOM queries
isJpTheme = true;
observerCallback();
assert.strictEqual(renderer.getColorForChange(2.0).main, "#f43f5e");
assert.strictEqual(themeQueries, 2);

// 3. Reduced motion settles and pauses rAF loop; ambient clock stays frozen
state.toggleReducedMotion(true);
const frozenBefore = renderer._ambientTime;
rafQueue.shift().fn(2000);
assert.strictEqual(renderer._ambientTime, frozenBefore, "ambient time must freeze when reducedMotion");
assert.strictEqual(rafQueue.length, 0, "rAF loop should pause once idle and settled");
assert.strictEqual(renderer._isIdleSleeping, true);

// Hover event wakes renderer to draw tooltip and settles back
state.setHoveredSymbol("MSFT");
assert.strictEqual(rafQueue.length, 1, "state update must wake paused renderer");
fillTextCalls.length = 0;
rafQueue.shift().fn(3000);
// Tooltip right-aligned sector and percentage never collide with fixed 75/90 offsets
const msftTooltipSector = fillTextCalls.find((c) => c.text === "その他");
const msftTooltipPct = fillTextCalls.find((c) => c.text === "-1.50%");
assert.ok(msftTooltipSector && msftTooltipSector.align === "right");
assert.ok(msftTooltipPct && msftTooltipPct.align === "right");

// 4. Hit test matches draw order (reverse of assigned, not raw stockList)
// Suppose raw stockList puts AAPL at index 0, MSFT at index 1, NVDA at index 2,
// and AAPL is center (drawn FIRST, at assigned[0]), MSFT is inner (assigned[1]),
// ^N225 is outer (assigned[2], drawn LAST).
// Place MSFT and ^N225 render coordinates identically at (200, 200): ^N225 must win hitTest!
const nodeMsft = renderer.nodeRenderData.get("MSFT");
const nodeIdx = renderer.nodeRenderData.get("^N225");
nodeMsft.currentX = 200; nodeMsft.currentY = 200; nodeMsft.currentRadius = 22;
nodeIdx.currentX = 200; nodeIdx.currentY = 200; nodeIdx.currentRadius = 22;
const hit = renderer.hitTest(200, 200);
assert.strictEqual(hit.symbol, "^N225");

// 5. Portfolio holdings go on inner ring and ring label becomes PORTFOLIO
labelsDrawn.length = 0;
state.setStocks([
  { symbol: "AAPL", market: "us", price: 180, changePercent: 1.0 },
  { symbol: "MSFT", market: "us", price: 420, changePercent: 1.0 },
  { symbol: "NVDA", market: "us", price: 120, changePercent: 2.0, portfolioWeight: 0.45 },
]);
const assignedPortfolio = renderer.assignOrbits(state.state.stockList, "AAPL");
assert.strictEqual(assignedPortfolio.find((s) => s.symbol === "NVDA").tier, "inner");
renderer.render(4000);
assert.ok(labelsDrawn.includes("PORTFOLIO"));
process.stdout.write("OK");
"""
    )
    assert output == "OK"


def test_uix006_gesture_pointerleave_clears_hover_and_unregisters():
    """GestureController resets hoveredSymbol & cursor on canvas pointerleave when not dragging, and cleans up."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const canvasListeners = new Map();
const windowListeners = new Map();
const canvas = {
  style: { cursor: "pointer" },
  getBoundingClientRect: () => ({ left: 0, top: 0 }),
  addEventListener(ev, fn) {
    canvasListeners.set(ev, fn);
  },
  removeEventListener(ev, fn) {
    if (canvasListeners.get(ev) === fn) canvasListeners.delete(ev);
  },
};

const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
  addEventListener(ev, fn) {
    windowListeners.set(ev, fn);
  },
  removeEventListener(ev, fn) {
    if (windowListeners.get(ev) === fn) windowListeners.delete(ev);
  },
};
window.window = window;

const ctx = {
  window,
  global: window,
  URLSearchParams,
  URL,
  Number,
  String,
  Math,
  Array,
  Map,
  Set,
  setTimeout,
  clearTimeout,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/gesture-controller.js", "utf8"), ctx);

const state = new window.ObservatoryState();
state.setHoveredSymbol("AAPL");
const renderer = { hitTest: () => null, centerX: 400, centerY: 300, triggerShockwave: () => {} };
const gc = new window.GestureController(canvas, state, renderer);

assert.ok(typeof canvasListeners.get("pointerleave") === "function");
// Not dragging -> clears hover & resets cursor
canvasListeners.get("pointerleave")();
assert.strictEqual(state.state.hoveredSymbol, null);
assert.strictEqual(canvas.style.cursor, "default");

// During active drag -> pointerleave leaves state undisturbed
state.setHoveredSymbol("AAPL");
gc.isDragging = true;
canvas.style.cursor = "grabbing";
canvasListeners.get("pointerleave")();
assert.strictEqual(state.state.hoveredSymbol, "AAPL");
assert.strictEqual(canvas.style.cursor, "grabbing");

gc.destroy();
assert.strictEqual(canvasListeners.has("pointerleave"), false);
process.stdout.write("OK");
"""
    )
    assert output == "OK"


def test_uix007_accessibility_modal_focus_trap_filters_hidden_and_redirects():
    """trapModalFocus filters hidden focusables and traps focus from modal root or outside into visible elements."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

let focused = null;
function makeBtn(name, opts = {}) {
  const classes = new Set(opts.hiddenClass ? ["hidden"] : []);
  const attrs = new Map();
  if (opts.hiddenAttr) attrs.set("hidden", "");
  if (opts.inert) attrs.set("inert", "");
  return {
    name,
    offsetParent: opts.offsetParent === null ? null : {},
    parentElement: opts.parentElement || null,
    style: opts.style || {},
    classList: { contains: (c) => classes.has(c) },
    hasAttribute: (k) => attrs.has(k),
    getAttribute: (k) => attrs.get(k) || null,
    focus() { focused = name; },
  };
}

const hiddenParent = {
  classList: { contains: () => false },
  hasAttribute: () => false,
  getAttribute: () => null,
  style: { display: "none" },
  parentElement: null,
};

const hiddenZero = makeBtn("hiddenZero", { offsetParent: null });
const visibleFirst = makeBtn("visibleFirst");
const hiddenChild = makeBtn("hiddenChild", { parentElement: hiddenParent });
const visibleLast = makeBtn("visibleLast");
const hiddenLast = makeBtn("hiddenLast", { hiddenClass: true });

const modal = {
  classList: { contains: () => false },
  getAttribute: () => "false",
  querySelectorAll: () => [hiddenZero, visibleFirst, hiddenChild, visibleLast, hiddenLast],
  contains: (el) => el === modal || [hiddenZero, visibleFirst, hiddenChild, visibleLast, hiddenLast].includes(el),
  focus() { focused = "modal"; },
};

const document = {
  body: { classList: { contains: () => false } },
  activeElement: modal,
  contains: () => true,
};
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
  addEventListener: () => {},
  removeEventListener: () => {},
  getComputedStyle: (el) => ({
    display: el.style?.display || "block",
    visibility: el.style?.visibility || "visible",
    position: el.style?.position || "static",
  }),
};
window.window = window;

const ctx = {
  window, global: window, document, URLSearchParams, URL,
  Number, String, Array, Map, Set,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/accessibility-controller.js", "utf8"), ctx);

const state = new window.ObservatoryState();
const ac = new window.AccessibilityController(state, { searchModal: modal });

// 1. Focus on modal root -> Tab redirects to visibleFirst; Shift+Tab redirects to visibleLast
let prevented = false;
ac.trapModalFocus({ shiftKey: false, preventDefault: () => { prevented = true; } }, modal);
assert.strictEqual(prevented, true);
assert.strictEqual(focused, "visibleFirst");

document.activeElement = { name: "outsideBody" };
ac.trapModalFocus({ shiftKey: true, preventDefault: () => {} }, modal);
assert.strictEqual(focused, "visibleLast");

// 2. Tab on visibleLast wraps to visibleFirst (ignoring hiddenLast)
document.activeElement = visibleLast;
ac.trapModalFocus({ shiftKey: false, preventDefault: () => {} }, modal);
assert.strictEqual(focused, "visibleFirst");
process.stdout.write("OK");
"""
    )
    assert output == "OK"


def test_uix008_template_placeholders_and_responsive_layout_css():
    """Template removes hardcoded AAPL/Mistral defaults; CSS uses flex shell and non-overlapping mobile stack."""
    html = (ROOT / "templates" / "experimental_orbit.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "experimental-orbit.css").read_text(encoding="utf-8")

    # Neutral defaults instead of hardcoded AAPL / Apple Inc. / Mistral AI
    overlay_section = html[html.index('id="ai-dive-overlay"') :]
    assert "Apple Inc." not in overlay_section
    assert ">AAPL<" not in overlay_section
    assert "Mistral AI" not in overlay_section
    assert 'id="ai-dive-provider-label"' in overlay_section
    assert 'class="observatory-hud-dock"' in html

    # Desktop flex layout prevents wrapped header clipping canvas or drawer
    assert "flex-direction: column;" in css
    assert "flex: 1 1 auto;\n  min-height: 0;" in css
    assert "top: 16px;" in css

    # Mobile HUD dock stacks cards vertically with gap rather than colliding absolute offsets
    mobile_section = css[css.index("@media (max-width: 768px)") :]
    assert ".observatory-hud-dock" in mobile_section
    assert "bottom: 120px;" not in mobile_section


def test_uix009_constellation_chart_finite_guards_and_safe_history_dates():
    """Relative return chart guards non-finite values & uses measureText; history dates avoid UTC shift."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const fillTextCalls = [];
const lineTos = [];
const mockCtx = {
  fillStyle: "",
  strokeStyle: "",
  lineWidth: 1,
  font: "",
  textAlign: "left",
  clearRect() {},
  setLineDash() {},
  beginPath() {},
  moveTo() {},
  lineTo(x, y) { lineTos.push({ x, y }); },
  stroke() {},
  fillText(text, x, y) { fillTextCalls.push({ text: String(text), x, y }); },
  measureText: (t) => ({ width: String(t).length * 9 }),
};

const classes = new Set();
const wrapper = {
  classList: {
    add: (c) => classes.add(c),
    remove: (c) => classes.delete(c),
    contains: (c) => classes.has(c),
  },
};
const canvas = { width: 280, height: 120, getContext: () => mockCtx };
const window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  history: { replaceState: () => {} },
};
window.window = window;

const ctx = {
  window, global: window,
  document: { body: { classList: { contains: () => false } } },
  console, URLSearchParams, URL, Number, String, Math, Array, Map, Set, Object, Date, isNaN,
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/constellation-controller.js", "utf8"), ctx);

// 1. Data adapter: preserve server date string & avoid UTC shift on numeric local timestamp & ignore NaN
const localMidnightTs = new Date(2026, 0, 15, 0, 15, 0).getTime();
const normalized = window.ObservatoryDataAdapter.normalizeHistory([
  { x: 1705276800000, date: "2026-01-14", close: 100 },
  { timestamp: localMidnightTs, close: 105 },
  { x: Number.NaN, date: "bad-date", close: 110 },
]);
assert.strictEqual(normalized[0].dateStr, "2026-01-14");
assert.strictEqual(normalized[1].dateStr, "2026-01-15");
assert.strictEqual(typeof normalized[2].dateStr, "string");
assert.ok(/^\d{4}-\d{2}-\d{2}$/.test(normalized[2].dateStr));

// Also market inference when options.market === "all"
const idxNorm = window.ObservatoryDataAdapter.normalizeStock({ symbol: "^N225", price: 39000 }, { market: "all" });
assert.strictEqual(idxNorm.market, "idx");

// 2. Chart handles NaN/0 gracefully and advances legend X dynamically via measureText
const state = new window.ObservatoryState();
state.setStocks([
  { symbol: "BAD", market: "us", chartData: [{ close: 0 }, { close: 10 }] },
  { symbol: "LONGNAME", market: "us", chartData: [{ close: 100 }, { close: Number.NaN }, { close: 110 }] },
  { symbol: "SECOND", market: "us", chartData: [{ close: 50 }, { close: 55 }] },
]);
const ctrl = new window.ConstellationController(state, {
  chartCanvas: canvas,
  chartWrapper: wrapper,
});
ctrl.renderRelativeReturnChart(["BAD", "LONGNAME", "SECOND"], state.state);
assert.strictEqual(wrapper.classList.contains("hidden"), false);
assert.ok(lineTos.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y)), "no NaN coordinates drawn");
assert.strictEqual(fillTextCalls.length, 2);
// "LONGNAME" measured width = 8 * 9 = 72 -> next legendX >= 8 + 72 + 12 = 92 (previous buggy code used 8 + 55 = 63!)
assert.ok(fillTextCalls[1].x >= 92, `expected dynamic legend spacing >= 92, got ${fillTextCalls[1].x}`);
process.stdout.write("OK");
"""
    )
    assert output == "OK"


def test_ext001_ext004_ext005_ext006_background_lifecycle_and_fallbacks():
    """background.js clears stale shutdown token, retries once on 403, short-circuits deterministic host errors, hydrates backendPort, protects badge races, and falls back to popup."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const nativeCalls = [];
const badgeHistory = [];
let popupConfig = null;
let storageDelayDone = false;
let shutdownAttempt = 0;
let tokenCalls = 0;
let nativeMode = "ok";

function makeCtx({ withSidePanel = true } = {}) {
  nativeCalls.length = 0;
  badgeHistory.length = 0;
  popupConfig = null;
  storageDelayDone = false;
  shutdownAttempt = 0;
  tokenCalls = 0;
  const listeners = [];

  const chrome = {
    runtime: {
      id: "ext-test-id",
      lastError: null,
      sendNativeMessage(_host, msg, cb) {
        nativeCalls.push(msg);
        if (nativeMode === "not-found") {
          chrome.runtime.lastError = { message: "Specified native messaging host not found." };
          cb(undefined);
          chrome.runtime.lastError = null;
          return;
        }
        if (msg.action === "get_backend_port") return cb({ ok: true, port: 5123 });
        if (msg.action === "start_backend") return cb({ ok: true, port: 5123 });
        if (msg.action === "get_shutdown_token") {
          tokenCalls += 1;
          if (nativeMode === "rate-limit" && tokenCalls >= 2) {
            return cb({ ok: false, error: "Rate limit exceeded" });
          }
          return cb({ ok: true, token: `tok-${tokenCalls}` });
        }
        cb({ ok: false });
      },
      onInstalled: { addListener() {} },
      onMessage: { addListener: (fn) => listeners.push(fn) },
    },
    storage: {
      local: {
        get(_keys, cb) {
          // Simulate delayed storage.local hydration returning custom port 5123
          setTimeout(() => {
            storageDelayDone = true;
            cb({ backendPort: 5123 });
          }, 25);
        },
        set() {},
        remove() {},
      },
      session: {
        get: (_k, cb) => cb({}),
        set: () => Promise.resolve(),
        remove: () => Promise.resolve(),
      },
    },
    action: {
      setBadgeText: ({ text }) => badgeHistory.push(text),
      setBadgeBackgroundColor: () => {},
      setPopup: (cfg) => { popupConfig = cfg; },
    },
    tabs: { create: async (args) => args },
    contextMenus: { removeAll: () => {}, create: () => {}, onClicked: { addListener: () => {} } },
    alarms: { get: (_n, cb) => cb({}), create: () => {}, onAlarm: { addListener: () => {} } },
    notifications: { create: () => {} },
  };
  if (withSidePanel) {
    chrome.sidePanel = {
      setPanelBehavior: () => Promise.resolve(),
      open: () => Promise.resolve(),
    };
  }

  const fetchMock = async (url, init = {}) => {
    const u = new URL(url);
    assert.strictEqual(storageDelayDone, true, `fetch ${url} ran before backendPortHydrated resolved!`);
    assert.strictEqual(u.port, "5123", `expected hydrated port 5123 in ${url}`);
    if (u.pathname === "/api/health") {
      return { ok: true, status: 200, json: async () => ({ ok: true, app: "Mistral NeX Stocks", ready: true, model: "m" }) };
    }
    if (u.pathname === "/api/indices") {
      return { ok: true, status: 200, json: async () => ({ N225: { percent: 2.0 } }) };
    }
    if (u.pathname === "/api/shutdown") {
      shutdownAttempt += 1;
      if (init.headers["X-MNS-Shutdown-Token"] === "tok-1") {
        return {
          ok: false,
          status: 403,
          json: async () => ({ ok: false, error: "HTTP 403", details: { reason: "invalid shutdown request" } }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    return { ok: false, status: 404, json: async () => ({ ok: false }) };
  };

  const context = {
    chrome,
    fetch: fetchMock,
    navigator: { userAgent: "Chrome/" },
    URL,
    AbortController,
    setTimeout,
    clearTimeout,
    console,
    Promise,
    Number,
    String,
    Set,
    Math,
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("chrome_extension/background.js", "utf8"), context);

  const invoke = (message) =>
    new Promise((resolve) => {
      listeners[0](message, { id: "ext-test-id" }, resolve);
    });
  return { context, invoke };
}

(async () => {
  // 1. Without sidePanel -> dynamically sets action popup fallback
  makeCtx({ withSidePanel: false });
  assert.strictEqual(popupConfig && popupConfig.popup, "popup.html");

  // 2. With sidePanel: no popup fallback, waits for backendPortHydrated, protects badge races, refresh/retry shutdown token
  const { context, invoke } = makeCtx({ withSidePanel: true });
  assert.strictEqual(popupConfig, null);

  // Initial updateBadge() awaits backendPortHydrated (25ms delayed port 5123) and writes "+2%"
  await new Promise((r) => setTimeout(r, 45));
  assert.strictEqual(storageDelayDone, true);
  assert.strictEqual(badgeHistory[badgeHistory.length - 1], "+2%");

  // Trigger a short transient badge, then let updateBadge commit "+2%" before clear timer fires
  context.setBadgeMessage("✓", "#10b981", 40);
  await context.updateBadge();
  // Wait longer than 40ms clear timer -> "+2%" must NOT be wiped by expired transient badge timer!
  await new Promise((r) => setTimeout(r, 70));
  assert.strictEqual(badgeHistory[badgeHistory.length - 1], "+2%");

  // getContext must NOT perform extra redundant get_backend_port beyond checkHealth's calls
  nativeCalls.length = 0;
  const ctxRes = await invoke({ action: "getContext" });
  assert.strictEqual(ctxRes.ok, true);
  assert.strictEqual(ctxRes.backendPort, 5123);
  const portQueries = nativeCalls.filter((c) => c.action === "get_backend_port").length;
  assert.strictEqual(portQueries, 1, `expected 1 get_backend_port call in getContext, got ${portQueries}`);

  // stopBackend: 403 -> clears & refreshes token once -> retries once -> succeeds
  const stopRes = await invoke({ action: "stopBackend" });
  assert.strictEqual(stopRes.ok, true);
  assert.strictEqual(tokenCalls, 2);
  assert.strictEqual(shutdownAttempt, 2);

  // startBackend clears any cached shutdown token
  context.setMnsShutdownToken("stale-leftover");
  await invoke({ action: "startBackend" });
  // Trigger stopBackend with rate-limit on refresh -> aborts gracefully without infinite retries
  nativeMode = "rate-limit";
  tokenCalls = 0;
  const rlRes = await invoke({ action: "stopBackend" });
  assert.strictEqual(rlRes.ok, false);
  assert.ok(rlRes.error.includes("Rate limit"));

  // sendNativeMessage short-circuits deterministic host error with ZERO retries (1 call only)
  nativeMode = "not-found";
  nativeCalls.length = 0;
  await assert.rejects(() => context.sendNativeMessage({ action: "start_backend" }, 3), /not found/i);
  assert.strictEqual(nativeCalls.length, 1);

  process.stdout.write("OK");
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
"""
    )
    assert output == "OK"


def test_ext002_content_ticker_detection_and_idempotency():
    """content.js guards duplicate injection, uses closest(), drops $1000, supports BRK.B/1-char/130A.T, and keys by market:symbol."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const source = fs.readFileSync("chrome_extension/content.js", "utf8");
const listeners = [];

const makeTextNode = (text, ancestorTags = [], closestMatch = false) => ({
  nodeValue: text,
  parentElement: {
    tagName: ancestorTags[0] || "P",
    isContentEditable: false,
    closest(selector) {
      const selectors = selector.split(",").map((s) => s.trim().toUpperCase());
      if (closestMatch) return { tagName: "CODE" };
      return ancestorTags.some((t) => selectors.includes(t.toUpperCase()))
        ? { tagName: ancestorTags[0] }
        : null;
    },
  },
});

const nodes = [
  makeTextNode("$1000 profit and $AAPL news", ["P"]),
  makeTextNode("Nested span inside code should be ignored $NVDA", ["SPAN"], true),
  makeTextNode("Blue chips BRK.B, V, A, T, F, MSFT and 130A.T plus 東証 (7203) 株価", ["DIV"]),
];

let walkerIdx = -1;
const NodeFilter = { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1, FILTER_SKIP: 3 };
const window = { location: { href: "https://example.com" } };
const document = {
  title: "Test",
  body: {},
  createTreeWalker(_root, _what, filter) {
    walkerIdx = -1;
    return {
      nextNode() {
        while (++walkerIdx < nodes.length) {
          const n = nodes[walkerIdx];
          if (filter.acceptNode(n) === NodeFilter.FILTER_ACCEPT) return n;
        }
        return null;
      },
    };
  },
};

const ctx = {
  window,
  document,
  NodeFilter,
  chrome: {
    runtime: {
      id: "ext-id",
      onMessage: { addListener: (fn) => listeners.push(fn) },
    },
  },
  console,
  Set,
  Map,
  RegExp,
  String,
  Array,
  Math,
};
vm.createContext(ctx);
// Run content.js TWICE -> idempotency guard ensures listener only registered once!
vm.runInContext(source, ctx);
vm.runInContext(source, ctx);
assert.strictEqual(listeners.length, 1);

let reply = null;
listeners[0]({ action: "detectTickers" }, { id: "ext-id" }, (r) => { reply = r; });
assert.strictEqual(reply.ok, true);

const byKey = new Map(reply.tickers.map((t) => [`${t.market}:${t.symbol}`, t]));
// $1000 must NEVER become jp:1000.T
assert.strictEqual(byKey.has("jp:1000.T"), false);
// Ignored code child $NVDA must NOT appear
assert.strictEqual(byKey.has("us:NVDA"), false);
// $AAPL must NOT be double counted
assert.strictEqual(byKey.get("us:AAPL").count, 1);
// Dotted & 1-letter known tickers detected
assert.ok(byKey.has("us:BRK.B"));
assert.ok(byKey.has("us:V"));
assert.ok(byKey.has("us:A"));
assert.ok(byKey.has("us:T"));
assert.ok(byKey.has("us:F"));
// TSE alphanumeric 130A.T and numeric 7203.T detected without triggering stray us:T
assert.ok(byKey.has("jp:130A.T"));
assert.ok(byKey.has("jp:7203.T"));

// Verify "7203.T 130A.T" alone does NOT match US ticker "T"
nodes.length = 0;
nodes.push(makeTextNode("Watching 7203.T and 130A.T today", ["P"]));
listeners[0]({ action: "detectTickers" }, { id: "ext-id" }, (r) => { reply = r; });
const isolatedKeys = new Set(reply.tickers.map((t) => `${t.market}:${t.symbol}`));
assert.strictEqual(isolatedKeys.has("us:T"), false);
assert.strictEqual(isolatedKeys.has("jp:7203.T"), true);
assert.strictEqual(isolatedKeys.has("jp:130A.T"), true);

process.stdout.write("OK");
"""
    )
    assert output == "OK"


def test_ext003_ext004_ext006_popup_poll_abort_offline_launchers_inline_error_and_currency():
    """popup.js aborts stock poll on stop, clears offline list, routes launchers via background, renders JPY/USD cleanly, and shows inline detector errors."""
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeDomNode(id = "", tag = "div") {
  const classes = new Set();
  const attrs = new Map([["id", id]]);
  const listeners = new Map();
  const node = {
    id,
    tagName: tag.toUpperCase(),
    className: "",
    get textContent() { return node._text; },
    set textContent(v) {
      node._text = String(v);
      if (node._text === "") node.children = [];
    },
    _text: "",
    value: "",
    disabled: false,
    title: "",
    scrollTop: 0,
    dataset: {},
    children: [],
    get childNodes() { return node.children; },
    classList: {
      add: (...n) => n.forEach((x) => classes.add(x)),
      remove: (...n) => n.forEach((x) => classes.delete(x)),
      toggle: (x, f) => (f ? classes.add(x) : classes.delete(x)),
      contains: (x) => classes.has(x),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: (k) => attrs.get(k) || null,
    appendChild(c) { node.children.push(c); return c; },
    querySelector: () => null,
    querySelectorAll: () => [],
    contains: () => false,
    addEventListener(ev, fn) {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    click() {
      for (const fn of listeners.get("click") || []) fn({ stopPropagation() {}, preventDefault() {} });
    },
  };
  return node;
}

const byId = new Map();
for (const id of [
  "healthPill", "healthMeta", "browserPill", "diagBox",
  "startBtn", "stopBtn", "refreshBtn",
  "stockListContainer", "stockRefreshTime", "extStockFilter",
  "openMainBtn", "openScreenerBtn", "openHeatmapBtn", "openOrbitBtn", "openSetupBtn", "openSettingsBtn",
  "detectedListContainer", "detectedPageTitle", "rescanDetectorBtn", "copyDiagBtn",
]) {
  byId.set(id, makeDomNode(id));
}

const sentMessages = [];
let pendingFetches = [];
let healthSequence = [];
let domReady = null;
let fakePage = { id: 1, url: "https://finance.example.com", title: "Market News" };

const context = {
  document: {
    activeElement: null,
    getElementById: (id) => byId.get(id) || null,
    querySelectorAll: () => [],
    createElement: (t) => makeDomNode("", t),
    createDocumentFragment: () => makeDomNode("", "fragment"),
    addEventListener: (ev, fn) => { if (ev === "DOMContentLoaded") domReady = fn; },
    removeEventListener: () => {},
  },
  window: {
    CSS: { escape: (s) => s },
    addEventListener: () => {},
    removeEventListener: () => {},
  },
  chrome: {
    storage: { local: { get: (_k, cb) => cb({ backendPort: 5000 }) } },
    runtime: {
      async sendMessage(msg) {
        sentMessages.push(msg);
        if (msg.action === "health") {
          return healthSequence.length ? healthSequence.shift() : { ok: false };
        }
        if (msg.action === "getContext") {
          return {
            ok: true,
            browserName: "Chrome",
            extensionId: "id",
            hostName: "host",
            backendUrls: ["http://127.0.0.1:5000"],
            health: { ok: true, base: "http://127.0.0.1:5000", data: { model: "m" } },
          };
        }
        if (msg.action === "addDetectedStock") {
          return { ok: false, error: "追加権限エラーテスト" };
        }
        if (msg.action.startsWith("open")) {
          return { ok: true };
        }
        return { ok: true };
      },
    },
    tabs: {
      query: async () => [fakePage],
      sendMessage: async () => ({
        ok: true,
        tickers: [{ symbol: "AAPL", market: "us", count: 3, snippet: "AAPL" }],
      }),
      create: () => { throw new Error("direct chrome.tabs.create must not be used for app launcher routes"); },
    },
  },
  fetch(_url, opts = {}) {
    return new Promise((resolve, reject) => {
      pendingFetches.push({ opts, resolve, reject });
    });
  },
  CSS: { escape: (s) => s },
  addEventListener: () => {},
  removeEventListener: () => {},
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  AbortController,
  navigator: { clipboard: { writeText: async () => {} } },
  console,
  Number, String, Math, Array, Map, Set, Promise, Date, Object, Error, TypeError,
  parseFloat, isNaN, encodeURIComponent,
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(fs.readFileSync("chrome_extension/popup.js", "utf8"), context);

(async () => {
  domReady();
  await new Promise((r) => setTimeout(r, 20));

  // 1. Currency formatting check
  const jpNode = context.renderStockItem("7203.T", "トヨタ", 2500.4, 1.2, "jp");
  const usNode = context.renderStockItem("AAPL", "Apple", 180.5, -0.5, "us");
  const usdJpyNode = context.renderStockItem("USDJPY=X", "米ドル/円", 154.25, 0.1, "idx");
  const n225Node = context.renderStockItem("^N225", "日経平均", 38500, 0.4, "idx");
  const nodePriceText = (el) => el.children[1].children[0].textContent;
  assert.strictEqual(nodePriceText(jpNode), "¥2,500");
  assert.strictEqual(nodePriceText(usNode), "$180.50");
  assert.strictEqual(nodePriceText(usdJpyNode), "¥154.25");
  assert.strictEqual(nodePriceText(n225Node), "38,500.00");

  // 2. startStockPolling supersedes bootstrap poll and stopStockPolling aborts pending fetch without triggering error text
  assert.strictEqual(pendingFetches.length, 1);
  const bootstrapFetch = pendingFetches[0];
  context.startStockPolling("http://127.0.0.1:5000");
  assert.strictEqual(bootstrapFetch.opts.signal.aborted, true);
  assert.strictEqual(pendingFetches.length, 2);
  const firstFetch = pendingFetches[1];
  assert.strictEqual(firstFetch.opts.signal.aborted, false);
  byId.get("stockRefreshTime").textContent = "12:00:00";

  context.stopStockPolling();
  assert.strictEqual(firstFetch.opts.signal.aborted, true);
  // Even if mock rejected with generic Error instead of AbortError, stale generation suppresses error overwrite
  firstFetch.reject(new Error("aborted"));
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(byId.get("stockRefreshTime").textContent, "12:00:00");

  // 3. setHealth({ ok: false }) clears stockListContainer, sets stale & offline status
  context.setHealth({ ok: false });
  assert.strictEqual(byId.get("stockListContainer").classList.contains("stale"), true);
  assert.strictEqual(byId.get("stockListContainer").children.length, 1);
  assert.ok(byId.get("stockListContainer").children[0].textContent.includes("バックエンド未起動"));
  assert.ok(byId.get("stockRefreshTime").textContent.includes("オフライン"));
  assert.strictEqual(vm.runInContext("allStocksData", context), null);

  // 4. Launcher buttons route through background.js rather than calling chrome.tabs.create directly
  sentMessages.length = 0;
  byId.get("openMainBtn").click();
  byId.get("openOrbitBtn").click();
  await context.openAppPage("/custom");
  await new Promise((r) => setTimeout(r, 10));
  assert.ok(sentMessages.some((m) => m.action === "openMain"));
  assert.ok(sentMessages.some((m) => m.action === "openOrbit"));
  assert.ok(sentMessages.some((m) => m.action === "openRoute" && m.route === "/custom"));

  // 5. Detector add failure displays inline card error and NEVER overwrites #healthPill
  byId.get("healthPill").textContent = "起動済み";
  byId.get("healthMeta").textContent = "http://127.0.0.1:5000";
  await context.loadDetectedTickers();
  const card = byId.get("detectedListContainer").children[0];
  const addBtn = card.children[0].children[1];
  addBtn.click();
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(byId.get("healthPill").textContent, "起動済み");
  assert.strictEqual(byId.get("healthMeta").textContent, "http://127.0.0.1:5000");
  const inlineErr = card.children.find((c) => c.className === "detector-inline-error");
  assert.ok(inlineErr && inlineErr.textContent.includes("追加権限エラーテスト"));

  // 6. stopBtn polls /api/health until backend reports down
  sentMessages.length = 0;
  healthSequence = [{ ok: true }, { ok: false }];
  byId.get("stopBtn").click();
  await new Promise((r) => setTimeout(r, 400));
  const healthPolls = sentMessages.filter((m) => m.action === "health").length;
  assert.ok(healthPolls >= 2, `expected >= 2 health down-polls, got ${healthPolls}`);

  process.stdout.write("OK");
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
"""
    )
    assert output == "OK"


def test_ext006_manifest_keeps_sidepanel_without_static_default_popup():
    """MV3 manifest retains side_panel without static default_popup so action.onClicked / setPanelBehavior stay active."""
    manifest = json.loads((ROOT / "chrome_extension" / "manifest.json").read_text(encoding="utf-8"))
    assert "default_popup" not in manifest.get("action", {})
    assert manifest.get("side_panel", {}).get("default_path") == "popup.html"
