"""Regression tests for comprehensive code review Unit 4 (ISS-13..ISS-18)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend VM tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert result.stdout.strip().endswith("ok")


def test_iss13_14_chart_stale_empty_prefetch_and_heikin_ashi_live_tick() -> None:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeEl(tag, cls, text) {
  const classes = new Set((cls || "").split(/\s+/).filter(Boolean));
  const attrs = {};
  const listeners = {};
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    dataset: {},
    style: { setProperty() {}, removeProperty() {} },
    hidden: false,
    textContent: String(text || ""),
    classList: {
      add(...items) { items.forEach((c) => classes.add(c)); },
      remove(...items) { items.forEach((c) => classes.delete(c)); },
      contains(c) { return classes.has(c); },
      toggle(c, force) {
        const next = force === undefined ? !classes.has(c) : Boolean(force);
        if (next) classes.add(c); else classes.delete(c);
        return next;
      },
    },
    get className() { return Array.from(classes).join(" "); },
    set className(val) {
      classes.clear();
      String(val || "").split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    replaceChildren(...nodes) { this.children = []; nodes.forEach((n) => this.appendChild(n)); },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    removeAttribute(k) { delete attrs[k]; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() {},
    querySelector(sel) {
      for (const ch of this.children) {
        if (sel.startsWith(".") && ch.classList.contains(sel.slice(1))) return ch;
        const deep = ch.querySelector(sel);
        if (deep) return deep;
      }
      return null;
    },
    querySelectorAll() { return []; },
    closest() { return null; },
    getContext() {
      return {
        createLinearGradient: () => ({ addColorStop() {} }),
      };
    },
  };
  return el;
}

const createdCharts = [];
function FakeChart(canvas, config) {
  this.canvas = canvas;
  this.data = config.data;
  this.options = config.options;
  this.config = config;
  this.updateCalls = [];
  this.update = (mode) => { this.updateCalls.push(mode); };
  this.destroy = () => {};
  this.resize = () => {};
  createdCharts.push(this);
}
FakeChart.register = () => {};

const storage = new Map();
const doc = {
  readyState: "complete",
  hidden: false,
  body: makeEl("body"),
  addEventListener() {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeEl(tag),
  createTextNode: (t) => makeEl("#text", "", t),
};
const win = {
  location: { pathname: "/", search: "" },
  APP_CONFIG: {},
  addEventListener() {},
  requestAnimationFrame(cb) { cb(); return 1; },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
};
const ctx = {
  console,
  document: doc,
  window: win,
  APP_CONFIG: win.APP_CONFIG,
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  navigator: {},
  URLSearchParams,
  AbortController,
  Chart: FakeChart,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  DOM: { get: () => null },
  fetch: async () => ({ ok: true, json: async () => ({ stale: true, history: [] }) }),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/utils.js", "utf8"), ctx, { filename: "utils.js" });
vm.runInContext(fs.readFileSync("static/js/state.js", "utf8"), ctx, { filename: "state.js" });
ctx.window.formatPtsPriceText = ctx.formatPtsPriceText;
vm.runInContext(fs.readFileSync("static/js/ui.js", "utf8"), ctx, { filename: "ui.js" });
vm.runInContext(fs.readFileSync("static/js/chart.js", "utf8"), ctx, { filename: "chart.js" });
ctx.window.getCurrencySymbol = ctx.getCurrencySymbol;

let asyncDone = false;
process.on("exit", (code) => {
  if (code === 0 && !asyncDone) {
    console.error("Async checks did not finish");
    process.exit(1);
  }
});

(async () => {
  // ISS-13a: { stale: true, history: [] } must reject instead of returning empty arrays
  ctx.apiFetch = async () => ({ data: { stale: true, history: [] } });
  let staleEmptyErr = null;
  try {
    await ctx.fetchStockHistoryPayload("AAPL", "us", "3mo", "1d");
  } catch (err) {
    staleEmptyErr = err;
  }
  assert.ok(
    staleEmptyErr && /表示可能なヒストリカルデータがありません/.test(String(staleEmptyErr.message || "")),
    "expected error for stale+empty history response, got: " + String(staleEmptyErr),
  );

  // ISS-13b: { stale: true, history: [valid points...] } still resolves cleanly
  ctx.apiFetch = async () => ({
    data: {
      stale: true,
      history: [
        { date: "2026-01-01", o: 100, h: 105, l: 95, c: 102, price: 102, v: 10 },
        { date: "2026-01-02", o: 102, h: 108, l: 101, c: 106, price: 106, v: 12 },
      ],
    },
  });
  const populated = await ctx.fetchStockHistoryPayload("AAPL", "us", "3mo", "1d");
  assert.strictEqual(populated.ohlcData.length, 2);

  // ISS-13c: _storePrefetchedHistory skips empty arrays and stores non-empty arrays
  const prefetchCache = vm.runInContext("historyPrefetchCache", ctx);
  prefetchCache.clear();
  ctx._storePrefetchedHistory("empty_key", [], []);
  assert.strictEqual(prefetchCache.has("empty_key"), false);
  ctx._storePrefetchedHistory("valid_key", populated.formattedData, populated.ohlcData);
  assert.strictEqual(prefetchCache.has("valid_key"), true);

  // ISS-14: drawChart (heikin_ashi) + live tick recalculation in updateChartLastPoint
  ctx.setChartPref("us:AAPL", "type", "heikin_ashi");
  const wrapper = makeEl("div", "stock-wrapper");
  wrapper.dataset.stockKey = "us:AAPL";
  wrapper.dataset.market = "us";
  wrapper.__stockData = { symbol: "AAPL", market: "us", currency: "USD" };
  const canvas = makeEl("canvas", "chart-canvas");
  wrapper.appendChild(canvas);

  const rawBars = [
    { x: Date.parse("2026-01-01"), o: 100, h: 106, l: 98, c: 104, v: 100 },
    { x: Date.parse("2026-01-02"), o: 104, h: 110, l: 102, c: 108, v: 120 },
    { x: Date.parse("2026-01-03"), o: 108, h: 112, l: 105, c: 110, v: 140 },
  ];
  ctx.drawChart(
    wrapper,
    rawBars.map((b) => ({ x: b.x, y: b.c, v: b.v })),
    rawBars,
    { period: "3mo", interval: "1d", animate: false },
  );
  const haChart = createdCharts[createdCharts.length - 1];
  assert.strictEqual(haChart.$chartType, "heikin_ashi");
  assert.ok(haChart.$rawLastCandle && haChart.$prevHeikinAshi);

  ctx.updateChartLastPoint(haChart, 120);
  const updatedBars = [
    rawBars[0],
    rawBars[1],
    { x: rawBars[2].x, o: 108, h: 120, l: 105, c: 120, v: 140 },
  ];
  const expectedHa = ctx.calculateHeikinAshi(updatedBars).at(-1);
  const actualLast = haChart.data.datasets[0].data.at(-1);
  assert.ok(Math.abs(actualLast.o - expectedHa.o) < 1e-9, "Heikin Ashi open mismatch");
  assert.ok(Math.abs(actualLast.h - expectedHa.h) < 1e-9, "Heikin Ashi high mismatch");
  assert.ok(Math.abs(actualLast.l - expectedHa.l) < 1e-9, "Heikin Ashi low mismatch");
  assert.ok(Math.abs(actualLast.c - expectedHa.c) < 1e-9, "Heikin Ashi close mismatch");
  assert.ok(haChart.updateCalls.includes("none"));

  // Invalid prices must not touch Heikin Ashi chart
  const snapshotBeforeInvalid = JSON.parse(JSON.stringify(actualLast));
  ctx.updateChartLastPoint(haChart, NaN);
  ctx.updateChartLastPoint(haChart, -10);
  ctx.updateChartLastPoint(haChart, 0);
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(haChart.data.datasets[0].data.at(-1))),
    snapshotBeforeInvalid,
  );

  asyncDone = true;
  process.stdout.write("ok");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
"""
    _run_node(script)


def test_iss15_realtime_client_immutability_and_invalid_price_guard() -> None:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeEl(cls, text = "") {
  const classes = new Set((cls || "").split(/\s+/).filter(Boolean));
  const attrs = {};
  const el = {
    children: [],
    dataset: {},
    hidden: false,
    isConnected: true,
    textContent: text,
    classList: {
      add(...items) { items.forEach((c) => classes.add(c)); },
      remove(...items) { items.forEach((c) => classes.delete(c)); },
      contains(c) { return classes.has(c); },
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    removeAttribute(k) { delete attrs[k]; },
    querySelector(sel) {
      if (sel === ".compact-price") return priceEl;
      if (sel === ".compact-pts") return ptsEl;
      return null;
    },
    querySelectorAll: () => [],
    closest(sel) { return sel === ".stock-wrapper" ? wrapper : null; },
  };
  return el;
}

const priceEl = makeEl("compact-price", "$100.00");
const ptsEl = makeEl("compact-pts", "PTS ¥2,500");
const wrapper = makeEl("stock-wrapper");
wrapper.dataset = { stockSymbol: "AAPL", stockKey: "us:AAPL", bareSymbol: "AAPL" };
wrapper.__stockData = { symbol: "AAPL", market: "us", price: 100, pts_price: 2500 };

const doc = {
  readyState: "complete",
  hidden: true,
  addEventListener() {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: (sel) => {
    if (sel.includes(".compact-pts")) return [ptsEl];
    if (sel.includes(".stock-wrapper")) return [wrapper];
    return [];
  },
};
const win = {
  state: {
    stocks: {
      us: [{ symbol: "AAPL", market: "us", price: 100, previous_close: 95 }],
      jp: [{ symbol: "7203.T", market: "jp", price: 2500, pts_price: 2520 }],
      idx: [],
    },
  },
  addEventListener() {},
  requestAnimationFrame: () => 1,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  formatPrice: (v, stock) => `${stock?.market === "jp" ? "¥" : "$"}${v}`,
  formatPtsPriceText: (v, stock) =>
    typeof v === "number" && Number.isFinite(v) && v > 0
      ? `PTS ${stock?.market === "jp" ? "¥" : "$"}${v}`
      : "",
};
const ctx = {
  console,
  document: doc,
  window: win,
  setTimeout,
  clearTimeout,
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/realtime_client.js", "utf8"), ctx, {
  filename: "realtime_client.js",
});

const rc = win.realtimeClient;
win.updateExistingCard = (w, stockData) => {
  w.__stockData = stockData;
};
// 1. Valid delta updates state while preserving pinned market-aware change rounding on payload
rc._applyDeltasNow({ AAPL: { price: 105.1234, change: 10.12346, market: "us" } });
assert.strictEqual(win.state.stocks.us[0].price, 105.1234);
assert.strictEqual(wrapper.__stockData.price, 105.1234);
assert.strictEqual(wrapper.__stockData.change, 10.1234);
const barePayload = { "7203.T": { price: 2550, change: 12.3456 } };
rc._getElements = () => ({ bareSymbol: "7203.T", cleanCode: "7203", wrappers: [], ptsElements: [] });
rc._applyDeltasNow(barePayload);
assert.strictEqual(barePayload["7203.T"].change, 12.35);

// Restore default wrapper lookup and verify invalid regular prices/changes never poison state or wrapper
delete rc._getElements;
const savedChange = wrapper.__stockData.change;
for (const bad of [NaN, Infinity, -5, 0, true, false, "not-a-number"]) {
  rc._applyDeltasNow({ AAPL: { price: bad, change: NaN, change_percent: Infinity, market: "us" } });
  assert.strictEqual(win.state.stocks.us[0].price, 105.1234, `bad price corrupted state: ${String(bad)}`);
  assert.strictEqual(wrapper.__stockData.price, 105.1234, `bad price corrupted wrapper: ${String(bad)}`);
  assert.strictEqual(wrapper.__stockData.change, savedChange, "NaN change must not overwrite wrapper");
}

// 3. Invalid non-clear PTS prices keep valid cached pts_price; explicit null clears
for (const badPts of [NaN, Infinity, -10, true, "bad"]) {
  rc._applyPtsDeltasNow({ AAPL: { price: badPts, market: "us" } });
  assert.strictEqual(wrapper.__stockData.pts_price, 2500);
}
rc._applyPtsDeltasNow({ AAPL: { price: null, market: "us" } });
assert.strictEqual(wrapper.__stockData.pts_price, null);
assert.strictEqual(ptsEl.hidden, true);
assert.strictEqual(ptsEl.getAttribute("aria-hidden"), "true");

process.stdout.write("ok");
"""
    _run_node(script)


def test_iss16_portfolio_info_elements_currency_formatting() -> None:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeEl(tag, cls) {
  const classes = new Set((cls || "").split(/\s+/).filter(Boolean));
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    dataset: {},
    classList: {
      add(...items) { items.forEach((c) => classes.add(c)); },
      remove(...items) { items.forEach((c) => classes.delete(c)); },
      contains(c) { return classes.has(c); },
    },
    get className() { return Array.from(classes).join(" "); },
    set className(val) {
      classes.clear();
      String(val || "").split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
    _text: "",
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v || ""); this.children = []; },
    appendChild(child) { this.children.push(child); return child; },
    querySelector(sel) {
      for (const ch of this.children) {
        if (sel.startsWith(".") && ch.classList.contains(sel.slice(1))) return ch;
        const deep = ch.querySelector(sel);
        if (deep) return deep;
      }
      return null;
    },
  };
  return el;
}

const doc = {
  readyState: "complete",
  addEventListener() {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeEl(tag),
  createTextNode: (t) => ({ textContent: String(t) }),
};
const ctx = {
  console,
  document: doc,
  window: { location: { pathname: "/", search: "" }, addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: {},
  URLSearchParams,
  AbortController,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  DOM: { get: () => null },
  state: { stocks: { us: [], jp: [], idx: [] }, chartPrefs: {} },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/utils.js", "utf8"), ctx, { filename: "utils.js" });
vm.runInContext(fs.readFileSync("static/js/ui.js", "utf8"), ctx, { filename: "ui.js" });

function makePortfolioCardShell() {
  const wrapper = makeEl("div", "stock-wrapper");
  const compactPf = makeEl("div", "compact-pf-info");
  const pfBlock = makeEl("div", "pf-detail-block");
  const pfShares = makeEl("strong", "pf-shares");
  const pfAvg = makeEl("strong", "pf-avgprice");
  const pfVal = makeEl("strong", "pf-value");
  const pfPl = makeEl("strong", "pf-pl");
  pfBlock.appendChild(pfShares);
  pfBlock.appendChild(pfAvg);
  pfBlock.appendChild(pfVal);
  pfBlock.appendChild(pfPl);
  wrapper.appendChild(compactPf);
  wrapper.appendChild(pfBlock);
  return { wrapper, compactPf, pfAvg, pfVal, pfPl };
}

// US holding with gain and loss (sign precedes currency symbol)
const usCard = makePortfolioCardShell();
ctx.updatePortfolioInfoElements(usCard.wrapper, {
  symbol: "AAPL",
  market: "us",
  currency: "USD",
  shares: 10,
  avg_price: 100,
  price: 110,
});
assert.strictEqual(usCard.compactPf.children[0].textContent, "+$100.00");
assert.strictEqual(usCard.pfAvg.textContent, "$100.00");
assert.strictEqual(usCard.pfVal.textContent, "$1,100.00");
assert.ok(usCard.pfPl.textContent.startsWith("+$100.00 (+10.00%)"));

ctx.updatePortfolioInfoElements(usCard.wrapper, {
  symbol: "AAPL",
  market: "us",
  currency: "USD",
  shares: 10,
  avg_price: 100,
  price: 90,
});
assert.strictEqual(usCard.compactPf.children[0].textContent, "-$100.00");
assert.ok(usCard.pfPl.textContent.startsWith("-$100.00 (-10.00%)"));

// JP holding inferred from market/symbol renders integer-rounded ¥
const jpCard = makePortfolioCardShell();
ctx.updatePortfolioInfoElements(jpCard.wrapper, {
  symbol: "7203.T",
  market: "jp",
  shares: 100,
  avg_price: 2000,
  price: 1900,
});
assert.strictEqual(jpCard.compactPf.children[0].textContent, "-¥10,000");
assert.strictEqual(jpCard.pfAvg.textContent, "¥2,000");
assert.strictEqual(jpCard.pfVal.textContent, "¥190,000");
assert.ok(jpCard.pfPl.textContent.startsWith("-¥10,000 (-5.00%)"));

process.stdout.write("ok");
"""
    _run_node(script)


def test_iss17_orbit_renderer_evicts_stale_nodes_when_assigned_empty() -> None:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const ctx = {
  console,
  performance: { now: () => 0 },
  document: undefined,
  window: { innerWidth: 800, innerHeight: 600, devicePixelRatio: 1, addEventListener() {} },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/orbit-renderer.js", "utf8"), ctx, {
  filename: "orbit-renderer.js",
});

const noopCtx = {
  save() {},
  restore() {},
  beginPath() {},
  arc() {},
  stroke() {},
  fill() {},
  fillText() {},
  createRadialGradient: () => ({ addColorStop() {} }),
  setLineDash() {},
};

const OrbitRenderer = ctx.window.OrbitRenderer;
const stateStub = { state: { stockList: [], connectedSymbols: [] }, subscribe: () => () => {} };
const canvasStub = {
  getBoundingClientRect: () => ({ width: 800, height: 600 }),
  getContext: () => noopCtx,
};
const renderer = new OrbitRenderer(canvasStub, stateStub);
renderer.nodeRenderData.set("AAPL", { currentX: 10, currentY: 10, trail: [] });
renderer.nodeRenderData.set("MSFT", { currentX: 20, currentY: 20, trail: [] });
renderer.nodeRenderData.set("HOV", { currentX: 30, currentY: 30, trail: [] });

renderer.updateAndRenderNodes(
  noopCtx,
  [],
  { connectedSymbols: [], hoveredSymbol: "HOV", draggedSymbol: null },
  0.016,
  1000,
);
assert.strictEqual(renderer.nodeRenderData.has("AAPL"), false);
assert.strictEqual(renderer.nodeRenderData.has("MSFT"), false);
assert.strictEqual(renderer.nodeRenderData.has("HOV"), true);

process.stdout.write("ok");
"""
    _run_node(script)


def test_iss18_toast_z_index_above_fullscreen_and_no_conflicting_skip_link() -> None:
    colors_css = (ROOT_DIR / "static" / "css" / "colors.css").read_text(encoding="utf-8")
    index_css = (ROOT_DIR / "static" / "css" / "index.css").read_text(encoding="utf-8")

    toast_match = re.search(r"--z-toast:\s*(\d+)\s*;", colors_css)
    fs_match = re.search(r"--z-fullscreen:\s*(\d+)\s*;", colors_css)
    assert toast_match is not None and fs_match is not None
    assert int(toast_match.group(1)) > int(fs_match.group(1))
    assert "z-index: var(--z-toast, 20100);" in colors_css

    assert ".skip-link" in colors_css
    assert ".skip-link:focus" in colors_css
    assert ".skip-link" not in index_css
    assert ".sr-only" in index_css
