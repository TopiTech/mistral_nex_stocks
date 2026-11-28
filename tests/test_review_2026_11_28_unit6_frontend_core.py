"""Regression tests for Unit 6 fixes (core frontend JS/TS — D6A-01..D6A-11)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend JS regression tests")
    proc = subprocess.run(
        [node, "-e", script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"Node script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_unit6_frontend_core_regressions_d6a_01_to_d6a_11() -> None:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function extractFn(src, marker) {
  const idx = src.indexOf(marker);
  assert(idx !== -1, "Missing marker: " + marker);
  let i = src.indexOf("(", idx), p = 0, seen = false;
  for (; i < src.length; i++) {
    if (src[i] === "(") { p++; seen = true; }
    else if (src[i] === ")") { p--; if (seen && p === 0) break; }
  }
  const open = src.indexOf("{", i);
  let d = 0, inStr = null, esc = false, inLineCmt = false, inBlkCmt = false;
  for (let j = open; j < src.length; j++) {
    const ch = src[j];
    const next = src[j + 1] || "";
    if (inLineCmt) { if (ch === "\n") inLineCmt = false; continue; }
    if (inBlkCmt) { if (ch === "*" && next === "/") { inBlkCmt = false; j++; } continue; }
    if (esc) { esc = false; continue; }
    if (inStr) {
      if (ch === "\\") esc = true;
      else if (ch === inStr) inStr = null;
      continue;
    }
    if (ch === "/" && next === "/") { inLineCmt = true; j++; continue; }
    if (ch === "/" && next === "*") { inBlkCmt = true; j++; continue; }
    if (ch === '"' || ch === "'" || ch === "`") { inStr = ch; continue; }
    if (ch === "{") d++;
    else if (ch === "}") { d--; if (d === 0) return src.slice(idx, j + 1); }
  }
  throw new Error("Unbalanced: " + marker);
}

// D6A-01: _applyDeltasNow strips identity fields
const rtSrc = fs.readFileSync("static/js/realtime_client.js", "utf8");
assert(rtSrc.includes("delete sanitizedDelta.symbol"));
assert(rtSrc.includes("delete sanitizedDelta.market"));
assert(rtSrc.includes("delete sanitizedDelta.name"));
assert(rtSrc.includes("delete sanitizedDelta.currency"));
assert(rtSrc.includes("delete sanitizedDelta.tv_symbol"));

// D6A-02: closeStockDetailDrawer aborts & destroys moved canvases; skipInlineDraw in chart.js/ui.js
const uiSrc = fs.readFileSync("static/js/ui.js", "utf8");
const chartSrc = fs.readFileSync("static/js/chart.js", "utf8");
assert(uiSrc.includes("currentDrawerActiveWrapper._chartAbortController?.abort()"));
assert(uiSrc.includes("currentDrawerActiveWrapper.__detailsReqController?.abort()"));
assert(uiSrc.includes("skipInlineDraw: !hasVisibleInlineOrDrawer"));
assert(chartSrc.includes("if (!options?.skipInlineDraw)"));

// D6A-03: updateStocksFromSseData runs diffUpdateMarket on non-reconciled markets
const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
const updateSseFn = extractFn(apiSrc, "function updateStocksFromSseData(");
const renderedMarkets = [];
const updatedSymbols = [];
const usWrappers = new Map([["us:AAPL", [{ dataset: { stockKey: "us:AAPL" } }]]]);
const sseCtx = {
  document: {
    querySelector: () => null,
    querySelectorAll: () => [{ dataset: { stockKey: "us:AAPL" } }],
    getElementById: (id) => ({
      querySelectorAll: () => (id === "us-stocks" ? [{ dataset: { stockKey: "us:AAPL" } }] : []),
    }),
  },
  _prepareSseIncomingStocks: () => ({
    incomingData: {
      us: [{ symbol: "AAPL", price: 200 }],
      jp: [{ symbol: "7203.T", price: 3000 }],
      idx: [],
    },
    removedEntries: [],
  }),
  mergeStocksWithExistingHistory: (inc) => inc,
  state: {
    stocks: {
      us: [{ symbol: "AAPL", market: "us", price: 200 }],
      jp: [{ symbol: "7203.T", market: "jp", price: 3000 }],
      idx: [],
    },
    updateStocks(next) { this.stocks = next; },
  },
  makeStockKey: (m, s) => `${m}:${s}`,
  renderStocks: (m) => renderedMarkets.push(m),
  stockHashMap: new Map(),
  stockUpdateSignature: (s) => `${s.price}`,
  findAllWrappersByStockKey: (k) => usWrappers.get(k) || [],
  updateExistingCard: (_w, s) => updatedSymbols.push(s.symbol),
  sseState: { skeletonShownAt: 0 },
  INITIAL_SKELETON_MAX_WAIT_MS: 5000,
  Set,
  Date,
};
vm.runInNewContext(updateSseFn + "; this.updateStocksFromSseData = updateStocksFromSseData;", sseCtx);
sseCtx.updateStocksFromSseData({ stream_event: "diff", stocks: {} });
assert.deepStrictEqual(renderedMarkets, ["jp"]);
assert(updatedSymbols.includes("AAPL"), "us should still get differential update when jp reconciles");

// D6A-04: searchStocks passes timeoutMs & renders timeout message
assert(apiSrc.includes("timeoutMs: CONSTANTS.TIMEOUT.SEARCH"));
assert(apiSrc.includes("検索がタイムアウトしました。"));

// D6A-05: renderPortfolio clears .no-results
const renderPortfolioFn = extractFn(uiSrc, "function renderPortfolio(");
{
  let staleRemoved = false;
  const staleBanner = { remove() { staleRemoved = true; } };
  const mockWrapper = { dataset: { stockKey: "us:AAPL" }, closest: () => true };
  const portfolioCtx = {
    DOM: {
      get: (id) =>
        id === "portfolio-stocks"
          ? {
              querySelectorAll: (sel) => {
                if (sel === ":scope > .no-results") return [staleBanner];
                return [];
              },
            }
          : null,
    },
    document: { getElementById: () => null },
    getAllStocks: () => [{ symbol: "AAPL", market: "us", shares: 5, avg_price: 100, price: 120 }],
    toFiniteNumber: (v, fb) => (typeof v === "number" && Number.isFinite(v) ? v : fb),
    makeStockKey: (m, s) => `${m}:${s}`,
    wrapperRegistryMap: new Map([["us:AAPL", new Set([mockWrapper])]]),
    updateExistingCard: () => {},
    renderFavorites: () => {},
    drawPortfolioSummaryChart: () => {},
    updateStockUI: () => {},
    updateTabCounts: () => {},
    Set,
  };
  vm.runInNewContext(renderPortfolioFn + "; this.renderPortfolio = renderPortfolio;", portfolioCtx);
  portfolioCtx.renderPortfolio();
  assert(staleRemoved, "renderPortfolio should remove stale .no-results placeholder when holdings exist");
}

// D6A-06: updateStockUI updates fullscreen canvas even when inline & drawer are closed
const updateUiFn = extractFn(uiSrc, "function updateStockUI(");
let fsUpdated = false;
const rafQueue = [];
const fsModalEl = {
  classList: { contains: () => false },
  dataset: { stockKey: "us:AAPL" },
};
const uiCtx = {
  document: {
    getElementById: (id) => {
      if (id === "chart-fullscreen-modal") return fsModalEl;
      if (id === "fs-chart-canvas") return { id: "fs-chart-canvas" };
      return null;
    },
    querySelector: () => null,
  },
  state: { stocks: { us: [{ symbol: "AAPL", market: "us", price: 155 }] } },
  makeStockKey: (m, s) => `${m}:${s}`,
  stockRenderHash: (s) => `${s.price}`,
  stockUpdateSignature: () => "sig",
  hasFreshSparklineData: () => false,
  stockRealtimeUpdateAt: new Map(),
  checkAlerts: () => {},
  updateOpenStockDetailDrawerHeader: () => {},
  setSparklineVisibility: () => {},
  formatPrice: () => "$155",
  formatStockChangeMetrics: () => ({
    cls: "pos", arrow: "▲", sign: "+", ariaPrefix: "上昇", changeText: "+1", pctText: "+1%"
  }),
  clearInlineSvgContent: () => {},
  createPriceChangeArrowSvg: () => ({}),
  formatVolume: () => "100",
  applyPriceFlash: () => {},
  renderPortfolioCardContent: () => {},
  getChartPref: () => "3mo",
  chartInstances: new Map([[{ id: "fs-chart-canvas" }, {}]]),
  updateChartLastPoint: () => { fsUpdated = true; },
  isStockDetailsCacheFresh: () => false,
  requestAnimationFrame: (cb) => rafQueue.push(cb),
  parseInt,
  Date,
};
// Ensure Map.get finds fsCanvas consistently
const sharedFsCanvas = { id: "fs-chart-canvas" };
uiCtx.document.getElementById = (id) => {
  if (id === "chart-fullscreen-modal") return fsModalEl;
  if (id === "fs-chart-canvas") return sharedFsCanvas;
  return null;
};
uiCtx.chartInstances = new Map([[sharedFsCanvas, { data: { datasets: [{ data: [1] }] } }]]);
vm.runInNewContext(updateUiFn + "; this.updateStockUI = updateStockUI;", uiCtx);
uiCtx.updateStockUI(
  {
    isConnected: true,
    dataset: { stockKey: "us:AAPL", marketContext: "us" },
    querySelector: () => null,
    classList: { contains: () => false },
  },
  { symbol: "AAPL", market: "us", price: 155 }
);
rafQueue.forEach((fn) => fn());
assert(fsUpdated, "fullscreen modal should receive live update when detail and drawer are closed");

// D6A-07: api_client.ts/js detects stuck CONNECTING SSE via watchdog & api.js sets ticket timeoutMs
const tsSrc = fs.readFileSync("static/js/api_client.ts", "utf8");
const compiledJs = fs.readFileSync("static/js/api_client.js", "utf8");
assert(tsSrc.includes("Initial connect timeout"), "api_client.ts must have initial connect watchdog");
assert(compiledJs.includes("Initial connect timeout"), "api_client.js must be compiled with initial connect watchdog");
assert(apiSrc.includes("timeoutMs:") && apiSrc.includes("/api/stocks/stream/ticket"));
{
  let nowMs = 1000;
  let intervalCb = null;
  let reconnectTimers = 0;
  class StuckEventSource {
    constructor() { this.readyState = 0; }
    addEventListener() {}
    removeEventListener() {}
    close() {}
  }
  const fakeDate = class extends Date { static now() { return nowMs; } };
  const acCtx = {
    console: { debug() {}, info() {}, warn() {}, error() {} },
    window: { addEventListener() {}, removeEventListener() {} },
    document: { hidden: false, addEventListener() {} },
    EventSource: StuckEventSource,
    DOMException,
    setTimeout: () => { reconnectTimers++; return reconnectTimers; },
    clearTimeout: () => {},
    setInterval: (fn) => { intervalCb = fn; return 1; },
    clearInterval: () => {},
    Date: fakeDate, Math, Number, Error, TypeError, Object, Array, Promise, JSON, Map, WeakMap, Set,
  };
  acCtx.globalThis = acCtx;
  vm.runInNewContext(compiledJs, acCtx);
  const client = new acCtx.window.APIClient("/api", { sseHeartbeatTimeout: 45000, watchdogInterval: 10000 });
  client.openSSE("/stocks/stream", () => {}, () => {}, { autoReconnect: true });
  assert(typeof intervalCb === "function", "watchdog interval should be registered");
  for (let tick = 0; tick < 5; tick++) {
    nowMs += 10000;
    intervalCb();
  }
  assert.strictEqual(reconnectTimers, 1, "stuck CONNECTING EventSource should trigger reconnect via watchdog");
}

// D6A-08: TradingViewManager removes widget in clearContainer and guards error handler
const tvSrc = fs.readFileSync("static/js/tradingview_manager.js", "utf8");
assert(tvSrc.includes("activeChartWidget.remove()"));
assert(tvSrc.includes("target.clearGen !== curClearGen") && tvSrc.includes("isConnected === false"));
assert(tvSrc.includes("_pendingErrorTargets.set(containerId, pendingTarget)"));

// D6A-09 & D6A-10: ensureStockDetails timeout banner + finally clear & chart retry clearTimeout(timeoutId) + TimeoutError
assert(uiSrc.includes("if (timedOut) {"));
assert(uiSrc.includes("wrapper.__detailsReqController = null;"));
assert(chartSrc.includes("clearTimeout(timeoutId);\n          const retryController = new AbortController();") || chartSrc.includes("clearTimeout(timeoutId);"));
const timeoutErrMatches = chartSrc.match(/timeoutErr\.name = "TimeoutError"/g) || [];
assert(timeoutErrMatches.length >= 3, "expected TimeoutError name on all polling exhaustion paths");

// D6A-11: formatIndexNumber and independent change/percent parsing
const fmtFn = extractFn(apiSrc, "const _parseFiniteIndexMetric =") + "\n" + extractFn(apiSrc, "const formatIndexNumber =");
const updateChipFn = extractFn(apiSrc, "function updateSingleIndexChip(");
const chgEl = { className: "", textContent: "" };
const chipCtx = { Number, NaN, console, document: {}, requestAnimationFrame: (cb) => cb(), clearTimeout, setTimeout };
vm.runInNewContext(fmtFn + "\n" + updateChipFn + "\nthis.formatIndexNumber = formatIndexNumber; this.updateSingleIndexChip = updateSingleIndexChip;", chipCtx);
assert.strictEqual(chipCtx.formatIndexNumber(""), "--");
assert.strictEqual(chipCtx.formatIndexNumber(true), "--");
assert.strictEqual(chipCtx.formatIndexNumber(NaN), "--");
assert.strictEqual(chipCtx.formatIndexNumber(0), "0");
chipCtx.updateSingleIndexChip(
  { querySelector: (s) => (s === ".index-change" ? chgEl : null) },
  { price: 100, change: null, percent: -1.5 }
);
assert.strictEqual(chgEl.className, "index-change neg");
assert.strictEqual(chgEl.textContent, "-- (-1.5%)");
"""
    _run_node(script)
