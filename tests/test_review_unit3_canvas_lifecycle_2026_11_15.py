"""Regression tests for Unit 3: canvas null guards, fullscreen chart lifecycle, and error extraction."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node(script: str, expected_marker: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for JavaScript unit tests")
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
    assert result.stdout.strip().endswith(expected_marker), (
        f"Expected {expected_marker}, got STDOUT={result.stdout!r} STDERR={result.stderr!r}"
    )


_NODE_PRELUDE = r"""
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let __finished = false;
process.on("beforeExit", () => {
  if (!__finished) {
    console.error("Node exited before async test completed (dangling promise)");
    process.exit(1);
  }
});

function readRel(relPath) {
  return fs.readFileSync(path.join(process.cwd(), relPath), "utf8");
}

function extractNamedBlock(src, marker) {
  const start = src.indexOf(marker);
  assert(start !== -1, "Missing marker: " + marker);
  const declStart = src.lastIndexOf("\n", start) + 1;
  let i = src.indexOf("(", start);
  let parens = 0;
  let inParen = false;
  for (; i < src.length; i++) {
    if (src[i] === "(") { parens++; inParen = true; }
    else if (src[i] === ")") { parens--; if (inParen && parens === 0) break; }
  }
  const open = src.indexOf("{", i);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") {
      depth--;
      if (depth === 0) return src.slice(declStart, j + 1);
    }
  }
  throw new Error("Unbalanced block for " + marker);
}

function makeElement(tag = "div") {
  const listeners = new Map();
  const classes = new Set();
  const children = [];
  const attrs = {};
  let _className = "";
  const el = {
    tagName: String(tag).toUpperCase(),
    type: "",
    id: "",
    title: "",
    disabled: false,
    hidden: false,
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    width: 300,
    height: 150,
    clientWidth: 300,
    clientHeight: 150,
    offsetWidth: 300,
    offsetHeight: 150,
    dataset: {},
    style: { setProperty() {} },
    children,
    parentElement: null,
    classList: {
      add(...items) {
        items.forEach((c) => c && classes.add(c));
        _className = Array.from(classes).join(" ");
      },
      remove(...items) {
        items.forEach((c) => classes.delete(c));
        _className = Array.from(classes).join(" ");
      },
      contains(c) { return classes.has(c); },
      toggle(c, force) {
        const next = force === undefined ? !classes.has(c) : !!force;
        if (next) classes.add(c); else classes.delete(c);
        _className = Array.from(classes).join(" ");
        return next;
      },
    },
    get className() { return _className; },
    set className(val) {
      _className = String(val || "");
      classes.clear();
      _className.split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    removeAttribute(k) { delete attrs[k]; },
    appendChild(child) {
      if (child && child.tagName === "FRAGMENT") {
        child.children.forEach((c) => { c.parentElement = el; children.push(c); });
      } else if (child) {
        child.parentElement = el;
        children.push(child);
      }
      return child;
    },
    replaceChildren(...nodes) {
      children.length = 0;
      nodes.forEach((n) => this.appendChild(n));
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const fns = listeners.get(type);
      if (fns && fn) {
        const idx = fns.indexOf(fn);
        if (idx !== -1) fns.splice(idx, 1);
      }
    },
    dispatchEvent(type, eventObj = {}) {
      const fns = listeners.get(type) || [];
      return Promise.all(fns.map((fn) => fn({ stopPropagation() {}, preventDefault() {}, target: el, currentTarget: el, ...eventObj })));
    },
    querySelectorAll(sel) {
      return findAll(el, (node) => matchesSimpleSelector(node, sel));
    },
    querySelector(sel) {
      return this.querySelectorAll(sel)[0] || null;
    },
    contains(target) {
      return target === el || findAll(el, (c) => c === target).length > 0;
    },
    remove() {},
    focus() {},
    getContext() { return null; },
    getBoundingClientRect() { return { width: 300, height: 150, left: 0, top: 0 }; },
  };
  return el;
}

function matchesSimpleSelector(node, sel) {
  if (!node || !sel) return false;
  for (const part of sel.split(",").map((s) => s.trim()).filter(Boolean)) {
    const attrMatch = part.match(/^\[([a-zA-Z0-9_-]+)\]$/);
    if (attrMatch && node.dataset && attrMatch[1].startsWith("data-")) {
      const dsKey = attrMatch[1].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      if (node.dataset[dsKey] !== undefined || node.getAttribute(attrMatch[1]) !== null) return true;
    }
    if (part.startsWith(".") && node.classList && node.classList.contains(part.slice(1))) return true;
    if (part.toUpperCase() === node.tagName) return true;
  }
  return false;
}

function findAll(root, predicate, out = []) {
  if (!root || typeof root !== "object") return out;
  if (predicate(root)) out.push(root);
  if (Array.isArray(root.children)) {
    for (const c of root.children) findAll(c, predicate, out);
  }
  return out;
}
"""


def test_fullscreen_toolbar_awaits_refresh_and_guards_stale_requests():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  const uiSrc = readRel("static/js/ui.js");
  const openBlock = extractNamedBlock(uiSrc, "function openFullscreenChart(wrapper)");

  const modal = makeElement("div");
  modal.classList.add("hidden");
  const symbolEl = makeElement("span");
  const nameEl = makeElement("span");
  const priceEl = makeElement("span");
  const toolbar = makeElement("div");
  const canvas = makeElement("canvas");
  canvas.parentElement = makeElement("div");
  const fsContainer = makeElement("div");
  const elementsById = {
    "chart-fullscreen-modal": modal,
    "fs-stock-symbol": symbolEl,
    "fs-stock-name": nameEl,
    "fs-stock-price": priceEl,
    "fs-chart-toolbar": toolbar,
    "fs-chart-canvas": canvas,
    "fs-chart-container": fsContainer,
  };

  const prefs = {};
  const historyMap = new Map();
  const drawCalls = [];
  const pendingRefreshes = [];

  const ctx = {
    Number,
    String,
    Promise,
    Date,
    Math,
    console,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (fn) => fn(),
    window: { innerWidth: 1280, TradingViewManager: null },
    document: {
      activeElement: null,
      createElement: (tag) => makeElement(tag),
      getElementById: (id) => elementsById[id] || null,
    },
    createEl: (tag, className, text) => {
      const e = makeElement(tag);
      if (className) e.className = className;
      if (text != null) e.textContent = text;
      return e;
    },
    getSseMode: () => 2,
    CONSTANTS: { PERIODS: ["1mo", "3mo", "6mo", "1y", "5y"] },
    lockBodyScroll: () => {},
    getStockByKey: () => ({ symbol: "AAPL", market: "us", name: "Apple", price: 200 }),
    currencyPrefixFromCode: () => "$",
    formatPrice: (p) => "$" + p,
    getChartPref: (_key, pref, def) => (pref in prefs ? prefs[pref] : def),
    setChartPref: (_key, pref, val) => { prefs[pref] = val; },
    getSupportedIntervalsForPeriod: () => ["1d", "1wk"],
    updateIntervalControlsVisibility: () => {},
    getFreshPrefetchedHistory: (_key, period, interval) =>
      historyMap.get(`${period}:${interval}`) || null,
    refreshStockChart: (_wrapper, period, interval) =>
      new Promise((resolve) => {
        pendingRefreshes.push({
          period,
          interval,
          resolve: () => {
            historyMap.set(`${period}:${interval}`, {
              formattedData: [{ x: 1, y: 10 }, { x: 2, y: 20 }],
              ohlcData: [],
            });
            resolve();
          },
        });
      }),
    drawChart: (_wrapper, _formatted, _ohlc, opts) => {
      drawCalls.push({ period: opts.period, interval: opts.interval });
    },
    triggerAiTechnicalLines: async () => {},
  };

  vm.runInNewContext(openBlock + "; this.openFullscreenChart = openFullscreenChart;", ctx);

  const wrapper = makeElement("div");
  wrapper.dataset.stockKey = "us:AAPL";
  wrapper.__stockData = { symbol: "AAPL", market: "us", name: "Apple", price: 200 };
  historyMap.set("3mo:auto", { formattedData: [{ x: 1, y: 1 }, { x: 2, y: 2 }], ohlcData: [] });

  ctx.openFullscreenChart(wrapper);
  assert.strictEqual(drawCalls.length, 1);

  // Scenario 1: Slow (>50ms) async refreshStockChart on ind_rsi toggle must still draw upon resolve
  const rsiBtn = toolbar.querySelectorAll("button").find((b) => b.dataset && b.dataset.ind === "ind_rsi");
  assert(rsiBtn, "RSI toolbar button must exist");
  await rsiBtn.dispatchEvent("click");
  assert.strictEqual(pendingRefreshes.length, 1);
  await new Promise((r) => setTimeout(r, 70));
  assert.strictEqual(drawCalls.length, 1, "must wait for refreshStockChart instead of fixed 50ms timeout");
  pendingRefreshes[0].resolve();
  await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(drawCalls.length, 2, "should draw after slow refresh resolves");

  // Scenario 2: Rapid out-of-order indicator clicks -> old request finishing late must be ignored
  const macdBtn = toolbar.querySelectorAll("button").find((b) => b.dataset && b.dataset.ind === "ind_macd");
  assert(macdBtn, "MACD toolbar button must exist");
  prefs.period = "1mo";
  await rsiBtn.dispatchEvent("click"); // pendingRefreshes[1]
  prefs.period = "6mo";
  await macdBtn.dispatchEvent("click"); // pendingRefreshes[2]
  pendingRefreshes[2].resolve();
  await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(drawCalls.length, 3);
  assert.strictEqual(drawCalls[2].period, "6mo");
  pendingRefreshes[1].resolve();
  await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(drawCalls.length, 3, "stale toolbar request must not overwrite newer draw");

  // Scenario 3: Close and reopen SAME stockKey -> in-flight request from previous session must be ignored
  await rsiBtn.dispatchEvent("click"); // pendingRefreshes[3]
  modal.classList.add("hidden");
  ctx.openFullscreenChart(wrapper); // bumps fsToolbarReqId even with identical stockKey
  const drawsAtReopen = drawCalls.length;
  pendingRefreshes[3].resolve();
  await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(drawCalls.length, drawsAtReopen, "reopened same stockKey must drop stale session redraw");

  __finished = true;
  console.log("FULLSCREEN LIFECYCLE PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "FULLSCREEN LIFECYCLE PASS")


def test_null_2d_canvas_contexts_fail_safely():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  // 1. ui.js: drawPortfolioSummaryChart, drawSectorPieChart, drawSparkline
  const uiSrc = readRel("static/js/ui.js");
  const isUsdBlock = extractNamedBlock(uiSrc, "function isUsdHolding(stock)");
  const summaryBlock = extractNamedBlock(uiSrc, "function drawPortfolioSummaryChart(holdings)");
  const sectorBlock = extractNamedBlock(uiSrc, "function drawSectorPieChart(holdings, usdJpyRate, chartAnimationControl)");
  const sparkBlock = extractNamedBlock(uiSrc, "const drawSparkline = (wrapper, data) =>");

  const nullCanvas = makeElement("canvas");
  nullCanvas.getContext = () => null;
  let newChartCalled = false;
  function ChartCtor() { newChartCalled = true; }

  const uiCtx = {
    Number,
    Math,
    Object,
    Array,
    Date,
    Set,
    Map,
    isNaN,
    DOM: { get: () => nullCanvas },
    document: { getElementById: () => nullCanvas },
    portfolioFixedExchangeRate: 150,
    state: { exchangeRate: 150 },
    lastPfChartSignature: null,
    makeStockKey: (m, s) => `${m}:${s}`,
    buildLocalDateKey: (d) => d.toISOString().slice(0, 10),
    toFiniteNumber: (v, d = 0) => (Number.isFinite(Number(v)) ? Number(v) : d),
    resolvePortfolioChartAnimationControl: () => ({ updateMode: "none", duration: 0, animation: false }),
    getCurrencyRate: () => 150,
    calculatePortfolioMetrics: () => ({ totalValue: 110, totalCost: 90, gain: 20, gainPercent: 22.2, bySector: { Tech: 110 } }),
    updatePortfolioHeader: () => {},
    getSparklineSignature: () => null,
    sparklineSignatureMap: new Map(),
    destroyChart: () => {},
    setSparklineVisibility: () => {},
    pfSummaryChartInstance: null,
    pfSectorChartInstance: null,
    Chart: ChartCtor,
    window: { devicePixelRatio: 1 },
    currentFxRate: 150,
    DEFAULT_USD_JPY_RATE: 150,
  };
  vm.runInNewContext(
    isUsdBlock + "\n" + summaryBlock + "\n" + sectorBlock + "\n" + sparkBlock +
    "; this.drawPortfolioSummaryChart = drawPortfolioSummaryChart; this.drawSectorPieChart = drawSectorPieChart; this.drawSparkline = drawSparkline;",
    uiCtx,
  );
  uiCtx.drawPortfolioSummaryChart([
    {
      symbol: "AAPL",
      market: "us",
      currency: "USD",
      shares: 1,
      price: 110,
      avg_price: 90,
      sector: "Tech",
      chart_data: [{ x: "2026-10-01", c: 100 }, { x: "2026-10-02", c: 110 }],
    },
  ]);
  uiCtx.drawSectorPieChart(
    [{ symbol: "AAPL", market: "us", currency: "USD", shares: 1, price: 110, avg_price: 90, sector: "Tech" }],
    150,
    null,
  );
  const sparkWrap = makeElement("div");
  sparkWrap.dataset.stockKey = "us:AAPL";
  sparkWrap.__stockData = { change: 1.5 };
  sparkWrap.querySelector = (sel) => (sel === ".spark-canvas" ? nullCanvas : null);
  uiCtx.drawSparkline(sparkWrap, [100, 102, 105]);
  assert.strictEqual(newChartCalled, false, "Chart must not be instantiated when getContext('2d') is null");

  // 2. chart.js: drawChart & drawPnLChart
  const chartSrc = readRel("static/js/chart.js");
  const drawChartBlock = extractNamedBlock(chartSrc, "function drawChart(wrapper, data, ohlcData, options = {})");
  const drawPnLBlock = extractNamedBlock(chartSrc, "function drawPnLChart(canvas, data, avgPrice, options = {})");
  let destroyedCount = 0;
  const chartWrapper = makeElement("div");
  chartWrapper.dataset.stockKey = "us:AAPL";
  const qCanvas = makeElement("canvas");
  qCanvas.getContext = () => null;
  chartWrapper.querySelector = (sel) => (sel === ".chart-canvas" ? qCanvas : null);

  const chartCtx = {
    Number,
    Math,
    Date,
    getChartAnimationEnabled: () => false,
    _safeDestroyChart: () => { destroyedCount++; },
    chartErrorHosts: () => [],
  };
  vm.runInNewContext(drawChartBlock + "\n" + drawPnLBlock + "; this.drawChart = drawChart; this.drawPnLChart = drawPnLChart;", chartCtx);
  chartCtx.drawChart(chartWrapper, [{ x: 1, y: 10 }, { x: 2, y: 20 }], [], {});
  chartCtx.drawPnLChart(nullCanvas, [{ x: 1, y: 10 }, { x: 2, y: 20 }], 5, {});
  assert.strictEqual(destroyedCount, 2, "drawChart and drawPnLChart should safely destroy old chart and return on null 2D ctx");

  // 2b. drawChart & drawPnLChart reject non-finite closes/prices when < 2 valid points remain
  let shownErrors = 0;
  let pnlChartConstructed = false;
  const liveChartCanvas = makeElement("canvas");
  liveChartCanvas.getContext = () => ({});
  chartWrapper.querySelector = (sel) => (sel === ".chart-canvas" ? liveChartCanvas : null);
  const finiteChartCtx = {
    Number,
    Math,
    Date,
    Array,
    Map,
    chartInstances: new Map(),
    getChartPref: (_k, _p, d) => d,
    getDatasetHiddenStateByLabel: () => null,
    getChartAnimationEnabled: () => false,
    _safeDestroyChart: () => { destroyedCount++; },
    showChartError: () => { shownErrors++; },
    NO_HISTORY_MSG: "no history",
    CHART_TOOLTIP_DEFAULTS: {},
    Chart: function () { pnlChartConstructed = true; },
  };
  vm.runInNewContext(drawChartBlock + "\n" + drawPnLBlock + "; this.drawChart = drawChart; this.drawPnLChart = drawPnLChart;", finiteChartCtx);
  finiteChartCtx.drawChart(chartWrapper, [{ x: 1000, c: NaN }, { x: 2000, c: null }, { x: 3000, price: 150 }], [], {});
  assert.strictEqual(shownErrors, 1, "drawChart must show info message when fewer than 2 finite closes remain");
  finiteChartCtx.drawPnLChart(liveChartCanvas, [{ x: 1000, price: NaN }, { x: 2000, price: 120 }], 100, {});
  assert.strictEqual(pnlChartConstructed, false, "drawPnLChart must not render when fewer than 2 finite prices remain");

  // 3. ai_portfolio.js: drawAiPortfolioCharts
  const aiPfSrc = readRel("static/js/ai_portfolio.js");
  const destroyAiChartsBlock = extractNamedBlock(aiPfSrc, "function destroyAiPortfolioCharts()");
  const drawAiChartsBlock = extractNamedBlock(aiPfSrc, "function drawAiPortfolioCharts(portfolio)");
  let aiDestroyed = false;
  let capturedSummaryCfg = null;
  const summaryCanvas = makeElement("canvas");
  summaryCanvas.getContext = () => ({
    createLinearGradient: () => ({ addColorStop: () => {} }),
  });
  const aiCtx = {
    Number,
    Array,
    Math,
    Chart: function (_ctx, cfg) {
      if (cfg?.options?.plugins?.tooltip?.callbacks?.label) {
        capturedSummaryCfg = cfg;
      }
      return { destroy() {} };
    },
    document: {
      getElementById: (id) =>
        id === "ai-pf-summary-canvas" || id === "ai-pf-sector-canvas"
          ? nullCanvas
          : null,
    },
    aiSectorChartInstance: null,
    aiSummaryChartInstance: { destroy() { aiDestroyed = true; } },
    AI_CHART_COLORS: ["#6bb6ff"],
    computeAiSimulation: () => ({ points: [100, 110, 120, 130, 140] }),
    VIRTUAL_BASE_CAPITAL_JPY: 1000000,
  };
  vm.runInNewContext(
    destroyAiChartsBlock + "\n" + drawAiChartsBlock + "; this.drawAiPortfolioCharts = drawAiPortfolioCharts;",
    aiCtx,
  );
  aiCtx.drawAiPortfolioCharts({ items: [{ symbol: "AAPL", weight_pct: 100 }] });
  assert.strictEqual(aiDestroyed, true, "existing AI summary chart must still be destroyed cleanly");
  assert.strictEqual(newChartCalled, false);
  // Also verify negative/zero/positive yen P/L tooltip sign rendering.
  aiCtx.document.getElementById = (id) => (id === "ai-pf-summary-canvas" ? summaryCanvas : nullCanvas);
  aiCtx.drawAiPortfolioCharts({ items: [{ symbol: "AAPL", weight_pct: 100 }] });
  const tipLabel = capturedSummaryCfg.options.plugins.tooltip.callbacks.label;
  const negTip = tipLabel({ datasetIndex: 0, dataset: { label: "AI" }, raw: 950000 });
  const posTip = tipLabel({ datasetIndex: 0, dataset: { label: "AI" }, raw: 1050000 });
  const zeroTip = tipLabel({ datasetIndex: 0, dataset: { label: "AI" }, raw: 1000000 });
  assert(negTip.includes("(-¥50,000 / -5.0%)"), "Negative P/L tooltip must show -¥ without --%: " + negTip);
  assert(posTip.includes("(+¥50,000 / +5.0%)"), "Positive P/L tooltip mismatch: " + posTip);
  assert(zeroTip.includes("(¥0 / 0.0%)"), "Zero P/L tooltip mismatch: " + zeroTip);

  // 4. experimental/orbit-renderer.js: null 2D ctx + scrubbed timeline viewStock changePercent
  const orbitSrc = readRel("static/js/experimental/orbit-renderer.js");
  const orbitGlobal = {
    window: {
      addEventListener: () => {},
      removeEventListener: () => {},
      devicePixelRatio: 1,
      matchMedia: () => ({ matches: false, addEventListener: () => {} }),
      ObservatoryDataAdapter: { formatPrice: (p) => "$" + p },
    },
    ObservatoryDataAdapter: { formatPrice: (p) => "$" + p },
    performance: { now: () => 1000 },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    setTimeout,
    clearTimeout,
    Date,
    Math,
    Number,
    String,
    Array,
    Map,
    Set,
    console,
  };
  orbitGlobal.window.ObservatoryDataAdapter = orbitGlobal.ObservatoryDataAdapter;
  orbitGlobal.globalThis = orbitGlobal;
  vm.runInNewContext(orbitSrc, orbitGlobal);

  const orbitStore = {
    state: { paused: false, timelineDays: 30, stocks: new Map(), isHidden: false },
    getState: () => ({ paused: false }),
    subscribe: () => () => {},
    on: () => {},
  };
  const rendererNull = new orbitGlobal.window.OrbitRenderer(nullCanvas, orbitStore);
  rendererNull.render();

  const colorCalls = [];
  const fake2dCtx = {
    save() {}, restore() {}, translate() {}, scale() {}, clearRect() {},
    beginPath() {}, rect() {}, roundRect() {}, fill() {}, stroke() {},
    fillText() {}, measureText(t) { return { width: String(t || "").length * 6 }; },
  };
  const liveCanvas = makeElement("canvas");
  liveCanvas.getContext = () => fake2dCtx;
  const rendererLive = new orbitGlobal.window.OrbitRenderer(liveCanvas, orbitStore);
  rendererLive.dpr = 1;
  rendererLive.width = 800;
  rendererLive.height = 600;
  rendererLive._resolveTimelineViewStock = () => ({
    symbol: "AAPL",
    market: "us",
    name: "Apple Inc",
    price: 150,
    sector: "Technology",
    changePercent: -4.25,
    currency: "USD",
  });
  const origGetColor = rendererLive.getColorForChange.bind(rendererLive);
  rendererLive.getColorForChange = (pct) => {
    colorCalls.push(pct);
    return origGetColor(pct);
  };
  const liveStock = {
    symbol: "AAPL",
    market: "us",
    name: "Apple Inc",
    price: 200,
    sector: "Technology",
    changePercent: 9.5,
    currency: "USD",
  };
  rendererLive.nodeRenderData.set("AAPL", { currentX: 400, currentY: 300, currentRadius: 20 });
  rendererLive.renderHoverTooltip(fake2dCtx, {
    hoveredSymbol: "AAPL",
    stocks: new Map([["AAPL", liveStock]]),
    timelineDays: 30,
  });
  assert(colorCalls.includes(-4.25), "hover tooltip must use timeline-scrubbed viewStock.changePercent: " + JSON.stringify(colorCalls));

  // 5. experimental/constellation-controller.js: null 2D ctx
  const constSrc = readRel("static/js/experimental/constellation-controller.js");
  const constGlobal = {
    window: {},
    document: { getElementById: () => null, createElement: (tag) => makeElement(tag) },
    Math,
    Number,
    String,
    Array,
    Map,
    Set,
    Promise,
    AbortController,
    Date,
    console,
  };
  constGlobal.window = constGlobal;
  constGlobal.globalThis = constGlobal;
  vm.runInNewContext(constSrc, constGlobal);
  const constChartWrapper = makeElement("div");
  const ctrl = new constGlobal.ConstellationController(
    {
      state: { connectedSymbols: ["AAPL", "MSFT"], stocks: new Map() },
      getState: () => ({ connectedSymbols: ["AAPL", "MSFT"], stocks: new Map() }),
      subscribe: () => () => {},
      on: () => {},
    },
    { chartWrapper: constChartWrapper, chartCanvas: nullCanvas },
  );
  ctrl.renderRelativeReturnChart(["AAPL", "MSFT"], {
    stocks: new Map([
      ["AAPL", { symbol: "AAPL", history: [{ c: 100 }, { c: 110 }] }],
      ["MSFT", { symbol: "MSFT", history: [{ c: 200 }, { c: 210 }] }],
    ]),
  });

  __finished = true;
  console.log("CANVAS GUARDS PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "CANVAS GUARDS PASS")


def test_error_extraction_prioritizes_details_reason():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  // 1. constellation-controller.js compare analysis HTTP error uses details.reason
  const constSrc = readRel("static/js/experimental/constellation-controller.js");
  const aiResultContainer = makeElement("div");
  const aiCompareBtn = makeElement("button");
  const constEls = {
    "constellation-ai-result": aiResultContainer,
    "constellation-ai-compare-btn": aiCompareBtn,
  };
  const constGlobal = {
    document: {
      getElementById: (id) => constEls[id] || null,
      createElement: (tag) => makeElement(tag),
      createTextNode: (t) => ({ nodeType: 3, textContent: String(t ?? "") }),
    },
    createRequestToken: () => "req-token-1",
    csrfFetch: async () => ({
      ok: false,
      status: 400,
      json: async () => ({
        details: { reason: "選択銘柄の履歴データが不足しています" },
        message: "generic msg",
        error: "generic err",
      }),
    }),
    AbortController,
    Math,
    Number,
    String,
    Array,
    Map,
    Set,
    Promise,
    Date,
    console,
  };
  constGlobal.window = constGlobal;
  constGlobal.globalThis = constGlobal;
  vm.runInNewContext(constSrc, constGlobal);
  const ctrl = new constGlobal.ConstellationController(
    {
      state: {
        connectedSymbols: ["AAPL", "MSFT"],
        stocks: new Map([
          ["AAPL", { symbol: "AAPL", market: "us", name: "Apple" }],
          ["MSFT", { symbol: "MSFT", market: "us", name: "Microsoft" }],
        ]),
      },
      getState: () => ({ connectedSymbols: ["AAPL", "MSFT"], stocks: new Map() }),
      subscribe: () => () => {},
      on: () => {},
    },
    { aiResultContainer, aiCompareBtn },
  );
  await ctrl.runAiComparison();
  assert(
    aiResultContainer.children.some((c) => c.textContent.includes("選択銘柄の履歴データが不足しています")),
    "constellation-controller must display details.reason",
  );

  // 2. settings.js verifyMistralBtn and verifyOpenAIBtn both extract details.reason
  const settingsSrc = readRel("static/js/settings.js");
  const domListeners = [];
  const mistralBtn = makeElement("button");
  const mistralStatus = makeElement("div");
  const openaiBtn = makeElement("button");
  const openaiStatus = makeElement("div");
  const settingsEls = {
    "verify-mistral-btn": mistralBtn,
    "mistral-verify-status": mistralStatus,
    "verify-openai-btn": openaiBtn,
    "openai-verify-status": openaiStatus,
    "openai-base-url-input": makeElement("input"),
    "openai-api-key-input": makeElement("input"),
  };
  const settingsCtx = {
    document: {
      addEventListener: (ev, fn) => { if (ev === "DOMContentLoaded") domListeners.push(fn); },
      getElementById: (id) => settingsEls[id] || null,
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: (tag) => makeElement(tag),
      createTextNode: (t) => ({ nodeType: 3, textContent: String(t ?? "") }),
    },
    window: {},
    localStorage: { getItem: () => null, setItem: () => {} },
    navigator: {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    getColorSchemePreference: () => "western",
    initThemeColorScheme: () => {},
    showSettingsMessage: () => {},
    setSafeText: (e, t) => { if (e) e.textContent = String(t ?? ""); },
    CONSTANTS: { refreshColors: () => {} },
    fetch: async () => ({
      ok: true,
      status: 200,
      text: async () => "{}",
      json: async () => ({ ok: true, stocks: { us: [], jp: [] } }),
    }),
    csrfFetch: async (url) => {
      if (String(url).includes("/api/credentials/verify")) {
        return {
          ok: false,
          status: 422,
          json: async () => ({
            ok: false,
            details: { reason: "APIキーの権限スコープが不足しています" },
            error: "generic verify failure",
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    },
    logger: { error: () => {}, warn: () => {}, debug: () => {} },
    showToast: () => {},
    AbortController,
    Promise,
    String,
    Number,
    Array,
    Object,
    Error,
    console,
  };
  settingsCtx.window = settingsCtx;
  settingsCtx.globalThis = settingsCtx;
  vm.runInNewContext(settingsSrc, settingsCtx);
  domListeners.forEach((fn) => fn());

  await mistralBtn.dispatchEvent("click");
  assert(
    mistralStatus.textContent.includes("APIキーの権限スコープが不足しています"),
    "Mistral verify error must surface details.reason: " + mistralStatus.textContent,
  );

  await openaiBtn.dispatchEvent("click");
  assert(
    openaiStatus.textContent.includes("APIキーの権限スコープが不足しています"),
    "OpenAI verify error must surface details.reason: " + openaiStatus.textContent,
  );

  // 3. settings.js readSettingsApiResponse + setup.js getCredentialState + constellation ok=false + background addDetectedStock
  const readRespBlock = extractNamedBlock(settingsSrc, "async function readSettingsApiResponse(res)");
  const settingsErrCtx = { Error, JSON };
  vm.runInNewContext(readRespBlock + "; this.readSettingsApiResponse = readSettingsApiResponse;", settingsErrCtx);
  let settings200Err = null;
  try {
    await settingsErrCtx.readSettingsApiResponse({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ error: "coarse err", details: { reason: "具体的理由: 設定不整合" } }),
    });
  } catch (e) {
    settings200Err = e;
  }
  assert(settings200Err && settings200Err.message === "具体的理由: 設定不整合");

  const setupSrc = readRel("static/js/setup.js");
  const getCredBlock = extractNamedBlock(setupSrc, "async function getCredentialState()");
  const setupErrCtx = {
    Error,
    FAST_FETCH_TIMEOUT_MS: 2000,
    getOrCreateSetupAbortController: () => ({ signal: {} }),
    fetchWithTimeout: async () => ({
      ok: false,
      status: 500,
      json: async () => ({ ok: false, error: "coarse", details: { reason: "キー復号失敗" } }),
    }),
  };
  vm.runInNewContext(getCredBlock + "; this.getCredentialState = getCredentialState;", setupErrCtx);
  let setupErr = null;
  try {
    await setupErrCtx.getCredentialState();
  } catch (e) {
    setupErr = e;
  }
  assert(setupErr && setupErr.message === "キー復号失敗");

  // 4. constellation-controller HTTP 200 ok=false prioritizes details.reason
  let nextComparePayload = { ok: false, details: { reason: "比較対象が上限超過" }, message: "generic", error: "err" };
  constGlobal.csrfFetch = async () => ({ ok: true, status: 200, json: async () => nextComparePayload });
  await ctrl.runAiComparison();
  assert(
    aiResultContainer.children.some((c) => c.textContent.includes("比較対象が上限超過")),
    "constellation ok=false must surface details.reason",
  );

  // 5. chrome_extension/background.js addDetectedStock returns details.reason
  const bgSrc = readRel("chrome_extension/background.js");
  let bgHandler = null;
  const bgListenerCode = bgSrc.slice(bgSrc.indexOf("chrome.runtime.onMessage.addListener("));
  const bgCtx = {
    String,
    chrome: { runtime: { id: "ext-id", onMessage: { addListener: (fn) => { bgHandler = fn; } } } },
    checkHealth: async () => ({ ok: true, base: "http://127.0.0.1:5000" }),
    addStockViaExtension: async () => ({
      res: { ok: false, status: 400 },
      data: { ok: false, error: "Bad Request", details: { reason: "銘柄登録上限に達しています" } },
    }),
    console,
  };
  vm.runInNewContext(bgListenerCode, bgCtx);
  const bgResp = await new Promise((resolve) => {
    bgHandler({ action: "addDetectedStock", symbol: "AAPL", market: "us" }, { id: "ext-id" }, resolve);
  });
  assert.strictEqual(bgResp.ok, false);
  assert.strictEqual(bgResp.error, "銘柄登録上限に達しています");

  __finished = true;
  console.log("ERROR EXTRACT PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "ERROR EXTRACT PASS")
