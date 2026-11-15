"""Regression tests for the 2026-11-15 frontend review fixes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _read(rel_path: str) -> str:
    return (ROOT_DIR / rel_path).read_text(encoding="utf-8")


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


_PRELUDE = r"""
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
  let _className = "";
  const el = {
    tagName: String(tag).toUpperCase(),
    type: "",
    textContent: "",
    dataset: {},
    style: {},
    children,
    classList: {
      add(...items) { items.forEach((c) => c && classes.add(c)); _className = Array.from(classes).join(" "); },
      remove(...items) { items.forEach((c) => classes.delete(c)); _className = Array.from(classes).join(" "); },
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
    appendChild(child) { children.push(child); return child; },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatchEvent(type, eventObj = {}) {
      const fns = listeners.get(type) || [];
      return Promise.all(fns.map((fn) => fn({ stopPropagation() {}, preventDefault() {}, ...eventObj })));
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  return el;
}
"""


def test_failed_refresh_keeps_healthy_board_and_arms_startup_watchdog():
    source = _read("static/js/index_main.js")
    # A failed GET must not wipe a healthy board: the catch path branches on
    # existing cards/state and surfaces a toast instead of the error state.
    assert "if (hasAnyCards || hasStateData)" in source
    assert "表示中のデータを継続します" in source
    # The startup path already has skeletons from connectSSE(), so the
    # watchdog must be armed even when hasSkeleton is true.
    assert "if (!hasSkeleton) renderSkeletons();" in source
    assert "armInitialSkeletonWatchdog();" in source
    # Portfolio save must pass the required (market, stocks) arguments.
    assert "renderStocks(market, state.stocks[market]);" in source
    assert "renderStocks();" not in source


def test_sse_skeleton_and_indices_guards_include_all_markets_and_change_gate():
    source = _read("static/js/api.js")
    assert "state.stocks.idx.length === 0" in source
    assert "lastIndicesSignature" in source
    assert "if (signature === lastIndicesSignature) return;" in source


def test_chart_error_host_prefers_open_fullscreen_modal():
    node_script = (
        _PRELUDE
        + r"""
(async () => {
  const src = readRel("static/js/ui.js");
  const block = extractNamedBlock(src, "function chartErrorHosts(wrapper)");
  const fsHost = makeElement("div");
  const inline = makeElement("div");
  const fsModal = makeElement("div");
  fsModal.dataset.stockKey = "us:AAPL";
  const ctx = {
    document: {
      getElementById: (id) =>
        id === "chart-fullscreen-modal"
          ? fsModal
          : id === "fs-chart-canvas-wrapper"
            ? fsHost
            : null,
    },
    currentDrawerActiveWrapper: null,
  };
  const wrapper = makeElement("div");
  wrapper.dataset.stockKey = "us:AAPL";
  wrapper.querySelector = (sel) =>
    sel === ".chart-container" ? inline : null;
  vm.runInNewContext(block + "; this.chartErrorHosts = chartErrorHosts;", ctx);
  const hosts = ctx.chartErrorHosts(wrapper);
  assert.strictEqual(hosts[0], fsHost, "fullscreen host must be preferred");
  assert(hosts.includes(inline), "inline host must still be cleared");
  __finished = true;
  console.log("FS ERROR HOST PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "FS ERROR HOST PASS")


def test_settings_stock_error_renders_retry_control():
    node_script = (
        _PRELUDE
        + r"""
(async () => {
  const src = readRel("static/js/settings.js");
  const block = extractNamedBlock(src, "function renderStocksError(message)");
  const lists = {
    "us-list": makeElement("ul"),
    "jp-list": makeElement("ul"),
    "idx-list": makeElement("ul"),
  };
  let reloads = 0;
  const ctx = {
    document: {
      getElementById: (id) => lists[id] || null,
      createElement: (tag) => makeElement(tag),
      createTextNode: (text) => ({ textContent: String(text) }),
    },
    loadStocks: () => { reloads += 1; },
  };
  vm.runInNewContext(block + "; this.renderStocksError = renderStocksError;", ctx);
  ctx.renderStocksError("取得に失敗しました");
  for (const list of Object.values(lists)) {
    assert.strictEqual(list.children.length, 1);
    const item = list.children[0];
    assert(item.textContent.includes("取得に失敗しました"));
    const button = item.children.find((c) => c.tagName === "BUTTON");
    assert(button && button.textContent === "再試行");
    await button.dispatchEvent("click");
  }
  assert.strictEqual(reloads, 3, "each market retry button must reload stocks");
  __finished = true;
  console.log("SETTINGS ERROR RETRY PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "SETTINGS ERROR RETRY PASS")


def test_review_api_client_sse_listener_dedup_and_capture_removal():
    node_script = (
        _PRELUDE
        + r"""
(async () => {
  const apiClientSrc = readRel("static/js/api_client.js");
  const ac = new AbortController();
  let registered = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
    }
    addEventListener(type, fn, opts) {
      const cap = typeof opts === "boolean" ? opts : Boolean(opts && opts.capture);
      registered.push({ type, fn, cap, opts });
    }
    removeEventListener(type, fn, opts) {
      const cap = typeof opts === "boolean" ? opts : Boolean(opts && opts.capture);
      registered = registered.filter((r) => !(r.type === type && r.fn === fn && r.cap === cap));
    }
    close() {
      this.readyState = 2;
    }
  }
  const win = {
    location: { origin: "http://127.0.0.1:5000" },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
  };
  const ctx = {
    window: win,
    document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {} },
    navigator: {},
    EventSource: FakeEventSource,
    AbortController,
    URL,
    CustomEvent: class CustomEvent { constructor(t, init) { this.type = t; this.detail = init?.detail; } },
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {},
    console,
  };
  win.APIClient = undefined;
  vm.runInNewContext(apiClientSrc, ctx);
  const Client = ctx.APIClient || win.APIClient;
  assert(Client, "APIClient must load cleanly");
  const client = new Client("http://127.0.0.1:5000");
  const es = client.openSSE("/api/stream", () => {}, () => {});

  let calls = 0;
  const listener = () => { calls += 1; };
  // Duplicate registration with same capture=false must only register once
  es.addEventListener("price", listener, false);
  es.addEventListener("price", listener, { capture: false });
  es.addEventListener("price", listener, { capture: true });
  const priceEntries = registered.filter((r) => r.type === "price");
  assert.strictEqual(priceEntries.length, 2, "must have 1 capture=false and 1 capture=true wrapper");

  // Trigger both wrappers -> listener executes cleanly
  for (const r of [...priceEntries]) r.fn({ lastEventId: "" });
  assert.strictEqual(calls, 2);

  // Removing capture=false must remove ONLY capture=false wrapper, keeping capture=true
  es.removeEventListener("price", listener, false);
  const remaining = registered.filter((r) => r.type === "price");
  assert.strictEqual(remaining.length, 1);
  assert.strictEqual(remaining[0].cap, true, "capture=true wrapper must remain untouched");
  es.removeEventListener("price", listener, { capture: true });
  assert.strictEqual(registered.filter((r) => r.type === "price").length, 0);

  // { once: true } must auto-clean so re-adding works later
  let onceCalls = 0;
  const onceFn = () => { onceCalls += 1; };
  es.addEventListener("tick", onceFn, { once: true });
  const tickEntry = registered.find((r) => r.type === "tick");
  tickEntry.fn({ lastEventId: "" });
  registered = registered.filter((r) => r.type !== "tick");
  es.addEventListener("tick", onceFn, { once: true });
  assert.strictEqual(registered.filter((r) => r.type === "tick").length, 1);
  assert.strictEqual(onceCalls, 1);

  // { signal } abort must clear tracked entry so later re-registration succeeds
  const sigFn = () => {};
  es.addEventListener("abortable", sigFn, { signal: ac.signal });
  assert.strictEqual(registered.filter((r) => r.type === "abortable").length, 1);
  ac.abort();
  registered = registered.filter((r) => r.type !== "abortable");
  es.addEventListener("abortable", sigFn, false);
  assert.strictEqual(registered.filter((r) => r.type === "abortable").length, 1);

  client.closeSSE();
  __finished = true;
  console.log("API CLIENT SSE LISTENER PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "API CLIENT SSE LISTENER PASS")


def test_review_open_stock_detail_drawer_same_wrapper_preserves_dom_and_trigger():
    node_script = (
        _PRELUDE
        + r"""
(async () => {
  const uiSrc = readRel("static/js/ui.js");
  const openBlock = extractNamedBlock(uiSrc, "function openStockDetailDrawer(stock, wrapper)");
  const closeBlock = extractNamedBlock(uiSrc, "function closeStockDetailDrawer()");

  const makeDomNode = (tag, cls) => {
    const el = makeElement(tag);
    el.className = cls || "";
    el.parentNode = null;
    el.isConnected = true;
    el.focus = () => { ctx.document.activeElement = el; };
    el.contains = (node) => {
      let cur = node;
      while (cur) {
        if (cur === el) return true;
        cur = cur.parentNode;
      }
      return false;
    };
    el.replaceChildren = () => {
      for (const c of el.children) c.parentNode = null;
      el.children.length = 0;
    };
    const origAppend = el.appendChild.bind(el);
    el.appendChild = (child) => {
      if (child.parentNode && child.parentNode !== el) {
        const idx = child.parentNode.children.indexOf(child);
        if (idx !== -1) child.parentNode.children.splice(idx, 1);
      }
      child.parentNode = el;
      return origAppend(child);
    };
    el.closest = (sel) => {
      let cur = el;
      while (cur) {
        if (sel === "#stock-detail-drawer" && cur === drawerEl) return cur;
        cur = cur.parentNode;
      }
      return null;
    };
    el.querySelector = (sel) => {
      const parts = sel.split(",").map((s) => s.trim()).filter(Boolean);
      const matchSingleClass = (node, token) => {
        const clean = token.replace(/^\./, "").replace(/:not\(.*\)$/, "");
        if (!node.classList.contains(clean)) return false;
        if (token.includes(":not(.pnl-chart-container)") && node.classList.contains("pnl-chart-container")) return false;
        return true;
      };
      const findDescendant = (root, tokens, idx) => {
        for (const c of root.children) {
          if (matchSingleClass(c, tokens[idx])) {
            if (idx === tokens.length - 1) return c;
            const deeper = findDescendant(c, tokens, idx + 1);
            if (deeper) return deeper;
          }
          const nested = findDescendant(c, tokens, idx);
          if (nested) return nested;
        }
        return null;
      };
      for (const rawPart of parts) {
        if (/^:scope\s*>\s*/.test(rawPart)) {
          const token = rawPart.replace(/^:scope\s*>\s*/, "").trim();
          for (const c of el.children) {
            if (matchSingleClass(c, token)) return c;
          }
        } else {
          const tokens = rawPart.split(/\s+/).filter(Boolean);
          const found = findDescendant(el, tokens, 0);
          if (found) return found;
        }
      }
      return null;
    };
    el.setAttribute = (k, v) => { el.attrs = el.attrs || {}; el.attrs[k] = String(v); };
    el.getAttribute = (k) => (el.attrs || {})[k] ?? null;
    el.removeAttribute = (k) => { if (el.attrs) delete el.attrs[k]; };
    return el;
  };

  const overlay = makeDomNode("div");
  const drawerEl = makeDomNode("aside");
  drawerEl.id = "stock-detail-drawer";
  const chartContent = makeDomNode("div");
  const aiContent = makeDomNode("div");
  const wrapper = makeDomNode("div", "stock-wrapper");
  wrapper.dataset.stockKey = "us:AAPL";
  const detailPanel = makeDomNode("div", "detail-panel");
  const detailInner = makeDomNode("div", "detail-inner");
  const detailInfo = makeDomNode("div", "detail-info");
  const chartContainer = makeDomNode("div", "chart-container");
  const aiSection = makeDomNode("div", "ai-section");
  const openBtn = makeDomNode("button");
  const insideBtn = makeDomNode("button");

  detailInner.appendChild(detailInfo);
  detailInner.appendChild(chartContainer);
  detailInner.appendChild(aiSection);
  detailPanel.appendChild(detailInner);
  wrapper.appendChild(detailPanel);
  drawerEl.appendChild(insideBtn);

  class FakeHTMLElement {}
  Object.setPrototypeOf(openBtn, FakeHTMLElement.prototype);
  Object.setPrototypeOf(insideBtn, FakeHTMLElement.prototype);

  let closeCalls = 0;
  const ctx = {
    currentDrawerActiveWrapper: null,
    stockDetailDrawerTrigger: null,
    stockDetailDrawerRefreshTimer: null,
    HTMLElement: FakeHTMLElement,
    state: { stocks: { us: [] } },
    document: {
      activeElement: openBtn,
      getElementById: (id) => ({
        "stock-detail-drawer-overlay": overlay,
        "stock-detail-drawer": drawerEl,
        "drawerTabChartContent": chartContent,
        "drawerTabAiContent": aiContent,
      }[id] || null),
      querySelectorAll: () => [],
      createElement: (tag) => makeDomNode(tag),
    },
    renderStockDetailDrawerHeader: () => {},
    getLatestStockForDrawer: (s) => s,
    switchDrawerTab: () => {},
    renderAiTechnicalLinesSummary: () => {},
    makeStockKey: (m, s) => `${m}:${s}`,
    getChartPref: (_k, _p, def) => def,
    refreshStockChart: () => {},
    ensureStockDetails: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    unlockBodyScroll: () => {},
    lockBodyScroll: () => {},
    resetDetailTabsToDefault: () => {},
    createEl: (tag, cls) => makeDomNode(tag, cls),
    appendChildren: (p, items) => items.forEach((c) => p.appendChild(c)),
  };
  vm.runInNewContext(
    "const _origCloseFn = " + closeBlock + "\nfunction closeStockDetailDrawer() { globalThis.__closeCount = (globalThis.__closeCount || 0) + 1; return _origCloseFn(); }\n" + openBlock + "; this.openStockDetailDrawer = openStockDetailDrawer; this.closeStockDetailDrawer = closeStockDetailDrawer;",
    ctx,
  );
  // Simpler wrapper around closeStockDetailDrawer inside VM:
  ctx.closeStockDetailDrawer = (() => {
    const orig = ctx.closeStockDetailDrawer;
    return () => { closeCalls += 1; return orig(); };
  })();

  const stock = { symbol: "AAPL", market: "us", price: 200 };
  ctx.openStockDetailDrawer(stock, wrapper);
  assert.strictEqual(chartContent.children.includes(chartContainer), true);
  assert.strictEqual(aiContent.children.includes(aiSection), true);
  assert.strictEqual(ctx.stockDetailDrawerTrigger, openBtn);

  // Move focus inside drawer, then reopen SAME wrapper
  insideBtn.focus();
  ctx.openStockDetailDrawer({ symbol: "AAPL", market: "us", price: 205 }, wrapper);
  assert.strictEqual(closeCalls >= 1, true, "reopening same wrapper must call closeStockDetailDrawer first");
  assert.strictEqual(detailInfo.parentNode, chartContent, "detailInfo must remain mounted inside drawer, not destroyed");
  assert.strictEqual(chartContainer.parentNode, chartContent, "chartContainer must remain mounted inside drawer, not destroyed");
  assert.strictEqual(aiSection.parentNode, aiContent, "aiSection must remain mounted inside drawer, not destroyed");
  assert.strictEqual(ctx.stockDetailDrawerTrigger, openBtn, "original external trigger must be preserved");
  __finished = true;
  console.log("SAME WRAPPER DRAWER PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "SAME WRAPPER DRAWER PASS")


def test_review_focus_trap_inert_ancestors_hidden_and_links():
    node_script = (
        _PRELUDE
        + r"""
(async () => {
  const utilsSrc = readRel("static/js/utils.js");
  const uiSrc = readRel("static/js/ui.js");

  const makeVisNode = (attrs = {}, rect = { width: 10, height: 10 }, parent = null) => ({
    attrs,
    parent,
    offsetParent: {},
    hasAttribute(name) { return Boolean(this.attrs[name]); },
    closest(sel) {
      let cur = this;
      while (cur) {
        if (sel === "[inert]" && cur.attrs.inert) return cur;
        if (sel === "[hidden]" && cur.attrs.hidden) return cur;
        cur = cur.parent;
      }
      return null;
    },
    getBoundingClientRect() { return rect; },
  });

  const isEffBlock = extractNamedBlock(utilsSrc, "function isEffectivelyVisible(el)");
  const isUiBlock = extractNamedBlock(uiSrc, "function isUiElementVisible(el)");

  const inertParent = makeVisNode({ inert: true });
  const hiddenParent = makeVisNode({ hidden: true });
  const childInHidden = makeVisNode({}, { width: 10, height: 10 }, hiddenParent);
  const childNormal = makeVisNode({});
  const childInInert = makeVisNode({}, { width: 10, height: 10 }, inertParent);

  const ctx = {
    window: { getComputedStyle: () => ({ display: "block", visibility: "visible" }) },
    document: { body: {} },
  };
  vm.runInNewContext(isEffBlock + "\n" + isUiBlock + "\nthis.isEffectivelyVisible = isEffectivelyVisible; this.isUiElementVisible = isUiElementVisible;", ctx);

  assert.strictEqual(ctx.isEffectivelyVisible(childNormal), true);
  assert.strictEqual(ctx.isEffectivelyVisible(childInHidden), false, "child inside [hidden] ancestor must not be visible");
  assert.strictEqual(ctx.isUiElementVisible(childInHidden), false, "isUiElementVisible must respect [hidden] ancestor too");
  assert.strictEqual(!childInInert.hasAttribute("inert") && !childInInert.closest("[inert]"), false);

  // Verify openModal focus trap, trapDrawerFocus, and openFullscreenChart Tab handler include closest('[inert]') & a[href]
  const trapModalBlock = extractNamedBlock(utilsSrc, "function openModal(modalId, onOpenCallback)");
  const trapDrawerBlock = extractNamedBlock(uiSrc, "function trapDrawerFocus(event, drawerOverlay)");
  const fsBlock = extractNamedBlock(uiSrc, "function openFullscreenChart(wrapper)");
  assert(trapModalBlock.includes('closest?.("[inert]")'), "openModal focus trap must check ancestor [inert]");
  assert(trapDrawerBlock.includes('closest?.("[inert]")'), "trapDrawerFocus must check ancestor [inert]");
  assert(fsBlock.includes("a[href]") && fsBlock.includes('closest?.("[inert]")'), "fullscreen Tab trap must include a[href] and ancestor [inert]");

  __finished = true;
  console.log("FOCUS TRAP INERT HIDDEN PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "FOCUS TRAP INERT HIDDEN PASS")


def test_review_resolve_card_chart_params_retry_and_storage_and_heatmap():
    node_script = (
        _PRELUDE
        + r"""
(async () => {
  const uiSrc = readRel("static/js/ui.js");
  const chartSrc = readRel("static/js/chart.js");
  const indexMainSrc = readRel("static/js/index_main.js");
  const heatmapSrc = readRel("static/js/heatmap.js");

  // 1. resolveCardChartParams
  const resolveBlock = extractNamedBlock(uiSrc, "function resolveCardChartParams(wrapper)");
  const uiCtx = {
    getChartPref: (_k, field, def) => (field === "period" ? "1y" : field === "interval" ? "1wk" : def),
  };
  vm.runInNewContext(resolveBlock + "; this.resolveCardChartParams = resolveCardChartParams;", uiCtx);
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(uiCtx.resolveCardChartParams({ dataset: { stockKey: "us:NVDA", marketContext: "us" } }))),
    { period: "1y", interval: "1wk" },
  );
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(uiCtx.resolveCardChartParams({ dataset: { stockKey: "us:NVDA", marketContext: "portfolio" } }))),
    { period: "3mo", interval: "auto" },
  );
  assert(uiSrc.includes("showChartError(wrapper") && uiSrc.includes("const { period, interval } = resolveCardChartParams(wrapper);"));

  // Test showChartError retry button forwards interval too
  const retryCalls = [];
  const showErrBlock = extractNamedBlock(uiSrc, "function showChartError(wrapper, msg, type = \"error\")");
  const containerHost = makeElement("div");
  const errWrapper = makeElement("div");
  errWrapper.dataset.stockKey = "us:NVDA";
  errWrapper.dataset.marketContext = "us";
  errWrapper.querySelector = () => containerHost;
  const errCtx = {
    resolveCardChartParams: uiCtx.resolveCardChartParams,
    refreshStockChart: (w, p, iv) => retryCalls.push({ w, p, iv }),
    chartErrorHosts: () => [containerHost],
    destroyChart: () => {},
    clearChartError: () => {},
    createEl: (tag, cls, txt) => {
      const el = makeElement(tag);
      el.className = cls || "";
      el.textContent = txt || "";
      return el;
    },
    currentDrawerActiveWrapper: null,
    document: {
      getElementById: () => null,
      createElement: (tag) => {
        const el = makeElement(tag);
        if (!el.setAttribute) {
          el.attrs = {};
          el.setAttribute = (k, v) => { el.attrs[k] = String(v); };
        }
        return el;
      },
    },
  };
  vm.runInNewContext(showErrBlock + "; this.showChartError = showChartError;", errCtx);
  errCtx.showChartError(errWrapper, "通信エラー", "error");
  const retryBtn = containerHost.children.flatMap((c) => c.children || []).find((c) => c.tagName === "BUTTON");
  assert(retryBtn, "showChartError must attach retry button");
  await retryBtn.dispatchEvent("click");
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify({ p: retryCalls[0].p, iv: retryCalls[0].iv })),
    { p: "1y", iv: "1wk" },
  );

  // 2. _loadStockColors and getAlertsConfig reject "null", arrays, and primitives
  const colorsBlock = extractNamedBlock(chartSrc, "function _loadStockColors()");
  const alertsBlock = extractNamedBlock(indexMainSrc, "function getAlertsConfig()");
  for (const badJson of ["null", "[]", "[1,2]", "42", '"hello"', "{broken"]) {
    const store = { stock_colors: badJson, userAlerts: badJson };
    const stCtx = {
      _stockColorsCache: null,
      localStorage: { getItem: (k) => store[k] ?? null },
    };
    vm.runInNewContext(colorsBlock + "\n" + alertsBlock + "\nthis._loadStockColors = _loadStockColors; this.getAlertsConfig = getAlertsConfig;", stCtx);
    const colors = stCtx._loadStockColors();
    const alerts = stCtx.getAlertsConfig();
    assert.strictEqual(typeof colors === "object" && colors !== null && !Array.isArray(colors), true, "colors bad=" + badJson);
    assert.strictEqual(Object.keys(colors).length, 0);
    assert.strictEqual(typeof alerts === "object" && alerts !== null && !Array.isArray(alerts), true, "alerts bad=" + badJson);
    assert.strictEqual(Object.keys(alerts).length, 0);
  }

  // 3. heatmap onMouseMove guards zero-sized canvas
  const mmStart = heatmapSrc.indexOf("const onMouseMove = (event) => {");
  assert(mmStart !== -1, "onMouseMove missing in heatmap.js");
  let mmDepth = 0;
  let mmInBrace = false;
  let mmEnd = -1;
  for (let i = heatmapSrc.indexOf("{", mmStart); i < heatmapSrc.length; i++) {
    if (heatmapSrc[i] === "{") { mmDepth++; mmInBrace = true; }
    else if (heatmapSrc[i] === "}") {
      mmDepth--;
      if (mmInBrace && mmDepth === 0) { mmEnd = i + 1; break; }
    }
  }
  assert(mmEnd > mmStart, "unbalanced onMouseMove block");
  const mmSnippet = heatmapSrc.slice(mmStart, mmEnd) + ";";
  const hmCtx = {
    state: { viewMode: "3d" },
    mouse: { x: 0.25, y: -0.5 },
    renderer: { domElement: { getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }) } },
    hoveredPoint: null,
    raycaster: { setFromCamera() {} },
    camera: {},
    pointsMesh: null,
    tooltip: null,
    hideTooltip() {},
  };
  vm.runInNewContext(mmSnippet + "; this.onMouseMove = onMouseMove;", hmCtx);
  hmCtx.onMouseMove({ clientX: 100, clientY: 100 });
  assert.strictEqual(Number.isNaN(hmCtx.mouse.x), false, "mouse.x must never become NaN");
  assert.strictEqual(Number.isNaN(hmCtx.mouse.y), false, "mouse.y must never become NaN");
  assert.strictEqual(hmCtx.mouse.x, 0.25);
  assert.strictEqual(hmCtx.mouse.y, -0.5);

  __finished = true;
  console.log("PARAMS STORAGE HEATMAP PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "PARAMS STORAGE HEATMAP PASS")


def test_review_experimental_observatory_wheel_pinch_and_http_error_guards():
    node_script = (
        _PRELUDE
        + r"""
(async () => {
  const temporalSrc = readRel("static/js/experimental/temporal-controller.js");
  const constellationSrc = readRel("static/js/experimental/constellation-controller.js");
  const aiDiveSrc = readRel("static/js/experimental/ai-dive-controller.js");
  const orbitEntrySrc = readRel("static/js/experimental/orbit-entry.js");

  // 1. TemporalController.handleWheel leaves ctrlKey/metaKey pinch-zoom untouched
  const wheelBlock = extractNamedBlock(temporalSrc, "\n    handleWheel(e) {");
  const cursorUpdates = [];
  const wheelCtx = {
    GRANULARITY_PERIODS: ["1d", "5d", "1mo", "3mo", "6mo", "1y"],
    obj: {
      historyCache: new Map([["AAPL:3mo", [{ t: 1 }, { t: 2 }, { t: 3 }]]]),
      els: {},
      state: {
        state: { selectedSymbol: "AAPL", timeGranularity: "3mo", timeCursor: 1 },
        setTimeCursor: (c) => cursorUpdates.push(c),
        setTimeGranularity: () => {},
      },
      stopTimelapse() {},
    },
  };
  vm.runInNewContext(
    "const GRANULARITY_PERIODS = ['1d', '5d', '1mo', '3mo', '6mo', '1y'];\nclass T { " +
      wheelBlock +
      " }\nthis.handleWheel = T.prototype.handleWheel;",
    wheelCtx,
  );
  let prevented = 0;
  const target = { closest: () => null };
  wheelCtx.handleWheel.call(wheelCtx.obj, { ctrlKey: true, metaKey: false, deltaY: -10, target, preventDefault: () => { prevented++; } });
  wheelCtx.handleWheel.call(wheelCtx.obj, { ctrlKey: false, metaKey: true, deltaY: 10, target, preventDefault: () => { prevented++; } });
  assert.strictEqual(prevented, 0, "ctrlKey/metaKey wheel events must not call preventDefault()");
  wheelCtx.handleWheel.call(wheelCtx.obj, { ctrlKey: false, metaKey: false, shiftKey: false, deltaY: 10, target, preventDefault: () => { prevented++; } });
  assert.strictEqual(prevented, 1, "normal wheel events still call preventDefault()");
  assert.deepStrictEqual(cursorUpdates, [2]);

  // 2. Runtime verification: all 4 experimental Observatory modules inspect res?.response ?? res and reject !resObj.ok
  const makeSimpleEl = () => {
    let _text = "";
    return {
      className: "",
      disabled: false,
      children: [],
      get textContent() { return _text; },
      set textContent(v) { this.children.length = 0; _text = String(v ?? ""); },
      classList: {
        add() {},
        remove() {},
        toggle() {},
        contains: () => false,
      },
      appendChild(c) { this.children.push(c); return c; },
      querySelectorAll: () => [],
      setAttribute() {},
      getAttribute: () => null,
    };
  };
  const fakeDoc = {
    createElement: () => makeSimpleEl(),
    createTextNode: (t) => ({ textContent: t }),
  };

  // 2a. TemporalController.fetchHistoryForSymbol wrapped ok:false
  const fetchHistoryBlock = extractNamedBlock(temporalSrc, "\n    async fetchHistoryForSymbol(symbol, granularity, attempt = 0) {");
  assert(fetchHistoryBlock.includes("const resObj = res?.response ?? res;") && fetchHistoryBlock.includes("!resObj.ok"));
  const timeDisplayEl = makeSimpleEl();
  const timeBadgeEl = makeSimpleEl();
  const timeSliderEl = makeSimpleEl();
  const temporalErrCtx = {
    HISTORY_CACHE_MAX_ENTRIES: 20,
    AbortController,
    console: { warn() {} },
    global: {
      apiFetch: async () => ({
        ok: false,
        status: 503,
        response: { ok: false, status: 503 },
        json: async () => ({ error: "hist unavailable" }),
      }),
    },
    obj: {
      activeHistoryKey: null,
      retryTimers: new Set(),
      historyCache: new Map(),
      inFlightRequests: new Map(),
      els: { timeDisplay: timeDisplayEl, timeBadge: timeBadgeEl, timeSlider: timeSliderEl },
      state: {
        state: {
          selectedSymbol: "AAPL",
          timeGranularity: "3mo",
          market: "us",
          stocks: new Map([["AAPL", { symbol: "AAPL", market: "us" }]]),
        },
        data: {},
      },
      clearRetryTimers() {},
      syncGranularityUI() {},
      isCurrentHistoryContext(s, p) { return s === "AAPL" && p === "3mo"; },
      showHistoryError() { timeBadgeEl.textContent = "ERROR_FN"; },
    },
  };
  vm.runInNewContext(
    "class T { " + fetchHistoryBlock + " }\nthis.run = () => T.prototype.fetchHistoryForSymbol.call(obj, 'AAPL', '3mo', 0);",
    temporalErrCtx,
  );
  await temporalErrCtx.run();
  assert.ok(timeDisplayEl.textContent.includes("hist unavailable"), timeDisplayEl.textContent);
  assert.strictEqual(timeBadgeEl.textContent, "ERROR");
  assert.strictEqual(timeSliderEl.disabled, true);

  // 2b. ConstellationController.runAiComparison wrapped ok:false
  const runCmpBlock = extractNamedBlock(constellationSrc, "\n    async runAiComparison() {");
  assert(runCmpBlock.includes("const resObj = res?.response ?? res;") && runCmpBlock.includes("!resObj.ok"));
  const cmpContainer = makeSimpleEl();
  const cmpBtn = makeSimpleEl();
  const cmpCtx = {
    AbortController,
    document: fakeDoc,
    global: {
      createRequestToken: () => "tok-cmp",
      apiFetch: async () => ({
        ok: false,
        status: 502,
        response: { ok: false, status: 502 },
        json: async () => ({ message: "cmp unavailable" }),
      }),
    },
    obj: {
      _abortController: null,
      els: { aiResultContainer: cmpContainer, aiCompareBtn: cmpBtn },
      state: {
        state: {
          connectedSymbols: ["AAPL", "MSFT"],
          stocks: new Map([
            ["AAPL", { symbol: "AAPL", market: "us", price: 100 }],
            ["MSFT", { symbol: "MSFT", market: "us", price: 200 }],
          ]),
        },
      },
    },
  };
  vm.runInNewContext(
    "class C { " + runCmpBlock + " }\nthis.run = () => C.prototype.runAiComparison.call(obj);",
    cmpCtx,
  );
  await cmpCtx.run();
  assert.strictEqual(cmpContainer.children.length, 1);
  assert.ok(cmpContainer.children[0].textContent.includes("cmp unavailable"), cmpContainer.children[0].textContent);
  assert.strictEqual(cmpBtn.disabled, false);

  // 2c. AiDiveController.fetchAndRenderNews & runDeepAiAnalysis wrapped ok:false
  const fetchNewsBlock = extractNamedBlock(aiDiveSrc, "\n    async fetchAndRenderNews(stock) {");
  const deepAnalyzeBlock = extractNamedBlock(aiDiveSrc, "\n    async runDeepAiAnalysis() {");
  assert(fetchNewsBlock.includes("const resObj = res?.response ?? res;") && fetchNewsBlock.includes("!resObj.ok"));
  assert(deepAnalyzeBlock.includes("const resObj = res?.response ?? res;") && deepAnalyzeBlock.includes("!resObj.ok"));
  const tier3El = makeSimpleEl();
  const tier4El = makeSimpleEl();
  const startAiBtn = makeSimpleEl();
  const diveCtx = {
    AbortController,
    document: fakeDoc,
    global: {
      createRequestToken: () => "tok-dive",
      apiFetch: async () => ({
        ok: false,
        status: 500,
        response: { ok: false, status: 500 },
        json: async () => ({ error: "dive unavailable" }),
      }),
    },
    obj: {
      _newsAbortController: null,
      _abortController: null,
      els: { tier3Container: tier3El, tier4Container: tier4El, startAiBtn },
      state: {
        state: {
          aiDiveOpen: true,
          aiDiveSymbol: "AAPL",
          stocks: new Map([["AAPL", { symbol: "AAPL", market: "us", price: 100 }]]),
        },
      },
      _resolveProviderLabel: () => "Mistral AI",
      isCurrentAiDiveSymbol: (s) => s === "AAPL",
      getMarketNewsItems: () => [{ title: "SHOULD_NOT_RENDER" }],
      renderAiAnalysisResult: () => { throw new Error("Must not render HTTP error payload as analysis result"); },
    },
  };
  vm.runInNewContext(
    "class D { " + fetchNewsBlock + "\n" + deepAnalyzeBlock + " }\nthis.runNews = () => D.prototype.fetchAndRenderNews.call(obj, { symbol: 'AAPL', market: 'us' });\nthis.runDeep = () => D.prototype.runDeepAiAnalysis.call(obj);",
    diveCtx,
  );
  await diveCtx.runNews();
  assert.strictEqual(tier3El.children.length, 1);
  assert.ok(tier3El.children[0].textContent.includes("dive unavailable"), tier3El.children[0].textContent);
  await diveCtx.runDeep();
  assert.strictEqual(tier4El.children.length, 1);
  assert.ok(tier4El.children[0].textContent.includes("dive unavailable"), tier4El.children[0].textContent);
  assert.strictEqual(startAiBtn.disabled, false);

  // 2d. orbit-entry.js loadObservatoryData wrapped ok:false on /api/stocks and /api/heatmap
  const loadObsBlock = extractNamedBlock(orbitEntrySrc, "\n  async function loadObservatoryData(");
  assert(loadObsBlock.includes("const resObj = res?.response ?? res;") && loadObsBlock.includes("!resObj.ok"));
  assert(loadObsBlock.includes("const hmResObj = hmRes?.response ?? hmRes;") && loadObsBlock.includes("!hmResObj.ok"));
  const runOrbitFail = async (handler) => {
    const orbitErrors = [];
    const orbitCtx = {
      AbortController,
      console: { warn() {}, error() {} },
      window: {
        apiFetch: handler,
        ObservatoryDataAdapter: { normalizeStock: (s) => s },
      },
      state: {
        state: { market: "us" },
        setStocks: () => { throw new Error("Must not setStocks on failure"); },
      },
      renderer: { start() {} },
      loadGeneration: 0,
      loadAbortController: null,
      loadRetryTimeout: null,
      cancelObservatoryLoadRetry: () => {},
      setLoading: () => {},
      hideError: () => {},
      showError: (msg) => orbitErrors.push(msg),
    };
    vm.runInNewContext(loadObsBlock + "\nthis.run = () => loadObservatoryData();", orbitCtx);
    await orbitCtx.run();
    return orbitErrors;
  };
  const stocksFailErrs = await runOrbitFail(async () => ({
    ok: false,
    status: 504,
    response: { ok: false, status: 504 },
    json: async () => ({ message: "orbit unavailable" }),
  }));
  assert.strictEqual(stocksFailErrs.length, 1);
  assert.ok(stocksFailErrs[0].includes("orbit unavailable"), stocksFailErrs[0]);
  const heatmapFailErrs = await runOrbitFail(async (url) => {
    if (url === "/api/stocks") {
      return { response: { ok: true, status: 200 }, json: async () => ({ stocks: { us: [] } }) };
    }
    return { response: { ok: false, status: 500 }, json: async () => ({ stocks: [{ symbol: "SHOULD_NOT_LOAD" }] }) };
  });
  assert.deepStrictEqual(heatmapFailErrs, ["表示可能な銘柄データを取得できませんでした。"]);

  __finished = true;
  console.log("EXPERIMENTAL OBSERVATORY PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "EXPERIMENTAL OBSERVATORY PASS")
