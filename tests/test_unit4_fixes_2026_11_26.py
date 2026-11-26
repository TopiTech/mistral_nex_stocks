"""Regression tests for Unit 4 (frontend JS/TS & experimental orbit) fixes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend behavioral tests")
    proc = subprocess.run(
        [node, "-"],
        cwd=str(ROOT_DIR),
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"Node failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    return proc.stdout


def test_u4_1_api_client_preserves_visibility_timeout_across_url_resolution_and_reconnect() -> None:
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const source = fs.readFileSync("static/js/api_client.js", "utf8");
const timers = new Map();
let nextId = 1;
const docHandlers = {};

function FakeEventSource(url) {
  this.url = url;
  this.closed = false;
  this.listeners = {};
}
FakeEventSource.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
FakeEventSource.prototype.removeEventListener = function () {};
FakeEventSource.prototype.close = function () { this.closed = true; };

const ctx = {
  console: { log() {}, info() {}, debug() {}, warn() {}, error() {} },
  setTimeout: (fn, ms) => {
    const id = nextId++;
    timers.set(id, { fn, ms });
    return id;
  },
  clearTimeout: (id) => timers.delete(id),
  setInterval: () => 1,
  clearInterval: () => {},
  EventSource: FakeEventSource,
  document: {
    hidden: true,
    addEventListener: (t, fn) => { docHandlers[t] = fn; },
    removeEventListener: () => {},
  },
  window: { addEventListener: () => {}, removeEventListener: () => {} },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(source, ctx);

(async () => {
  const client = new ctx.window.APIClient("/api");
  let ticketNum = 0;
  client.openSSE("", () => {}, () => {}, {
    urlProvider: async () => `/api/stocks/stream?sse_ticket=t${++ticketNum}`,
    autoReconnect: true,
    reconnectDelay: 50,
  });
  const armedTimerId = client._visibilityTimeout;
  assert.ok(armedTimerId, "visibility timer must arm while hidden");
  await new Promise((r) => setImmediate(r));
  assert.ok(client.currentEventSource, "EventSource must be opened after urlProvider resolves");
  assert.strictEqual(
    client._visibilityTimeout,
    armedTimerId,
    "resolving URL via _openWithResolvedUrl must NOT reset _visibilityTimeout",
  );
  // Trigger error -> reconnect -> resolve URL again
  client.currentEventSource.onerror(new Error("drop"));
  const reconnectTimer = Array.from(timers.values()).find((t) => t.ms !== client.visibilityTimeout);
  reconnectTimer.fn();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    client._visibilityTimeout,
    armedTimerId,
    "reconnect via _openWithResolvedUrl must preserve original _visibilityTimeout",
  );
  client.destroy();
  process.stdout.write("OK");
})();
"""
    )
    assert out == "OK"


def test_u4_2_u4_3_u4_8_u4_11_api_js_news_abort_sse_frames_flash_search_and_date() -> None:
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
const uiSrc = fs.readFileSync("static/js/ui.js", "utf8");

function makeClassList() {
  const set = new Set();
  return {
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    contains: (c) => set.has(c),
    toggle: (c, f) => (f ? set.add(c) : set.delete(c)),
  };
}
function makeEl(tag = "div") {
  const listeners = {};
  const attrs = {};
  let _text = "";
  const children = [];
  return {
    tagName: tag.toUpperCase(),
    className: "",
    classList: makeClassList(),
    style: {},
    children,
    get textContent() { return _text; },
    set textContent(v) {
      _text = String(v ?? "");
      if (_text === "") children.length = 0;
    },
    dataset: {},
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => attrs[k],
    appendChild: function (c) { children.push(c); return c; },
    addEventListener: function (t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
    dispatchEvent: function (ev) { (listeners[ev?.type] || []).forEach((fn) => fn(ev)); },
    dispatch: function (t, ev = {}) { (listeners[t] || []).forEach((fn) => fn(ev)); },
    querySelector: function () { return null; },
    querySelectorAll: () => [],
    closest: () => null,
    get offsetWidth() { throw new Error("forced synchronous reflow must not read offsetWidth"); },
  };
}

const rafQueue = [];
const timers = new Map();
let nextTimer = 1;
const ctx = {
  console,
  AbortController,
  TextDecoder,
  CustomEvent: function (t) { this.type = t; },
  APIClient: function () {
    this.openSSE = () => null;
    this.closeSSE = () => {};
    this.destroy = () => {};
    this.hasActiveSSE = () => false;
    this.resetHeartbeat = () => {};
    this.reconnectWithBackoff = () => {};
  },
  Number,
  Date,
  Math,
  JSON,
  Set,
  Map,
  Array,
  Object,
  Promise,
  setTimeout: (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id; },
  clearTimeout: (id) => timers.delete(id),
  requestAnimationFrame: (fn) => { const id = rafQueue.length + 1; rafQueue.push(fn); return id; },
  cancelAnimationFrame: () => {},
  HAS_MISTRAL_API_KEY: true,
  CONSTANTS: { TIMEOUT: { NEWS_REQUEST: 10000, API_REQUEST: 5000 } },
  state: { isLoadingNews: false, stocks: { us: [], jp: [], idx: [] } },
  logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  showToast: () => { ctx.toastCount += 1; },
  toastCount: 0,
  setButtonLoading: () => {},
  resetButton: () => {},
  createRequestToken: () => "tok_" + "a".repeat(20),
  createEl: (tag, cls, txt) => { const e = makeEl(tag); e.className = cls; e.textContent = txt; return e; },
  addStockPromptCalls: [],
  addStockPrompt: (sym, name) => ctx.addStockPromptCalls.push([sym, name]),
  DOM: {
    els: {},
    get(id) { return this.els[id] || null; },
  },
  document: {
    createElement: (tag) => makeEl(tag),
    createTextNode: (t) => ({ textContent: t }),
    getElementById: (id) => ctx.DOM.els[id] || null,
    addEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
  },
  window: { addEventListener: () => {}, removeEventListener: () => {}, location: { origin: "http://localhost" } },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(apiSrc, ctx);
vm.runInContext(uiSrc.slice(uiSrc.indexOf("function triggerPriceFlash"), uiSrc.indexOf("// (L16 と同じ契約)")), ctx);
ctx.addStockPrompt = (sym, name) => ctx.addStockPromptCalls.push([sym, name]);

(async () => {
  // U4-2: Abort from pagehide/cleanupApiLifecycleResources must NOT overwrite newsMetaStats with timeout
  const metaEl = makeEl("div");
  const usBox = makeEl("div");
  const jpBox = makeEl("div");
  const trBox = makeEl("div");
  ctx.DOM.els["news-meta-stats"] = metaEl;
  ctx.DOM.els["news-us"] = usBox;
  ctx.DOM.els["news-jp"] = jpBox;
  ctx.DOM.els["news-trends"] = trBox;
  ctx.DOM.els["newsRefreshBtn"] = makeEl("button");

  ctx.apiFetch = (_url, opts) => new Promise((_resolve, reject) => {
    opts.signal.addEventListener("abort", () => {
      const err = new Error("Aborted");
      err.name = "AbortError";
      reject(err);
    });
    ctx.cleanupApiLifecycleResources();
  });
  await ctx.loadNews();
  assert.ok(!metaEl.textContent.includes("タイムアウト"), "non-timeout abort must not render timeout banner");

  // U4-3: streamChatReply handles multiline data: and spaceless data:{...}
  const sseChunks = [
    'data:{"delta":"Hello "}\n\n',
    'data:{"delta":\ndata:"World"}\n\n',
    'data: {"done":true,"reply":"Hello World"}\n\n',
  ];
  const enc = new TextEncoder();
  let cIdx = 0;
  ctx.csrfFetch = async () => ({
    ok: true,
    headers: {
      get: (h) => (String(h).toLowerCase() === "content-type" ? "text/event-stream" : null),
    },
    body: {
      getReader: () => ({
        read: async () => (cIdx < sseChunks.length ? { done: false, value: enc.encode(sseChunks[cIdx++]) } : { done: true }),
        releaseLock: () => {},
      }),
    },
  });
  ctx.fetch = ctx.csrfFetch;
  ctx.readCsrfToken = () => "csrf";
  ctx.ensureValidCsrfToken = async () => "csrf";
  const seen = [];
  const reply = await ctx.streamChatReply({ message: "hi" }, (partial) => seen.push(partial));
  assert.strictEqual(reply, "Hello World");

  // U4-8: triggerPriceFlash avoids offsetWidth and clears via fallback timeout
  const priceEl = makeEl("span");
  ctx.triggerPriceFlash(priceEl, "flash-up");
  assert.strictEqual(priceEl.classList.contains("flash-up"), false);
  while (rafQueue.length) rafQueue.shift()();
  assert.strictEqual(priceEl.classList.contains("flash-up"), true);
  // Fire fallback timer
  const cleanupTimers = Array.from(timers.values()).filter((t) => t.ms === 1300);
  assert.strictEqual(cleanupTimers.length, 1);
  cleanupTimers[0].fn();
  assert.strictEqual(priceEl.classList.contains("flash-up"), false);

  // U4-11: searchStocks passes displayName ("名称不明") and applyAnalysisResult skips Invalid Date
  const listEl = makeEl("div");
  const boxEl = makeEl("div");
  const inputEl = makeEl("input");
  inputEl.value = "AAPL";
  ctx.DOM.els["search-results"] = boxEl;
  ctx.DOM.els["search-results-list"] = listEl;
  ctx.DOM.els["searchInput"] = inputEl;
  ctx.apiFetch = async () => ({ data: { results: [{ symbol: "AAPL", name: "", exchange: "NMS" }] } });
  await ctx.searchStocks();
  listEl.children[0].dispatch("click");
  assert.deepStrictEqual(ctx.addStockPromptCalls[0], ["AAPL", "名称不明"]);

  const aiSlider = makeEl("div");
  const aiSection = makeEl("div");
  aiSection.querySelector = (s) => (s === ".ai-slider" ? aiSlider : null);
  const wrapper = makeEl("div");
  wrapper.dataset.stockKey = "us:AAPL";
  wrapper.querySelector = (s) => (s === ".ai-section" ? aiSection : null);
  ctx.applyAnalysisResult(
    wrapper,
    { symbol: "AAPL", market: "us" },
    { analyzed_at: "not-a-valid-date", summary: "ok" },
  );
  assert.strictEqual(aiSlider.children.length, 0, "malformed analyzed_at must not append Invalid Date card");

  process.stdout.write("OK");
})();
"""
    )
    assert out == "OK"


def test_u4_4_u4_5_u4_6_u4_7_u4_9_u4_10_frontend_modules() -> None:
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// U4-4: ai_portfolio.js negative currency formatting & beforeunload chart cleanup
const aiPortfolioSrc = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
assert.ok(
  aiPortfolioSrc.includes("destroyAiPortfolioCharts();") &&
    aiPortfolioSrc.includes('addEventListener("beforeunload"'),
  "beforeunload must call destroyAiPortfolioCharts",
);
const totalPlEl = { textContent: "", classList: { toggle() {} } };
const todayPlEl = { textContent: "", classList: { toggle() {} } };
const aiCtx = {
  console,
  Math,
  Number,
  String,
  Array,
  Object,
  JSON,
  document: {
    getElementById: (id) =>
      id === "ai-pf-total-pl" ? totalPlEl : id === "ai-pf-today-pl" ? todayPlEl : null,
    addEventListener: () => {},
  },
  window: { addEventListener: () => {} },
  state: { stocks: { us: [{ symbol: "NVDA", market: "us", price: 95, avg_price: 100, shares: 10, change_percent: -2.0 }], jp: [], idx: [] } },
  Chart: undefined,
};
aiCtx.globalThis = aiCtx;
vm.createContext(aiCtx);
vm.runInContext(aiPortfolioSrc, aiCtx);
// Extract renderAiKpiGrid behavior via direct block execution to verify negative format
const kpiMatch = aiPortfolioSrc.match(/function renderAiKpiGrid\(portfolio\) \{[\s\S]*?\n  \}/);
assert.ok(kpiMatch);
const kpiFn = new Function(
  "portfolio",
  "document",
  "VIRTUAL_BASE_CAPITAL_JPY",
  "computeAiSimulation",
  "toNumber",
  "findStockInState",
  kpiMatch[0] + "; return renderAiKpiGrid(portfolio);",
);
kpiFn(
  { items: [{ symbol: "NVDA", market: "us", weight_pct: 100 }] },
  {
    getElementById: (id) => {
      if (id === "ai-pf-total-pl") return totalPlEl;
      if (id === "ai-pf-today-pl") return todayPlEl;
      return { textContent: "", classList: { toggle() {} } };
    },
  },
  10000000,
  () => ({ points: [], monthlyReturn: -0.05 }),
  (v) => (typeof v === "number" ? v : null),
  () => ({ price: 95, avg_price: 100, change_percent: -2.0 }),
);
assert.ok(totalPlEl.textContent.startsWith("-¥"), `expected -¥ prefix, got ${totalPlEl.textContent}`);
assert.ok(todayPlEl.textContent.startsWith("-¥"), `expected -¥ prefix, got ${todayPlEl.textContent}`);

// U4-5: chart.js portfolio loading targets .pnl-chart-container
const chartSrc = fs.readFileSync("static/js/chart.js", "utf8");
assert.ok(
  chartSrc.includes('isPortfolioContext\n    ? ".pnl-chart-container"') ||
    chartSrc.includes('? ".pnl-chart-container"'),
  "refreshStockChart must select .pnl-chart-container on portfolio context",
);

// U4-6: ui.js trapDrawerFocus & fullscreen modal handle activeElement === container
const uiSrc = fs.readFileSync("static/js/ui.js", "utf8");
const trapSlice = uiSrc.slice(uiSrc.indexOf("function trapDrawerFocus"), uiSrc.indexOf("function initAiDrawerEvents"));
let focused = null;
const btn1 = { id: "b1", hasAttribute: () => false, closest: () => null, focus: () => { focused = "b1"; } };
const btn2 = { id: "b2", hasAttribute: () => false, closest: () => null, focus: () => { focused = "b2"; } };
const overlay = {
  querySelectorAll: () => [btn1, btn2],
  contains: (el) => el === btn1 || el === btn2,
  focus: () => { focused = "overlay"; },
};
const uiCtx = {
  isUiElementVisible: () => true,
  document: { activeElement: overlay },
};
vm.createContext(uiCtx);
vm.runInContext(trapSlice, uiCtx);
uiCtx.trapDrawerFocus({ key: "Tab", shiftKey: true, preventDefault: () => {} }, overlay);
assert.strictEqual(focused, "b2", "Shift+Tab on overlay container must wrap to last focusable");
assert.ok(uiSrc.includes("active === modal || !modal.contains(active)"), "fullscreen modal must also handle container/outside focus");

// U4-7: heatmap.js negative modulo fix on ArrowLeft from -1
const hmSrc = fs.readFileSync("static/js/heatmap.js", "utf8");
assert.ok(hmSrc.includes("((index % meshes.length) + meshes.length) % meshes.length"));
assert.ok(hmSrc.includes("state.three.keyboardIndex < 0"));

// U4-9: realtime_client.js destroy cancels pending rAF + reconnectTimer; orbit-entry uses addManagedListener for keydown
const rtSrc = fs.readFileSync("static/js/realtime_client.js", "utf8");
let cancelledFrames = [];
let clearedTimers = [];
const rtWinListeners = {};
const rtCtx = {
  console,
  Map,
  Set,
  Number,
  Math,
  Array,
  String,
  setTimeout: () => 99,
  clearTimeout: (id) => clearedTimers.push(id),
  window: {
    addEventListener: (t, fn) => { (rtWinListeners[t] = rtWinListeners[t] || []).push(fn); },
    cancelAnimationFrame: (id) => cancelledFrames.push(id),
    requestAnimationFrame: () => 42,
  },
  document: { readyState: "complete", addEventListener: () => {} },
};
rtCtx.globalThis = rtCtx;
vm.createContext(rtCtx);
vm.runInContext(rtSrc, rtCtx);
rtCtx.window.realtimeClient._pendingDeltaFrames.push({ frame: 42, apply: () => {} });
rtCtx.window.realtimeClient.reconnectTimer = 99;
rtWinListeners.beforeunload.forEach((fn) => fn());
assert.deepStrictEqual(cancelledFrames, [42]);
assert.deepStrictEqual(clearedTimers, [99]);

const orbitEntrySrc = fs.readFileSync("static/js/experimental/orbit-entry.js", "utf8");
assert.ok(orbitEntrySrc.includes('addManagedListener(canvas, "keydown"'));

// U4-10: orbit-state.js notifies timeCursor on setSelectedSymbol when resetting from > 0
const orbitStateSrc = fs.readFileSync("static/js/experimental/orbit-state.js", "utf8");
const osCtx = {
  window: { location: { search: "", pathname: "/experimental/orbit" }, history: { replaceState: () => {} } },
  URLSearchParams,
  String,
  Math,
  Number,
  Array,
  Object,
  Set,
  Map,
};
osCtx.globalThis = osCtx;
vm.createContext(osCtx);
vm.runInContext(orbitStateSrc, osCtx);
const st = new osCtx.window.ObservatoryState();
const notifications = [];
st.subscribe((key, v) => {
  if (key === "timeCursor") notifications.push(v);
});
st.data.selectedSymbol = "AAPL";
st.data.timeCursor = 5;
st.setSelectedSymbol("NVDA");
assert.deepStrictEqual(notifications, [0]);

process.stdout.write("OK");
"""
    )
    assert out == "OK"
