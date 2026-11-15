"""Regression tests for Unit 2: Chrome extension & shared numeric/timer discipline."""

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


_COMMON_HELPERS = r"""
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
    href: "",
    target: "",
    rel: "",
    disabled: false,
    hidden: false,
    textContent: "",
    value: "",
    colSpan: 1,
    dataset: {},
    style: {},
    children,
    classList: {
      add(...items) {
        items.forEach((c) => c && classes.add(c));
        _className = Array.from(classes).join(" ");
      },
      remove(...items) {
        items.forEach((c) => classes.delete(c));
        _className = Array.from(classes).join(" ");
      },
      contains(c) {
        return classes.has(c);
      },
      toggle(c, force) {
        const next = force === undefined ? !classes.has(c) : !!force;
        if (next) classes.add(c); else classes.delete(c);
        _className = Array.from(classes).join(" ");
        return next;
      },
    },
    get className() {
      return _className;
    },
    set className(val) {
      _className = String(val || "");
      classes.clear();
      _className.split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
    setAttribute(k, v) {
      attrs[k] = String(v);
      this[k] = String(v);
    },
    getAttribute(k) {
      return attrs[k] ?? null;
    },
    appendChild(child) {
      if (child && child.tagName === "FRAGMENT") {
        children.push(...child.children);
      } else {
        children.push(child);
      }
      return child;
    },
    replaceChildren(...nodes) {
      children.length = 0;
      for (const n of nodes) this.appendChild(n);
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatchEvent(type, eventObj = {}) {
      const fns = listeners.get(type) || [];
      return Promise.all(fns.map((fn) => fn({ stopPropagation() {}, preventDefault() {}, ...eventObj })));
    },
    querySelectorAll() {
      return [];
    },
    querySelector(sel) {
      if (sel === ".detector-inline-error") {
        return children.find((c) => c.className === "detector-inline-error") || null;
      }
      return null;
    },
    closest() {
      return null;
    },
  };
  return el;
}

function createFakeTimers() {
  let nextId = 1;
  const tasks = new Map();
  return {
    tasks,
    setTimeout(fn, delay) {
      const id = nextId++;
      tasks.set(id, { id, fn, delay });
      return id;
    },
    clearTimeout(id) {
      tasks.delete(id);
    },
    runAll() {
      const entries = Array.from(tasks.values());
      tasks.clear();
      for (const entry of entries) entry.fn();
    },
  };
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


def test_background_badge_rejects_non_finite():
    node_script = (
        _COMMON_HELPERS
        + r"""
(async () => {
  const bgSrc = readRel("chrome_extension/background.js");
  const updateBadgeBlock = extractNamedBlock(bgSrc, "async function updateBadge()");
  const badgeCommits = [];
  let payloadPct = Infinity;
  const bgCtx = {
    Number,
    Math,
    parseFloat,
    String,
    isUpdatingBadge: false,
    badgeToken: 0,
    backendPortHydrated: Promise.resolve(),
    checkHealth: async () => ({ ok: true, base: "http://127.0.0.1:5000" }),
    fetchWithTimeout: async () => ({
      ok: true,
      status: 200,
      json: async () => ({ N225: { percent: payloadPct } }),
    }),
    BADGE_FETCH_TIMEOUT_MS: 2000,
    commitBadgeText: (text, color) => badgeCommits.push({ text, color }),
    console,
  };
  vm.runInNewContext(updateBadgeBlock + "; this.updateBadge = updateBadge;", bgCtx);
  payloadPct = Infinity;
  await bgCtx.updateBadge();
  payloadPct = -Infinity;
  await bgCtx.updateBadge();
  assert.strictEqual(badgeCommits.length, 2);
  assert.strictEqual(badgeCommits[0].text, "");
  assert.strictEqual(badgeCommits[1].text, "");
  payloadPct = 3.4;
  await bgCtx.updateBadge();
  assert.strictEqual(badgeCommits.length, 3);
  assert.strictEqual(badgeCommits[2].text, "+3%");
  __finished = true;
  console.log("BG BADGE PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "BG BADGE PASS")


def test_popup_render_stock_item_rejects_infinity():
    node_script = (
        _COMMON_HELPERS
        + r"""
(async () => {
  const popupSrc = readRel("chrome_extension/popup.js");
  const renderStockBlock = extractNamedBlock(popupSrc, "function renderStockItem(");
  const popupRenderCtx = {
    document: { createElement: (tag) => makeElement(tag) },
    Number,
    parseFloat,
    isNaN,
    String,
    setSafeText: (el, t) => { el.textContent = String(t ?? ""); },
    formatPopupPrice: () => "$100.00",
    buildAppUrl: () => "http://127.0.0.1:5000/",
    openAppPage: () => {},
  };
  vm.runInNewContext(renderStockBlock + "; this.renderStockItem = renderStockItem;", popupRenderCtx);
  for (const bad of [Infinity, -Infinity, NaN, "abc"]) {
    const row = popupRenderCtx.renderStockItem("AAPL", "Apple", 100, bad, "us", "USD");
    const badge = findAll(row, (el) => el.classList && el.classList.contains("stock-change"))[0];
    assert(badge && badge.classList.contains("neutral"), "Non-finite value must have neutral class: " + bad);
    assert.strictEqual(badge.textContent, "--%");
  }
  const posRow = popupRenderCtx.renderStockItem("AAPL", "Apple", 100, 2.5, "us", "USD");
  const posBadge = findAll(posRow, (el) => el.classList && el.classList.contains("stock-change"))[0];
  assert(posBadge && posBadge.classList.contains("plus") && posBadge.textContent === "+2.50%");
  __finished = true;
  console.log("POPUP RENDER PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "POPUP RENDER PASS")


def test_popup_detected_add_retry_cancels_stale_revert_timer():
    node_script = (
        _COMMON_HELPERS
        + r"""
(async () => {
  const popupSrc = readRel("chrome_extension/popup.js");
  const loadDetectedBlock = extractNamedBlock(popupSrc, "async function loadDetectedTickers()");
  const detectedListContainer = makeElement("div");
  const detectorPageTitle = makeElement("div");
  const fakeTimers = createFakeTimers();
  let resolveSecondSend = null;
  let sendCalls = 0;
  const popupDetectCtx = {
    document: {
      createElement: (tag) => makeElement(tag),
      getElementById: (id) =>
        ({ detectedListContainer, detectorPageTitle }[id] || null),
    },
    $: (id) => ({ detectedListContainer, detectorPageTitle }[id] || null),
    detectorGeneration: 0,
    setSafeText: (el, t) => { el.textContent = String(t ?? ""); },
    setTimeout: fakeTimers.setTimeout,
    clearTimeout: fakeTimers.clearTimeout,
    isInjectableUrl: () => true,
    chrome: {
      tabs: {
        query: async () => [{ id: 1, url: "https://finance.yahoo.com/quote/AAPL", title: "Yahoo" }],
        sendMessage: async () => ({
          ok: true,
          tickers: [{ symbol: "AAPL", market: "us", name: "Apple", count: 1 }],
        }),
      },
    },
    send: async () => {
      sendCalls++;
      if (sendCalls === 1) return { ok: false, error: "一時エラー" };
      return await new Promise((resolve) => { resolveSecondSend = resolve; });
    },
    console,
  };
  vm.runInNewContext(loadDetectedBlock + "; this.loadDetectedTickers = loadDetectedTickers;", popupDetectCtx);
  await popupDetectCtx.loadDetectedTickers();
  const popupAddBtn = findAll(detectedListContainer, (el) => el.className === "mini-add-btn")[0];
  assert(popupAddBtn, "mini-add-btn should render");

  await popupAddBtn.dispatchEvent("click");
  assert.strictEqual(popupAddBtn.textContent, "失敗");
  assert.strictEqual(fakeTimers.tasks.size, 1, "failure schedules one revert timer");

  const secondClickPromise = popupAddBtn.dispatchEvent("click");
  assert.strictEqual(popupAddBtn.textContent, "追加中...");
  assert.strictEqual(fakeTimers.tasks.size, 0, "retry click cancels pending revert timer");
  await popupAddBtn.dispatchEvent("click");
  assert.strictEqual(sendCalls, 2, "double click during in-flight is ignored");
  fakeTimers.runAll();
  assert.strictEqual(popupAddBtn.textContent, "追加中...");
  resolveSecondSend({ ok: true });
  await secondClickPromise;
  assert.strictEqual(popupAddBtn.textContent, "✓ 追加済");
  __finished = true;
  console.log("POPUP TIMER PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "POPUP TIMER PASS")


def test_screener_rows_reject_infinity_and_cancel_stale_revert_timer():
    node_script = (
        _COMMON_HELPERS
        + r"""
(async () => {
  const screenerSrc = readRel("static/js/screener.js");
  const renderRowsBlock = extractNamedBlock(screenerSrc, "function renderTableRows(stocks, requestGeneration)");
  const screenerTbody = makeElement("tbody");
  const screenerTimers = createFakeTimers();
  let screenerFetchCount = 0;
  let resolveSecondScreener = null;
  const screenerCtx = {
    document: {
      createElement: (tag) => makeElement(tag),
      createDocumentFragment: () => makeElement("fragment"),
      getElementById: (id) => (id === "screenerTableBody" ? screenerTbody : null),
    },
    setTimeout: screenerTimers.setTimeout,
    clearTimeout: screenerTimers.clearTimeout,
    requestAnimationFrame: (fn) => fn(),
    Number,
    parseFloat,
    isNaN,
    String,
    Array,
    Math,
    screenerRequestGeneration: 1,
    formatCurrency: (v) => String(v),
    formatMarketCap: (v) => String(v),
    csrfFetch: async () => {
      screenerFetchCount++;
      if (screenerFetchCount === 1) {
        return { ok: false, status: 400, json: async () => ({ error: "failed" }) };
      }
      return await new Promise((resolve) => { resolveSecondScreener = resolve; });
    },
    showToast: () => {},
    encodeURIComponent,
  };
  vm.runInNewContext(renderRowsBlock + "; this.renderTableRows = renderTableRows;", screenerCtx);
  screenerCtx.renderTableRows(
    [
      { symbol: "INF", market: "us", name: "Inf", price: 10, change_percent: Infinity },
      { symbol: "NEG", market: "us", name: "Neg", price: 10, change_percent: -Infinity },
      { symbol: "POS", market: "us", name: "Pos", price: 10, change_percent: 1.5 },
    ],
    1,
  );
  const pctCells = findAll(
    screenerTbody,
    (el) => el.tagName === "TD" && el.textContent.endsWith("%"),
  );
  assert.strictEqual(pctCells.length, 3);
  assert(pctCells[0].className.includes("neutral") && pctCells[0].textContent === "--%");
  assert(pctCells[1].className.includes("neutral") && pctCells[1].textContent === "--%");
  assert(pctCells[2].className.includes("plus") && pctCells[2].textContent === "+1.50%");

  const screenerAddBtn = findAll(screenerTbody, (el) => el.tagName === "BUTTON")[0];
  await screenerAddBtn.dispatchEvent("click");
  assert.strictEqual(screenerAddBtn.textContent, "再試行");
  assert.strictEqual(screenerTimers.tasks.size, 1);

  const retryScreener = screenerAddBtn.dispatchEvent("click");
  assert.strictEqual(screenerAddBtn.textContent, "追加中...");
  assert.strictEqual(screenerTimers.tasks.size, 0, "screener retry click cancels pending revert timer");
  await screenerAddBtn.dispatchEvent("click");
  assert.strictEqual(screenerFetchCount, 2);
  screenerTimers.runAll();
  assert.strictEqual(screenerAddBtn.textContent, "追加中...");
  resolveSecondScreener({ ok: true, status: 200, json: async () => ({ ok: true }) });
  await retryScreener;
  assert.strictEqual(screenerAddBtn.textContent, "✓ 追加済");
  __finished = true;
  console.log("SCREENER PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "SCREENER PASS")


def test_index_fx_rate_rejects_infinity():
    node_script = (
        _COMMON_HELPERS
        + r"""
(async () => {
  const indexMainSrc = readRel("static/js/index_main.js");
  const modalTotalBlock = extractNamedBlock(indexMainSrc, "function updatePortfolioModalTotalCost()");
  const fxInput = makeElement("input");
  const sharesInput = makeElement("input");
  const costInput = makeElement("input");
  const totalDisplay = makeElement("span");
  sharesInput.value = "2";
  costInput.value = "100";
  const byId = {
    "pf-shares-input": sharesInput,
    "pf-price-input": costInput,
    "pf-fx-rate-input": fxInput,
    "pf-modal-total-cost": totalDisplay,
  };
  const indexCtx = {
    Number,
    parseFloat,
    isNaN,
    Math,
    DOM: { get: (id) => byId[id] || null },
    _currentPfStock: { market: "us", currency: "USD" },
    state: { exchangeRate: 150 },
    window: { State: { exchangeRate: 150 } },
  };
  vm.runInNewContext(modalTotalBlock + "; this.updatePortfolioModalTotalCost = updatePortfolioModalTotalCost;", indexCtx);
  for (const badFx of ["Infinity", "-5", "abc", "0"]) {
    fxInput.value = badFx;
    indexCtx.updatePortfolioModalTotalCost();
    assert(!totalDisplay.textContent.includes("Infinity"), "Infinity fxRate must fall back: " + totalDisplay.textContent);
    assert(!totalDisplay.textContent.includes("NaN"), "Total cost display must not be NaN: " + totalDisplay.textContent);
    assert(totalDisplay.textContent.includes("30,000") || totalDisplay.textContent.includes("30000"), totalDisplay.textContent);
  }
  fxInput.value = "160";
  indexCtx.updatePortfolioModalTotalCost();
  assert(totalDisplay.textContent.includes("32,000") || totalDisplay.textContent.includes("32000"));
  __finished = true;
  console.log("INDEX FX PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "INDEX FX PASS")


def test_state_and_api_reject_infinity_fx_and_chip_prices():
    node_script = (
        _COMMON_HELPERS
        + r"""
(async () => {
  // 1. StateManager.updateIndices must not accept Infinity as exchangeRate
  const stateSrc = readRel("static/js/state.js");
  const updateIndicesBlock = extractNamedBlock(stateSrc, "updateIndices(data)");
  const stateObj = { indices: null, exchangeRate: 150.0 };
  const stateCtx = { Number };
  vm.runInNewContext("function " + updateIndicesBlock + "; this.updateIndices = updateIndices;", stateCtx);
  stateCtx.updateIndices.call(stateObj, { USDJPY: { price: Infinity } });
  assert(Number.isFinite(stateObj.exchangeRate) && stateObj.exchangeRate === 150.0, "exchangeRate must reject Infinity: " + stateObj.exchangeRate);
  stateCtx.updateIndices.call(stateObj, { USDJPY: { price: 158.25 } });
  assert.strictEqual(stateObj.exchangeRate, 158.25);

  // 2. api.js updateSingleIndexChip must not trigger price flash on Infinity
  const apiSrc = readRel("static/js/api.js");
  const chipBlock = extractNamedBlock(apiSrc, "function updateSingleIndexChip(chip, idx)");
  const priceEl = makeElement("span");
  priceEl.className = "index-price";
  priceEl.textContent = "100.00";
  const chipEl = makeElement("div");
  chipEl.querySelector = (sel) => (sel === ".index-price" ? priceEl : null);
  const apiCtx = {
    Number,
    parseFloat,
    setTimeout: () => 1,
    clearTimeout: () => {},
    formatIndexNumber: (val) => (val === Infinity ? "Infinity" : String(val)),
  };
  vm.runInNewContext(chipBlock + "; this.updateSingleIndexChip = updateSingleIndexChip;", apiCtx);
  apiCtx.updateSingleIndexChip(chipEl, { price: Infinity, change: 0, percent: 0 });
  assert(!priceEl.classList.contains("flash-up") && !priceEl.classList.contains("flash-down"));

  __finished = true;
  console.log("STATE API FINITE PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "STATE API FINITE PASS")
