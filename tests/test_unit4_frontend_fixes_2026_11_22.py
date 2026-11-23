"""Regression tests for Unit 4 frontend core, views, UI, Observatory, and verify scripts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend JS/TS regression checks")
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
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""


def test_unit4_api_client_visibility_timer_and_api_core() -> None:
    """APIClient preserves visibility pause countdown across reconnects; markStreamAlive & live merge work."""
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// 1. api_client.js visibility timeout must NOT reset countdown on reconnect churn while hidden
const timers = new Map();
let nextTimerId = 1;
const docHandlers = {};
const instances = [];

function FakeES(url) {
  this.url = url;
  this.closed = false;
  this.listeners = {};
  instances.push(this);
}
FakeES.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
FakeES.prototype.removeEventListener = function () {};
FakeES.prototype.close = function () { this.closed = true; };

const apiClientCtx = {
  console,
  setTimeout: (fn, ms) => { const id = nextTimerId++; timers.set(id, { fn, ms }); return id; },
  clearTimeout: (id) => { timers.delete(id); },
  setInterval: () => 1,
  clearInterval: () => {},
  EventSource: FakeES,
  document: {
    hidden: false,
    addEventListener: (t, fn) => { docHandlers[t] = fn; },
    removeEventListener: () => {},
  },
  window: { addEventListener: () => {}, removeEventListener: () => {} },
};
apiClientCtx.globalThis = apiClientCtx;
vm.createContext(apiClientCtx);
vm.runInContext(fs.readFileSync("static/js/api_client.js", "utf8"), apiClientCtx);

const client = new apiClientCtx.window.APIClient("/api");
const es1 = client.openSSE("/api/stocks/stream", () => {}, () => {}, {});
apiClientCtx.document.hidden = true;
docHandlers.visibilitychange();
const initialTimerId = client._visibilityTimeout;
assert.ok(initialTimerId, "visibility timer armed on hide");
es1.onerror(new Error("drop-1"));
assert.strictEqual(
  client._visibilityTimeout,
  initialTimerId,
  "reconnect error while hidden must preserve existing countdown timer ID",
);
client.destroy();

// 2. api.js mergeStocksWithExistingHistory protects Mode 2 live quotes,
//    loadIndicesLoop skips fetching:true, and markStreamAlive stops fallback polling
const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
function extractFn(src, marker) {
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `missing ${marker}`);
  const parenStart = src.indexOf("(", start);
  let pDepth = 0;
  let open = -1;
  for (let i = parenStart; i < src.length; i += 1) {
    if (src[i] === "(") pDepth += 1;
    else if (src[i] === ")") {
      pDepth -= 1;
      if (pDepth === 0) {
        open = src.indexOf("{", i);
        break;
      }
    }
  }
  assert.notStrictEqual(open, -1, `missing body for ${marker}`);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${marker}`);
}

const mergeFieldsMatch = apiSrc.match(/const LIVE_QUOTE_FIELDS = \[[\s\S]*?\];/);
assert.ok(mergeFieldsMatch, "LIVE_QUOTE_FIELDS must exist in api.js");
const apiMergeCtx = { Array, Map, Object, Boolean, Number, String };
vm.createContext(apiMergeCtx);
vm.runInContext(
  mergeFieldsMatch[0] + "\n" + extractFn(apiSrc, "function mergeStocksWithExistingHistory("),
  apiMergeCtx,
);
const existing = {
  us: [{ symbol: "AAPL", price: 215.5, change: 3.1, change_percent: 1.46, __live_update: true, is_realtime: true }],
  jp: [],
  idx: [],
};
const staleSnapshot = {
  us: [{ symbol: "AAPL", price: 201.0, change: -1.0, change_percent: -0.5, shares: 5, avg_price: 180 }],
  jp: [],
  idx: [],
};
const mergedPreserved = apiMergeCtx.mergeStocksWithExistingHistory(staleSnapshot, existing, false, { preserveLiveQuotes: true });
assert.strictEqual(mergedPreserved.us[0].price, 215.5, "live Mode 2 price must be preserved over stale HTTP snapshot");
assert.strictEqual(mergedPreserved.us[0].shares, 5, "portfolio shares must still merge");
const mergedForced = apiMergeCtx.mergeStocksWithExistingHistory(staleSnapshot, existing, false, { preserveLiveQuotes: false });
assert.strictEqual(mergedForced.us[0].price, 201.0, "forced sync may overwrite price");

// loadIndicesLoop skips {fetching: true}
let indexBarUpdates = 0;
let intervalCb = null;
const idxLoopCtx = {
  _loadIndicesInterval: null,
  _indicesFetchGeneration: 0,
  setInterval: (fn) => { intervalCb = fn; return 99; },
  clearInterval: () => {},
  $logger: { warn() {} },
  apiFetch: async () => ({ data: { fetching: true } }),
  updateIndicesBar: () => { indexBarUpdates += 1; },
};
vm.createContext(idxLoopCtx);
vm.runInContext(
  extractFn(apiSrc, "async function loadIndicesLoop(") + "\n" + extractFn(apiSrc, "function stopLoadIndicesLoop("),
  idxLoopCtx,
);
(async () => {
  await idxLoopCtx.loadIndicesLoop();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(indexBarUpdates, 0, "loadIndicesLoop must ignore {fetching:true}");
  assert.ok(typeof intervalCb === "function");

  // markStreamAlive stops fallback polling after sleep recovery zeroed reconnectAttempt
  let stopFallbackCalled = 0;
  const markCtx = {
    sseApiClient: { sseReconnectAttempt: 0 },
    sseState: { reconnectAttempts: 0, disconnectedSince: Date.now() - 5000, fallbackPolling: 123 },
    stopSseFallbackPolling: () => { stopFallbackCalled += 1; },
  };
  vm.createContext(markCtx);
  const mStart = apiSrc.indexOf("const markStreamAlive = () => {");
  const mOpen = apiSrc.indexOf("{", mStart);
  let d = 0, mEnd = -1;
  for (let i = mOpen; i < apiSrc.length; i += 1) {
    if (apiSrc[i] === "{") d += 1;
    else if (apiSrc[i] === "}") { d -= 1; if (d === 0) { mEnd = i + 1; break; } }
  }
  vm.runInContext(apiSrc.slice(mStart, mEnd) + "; this.markStreamAlive = markStreamAlive;", markCtx);
  markCtx.markStreamAlive();
  assert.strictEqual(stopFallbackCalled, 1, "markStreamAlive must stop fallback polling on sleep recovery");
  assert.strictEqual(markCtx.sseState.disconnectedSince, 0);

  // utils.js CSRF auto-refresh interval is 15 min (< 30m WTF_CSRF_TIME_LIMIT)
  const utilsSrc = fs.readFileSync("static/js/utils.js", "utf8");
  let csrfDelay = 0;
  const csrfCtx = {
    window: {},
    document: { querySelector: () => ({}) },
    setInterval: (_fn, ms) => { csrfDelay = ms; return 1; },
    clearInterval: () => {},
    refreshCsrfToken: () => {},
  };
  vm.createContext(csrfCtx);
  vm.runInContext(extractFn(utilsSrc, "function startCsrfAutoRefresh("), csrfCtx);
  csrfCtx.startCsrfAutoRefresh();
  assert.strictEqual(csrfDelay, 15 * 60 * 1000, "CSRF refresh interval must be 15 minutes");

  process.stdout.write("OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    assert out == "OK"


def test_unit4_state_send_chat_and_alerts_config() -> None:
    """state.replaceStock notifies stocks; debouncedRenderPortfolio exported; sendChat propagates AbortSignal; getAlertsConfig clones/caches."""
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// 1. state.js: replaceStock emits "stocks" & window.debouncedRenderPortfolio exported
const stateSrc = fs.readFileSync("static/js/state.js", "utf8");
const stateWin = {};
const stateCtx = {
  window: stateWin,
  document: { querySelectorAll: () => [], addEventListener: () => {} },
  CONSTANTS: { POLLING: { PORTFOLIO_DEBOUNCE: 50 }, UI: { MAX_FAVORITES: 50 } },
  APP_CONFIG: {},
  HAS_MISTRAL_API_KEY: false,
  _hasAiAccess: () => false,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  renderPortfolio: () => {},
  setTimeout,
  clearTimeout,
  Map,
  Set,
  Array,
  Object,
  Number,
  Date,
};
vm.createContext(stateCtx);
vm.runInContext(stateSrc, stateCtx);
const seen = [];
stateWin.state.updateStocks({ us: [{ symbol: "AAPL", price: 100 }], jp: [], idx: [] });
stateWin.state.subscribe("stocks", () => seen.push("stocks"));
const replaced = stateWin.state.replaceStock("us", "AAPL", { symbol: "AAPL", price: 105 });
assert.strictEqual(replaced, true);
assert.strictEqual(seen.length, 1, "replaceStock must fire stocks subscriber notification");
assert.strictEqual(typeof stateWin.debouncedRenderPortfolio, "function", "window.debouncedRenderPortfolio must be exported");

// 2. api.js: sendChat registers controller in activeSendChatControllers, forwards signal, handles AbortError cleanly
const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
function extractFn(src, marker) {
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `missing ${marker}`);
  const parenStart = src.indexOf("(", start);
  let pDepth = 0;
  let open = -1;
  for (let i = parenStart; i < src.length; i += 1) {
    if (src[i] === "(") pDepth += 1;
    else if (src[i] === ")") {
      pDepth -= 1;
      if (pDepth === 0) {
        open = src.indexOf("{", i);
        break;
      }
    }
  }
  assert.notStrictEqual(open, -1, `missing body for ${marker}`);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${marker}`);
}
let capturedSignal = null;
const toasts = [];
const inputEl = { value: "analyze AAPL", disabled: false, isConnected: true, closest: () => null, focus() {} };
const sendBtn = { disabled: false };
const logEl = { children: [], scrollHeight: 10, scrollTop: 0, appendChild(c) { this.children.push(c); } };
const chatSection = { querySelector: (s) => (s === ".chat-send-btn" ? sendBtn : s === ".chat-log" ? logEl : null) };
inputEl.closest = () => chatSection;

const activeSet = new Set();
const chatCtx = {
  AbortController,
  Set,
  HAS_MISTRAL_API_KEY: true,
  activeSendChatControllers: activeSet,
  $logger: { warn() {} },
  document: {
    getElementById: () => null,
    querySelector: () => null,
    createElement: () => ({ className: "", textContent: "" }),
  },
  getStockByKey: () => ({ symbol: "AAPL", market: "us" }),
  ensureChatDisclaimer: () => {},
  createRequestToken: () => "tok-1",
  showToast: (m) => toasts.push(m),
  streamChatReply: (_p, _cb, opts) => {
    capturedSignal = opts?.signal;
    return new Promise((_res, rej) => {
      capturedSignal.addEventListener("abort", () => {
        const err = new Error("aborted");
        err.name = "AbortError";
        rej(err);
      });
    });
  },
};
vm.createContext(chatCtx);
vm.runInContext(extractFn(apiSrc, "async function sendChat("), chatCtx);

(async () => {
  const p = chatCtx.sendChat({ dataset: { stockKey: "us:AAPL" } }, inputEl);
  await new Promise((r) => setImmediate(r));
  assert.ok(capturedSignal, "sendChat must pass AbortSignal to streamChatReply");
  assert.strictEqual(activeSet.size, 1, "activeSendChatControllers tracks in-flight request");
  for (const c of activeSet) c.abort();
  await p;
  assert.strictEqual(activeSet.size, 0, "finally removes controller");
  assert.strictEqual(inputEl.disabled, false, "input re-enabled after abort");
  assert.strictEqual(toasts.length, 0, "AbortError must not flash error toast");

  // 3. index_main.js: getAlertsConfig shallow-clones and revalidates when storage changes externally
  const idxSrc = fs.readFileSync("static/js/index_main.js", "utf8");
  let store = JSON.stringify({ "us:AAPL": { priceUp: 250, triggeredUp: false } });
  let parseCount = 0;
  const OrigJSON = JSON;
  const idxCtx = {
    localStorage: {
      getItem: () => store,
      setItem: (_k, v) => { store = v; },
    },
    JSON: {
      parse: (s) => { parseCount += 1; return OrigJSON.parse(s); },
      stringify: (v) => OrigJSON.stringify(v),
    },
    Object,
    Array,
  };
  vm.createContext(idxCtx);
  vm.runInContext(
    extractFn(idxSrc, "function getAlertsConfig(") + "\n" + extractFn(idxSrc, "function saveAlertsConfig("),
    idxCtx,
  );
  const cfg1 = idxCtx.getAlertsConfig();
  cfg1["us:AAPL"].triggeredUp = true;
  const cfg2 = idxCtx.getAlertsConfig();
  assert.strictEqual(cfg2["us:AAPL"].triggeredUp, false, "caller mutation must not leak into cached config");
  assert.strictEqual(parseCount, 1, "repeated unchanged localStorage reads avoid redundant JSON.parse");
  store = JSON.stringify({ "us:AAPL": { priceUp: 300, triggeredUp: false } });
  const cfg3 = idxCtx.getAlertsConfig();
  assert.strictEqual(cfg3["us:AAPL"].priceUp, 300, "external storage edit must invalidate cache");
  assert.strictEqual(parseCount, 2);

  process.stdout.write("OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    assert out == "OK"


def test_unit4_views_bfcache_ai_portfolio_settings_and_setup() -> None:
    """ai_portfolio FE-04, settings FE-03/07/09, heatmap FE-01, screener FE-02/08, setup pagehide."""
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// 1. ai_portfolio.js: FE-04 separates post-save refresh error from copy failure
const apSrc = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
const toasts = [];
let failureShown = 0;
function extractFn(src, marker) {
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `missing ${marker}`);
  const parenStart = src.indexOf("(", start);
  let pDepth = 0;
  let open = -1;
  for (let i = parenStart; i < src.length; i += 1) {
    if (src[i] === "(") pDepth += 1;
    else if (src[i] === ")") {
      pDepth -= 1;
      if (pDepth === 0) {
        open = src.indexOf("{", i);
        break;
      }
    }
  }
  assert.notStrictEqual(open, -1, `missing body for ${marker}`);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${marker}`);
}
const apCtx = {
  console: { warn() {}, error() {} },
  currentPortfolio: { items: [{ symbol: "AAPL", market: "us", weight: 100 }] },
  csrfFetch: async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, added_count: 1 }),
  }),
  getApiErrorMessage: (_d, fallback) => fallback,
  showToast: (m) => toasts.push(m),
  showAiPortfolioFailure: () => { failureShown += 1; },
  fetchInitialStocks: async () => { throw new Error("network down"); },
  loadPortfolioSnapshot: async () => {},
  Array,
};
vm.createContext(apCtx);
vm.runInContext(extractFn(apSrc, "async function copyAiPortfolioToMy("), apCtx);

(async () => {
  await apCtx.copyAiPortfolioToMy();
  assert.strictEqual(failureShown, 0, "successful save + refresh error must NOT invoke showAiPortfolioFailure");
  assert.ok(toasts.some((t) => t.includes("反映しました")), "success toast must show");
  assert.ok(toasts.some((t) => t.includes("再読み込みに失敗")), "warning toast must show");

  // 2. setup.js registers both beforeunload and pagehide
  const setupSrc = fs.readFileSync("static/js/setup.js", "utf8");
  assert.ok(setupSrc.includes('addEventListener("pagehide"'), "setup.js must clean up on pagehide");

  // 3. screener.js resets countEl="0", filters null items, handles AbortError placeholder & pageshow
  const scrSrc = fs.readFileSync("static/js/screener.js", "utf8");
  assert.ok(scrSrc.includes('countEl.textContent = "0"'), "screener catch must clear stale results count");
  assert.ok(scrSrc.includes("Array.isArray(stocks)"), "renderTableRows must guard non-array stocks");
  assert.ok(scrSrc.includes('addEventListener("pageshow"'), "screener must register pageshow recovery");

  // 4. heatmap.js resets isInit=false in cleanupHeatmapResources and recovers on pageshow
  const hmSrc = fs.readFileSync("static/js/heatmap.js", "utf8");
  assert.ok(hmSrc.includes("state.three.isInit = false;"), "cleanupHeatmapResources must reset isInit");
  assert.ok(hmSrc.includes('addEventListener("pageshow"'), "heatmap must recover on pageshow");

  // 5. settings.js _scheduleStatusClear cancels overlapping timer & prompt preserves edited value
  const setSrc = fs.readFileSync("static/js/settings.js", "utf8");
  assert.ok(setSrc.includes("if (data.custom_ai_prompt && !promptInput.value)"), "FE-03 guard present");
  assert.ok(setSrc.includes("openaiDiscoveredModels.textContent = \"\""), "FE-07 clear present");
  const statusEl = { textContent: "first" };
  let nextId = 1;
  const activeTimers = new Map();
  const clearedIds = [];
  const timerCtx = {
    Map,
    setTimeout: (fn) => { const id = nextId++; activeTimers.set(id, fn); return id; },
    clearTimeout: (id) => { clearedIds.push(id); activeTimers.delete(id); },
  };
  vm.createContext(timerCtx);
  vm.runInContext(
    "const _settingsStatusClearTimers = new Map();\n" + extractFn(setSrc, "function _scheduleStatusClear("),
    timerCtx,
  );
  timerCtx._scheduleStatusClear(statusEl, 3000);
  timerCtx._scheduleStatusClear(statusEl, 3000);
  assert.strictEqual(clearedIds.length, 1, "rapid re-save must cancel prior status clear timer");

  process.stdout.write("OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    assert out == "OK"


def test_unit4_ui_chart_and_tradingview_isolation() -> None:
    """ui.js UI-1/2/3/4 + transitionend cleanup + fullscreen loading, chart.js fullscreen NO_HISTORY_MSG, TV isolation."""
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const uiSrc = fs.readFileSync("static/js/ui.js", "utf8");
const chartSrc = fs.readFileSync("static/js/chart.js", "utf8");
const tvSrc = fs.readFileSync("static/js/tradingview_manager.js", "utf8");

function extractFn(src, marker) {
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `missing ${marker}`);
  const parenStart = src.indexOf("(", start);
  let pDepth = 0;
  let open = -1;
  for (let i = parenStart; i < src.length; i += 1) {
    if (src[i] === "(") pDepth += 1;
    else if (src[i] === ")") {
      pDepth -= 1;
      if (pDepth === 0) {
        open = src.indexOf("{", i);
        break;
      }
    }
  }
  assert.notStrictEqual(open, -1, `missing body for ${marker}`);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${marker}`);
}

// 1. UI-1 & UI-3: ensureStockDetails must not delete card A's banner when fetching card B, nor write card A's status into drawer showing B
function makeFakeNode(cls = "") {
  const listeners = new Map();
  let removed = false;
  return {
    className: cls,
    dataset: {},
    style: {},
    textContent: "",
    children: [],
    listeners,
    get isRemoved() { return removed; },
    remove() { removed = true; },
    classList: { contains: (c) => cls.split(/\s+/).includes(c), add() {}, remove() {}, toggle() {} },
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: (t, fn) => { listeners.set(t, fn); },
    removeEventListener: (t, fn) => { if (listeners.get(t) === fn) listeners.delete(t); },
    setAttribute() {},
    removeAttribute() {},
    closest: () => null,
  };
}
const bannerA = makeFakeNode("detail-error-banner");
const bannerB = makeFakeNode("detail-error-banner");
const drawerSectorB = makeFakeNode("detail-sector");
drawerSectorB.textContent = "Technology";
const wrapperA = {
  dataset: { stockKey: "us:AAPL", symbol: "AAPL", market: "us" },
  querySelector: (s) => (s === ".detail-inner" ? makeFakeNode("detail-inner") : null),
  querySelectorAll: (s) => (s === ".detail-error-banner" ? [bannerA] : []),
};
const wrapperB = {
  dataset: { stockKey: "us:MSFT", symbol: "MSFT", market: "us" },
  querySelector: (s) => (s === ".detail-inner" ? makeFakeNode("detail-inner") : null),
  querySelectorAll: (s) => (s === ".detail-error-banner" ? [bannerB] : []),
};
const drawerEl = makeFakeNode("open");
drawerEl.dataset.stockKey = "us:MSFT";

const uiCtx = {
  AbortController,
  setTimeout,
  clearTimeout,
  currentDrawerActiveWrapper: wrapperB,
  stockDetailsCache: new Map(),
  stockDetailsInflight: new Map(),
  isStockDetailsCacheFresh: () => false,
  getDetailFetchController: () => new AbortController(),
  $logger: { debug() {}, error() {} },
  document: {
    getElementById: (id) => (id === "stock-detail-drawer" ? drawerEl : null),
    querySelector: (sel) => (sel === "#stock-detail-drawer .detail-sector" ? drawerSectorB : null),
    querySelectorAll: () => [bannerA, bannerB],
  },
  apiFetch: async () => ({ data: {} }),
};
vm.createContext(uiCtx);
vm.runInContext(extractFn(uiSrc, "async function ensureStockDetails("), uiCtx);
const pDet = uiCtx.ensureStockDetails(wrapperA).catch(() => {});
assert.strictEqual(bannerA.isRemoved, true, "own stale banner on card A is cleared");
assert.strictEqual(bannerB.isRemoved, false, "UI-1: card B banner must stay isolated from card A's cleanup");
assert.strictEqual(drawerSectorB.textContent, "Technology", "UI-3: card A load must NOT overwrite drawer showing card B");

// 2. transitionend listener cleanup on rapid open->close / cancelScheduledDestroy
const detailPanel = makeFakeNode("detail-panel");
detailPanel.dataset.stockKey = "us:AAPL";
const transCtx = {
  document: { querySelectorAll: () => [], getElementById: () => null },
  window: { matchMedia: () => ({ matches: false }) },
  setTimeout: () => 55,
  clearTimeout: () => {},
  getTransitionFallbackMs: () => 300,
  detailCloseGeneration: new Map(),
  cancelScheduledDestroy: null,
  clearStockCardMinHeights: () => {},
  compactStockCardLayout: () => {},
  destroyChart: () => {},
};
vm.createContext(transCtx);
vm.runInContext(
  extractFn(uiSrc, "function cancelScheduledDestroy(") + "\n" + extractFn(uiSrc, "function closeDetailPanel("),
  transCtx,
);
transCtx.closeDetailPanel(detailPanel);
assert.ok(detailPanel.listeners.has("transitionend"), "transitionend attached");
transCtx.cancelScheduledDestroy(detailPanel);
assert.strictEqual(detailPanel.listeners.has("transitionend"), false, "cancelScheduledDestroy cleans up transitionend listener");

// 3. chart.js: normalizedOhlc.length < 2 destroys chart & shows NO_HISTORY_MSG in fullscreen targetCanvas
const fsErrorHost = { children: [], querySelectorAll: () => [], appendChild(c) { this.children.push(c); } };
let destroyedCanvas = null;
const fakeCanvas = { getContext: () => ({}) };
const chartCtx = {
  NO_HISTORY_MSG: "表示可能なヒストリカルデータがありません。",
  chartInstances: new Map(),
  document: { getElementById: (id) => (id === "fs-chart-canvas-wrapper" ? fsErrorHost : null), querySelector: () => null },
  createEl: (_t, cls, text) => ({ cls, text }),
  getChartAnimationEnabled: () => false,
  getChartPref: (_k, _p, d) => d,
  _safeDestroyChart: (c) => { destroyedCanvas = c; },
  showChartError: () => { throw new Error("card error should not be shown for fullscreen targetCanvas"); },
  _finiteOrNull: (v) => (typeof v === "number" && Number.isFinite(v) ? v : null),
  Array,
  Number,
  Date,
};
vm.createContext(chartCtx);
vm.runInContext(extractFn(chartSrc, "function drawChart("), chartCtx);
chartCtx.drawChart(null, [{ x: "bad", c: 1 }, { x: "bad2", c: 2 }], [{ x: "bad", c: 1 }, { x: "bad2", c: 2 }], { targetCanvas: fakeCanvas, stockKey: "us:AAPL" });
assert.strictEqual(destroyedCanvas, fakeCanvas, "stale chart must be destroyed when normalized points < 2");
assert.strictEqual(fsErrorHost.children.length, 1, "fullscreen error host receives NO_HISTORY_MSG");

// 4. TradingViewManager: tickerTape clear does not invalidate chart generation; messageHandler rejects missing iframe source
const dom = new Map();
const makeTvEl = (id) => ({
  id,
  isConnected: true,
  classList: { contains: () => false, add() {}, remove() {} },
  style: {},
  children: [],
  appendChild(c) { this.children.push(c); },
  replaceChildren() { this.children = []; },
  setAttribute() {},
  removeAttribute() {},
  querySelector: () => null,
});
dom.set("tradingview-chart-container", makeTvEl("tradingview-chart-container"));
dom.set("tradingview-ticker-tape-container", makeTvEl("tradingview-ticker-tape-container"));
let msgHandler = null;
const tvCtx = {
  window: {
    addEventListener: (t, fn) => { if (t === "message") msgHandler = fn; },
    removeEventListener: () => {},
  },
  document: {
    body: { classList: { contains: () => false } },
    getElementById: (id) => dom.get(id) || null,
    createElement: (tag) => {
      const el = makeTvEl(tag);
      el.remove = () => {};
      return el;
    },
    querySelector: () => null,
    head: { appendChild() {} },
  },
  TradingView: undefined,
  setTimeout: () => 1,
  clearTimeout: () => {},
  Set,
  Promise,
  String,
  JSON,
};
tvCtx.window.window = tvCtx.window;
vm.createContext(tvCtx);
vm.runInContext(tvSrc, tvCtx);
tvCtx.window.TradingViewManager.renderAdvancedChart("tradingview-chart-container", "AAPL", "NASDAQ", { forceTvSymbol: "NASDAQ:AAPL" });
let fallbackCalled = false;
// Send message from tradingview origin when NO iframe exists yet (or foreign event.source) -> must be ignored!
assert.ok(typeof msgHandler === "function");
msgHandler({ origin: "https://www.tradingview.com", source: {}, data: "invalid-symbol" });
assert.strictEqual(tvCtx.window.TradingViewManager.hasSwitched, false, "unmatched event.source / missing iframe must be rejected");
// Clearing ticker tape container must not throw or corrupt chart state
tvCtx.window.TradingViewManager.clearContainer("tradingview-ticker-tape-container");
assert.ok(!fallbackCalled);

pDet.then(() => process.stdout.write("OK")).catch((e) => { console.error(e); process.exit(1); });
"""
    )
    assert out == "OK"


def test_unit4_observatory_new_obs_and_verify_scripts() -> None:
    """NEW-OBS-1..4, OBS-01..08, and verify script contract tests execute cleanly."""
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// 1. NEW-OBS-2: orbit-state setTimeGranularity notifies timeCursor=0 when reset from scrubbed position
const stateSrc = fs.readFileSync("static/js/experimental/orbit-state.js", "utf8");
const stateWin = {
  location: { search: "", href: "http://localhost/" },
  history: { replaceState() {} },
  matchMedia: () => ({ matches: false }),
};
const stateCtx = {
  window: stateWin,
  URLSearchParams,
  URL,
  localStorage: { getItem: () => null, setItem() {} },
  console,
  Map,
  Set,
  Array,
  Number,
  String,
  Math,
  Boolean,
};
vm.createContext(stateCtx);
vm.runInContext(stateSrc, stateCtx);
const obs = new stateWin.ObservatoryState();
obs.setTimeCursor(4);
const events = [];
obs.subscribe((k, v) => events.push([k, v]));
obs.setTimeGranularity("1y");
assert.ok(events.some(([k, v]) => k === "timeCursor" && v === 0), "granularity change must emit timeCursor=0 reset");

// 2. OBS-02 & NEW-OBS-4: orbit-renderer frame loop survives render exception; tier alpha regex safe
const renSrc = fs.readFileSync("static/js/experimental/orbit-renderer.js", "utf8");
assert.ok(!renSrc.includes('.replace("0.", "0.4")'), "unsafe tier.color.replace must be removed");
let scheduledRaf = [];
const renCtx = {
  console: { error() {}, warn() {} },
  document: { hidden: false, body: { classList: { contains: () => false } }, addEventListener() {}, removeEventListener() {} },
  window: { addEventListener() {}, removeEventListener() {}, devicePixelRatio: 1 },
  performance: { now: () => 10 },
  requestAnimationFrame: (fn) => { scheduledRaf.push(fn); return scheduledRaf.length; },
  cancelAnimationFrame: () => {},
  Map,
  Set,
  Math,
};
vm.createContext(renCtx);
vm.runInContext(renSrc, renCtx);
const fakeCanvas = { getContext: () => ({}), getBoundingClientRect: () => ({ width: 400, height: 300 }), style: {} };
const renderer = new renCtx.window.OrbitRenderer(fakeCanvas, { subscribe: () => () => {}, state: { stockList: [] } });
renderer.start();
renderer.render = () => { throw new Error("transient render glitch"); };
renderer._shouldPauseLoop = () => false;
scheduledRaf.shift()(20);
assert.strictEqual(scheduledRaf.length, 1, "OBS-02: rAF loop must still reschedule after render() exception");

// 3. NEW-OBS-3 & OBS-08: AccessibilityController passes <summary> through & isolates modal return focus
const a11ySrc = fs.readFileSync("static/js/experimental/accessibility-controller.js", "utf8");
assert.ok(a11ySrc.includes("summary"), "NEW-OBS-3: <summary> must be ignored by global shortcut handler");
const summaryEl = {
  tagName: "SUMMARY",
  closest: (sel) => (sel.includes("summary") ? summaryEl : null),
};
let toggled = 0;
const helpBtn = { focusCalls: 0, focus() { this.focusCalls += 1; } };
const searchBtn = { focusCalls: 0, focus() { this.focusCalls += 1; } };
const helpClose = { focus() {} };
const helpModal = {
  classList: { tokens: new Set(["hidden"]), add(c) { this.tokens.add(c); }, remove(c) { this.tokens.delete(c); }, contains(c) { return this.tokens.has(c); } },
  setAttribute() {}, removeAttribute() {}, contains: () => false, querySelector: () => helpClose,
};
const searchModal = {
  classList: { tokens: new Set(["hidden"]), add(c) { this.tokens.add(c); }, remove(c) { this.tokens.delete(c); }, contains(c) { return this.tokens.has(c); } },
  setAttribute() {}, removeAttribute() {}, contains: () => false,
};
const searchInput = { value: "", setAttribute() {}, removeAttribute() {}, focus() {} };
const docStub = { activeElement: summaryEl, contains: () => true };
const a11yWin = { addEventListener() {}, removeEventListener() {} };
const a11yCtx = {
  window: a11yWin,
  document: docStub,
  WeakMap,
  Set,
  Array,
  localStorage: { getItem: () => null },
};
vm.createContext(a11yCtx);
vm.runInContext(a11ySrc, a11yCtx);
const a11y = new a11yWin.AccessibilityController(
  { subscribe: () => () => {}, state: { stockList: [], aiDiveOpen: false, connectedSymbols: [] }, togglePause() { toggled += 1; } },
  { helpModal, searchModal, searchInput },
);
a11y.handleKeydown({ key: " ", preventDefault() { throw new Error("must not intercept summary Space"); } });
assert.strictEqual(toggled, 0);
docStub.activeElement = searchBtn;
a11y.openSearchModal();
docStub.activeElement = helpBtn;
a11y.openHelpModal();
a11y.closeHelpModal();
assert.strictEqual(helpBtn.focusCalls, 1, "help close restores help trigger");
a11y.closeSearchModal();
assert.strictEqual(searchBtn.focusCalls, 1, "search close still restores search trigger independently");
a11y.destroy();

// 4. NEW-OBS-1: ConstellationController hides chart wrapper when closed/cleared
const conSrc = fs.readFileSync("static/js/experimental/constellation-controller.js", "utf8");
const chartWrapper = { classList: { tokens: new Set(), add(c) { this.tokens.add(c); }, contains(c) { return this.tokens.has(c); } } };
const clearedRect = [];
const chartCanvas = { width: 300, height: 120, getContext: () => ({ clearRect: (...a) => clearedRect.push(a) }) };
const conDrawer = { classList: { add() {}, remove() {} }, setAttribute() {}, removeAttribute() {}, contains: () => false };
const conCtx = { window: {}, document: { getElementById: () => null, activeElement: null }, ObservatoryDataAdapter: {} };
conCtx.window = conCtx;
vm.createContext(conCtx);
vm.runInContext(conSrc, conCtx);
const cc = new conCtx.ConstellationController(
  { subscribe: () => () => {}, toggleConnectMode() {}, clearConnectedSymbols() {} },
  { drawer: conDrawer, chartWrapper, chartCanvas },
);
cc.renderHUD({ isConnectMode: false, connectedSymbols: [], stocks: new Map() });
assert.strictEqual(chartWrapper.classList.contains("hidden"), true, "NEW-OBS-1: chart wrapper hidden when drawer closes");
assert.strictEqual(clearedRect.length, 1);
cc.destroy();

process.stdout.write("OK");
"""
    )
    assert out == "OK"


def test_unit4_css_sparkline_drawer_and_symbol_fixes() -> None:
    """CSS tokens/pills, multi-context sparkline cache, drawer ownership isolation, JP deep link, AI portfolio clamps."""
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// 1. colors.css: :root tokens are not swallowed by a nested comment before `:root`
const colorsCss = fs.readFileSync("static/css/colors.css", "utf8");
assert.ok(
  !/\/\*[^*]*(?:\*(?!\/)[^*]*)*\/\*[^*]*\*\/\s*\n\s*\*[^\n]*\*\/\s*:root\s*\{/.test(colorsCss),
  "colors.css must not nest a comment inside the breakpoint header before :root",
);
const strippedColors = colorsCss.replace(/\/\*[\s\S]*?\*\//g, "");
assert.ok(!strippedColors.includes("= */"), "colors.css must have balanced CSS comments before :root");
const allRoots = Array.from(strippedColors.matchAll(/:root\s*\{([\s\S]*?)\}/g))
  .map((m) => m[1])
  .join("\n");
assert.ok(allRoots.includes("--bp-mobile: 640px;"), "--bp-mobile token must be active");
assert.ok(allRoots.includes("--z-modal: 1300;"), "--z-modal token must be active");

// 2. setup.css: .preset-pill-btn overrides global button width/margin
const setupCss = fs.readFileSync("static/css/setup.css", "utf8");
const pillRule = setupCss.match(/\.preset-pill-btn\s*\{([\s\S]*?)\}/);
assert.ok(pillRule, ".preset-pill-btn rule present");
assert.ok(/width:\s*auto\s*;/.test(pillRule[1]), ".preset-pill-btn sets width: auto");
assert.ok(/margin:\s*0\s*;/.test(pillRule[1]), ".preset-pill-btn sets margin: 0");

// 3. state.js + ui.js: sparkline caches isolate watchlist vs portfolio wrappers and purgeStockKeyCaches clears both
const stateSrc = fs.readFileSync("static/js/state.js", "utf8");
const uiSrc = fs.readFileSync("static/js/ui.js", "utf8");
const noop = () => {};
const stateCtx = {
  window: {
    addEventListener: noop,
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    matchMedia: () => ({ matches: false, addEventListener: noop }),
    location: { href: "" },
  },
  document: {
    documentElement: {},
    body: { classList: { add: noop, remove: noop, toggle: noop } },
    addEventListener: noop,
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: () => ({
      classList: { add: noop, remove: noop, toggle: noop },
      style: {},
      appendChild: noop,
      setAttribute: noop,
    }),
  },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  matchMedia: () => ({ matches: false, addEventListener: noop }),
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  APP_CONFIG: {},
  DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
  logger: { warn: noop, info: noop, debug: noop, error: noop },
  localStorage: { removeItem() {} },
  JSON,
  Date,
  Array,
  Map,
  Set,
  WeakMap,
  Number,
  String,
  Boolean,
  Math,
  Object,
  Error,
};
stateCtx.globalThis = stateCtx;
vm.createContext(stateCtx);
vm.runInContext(
  stateSrc +
    "\nthis.__stateProbe = { sparklineSignatureMap, sparklineUpdateAt, shouldUpdateSparkline, purgeStockKeyCaches };",
  stateCtx,
);
const watchWrap = { dataset: { stockKey: "us:AAPL", marketContext: "watchlist", visible: "true" } };
const portWrap = { dataset: { stockKey: "us:AAPL", marketContext: "portfolio", visible: "true" } };
const pts = [100, 101, 102];
assert.strictEqual(stateCtx.__stateProbe.shouldUpdateSparkline(watchWrap, "us:AAPL", pts), true);
assert.strictEqual(
  stateCtx.__stateProbe.shouldUpdateSparkline(portWrap, "us:AAPL", pts),
  true,
  "portfolio wrapper must not be suppressed by watchlist sparkline signature",
);
assert.strictEqual(stateCtx.__stateProbe.shouldUpdateSparkline(watchWrap, "us:AAPL", pts), false);
assert.strictEqual(stateCtx.__stateProbe.shouldUpdateSparkline(portWrap, "us:AAPL", pts), false);
stateCtx.__stateProbe.purgeStockKeyCaches("us:AAPL");
assert.strictEqual(stateCtx.__stateProbe.shouldUpdateSparkline(watchWrap, "us:AAPL", pts), true);
assert.strictEqual(stateCtx.__stateProbe.shouldUpdateSparkline(portWrap, "us:AAPL", pts), true);

function extractFnBalanced(src, marker) {
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `missing ${marker}`);
  const parenStart = src.indexOf("(", start);
  let pDepth = 0;
  let open = -1;
  for (let i = parenStart; i < src.length; i += 1) {
    if (src[i] === "(") pDepth += 1;
    else if (src[i] === ")") {
      pDepth -= 1;
      if (pDepth === 0) {
        open = src.indexOf("{", i);
        break;
      }
    }
  }
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === "'" || ch === '"' || ch === "`") {
      const q = ch;
      i += 1;
      while (i < src.length && src[i] !== q) {
        if (src[i] === "\\") i += 1;
        i += 1;
      }
      continue;
    }
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${marker}`);
}

const sparkSigMap = new Map();
const nullSparkCanvas = { getContext: () => null, getBoundingClientRect: () => ({ width: 100, height: 32 }) };
const sparkCtx = {
  Array,
  Number,
  Math,
  Map,
  window: { devicePixelRatio: 1 },
  sparklineSignatureMap: sparkSigMap,
  getSparklineSignature: (d) => `sig:${d.length}`,
  destroyChart: () => {},
  setSparklineVisibility: () => {},
};
vm.createContext(sparkCtx);
vm.runInContext(
  extractFnBalanced(uiSrc, "const drawSparkline = (wrapper, data) =>") + "; this.drawSparkline = drawSparkline;",
  sparkCtx,
);
sparkCtx.drawSparkline({ dataset: { stockKey: "us:NVDA", marketContext: "watchlist" }, querySelector: () => nullSparkCanvas }, pts);
sparkCtx.drawSparkline({ dataset: { stockKey: "us:NVDA", marketContext: "portfolio" }, querySelector: () => nullSparkCanvas }, pts);
assert.strictEqual(sparkSigMap.has("us:NVDA|watchlist"), true);
assert.strictEqual(sparkSigMap.has("us:NVDA|portfolio"), true);

// 4. chart.js: drawChart & refreshStockChart ignore drawer when open for another stock
const chartSrc = fs.readFileSync("static/js/chart.js", "utf8");
let drawerCanvasTouched = false;
let intervalUpdates = [];
let drawerPnlCalls = 0;
const drawerCanvasB = { getContext: () => { drawerCanvasTouched = true; return null; } };
const drawerPnlCanvasB = { id: "drawer-pnl-b" };
const drawerContainerB = {
  loadingAdded: false,
  classList: { add(c) { if (c === "loading") drawerContainerB.loadingAdded = true; }, remove() {} },
};
const drawerElB = {
  dataset: { stockKey: "us:MSFT" },
  classList: { contains: (c) => c === "open" },
};
const wrapperA = {
  isConnected: true,
  dataset: { stockKey: "us:AAPL", marketContext: "portfolio" },
  querySelector: () => null,
};
const wrapperB = {
  isConnected: true,
  dataset: { stockKey: "us:MSFT", marketContext: "watchlist" },
};
const chartGuardCtx = {
  Array,
  Number,
  Math,
  Date,
  Promise,
  setTimeout,
  clearTimeout,
  globalThis: { crypto: { randomUUID: () => "uuid-1" } },
  currentDrawerActiveWrapper: wrapperB,
  document: {
    getElementById: (id) => (id === "stock-detail-drawer" ? drawerElB : null),
    querySelector: (sel) => {
      if (sel === "#stock-detail-drawer .chart-canvas") return drawerCanvasB;
      if (sel === "#stock-detail-drawer .chart-canvas-pnl") return drawerPnlCanvasB;
      if (sel === "#stock-detail-drawer .chart-container") return drawerContainerB;
      return null;
    },
  },
  window: {
    updateIntervalControlsVisibility: (target, key) => intervalUpdates.push([target, key]),
  },
  logger: { error() {}, warn() {}, info() {}, debug() {} },
  NO_HISTORY_MSG: "no history",
  showChartError: () => {},
  getChartAnimationEnabled: () => false,
  getStockByKey: () => ({ symbol: "AAPL", market: "us", avg_price: 100 }),
  getChartPref: (_k, _p, d) => d,
  getFreshPrefetchedHistory: () => ({ formattedData: [{ x: 1, c: 10 }, { x: 2, c: 11 }], ohlcData: [] }),
  clearChartError: () => {},
  applyHistoryToStockAndWrapper: () => {},
  drawPnLChart: () => { drawerPnlCalls += 1; },
  drawFullscreenChartIfActive: () => {},
  fetchStockHistoryPayload: async () => null,
  AbortController,
};
vm.createContext(chartGuardCtx);
vm.runInContext(
  extractFnBalanced(chartSrc, "function drawChart(wrapper, data, ohlcData, options = {})") +
    "\n" +
    extractFnBalanced(chartSrc, "async function refreshStockChart(wrapper, period, interval, options = {})"),
  chartGuardCtx,
);
chartGuardCtx.drawChart(wrapperA, [{ x: 1, c: 10 }, { x: 2, c: 11 }], [], {});
assert.strictEqual(drawerCanvasTouched, false, "drawChart for wrapperA must not draw onto drawer open for MSFT");
(async () => {
  await chartGuardCtx.refreshStockChart(wrapperA, "1mo", "1d", {});
  assert.strictEqual(drawerPnlCalls, 0, "refreshStockChart(portfolio) for AAPL must not draw into drawer open for MSFT");
  assert.strictEqual(
    intervalUpdates.some(([t]) => t === drawerElB),
    false,
    "refreshStockChart for AAPL must not update drawer interval controls belonging to MSFT",
  );
  // Test loading container + non-prefetch PnL isolation
  chartGuardCtx.getFreshPrefetchedHistory = () => null;
  chartGuardCtx.fetchStockHistoryPayload = async () => ({ formattedData: [{ x: 1, c: 10 }, { x: 2, c: 11 }], ohlcData: [] });
  await chartGuardCtx.refreshStockChart(wrapperA, "1mo", "1d", {});
  assert.strictEqual(drawerContainerB.loadingAdded, false, "refreshStockChart for AAPL must not add .loading on MSFT drawer container");
  assert.strictEqual(drawerPnlCalls, 0, "non-prefetch refreshStockChart(portfolio) for AAPL must not draw into MSFT drawer PnL canvas");

  // 5. api.js: sendChat & clearAnalysisError guard keyless drawer & cross-stock drawer fallback
  const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
  const drawerBannerRemoved = { hit: false };
  const keylessDrawer = {
    dataset: {},
    classList: { contains: (c) => c === "open" },
    querySelector: (s) => (s === ".ai-error-banner" ? { remove() { drawerBannerRemoved.hit = true; } } : null),
  };
  let drawerInputRead = false;
  const drawerChatInput = {
    get value() { drawerInputRead = true; return "hello"; },
    disabled: false,
    closest: () => null,
  };
  const apiGuardCtx = {
    HAS_MISTRAL_API_KEY: false,
    makeStockKey: (m, s) => `${m}:${s}`,
    currentDrawerActiveWrapper: wrapperB,
    showToast: () => {},
    document: {
      getElementById: (id) => {
        if (id === "stock-detail-drawer") return apiGuardCtx.__drawerEl;
        if (id === "drawerTabAiContent") return null;
        return null;
      },
      querySelector: (sel) => (sel === "#stock-detail-drawer .chat-input" ? drawerChatInput : null),
    },
    __drawerEl: keylessDrawer,
  };
  vm.createContext(apiGuardCtx);
  vm.runInContext(
    extractFnBalanced(apiSrc, "function clearAnalysisError(wrapper)") +
      "\n" +
      extractFnBalanced(apiSrc, "async function sendChat(wrapper, inputEl = null)"),
    apiGuardCtx,
  );
  apiGuardCtx.clearAnalysisError({ dataset: {}, querySelector: () => null });
  assert.strictEqual(drawerBannerRemoved.hit, false, "keyless wrapper must not clear keyless drawer error banner");
  apiGuardCtx.__drawerEl = drawerElB;
  await apiGuardCtx.sendChat(wrapperA, null);
  assert.strictEqual(drawerInputRead, false, "sendChat(wrapperA) must not hijack MSFT drawer chat input");

  // 5b. ui.js: setDetailItemVisibility isolates drawer owned by another stock
  const drawerPeRowB = {
    hiddenSet: null,
    style: {},
    classList: { toggle() {} },
    setAttribute(k, v) { if (k === "aria-hidden") this.hiddenSet = v; },
  };
  const visCtx = {
    currentDrawerActiveWrapper: wrapperB,
    document: {
      getElementById: (id) => (id === "stock-detail-drawer" ? drawerElB : null),
      querySelector: (sel) => (sel === "#stock-detail-drawer .detail-item-pe" ? drawerPeRowB : null),
    },
  };
  vm.createContext(visCtx);
  vm.runInContext(extractFnBalanced(uiSrc, "function setDetailItemVisibility(wrapper, field, visible)"), visCtx);
  visCtx.setDetailItemVisibility(wrapperA, "pe", false);
  assert.strictEqual(drawerPeRowB.hiddenSet, null, "setDetailItemVisibility(wrapperA) must not hide MSFT drawer row");

  // 6. index_main.js: JP 4-char deep link (?q=7203&market=jp) normalizes to jp:7203.T
  const indexMainSrc = fs.readFileSync("static/js/index_main.js", "utf8");
  let openedKey = null;
  const scheduledCbs = [];
  const idxCtx = {
    URLSearchParams,
    window: { location: { search: "?q=7203&market=jp" } },
    DOM: { get: () => null },
    setTimeout: (fn) => { scheduledCbs.push(fn); return scheduledCbs.length; },
    makeStockKey: (m, s) => `${m}:${s}`,
    normalizeSymbolForMarketClient: (s, m) => (m === "jp" && /^\d{3}[0-9A-Z]$/.test(String(s).toUpperCase()) ? `${String(s).toUpperCase()}.T` : String(s).toUpperCase()),
    getStockByKey: (k) => (k === "jp:7203.T" ? { market: "jp", symbol: "7203.T" } : null),
    findWrapperByStockKey: (k) => (k === "jp:7203.T" ? { scrollIntoView() {} } : null),
    getLatestStockForDrawer: (s) => s,
    openStockDetailDrawer: (s) => { openedKey = `${s.market}:${s.symbol}`; },
    searchStocks: () => {},
  };
  vm.createContext(idxCtx);
  vm.runInContext(extractFnBalanced(indexMainSrc, "function handleUrlSearchParam()"), idxCtx);
  idxCtx.handleUrlSearchParam();
  while (scheduledCbs.length) scheduledCbs.shift()();
  assert.strictEqual(openedKey, "jp:7203.T", "JP 4-char deep link must resolve jp:7203.T");

  // 7. ai_portfolio.js: verify non-negative clamp + JP favorite key normalization logic
  const pfSrc = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
  assert.ok(pfSrc.includes("Math.max(0, Math.min(100, weightPct))"), "ai_portfolio.js clamps weight bar to [0, 100]");
  assert.ok(pfSrc.includes("normalizeSymbolForMarketClient"), "ai_portfolio.js normalizes JP favKey");

  process.stdout.write("OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    assert out == "OK"


def test_unit4_additional_review_regressions_2026_11_24() -> None:
    """Verify heatmap 3D bfcache restore, ai_portfolio symbol lookup, screener colspan/resize cleanup, and CSS tokens."""
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function extractFn(src, marker) {
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `missing ${marker}`);
  const parenStart = src.indexOf("(", start);
  let pDepth = 0;
  let open = -1;
  for (let i = parenStart; i < src.length; i += 1) {
    if (src[i] === "(") pDepth += 1;
    else if (src[i] === ")") {
      pDepth -= 1;
      if (pDepth === 0) {
        open = src.indexOf("{", i);
        break;
      }
    }
  }
  assert.notStrictEqual(open, -1, `missing body for ${marker}`);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${marker}`);
}

// 1. heatmap.js: bfcache pageshow restores 3D WebGL scene when viewMode==='3d' & isInit===false
const hmSrc = fs.readFileSync("static/js/heatmap.js", "utf8");
const cleanupStart = hmSrc.indexOf("const cleanupHeatmapResources = () => {");
assert.notStrictEqual(cleanupStart, -1, "missing cleanupHeatmapResources");
const loadTail = hmSrc.lastIndexOf("loadHeatmap();");
assert.ok(loadTail > cleanupStart, "missing trailing loadHeatmap call");
const lifecycleBlock = hmSrc.slice(cleanupStart, loadTail);

let initCalls = 0;
let startAnimCalls = 0;
let render3dCalls = 0;
let loadCalls = 0;
const winListeners = new Map();
const makeFakeBtn = () => {
  const classes = new Set();
  const attrs = new Map();
  return {
    classList: {
      toggle: (cls, force) => {
        if (force) classes.add(cls);
        else classes.delete(cls);
      },
      add: (cls) => classes.add(cls),
      remove: (cls) => classes.delete(cls),
    },
    setAttribute: (k, v) => attrs.set(k, v),
    getAttribute: (k) => attrs.get(k),
  };
};
const hmCtx = {
  THREE: {},
  _resizeHandler: () => {},
  state: {
    viewMode: "3d",
    rawStocks: [{ symbol: "AAPL", size: 10 }],
    lastErrorMessage: null,
    timeoutId: null,
    controller: null,
    three: {
      isInit: true,
      controls: { dispose() {} },
      stockMeshes: [],
      renderer: { dispose() {} },
      scene: { remove() {} },
      camera: {},
      hoveredMesh: null,
      raycaster: {},
      mouse: {},
    },
  },
  els: {
    view2d: makeFakeBtn(),
    view3d: makeFakeBtn(),
    canvas: makeFakeBtn(),
    canvas3d: makeFakeBtn(),
    controls3d: makeFakeBtn(),
  },
  document: {
    getElementById: () => null,
  },
  window: {
    addEventListener: (ev, fn) => {
      if (!winListeners.has(ev)) winListeners.set(ev, new Set());
      winListeners.get(ev).add(fn);
    },
    removeEventListener: (ev, fn) => {
      winListeners.get(ev)?.delete(fn);
    },
  },
  clearTimeout: () => {},
  clear3DErrorBanner: () => {},
  stop3DAnimation: () => {},
  disposeObject: () => {},
  init3DScene: () => {
    initCalls += 1;
    hmCtx.state.three.isInit = true;
    return true;
  },
  sync3DViewportSize: () => {},
  start3DAnimation: () => {
    startAnimCalls += 1;
  },
  stop3DAnimation: () => {},
  hasRenderableStocks: () => true,
  normalizeStock: (s) => s,
  render3DHeatmap: () => {
    render3dCalls += 1;
  },
  renderHeatmap: () => {},
  showError: () => {},
  loadHeatmap: () => {
    loadCalls += 1;
  },
};
vm.createContext(hmCtx);
vm.runInContext(
  extractFn(hmSrc, "function switchViewMode(mode)") + "\n" + lifecycleBlock,
  hmCtx,
);
winListeners.get("pagehide").forEach((fn) => fn());
assert.strictEqual(hmCtx.state.three.isInit, false, "pagehide must tear down 3D state");
assert.strictEqual(hmCtx.state.three.renderer, null);
winListeners.get("pageshow").forEach((fn) => fn({ persisted: true }));
assert.strictEqual(initCalls, 1, "pageshow in 3D mode must re-initialize 3D scene");
assert.strictEqual(startAnimCalls, 1, "pageshow in 3D mode must restart animation loop");
assert.strictEqual(render3dCalls, 1, "pageshow in 3D mode must re-render 3D heatmap");
assert.strictEqual(loadCalls, 1, "pageshow in 3D mode must also refresh heatmap data");

// 2. ai_portfolio.js: findStockInState normalizes JP 4-char codes & casing
const pfSrc = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
const pfCtx = {
  state: {
    stocks: {
      jp: [{ symbol: "7203.T", price: 2500 }, { symbol: "130A.T", price: 900 }],
      us: [{ symbol: "AAPL", price: 190 }],
    },
  },
};
vm.createContext(pfCtx);
const normDecl = apiSrc.slice(
  apiSrc.indexOf("const normalizeSymbolForMarketClient"),
  apiSrc.indexOf("};", apiSrc.indexOf("const normalizeSymbolForMarketClient")) + 2,
);
vm.runInContext(
  normDecl +
    "\n" +
    extractFn(pfSrc, "function normalizePortfolioLookupSymbol(") +
    "\n" +
    extractFn(pfSrc, "function findStockInState("),
  pfCtx,
);
assert.strictEqual(pfCtx.findStockInState("7203", "jp")?.symbol, "7203.T");
assert.strictEqual(pfCtx.findStockInState("7203.T", "jp")?.symbol, "7203.T");
assert.strictEqual(pfCtx.findStockInState("130a", "jp")?.symbol, "130A.T");
assert.strictEqual(pfCtx.findStockInState(" aapl ", "us")?.symbol, "AAPL");
assert.strictEqual(pfCtx.findStockInState("", "us"), null);
assert.strictEqual(pfCtx.findStockInState("9999", "jp"), null);

// 3. screener.js: dynamic placeholders carry data-colspan-full='8' and resize listener cleans up/restores
const scrSrc = fs.readFileSync("static/js/screener.js", "utf8");
let visibleColSpan = 8;
const placeholderCells = [];
const scrWinListeners = new Map();
const scrCtx = {
  fetchTimeout: 123,
  screenerAbortController: { abort() {} },
  getVisibleColSpan: () => visibleColSpan,
  document: {
    createElement: () => {
      const attrs = {};
      return {
        colSpan: 0,
        className: "",
        textContent: "",
        setAttribute: (k, v) => { attrs[k] = v; },
        getAttribute: (k) => attrs[k],
      };
    },
    querySelectorAll: (sel) =>
      sel === 'td[data-colspan-full="8"]'
        ? placeholderCells.filter((c) => c.getAttribute("data-colspan-full") === "8")
        : [],
  },
  window: {
    addEventListener: (ev, fn) => {
      if (!scrWinListeners.has(ev)) scrWinListeners.set(ev, new Set());
      scrWinListeners.get(ev).add(fn);
    },
    removeEventListener: (ev, fn) => {
      scrWinListeners.get(ev)?.delete(fn);
    },
  },
  clearTimeout: () => {},
  fetchScreenerResults: () => {},
};
vm.createContext(scrCtx);
const scrLifecycleStart = scrSrc.indexOf('window.addEventListener("resize", syncStaticColspanPlaceholders);');
const scrIifeEnd = scrSrc.lastIndexOf("})();");
const scrBlockClose = scrSrc.lastIndexOf("}", scrIifeEnd - 1);
assert.ok(scrBlockClose > scrLifecycleStart, "missing screener lifecycle closing brace");
vm.runInContext(
  extractFn(scrSrc, "function syncStaticColspanPlaceholders(") +
    "\n" +
    extractFn(scrSrc, "function createPlaceholderRowCell(") +
    "\n" +
    scrSrc.slice(scrLifecycleStart, scrBlockClose),
  scrCtx,
);
const dynCell = scrCtx.createPlaceholderRowCell("text-center empty-cell", "empty");
placeholderCells.push(dynCell);
assert.strictEqual(dynCell.colSpan, 8);
assert.strictEqual(dynCell.getAttribute("data-colspan-full"), "8");
visibleColSpan = 5;
scrCtx.syncStaticColspanPlaceholders();
assert.strictEqual(dynCell.colSpan, 5, "syncStaticColspanPlaceholders must update dynamic placeholders");
assert.strictEqual(scrWinListeners.get("resize").size, 1);
scrWinListeners.get("pagehide").forEach((fn) => fn());
assert.strictEqual(scrWinListeners.get("resize").size, 0, "pagehide must remove resize listener");
scrWinListeners.get("pageshow").forEach((fn) => fn({ persisted: true }));
assert.strictEqual(scrWinListeners.get("resize").size, 1, "pageshow must re-attach resize listener once");

// 4. CSS contracts: colors.css theme-aware flash vars & single @keyframes spin definition
const colorsCss = fs.readFileSync("static/css/colors.css", "utf8");
const indexCss = fs.readFileSync("static/css/index.css", "utf8");
assert.ok(colorsCss.includes("--pos-flash-bg:"), "colors.css defines --pos-flash-bg");
assert.ok(colorsCss.includes("--neg-flash-bg:"), "colors.css defines --neg-flash-bg");
assert.ok(
  colorsCss.includes("var(--pos-flash-bg, rgba(16, 185, 129, 0.25))"),
  "flash-green consumes --pos-flash-bg token",
);
assert.ok(
  colorsCss.includes("var(--neg-flash-bg, rgba(244, 63, 94, 0.25))"),
  "flash-red consumes --neg-flash-bg token",
);
assert.strictEqual(
  (colorsCss.match(/@keyframes\s+spin\b/g) || []).length,
  1,
  "colors.css defines @keyframes spin once",
);
assert.strictEqual(
  (indexCss.match(/@keyframes\s+spin\b/g) || []).length,
  0,
  "index.css must not duplicate @keyframes spin",
);

process.stdout.write("OK");
"""
    )
    assert out == "OK"
