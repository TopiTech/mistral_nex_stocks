"""Regression suite for Phase 3 Track C1 Core Frontend/UI fixes (UI-001..UI-020).

Executes the real frontend JS files inside Node.js ``vm`` sandboxes (plus
targeted CSS/HTML/JS contract checks) to prevent regressions across SSE lifecycle,
PTS formatting, Escape overlay stacking, chat streaming/drawer race guards,
TradingView loading, news timeout/polling, chart abort/empty states, portfolio
math, heatmap tooltip/error persistence, settings/setup controls, theme CSS
tokens, and alert/AI-portfolio error propagation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node_script(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute frontend UI regression tests")

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


_SHARED_NODE_HELPERS = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(cond, msg) {
  if (!cond) throw new Error("Assertion failed: " + msg);
}

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

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add(...items) { items.forEach((c) => c && set.add(c)); },
    remove(...items) { items.forEach((c) => set.delete(c)); },
    toggle(c, force) {
      if (force === undefined) {
        if (set.has(c)) { set.delete(c); return false; }
        set.add(c); return true;
      }
      if (force) set.add(c); else set.delete(c);
      return !!force;
    },
    contains(c) { return set.has(c); },
    replaceAll(str) {
      set.clear();
      String(str || "").split(/\s+/).filter(Boolean).forEach((c) => set.add(c));
    },
    toString() { return Array.from(set).join(" "); },
  };
}

function makeElement(tagName = "div", id = "") {
  const listeners = new Map();
  const attrs = new Map();
  const children = [];
  const clsList = makeClassList();
  const el = {
    tagName: String(tagName).toUpperCase(),
    id,
    get className() { return clsList.toString(); },
    set className(v) { clsList.replaceAll(v); },
    classList: clsList,
    dataset: {},
    style: {
      setProperty(k, v) { this[k] = v; },
      display: "",
    },
    hidden: false,
    disabled: false,
    value: "",
    textContent: "",
    children,
    parentNode: null,
    parentElement: null,
    scrollTop: 0,
    scrollHeight: 100,
    _rect: { left: 10, top: 20, width: 100, height: 40, bottom: 60 },
    focused: false,
    focus() { this.focused = true; if (el._ownerDoc) el._ownerDoc.activeElement = el; },
    blur() { this.focused = false; },
    click() {
      if (typeof el.onclick === "function") el.onclick();
      el.dispatchEvent({ type: "click", target: el, preventDefault() {}, stopPropagation() {} });
    },
    getBoundingClientRect() { return { ...el._rect }; },
    setAttribute(k, v) {
      attrs.set(k, String(v));
      if (k === "class") el.className = String(v);
    },
    getAttribute(k) { return attrs.has(k) ? attrs.get(k) : null; },
    hasAttribute(k) { return attrs.has(k); },
    removeAttribute(k) { attrs.delete(k); },
    appendChild(child) {
      if (!child) return child;
      child.parentNode = el;
      child.parentElement = el;
      children.push(child);
      return child;
    },
    append(...nodes) { nodes.forEach((n) => el.appendChild(n)); },
    insertBefore(newNode, refNode) {
      if (!newNode) return newNode;
      newNode.parentNode = el;
      newNode.parentElement = el;
      const idx = children.indexOf(refNode);
      if (idx === -1) children.push(newNode);
      else children.splice(idx, 0, newNode);
      return newNode;
    },
    replaceChildren(...nodes) {
      children.length = 0;
      nodes.forEach((n) => el.appendChild(n));
    },
    remove() {
      if (el.parentNode && Array.isArray(el.parentNode.children)) {
        const i = el.parentNode.children.indexOf(el);
        if (i !== -1) el.parentNode.children.splice(i, 1);
      }
      el.parentNode = null;
      el.parentElement = null;
    },
    contains(other) {
      if (other === el) return true;
      return children.some((c) => typeof c.contains === "function" && c.contains(other));
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type);
      if (!list) return;
      const idx = list.indexOf(fn);
      if (idx !== -1) list.splice(idx, 1);
    },
    dispatchEvent(evt) {
      const list = listeners.get(evt.type) || [];
      list.slice().forEach((fn) => fn(evt));
    },
    matches(sel) {
      if (!sel) return false;
      return sel.split(",").map((s) => s.trim()).some((part) => {
        if (part.startsWith("#")) return el.id === part.slice(1);
        const clsMatch = part.match(/\.([a-zA-Z0-9_-]+)/g);
        if (!clsMatch) return el.tagName.toLowerCase() === part.toLowerCase();
        return clsMatch.every((dotCls) => {
          const name = dotCls.slice(1);
          return el.classList.contains(name) || String(el.className || "").split(/\s+/).includes(name);
        });
      });
    },
    querySelector(sel) {
      for (const c of children) {
        if (typeof c.matches === "function" && c.matches(sel)) return c;
        if (typeof c.querySelector === "function") {
          const nested = c.querySelector(sel);
          if (nested) return nested;
        }
      }
      return null;
    },
    querySelectorAll(sel) {
      const out = [];
      for (const c of children) {
        if (typeof c.matches === "function" && c.matches(sel)) out.push(c);
        if (typeof c.querySelectorAll === "function") {
          out.push(...c.querySelectorAll(sel));
        }
      }
      return out;
    },
    closest(sel) {
      let cur = el;
      while (cur) {
        if (typeof cur.matches === "function" && cur.matches(sel)) return cur;
        cur = cur.parentElement;
      }
      return null;
    },
    getContext() {
      return {
        save() {}, restore() {}, clearRect() {}, beginPath() {}, moveTo() {},
        lineTo() {}, stroke() {}, fillRect() {}, fill() {}, arc() {},
        setLineDash() {}, createLinearGradient() { return { addColorStop() {} }; },
      };
    },
  };
  return el;
}
"""


def test_ui_001_sse_visibility_and_teardown() -> None:
    """UI-001: APIClient resets connect state on teardown and index_main avoids duplicate SSE tickets."""
    script = _SHARED_NODE_HELPERS + r"""
(async () => {
  // Part A: APIClient teardown, chain invalidation, and paused rejection handling
  const docListeners = {};
  const FakeES = function (url) { this.url = url; this.readyState = 0; this.closed = false; };
  FakeES.CONNECTING = 0;
  FakeES.OPEN = 1;
  FakeES.CLOSED = 2;
  FakeES.prototype.addEventListener = function () {};
  FakeES.prototype.removeEventListener = function () {};
  FakeES.prototype.close = function () { this.closed = true; this.readyState = 2; };

  const clientCtx = {
    window: { addEventListener() {}, removeEventListener() {} },
    document: {
      hidden: false,
      addEventListener(t, fn) { (docListeners[t] ||= []).push(fn); },
      removeEventListener() {},
    },
    EventSource: FakeES,
    console: { info() {}, warn() {}, error() {}, debug() {} },
    setTimeout,
    clearTimeout,
    setInterval() { return 1; },
    clearInterval() {},
  };
  clientCtx.window.window = clientCtx.window;
  vm.createContext(clientCtx);
  vm.runInContext(readRel("static/js/api_client.js"), clientCtx);

  const client = new clientCtx.window.APIClient("/api");
  let rejectPendingUrl = null;
  let errorCount = 0;
  client.openSSE("", () => {}, () => { errorCount += 1; }, {
    urlProvider: () => new Promise((_res, rej) => { rejectPendingUrl = rej; }),
  });
  assert(client.isConnecting === true, "isConnecting should be true while urlProvider pending");
  assert(client.hasActiveSSE() === true, "hasActiveSSE should report true while connecting");

  // Pausing while ticket request is in flight should tear down & suppress late rejection reconnect
  client.isVisibilityPaused = true;
  client._teardownSSE();
  assert(client.isConnecting === false, "_teardownSSE must reset isConnecting");
  assert(client.hasActiveSSE() === true, "hasActiveSSE stays true while paused with _lastSSEParams");

  rejectPendingUrl(new Error("late ticket fail"));
  await new Promise((r) => setTimeout(r, 20));
  assert(errorCount === 0, "paused/invalidated rejection must not trigger onError");
  assert(client.ssePendingReconnectTimeout === null, "paused rejection must not schedule reconnect");

  // closeSSE clears everything cleanly
  client.closeSSE();
  assert(client.hasActiveSSE() === false, "closeSSE must clear hasActiveSSE");
  assert(client.isConnecting === false, "closeSSE must reset isConnecting");

  // Part B: index_main initVisibilityHandler avoids duplicate connectSSE during APIClient resume
  const indexMainSrc = readRel("static/js/index_main.js");
  let initVisFn = "";
  {
    const marker = "function initVisibilityHandler(";
    const start = indexMainSrc.indexOf(marker);
    let depth = 0, opened = false, end = -1;
    for (let i = start; i < indexMainSrc.length; i++) {
      if (indexMainSrc[i] === "{") { depth++; opened = true; }
      else if (indexMainSrc[i] === "}") { depth--; if (opened && depth === 0) { end = i + 1; break; } }
    }
    initVisFn = indexMainSrc.slice(start, end);
  }
  const visHandlers = [];
  let fetchCount = 0;
  let connectCount = 0;
  const idxCtx = {
    document: {
      hidden: false,
      addEventListener(t, fn) { if (t === "visibilitychange") visHandlers.push(fn); },
    },
    EventSource: FakeES,
    sseState: { stockEventSource: null },
    sseApiClient: {
      currentEventSource: null,
      isConnecting: true,
      ssePendingReconnectTimeout: null,
      hasActiveSSE() { return this.isConnecting; },
    },
    fetchInitialStocks() { fetchCount += 1; },
    getSseMode() { return 1; },
    connectSSE() { connectCount += 1; },
  };
  vm.createContext(idxCtx);
  vm.runInContext(initVisFn + "\ninitVisibilityHandler();", idxCtx);
  assert(visHandlers.length === 1, "visibility handler must register");
  visHandlers[0]();
  assert(fetchCount === 1, "fetchInitialStocks runs once when source not OPEN");
  assert(connectCount === 0, "must NOT call connectSSE while sseApiClient is already resuming/connecting");

  // When sseApiClient is truly inactive and readyState is CLOSED, connectSSE IS called
  idxCtx.sseApiClient.isConnecting = false;
  idxCtx.sseState.stockEventSource = { readyState: FakeES.CLOSED };
  visHandlers[0]();
  assert(fetchCount === 2, "second visible event fetches stocks");
  assert(connectCount === 1, "must call connectSSE when stream is fully inactive/CLOSED");

  process.stdout.write("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(script)


def test_ui_002_pts_currency_and_hidden_sync() -> None:
    """UI-002: realtime_client formats PTS using stock context currency and syncs hidden/aria-hidden."""
    script = _SHARED_NODE_HELPERS + r"""
(async () => {
  const wrapper = makeElement("div");
  wrapper.classList.add("stock-wrapper");
  wrapper.dataset.stockKey = "jp:7203.T";
  wrapper.isConnected = true;
  wrapper.__stockData = { symbol: "7203.T", market: "jp", currency: "JPY" };

  const ptsEl = makeElement("div");
  ptsEl.classList.add("compact-pts");
  ptsEl.isConnected = true;
  ptsEl.hidden = true;
  ptsEl.setAttribute("aria-hidden", "true");
  wrapper.appendChild(ptsEl);

  let onReady = null;
  const rootDoc = {
    querySelectorAll(sel) {
      if (sel.includes(".compact-pts")) return [ptsEl];
      if (sel.includes("7203")) return [wrapper];
      return [];
    },
    addEventListener(t, fn) { if (t === "DOMContentLoaded") onReady = fn; },
  };
  const rtCtx = {
    window: {
      formatPrice(price, stock) {
        const sym = stock && (stock.currency === "JPY" || stock.market === "jp") ? "¥" : "$";
        return `${sym}${price}`;
      },
      requestAnimationFrame(fn) { fn(); },
      cancelAnimationFrame() {},
    },
    document: rootDoc,
    console: { log() {}, warn() {}, error() {} },
    setTimeout,
    clearTimeout,
  };
  rtCtx.window.window = rtCtx.window;
  vm.createContext(rtCtx);
  vm.runInContext(readRel("static/js/realtime_client.js"), rtCtx);
  if (typeof onReady === "function") onReady();

  const rc = rtCtx.window.realtimeClient || new rtCtx.window.RealtimeStockClient();
  rc._applyPtsDeltasNow({ "7203.T": { price: 2580 } });
  assert(ptsEl.textContent === "PTS ¥2580", "Expected JPY symbol from wrapper.__stockData, got " + ptsEl.textContent);
  assert(ptsEl.hidden === false, "ptsEl.hidden must be false when text is non-empty");
  assert(ptsEl.getAttribute("aria-hidden") === null, "aria-hidden must be removed when PTS shown");
  assert(wrapper.__stockData.pts_price === 2580, "__stockData.pts_price updated");

  // Clearing PTS syncs both hidden=true and aria-hidden="true"
  rc._applyPtsDeltasNow({ "7203.T": { price: null } });
  assert(ptsEl.textContent === "", "ptsEl cleared");
  assert(ptsEl.hidden === true, "ptsEl.hidden must be true when cleared");
  assert(ptsEl.getAttribute("aria-hidden") === "true", "aria-hidden must be true when cleared");

  // Also check ui.js keeps aria-hidden synced on .compact-pts updates
  const uiSrc = readRel("static/js/ui.js");
  assert(
    uiSrc.includes('ptsEl.removeAttribute("aria-hidden")') &&
      uiSrc.includes('ptsEl.setAttribute("aria-hidden", "true")'),
    "ui.js must synchronize aria-hidden alongside hidden on .compact-pts",
  );
  process.stdout.write("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(script)


def test_ui_003_escape_priority_and_layering() -> None:
    """UI-003: Escape closes strictly one topmost visible overlay by z-index priority."""
    script = _SHARED_NODE_HELPERS + r"""
(async () => {
  const closed = [];
  const docElements = new Map();
  const makeOverlay = (id, initiallyHidden = false, extraClasses = []) => {
    const el = makeElement("div", id);
    if (initiallyHidden) el.classList.add("hidden");
    extraClasses.forEach((c) => el.classList.add(c));
    docElements.set(id, el);
    return el;
  };
  const fsModal = makeOverlay("chart-fullscreen-modal", false);
  const aiPfModal = makeOverlay("ai-portfolio-modal", false, ["show"]);
  const alertModal = makeOverlay("alertModal", false, ["show"]);
  const pfModal = makeOverlay("portfolioModal", false, ["show"]);
  const aiDrawer = makeOverlay("ai-drawer-overlay", false);
  const detailDrawer = makeOverlay("stock-detail-drawer-overlay", false);
  const searchResults = makeOverlay("search-results", false);
  searchResults.style.display = "block";

  const fakeDoc = {
    body: { classList: makeClassList(), style: {} },
    activeElement: null,
    getElementById(id) { return docElements.get(id) || null; },
    addEventListener() {},
  };

  const utilsCtx = {
    window: {
      location: { pathname: "/", search: "" },
      getComputedStyle(el) { return { display: el.style.display || "block", visibility: "visible" }; },
      matchMedia() { return { matches: false }; },
      addEventListener() {},
      removeEventListener() {},
    },
    document: fakeDoc,
    localStorage: { getItem() { return null; }, setItem() {} },
    navigator: {},
    URLSearchParams,
    console: { log() {}, warn() {}, error() {} },
    setTimeout,
    clearTimeout,
    setInterval() { return 1; },
    clearInterval() {},
    DOM: { get(id) { return docElements.get(id) || null; } },
    closeFsChartModal() { fsModal.classList.add("hidden"); closed.push("fs"); },
    closeAiDrawer() { aiDrawer.classList.add("hidden"); closed.push("ai-drawer"); },
    closeStockDetailDrawer() { detailDrawer.classList.add("hidden"); closed.push("detail-drawer"); },
  };
  utilsCtx.window.closeFsChartModal = utilsCtx.closeFsChartModal;
  utilsCtx.window.closeAiPortfolioModal = () => { aiPfModal.classList.add("hidden"); aiPfModal.classList.remove("show"); closed.push("ai-pf"); };
  utilsCtx.window.closeAiDrawer = utilsCtx.closeAiDrawer;
  utilsCtx.window.closeStockDetailDrawer = utilsCtx.closeStockDetailDrawer;
  utilsCtx.window.window = utilsCtx.window;

  vm.createContext(utilsCtx);
  vm.runInContext(readRel("static/js/utils.js"), utilsCtx);

  // Override closeModal tracking on top of utils.js closeModal
  const origCloseModal = utilsCtx.closeModal;
  utilsCtx.closeModal = (id) => {
    origCloseModal(id);
    closed.push(id);
  };

  // IME composing Escape must be ignored
  let imePrevented = false;
  assert(
    utilsCtx.window.closeTopmostOverlayOnEscape({
      key: "Escape", isComposing: true, preventDefault() { imePrevented = true; }, stopPropagation() {},
    }) === false,
    "IME composing Escape must return false",
  );
  assert(!imePrevented, "IME Escape must not preventDefault");

  // Step through entire priority order one Escape press at a time
  for (let i = 0; i < 7; i++) {
    let prevented = false, stopped = false;
    const handled = utilsCtx.window.closeTopmostOverlayOnEscape({
      key: "Escape",
      preventDefault() { prevented = true; },
      stopPropagation() { stopped = true; },
    });
    assert(handled === true, "Expected Escape #" + i + " to close an overlay");
    assert(prevented && stopped, "Expected preventDefault & stopPropagation on Escape #" + i);
  }
  assert(
    JSON.stringify(closed) === JSON.stringify([
      "fs", "ai-pf", "alertModal", "portfolioModal", "ai-drawer", "detail-drawer",
    ]),
    "Unexpected Escape close order: " + JSON.stringify(closed),
  );
  assert(searchResults.style.display === "none", "Final Escape should hide #search-results");
  assert(
    utilsCtx.window.closeTopmostOverlayOnEscape({
      key: "Escape", preventDefault() {}, stopPropagation() {},
    }) === false,
    "Nothing left open should return false",
  );

  // Ensure index_main.js has no duplicate global keydown Escape handler
  const indexMainSrc = readRel("static/js/index_main.js");
  assert(
    !indexMainSrc.includes("Global Escape key handler for drawers and floating UI"),
    "index_main.js must delegate Escape to unified utils.js handler",
  );

  process.stdout.write("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(script)


def test_ui_004_005_007_api_and_ai_drawer_chat_news() -> None:
    """UI-004, UI-005, UI-007/API-001: streamChatReply !resp.ok, drawer abort/generation gating, news force polling & timeout UI."""
    script = _SHARED_NODE_HELPERS + r"""
(async () => {
  // --- Part A: UI-004 streamChatReply & sendChat + chatPollingReply ---
  const apiSrc = readRel("static/js/api.js");
  const extractFn = (src, name) => extractNamedBlock(src, `function ${name}(`);

  let fetchCalls = [];
  let apiFetchCalls = [];
  const apiSubCtx = {
    CHAT_STREAM_IDLE_TIMEOUT_MS: 5000,
    CHAT_POLL_MAX_ATTEMPTS: 2,
    CHAT_POLL_INTERVAL_MS: 1,
    HAS_MISTRAL_API_KEY: true,
    $logger: { warn() {}, error() {}, info() {} },
    TextDecoder,
    AbortController,
    setTimeout,
    clearTimeout,
    document: { createElement: (t) => makeElement(t), getElementById() { return null; }, querySelector() { return null; } },
    ensureChatDisclaimer() {},
    getStockByKey() { return { symbol: "NVDA", market: "us" }; },
    createRequestToken() { return "tok-1"; },
    showToast() {},
    async csrfFetch(url, opts) {
      fetchCalls.push({ url, opts });
      return {
        ok: false,
        status: 429,
        headers: { get() { return "application/json"; } },
        async json() { return { error: "rate_limit", details: { reason: "上限超過です" } }; },
      };
    },
    async apiFetch(url, opts) {
      apiFetchCalls.push({ url, opts });
      return { response: { ok: true, status: 200 }, data: { fetching: false, reply: "polled" } };
    },
  };
  vm.createContext(apiSubCtx);
  vm.runInContext(
    [
      extractFn(apiSrc, "abortableSleep"),
      extractFn(apiSrc, "readStreamChunk"),
      extractFn(apiSrc, "streamChatReply"),
      extractFn(apiSrc, "chatPollingReply"),
      extractFn(apiSrc, "sendChat"),
    ].join("\n"),
    apiSubCtx,
  );

  // !resp.ok must throw isMistralError=true without isStreamFallback
  let caughtErr = null;
  try {
    await apiSubCtx.streamChatReply({ symbol: "NVDA", market: "us", message: "hi" }, () => {});
  } catch (e) { caughtErr = e; }
  assert(caughtErr && caughtErr.isMistralError === true, "!resp.ok must set isMistralError");
  assert(caughtErr.isStreamFallback !== true, "!resp.ok must NOT set isStreamFallback");
  assert(caughtErr.message === "上限超過です", "must prefer details.reason, got " + caughtErr.message);

  // sendChat must NOT call chatPollingReply on !resp.ok (no duplicate POST)
  const wrapper = makeElement("div");
  const chatSec = makeElement("div"); chatSec.classList.add("chat-section");
  const chatInput = makeElement("input"); chatInput.classList.add("chat-input"); chatInput.value = "test msg";
  const sendBtn = makeElement("button"); sendBtn.classList.add("chat-send-btn");
  const chatLog = makeElement("div"); chatLog.classList.add("chat-log");
  chatSec.append(chatInput, sendBtn, chatLog);
  wrapper.appendChild(chatSec);
  await apiSubCtx.sendChat(wrapper, chatInput);
  assert(apiFetchCalls.length === 0, "sendChat must NOT invoke polling fallback on !resp.ok HTTP error");

  // --- Part B: UI-005 openAiDrawer aborts & generation-gates old request ---
  const uiSrc = readRel("static/js/ui.js");
  const els = new Map();
  ["ai-drawer-overlay", "ai-drawer-symbol", "ai-drawer-name", "ai-drawer-chat-messages", "aiDrawerInput", "sendAiDrawerBtn"]
    .forEach((id) => els.set(id, makeElement("div", id)));
  els.get("sendAiDrawerBtn").textContent = "送信";

  let rejectFirstReq = null;
  let capturedSignal = null;
  const uiDrawerCtx = {
    HTMLElement: function () {},
    document: {
      activeElement: null,
      getElementById(id) { return els.get(id) || null; },
      createElement(t) { return makeElement(t); },
      createTextNode(txt) { return { nodeType: 3, textContent: txt, contains() { return false; } }; },
    },
    AbortController,
    setTimeout,
    clearTimeout,
    HAS_MISTRAL_API_KEY: true,
    createRequestToken() { return "tok-drawer"; },
    lockBodyScroll() {},
    streamChatReply(payload, onDelta, opts) {
      capturedSignal = opts?.signal;
      return new Promise((_res, rej) => { rejectFirstReq = rej; });
    },
  };
  vm.createContext(uiDrawerCtx);
  const drawerVarsIdx = uiSrc.indexOf("let currentDrawerSymbol =");
  const drawerEndIdx = uiSrc.indexOf("function updateTabCounts(");
  vm.runInContext(uiSrc.slice(drawerVarsIdx, drawerEndIdx), uiDrawerCtx);

  uiDrawerCtx.openAiDrawer("AAPL", "Apple", "us");
  els.get("aiDrawerInput").value = "First question";
  const pendingSend = uiDrawerCtx.sendAiDrawerMessage();
  assert(capturedSignal && !capturedSignal.aborted, "Signal should be un-aborted initially");
  assert(els.get("aiDrawerInput").disabled === true, "Input disabled while sending");

  // Switch to another stock while request #1 is still in flight
  uiDrawerCtx.openAiDrawer("MSFT", "Microsoft", "us");
  assert(capturedSignal.aborted === true, "openAiDrawer must abort previous in-flight request");
  assert(els.get("aiDrawerInput").disabled === false, "openAiDrawer must re-enable input");
  assert(els.get("sendAiDrawerBtn").disabled === false, "openAiDrawer must re-enable send button");
  assert(els.get("sendAiDrawerBtn").textContent === "送信", "openAiDrawer must reset send button label");

  // Simulate late error settling from request #1
  rejectFirstReq(new Error("late failure"));
  await pendingSend;
  assert(els.get("aiDrawerInput").disabled === false, "stale finally must not corrupt new state");
  assert(
    !els.get("ai-drawer-chat-messages").querySelector(".ai-msg")?.textContent?.includes("late failure"),
    "stale catch must not overwrite new drawer messages",
  );

  // --- Part C: UI-007 loadNews polls newsUrl and updates Timeout placeholders ---
  const usBox = makeElement("div", "news-us");
  const jpBox = makeElement("div", "news-jp");
  const trendsBox = makeElement("div", "news-trends");
  const metaStats = makeElement("div", "news-meta-stats");
  const refreshBtn = makeElement("button", "newsRefreshBtn");
  const newsPollUrls = [];
  const newsCtx = {
    HAS_MISTRAL_API_KEY: true,
    CONSTANTS: { TIMEOUT: { NEWS_REQUEST: 30 } },
    state: { isLoadingNews: false },
    DOM: {
      get(id) {
        return {
          "news-us": usBox, "news-jp": jpBox, "news-trends": trendsBox,
          "news-meta-stats": metaStats, "newsRefreshBtn": refreshBtn,
        }[id] || null;
      },
    },
    AbortController,
    setTimeout,
    clearTimeout,
    requestAnimationFrame(fn) { fn(); },
    document: { createElement(t) { return makeElement(t); } },
    $logger: { error() {}, warn() {}, info() {} },
    setButtonLoading() {},
    resetButton() {},
    createRequestToken() { return "tok-news"; },
    showToast() {},
    async apiFetch(url) {
      newsPollUrls.push(url);
      return { response: { ok: true }, data: { fetching: true, message: "取得中" } };
    },
  };
  vm.createContext(newsCtx);
  vm.runInContext(
    extractFn(apiSrc, "abortableSleep") + "\n" + extractFn(apiSrc, "loadNews"),
    newsCtx,
  );
  await newsCtx.loadNews(true);
  assert(
    newsPollUrls.length >= 1 && newsPollUrls.every((u) => u === "/api/news?force=true"),
    "loadNews(true) must preserve ?force=true on polling requests, got " + JSON.stringify(newsPollUrls),
  );
  [usBox, jpBox, trendsBox].forEach((b) => {
    assert(b.textContent !== "最新情報を検索・分析中...", "Timed out box must not stay stuck in loading text");
    assert(b.textContent.includes("タイムアウト"), "Timed out box should show timeout retry prompt");
  });
  const metaText = metaStats.children.map((c) => c.textContent).join("") || metaStats.textContent;
  assert(metaText.includes("タイムアウト"), "metaStats should reflect timeout, got " + metaText);
  assert(newsCtx.state.isLoadingNews === false, "isLoadingNews reset in finally");

  process.stdout.write("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(script)


def test_ui_006_008_009_010_011_012_017_018_019_020_core_widgets() -> None:
    """UI-006,008,009,010,011,012,017,018,019,020: TradingView, charts, portfolio math, marquee a11y, AI analysis."""
    script = _SHARED_NODE_HELPERS + r"""
(async () => {
  // --- UI-006 & UI-020: TradingViewManager loadSymbol + tvScript.onerror ---
  const tvContainer = makeElement("div", "tradingview-chart-container");
  const customContainer = makeElement("div", "custom-tv-box");
  const headEl = makeElement("head");
  const headChildren = headEl.children;
  const tvDoc = {
    body: { classList: makeClassList() },
    head: headEl,
    getElementById(id) {
      if (id === "tradingview-chart-container") return tvContainer;
      if (id === "tradingview-chart-container-inner") return tvContainer.querySelector("#tradingview-chart-container-inner");
      if (id === "custom-tv-box") return customContainer;
      if (id === "tv-script-js") return headChildren.find((n) => n.id === "tv-script-js") || null;
      return null;
    },
    querySelector() { return null; },
    createElement(t) { return makeElement(t); },
  };
  const tvCtx = {
    window: { addEventListener() {}, removeEventListener() {} },
    document: tvDoc,
    console: { log() {}, warn() {}, error() {} },
  };
  tvCtx.window.document = tvDoc;
  vm.createContext(tvCtx);
  vm.runInContext(readRel("static/js/tradingview_manager.js"), tvCtx);

  const mgr = tvCtx.window.TradingViewManager;
  let capturedRenderArgs = null;
  const origRender = mgr.renderAdvancedChart.bind(mgr);
  mgr.renderAdvancedChart = function (cid, ticker, exchange, opts) {
    capturedRenderArgs = { cid, ticker, exchange, opts };
    return origRender(cid, ticker, exchange, opts);
  };

  mgr.loadSymbol("NVDA", "NASDAQ");
  assert(
    capturedRenderArgs.cid === "tradingview-chart-container" &&
      capturedRenderArgs.ticker === "NVDA" &&
      capturedRenderArgs.exchange === "NASDAQ",
    "loadSymbol('NVDA','NASDAQ') misrouted: " + JSON.stringify(capturedRenderArgs),
  );
  mgr.loadSymbol("custom-tv-box", "AAPL", "NASDAQ");
  assert(
    capturedRenderArgs.cid === "custom-tv-box" && capturedRenderArgs.ticker === "AAPL",
    "loadSymbol('custom-tv-box','AAPL','NASDAQ') failed",
  );

  // Trigger script error on #tradingview-chart-container
  const scriptTag = headChildren.find((n) => n.id === "tv-script-js");
  assert(scriptTag && typeof scriptTag.onerror === "function", "tv-script-js must attach onerror");
  scriptTag.onerror();
  assert(scriptTag.dataset.loadFailed === "true", "onerror must mark dataset.loadFailed = true");
  assert(!headChildren.includes(scriptTag), "onerror must remove failed script tag");
  const tvErrEl = tvContainer.querySelector(".tv-load-error");
  assert(tvErrEl && tvErrEl.getAttribute("role") === "alert", "tv-load-error alert must render inside #tradingview-chart-container");

  // Retry after error must create a fresh script tag
  mgr.loadSymbol("NVDA", "NASDAQ");
  assert(headChildren.some((n) => n.id === "tv-script-js"), "retry after error must append new tv.js script");
  assert(readRel("static/css/index.css").includes(".tv-load-error"), "index.css must style .tv-load-error");

  // --- UI-009, UI-010, UI-017: api.js analysis + marquee a11y ---
  const apiSrc = readRel("static/js/api.js");
  const uiSrc = readRel("static/js/ui.js");
  const sliceFn = extractNamedBlock;

  const marqueeBar = makeElement("div", "indices-bar");
  const toasts = [];
  const miscCtx = {
    document: {
      createElement: (t) => makeElement(t),
      createDocumentFragment() {
        return makeElement("fragment");
      },
      getElementById() { return null; },
    },
    INDEX_BAR_CONFIG: [{ label: "NYダウ", key: "DOW" }, { label: "日経平均", key: "N225" }],
    createEl: (t, c, txt) => { const e = makeElement(t); e.className = c || ""; if (txt != null) e.textContent = txt; return e; },
    showIndexTooltip() {},
    moveIndexTooltip() {},
    hideIndexTooltip() {},
    state: { isAnalyzing: true },
    HAS_MISTRAL_API_KEY: true,
    showToast(msg, color) { toasts.push({ msg, color }); },
    applyAnalysisError() {},
    clearAnalysisError() {},
    applyArrowToElement() {},
    renderAiSummaryStructured() {},
    formatPrice(v) { return `$${v}`; },
    ensureStockDetails() {},
  };
  vm.createContext(miscCtx);
  vm.runInContext(
    [
      sliceFn(apiSrc, "function buildIndexChip("),
      sliceFn(apiSrc, "function ensureIndicesBarStructure("),
      sliceFn(apiSrc, "function applyAnalysisResult("),
      sliceFn(apiSrc, "async function analyzeStock("),
    ].join("\n"),
    miscCtx,
  );
  // Make replaceChildren understand fragment children
  marqueeBar.replaceChildren = (frag) => {
    marqueeBar.children = [...frag.children];
  };
  miscCtx.ensureIndicesBarStructure(marqueeBar);
  assert(marqueeBar.children.length === 4, "2 original + 2 duplicate marquee chips");
  assert(marqueeBar.children[0].tabIndex === 0 && !marqueeBar.children[0].getAttribute("aria-hidden"), "Original chips tabIndex=0");
  assert(
    marqueeBar.children[2].tabIndex === -1 && marqueeBar.children[2].getAttribute("aria-hidden") === "true",
    "Duplicate marquee chips must be tabIndex=-1 & aria-hidden=true",
  );

  // applyAnalysisResult with numeric upside_3m & analyzed_at only
  const analysisWrapper = makeElement("div");
  analysisWrapper.dataset.stockKey = "us:NVDA";
  const aiSec = makeElement("div"); aiSec.classList.add("ai-section");
  const upsideEl = makeElement("span"); upsideEl.classList.add("ai-upside");
  const sliderEl = makeElement("div"); sliderEl.classList.add("ai-slider");
  aiSec.append(upsideEl, sliderEl);
  analysisWrapper.appendChild(aiSec);
  miscCtx.applyAnalysisResult(analysisWrapper, { symbol: "NVDA", market: "us" }, {
    upside_3m: 15.5,
    analyzed_at: "2026-11-14T00:00:00Z",
    sentiment: "Bullish",
  });
  assert(upsideEl.style.color === "#7dffb0", "Numeric upside_3m > 0 must succeed without TypeError");
  assert(sliderEl.querySelectorAll(".ai-extra-card").length >= 1, "analyzed_at alone must render .ai-extra-card");

  // analyzeStock shows warning toast when state.isAnalyzing
  await miscCtx.analyzeStock(makeElement("button"), analysisWrapper);
  assert(toasts.length === 1 && toasts[0].color === "#ffcc66", "Expected warning toast while analyzing");

  // --- UI-008 & API-006: ui.js interval forwarding & summary rehydration ---
  const drawerOverlay = makeElement("div", "stock-detail-drawer-overlay");
  drawerOverlay.classList.add("hidden");
  const drawerEl = makeElement("div", "stock-detail-drawer");
  const drawerChartContent = makeElement("div", "drawerTabChartContent");
  const drawerAiContent = makeElement("div", "drawerTabAiContent");
  drawerOverlay.appendChild(drawerEl);
  const cardWrapper = makeElement("div");
  cardWrapper.dataset.stockKey = "us:NVDA";
  cardWrapper.__aiTechnicalLines = { summary: "サポート堅調", trend_bias: "Bullish", lines: [] };
  const detailPanel = makeElement("div"); detailPanel.classList.add("detail-panel");
  const ctnr = makeElement("div"); ctnr.classList.add("chart-container");
  detailPanel.appendChild(ctnr);
  cardWrapper.appendChild(detailPanel);

  const refreshCalls = [];
  const drawerCtx = {
    HTMLElement: function () {},
    document: {
      activeElement: null,
      getElementById(id) {
        return {
          "stock-detail-drawer-overlay": drawerOverlay,
          "stock-detail-drawer": drawerEl,
          "drawerTabChartContent": drawerChartContent,
          "drawerTabAiContent": drawerAiContent,
        }[id] || null;
      },
      createElement: (t) => makeElement(t),
      querySelector() { return null; },
    },
    setTimeout(fn) { fn(); return 1; },
    clearTimeout() {},
    currentDrawerActiveWrapper: null,
    stockDetailDrawerTrigger: null,
    stockDetailDrawerRefreshTimer: null,
    createEl: (t, c, txt) => { const e = makeElement(t); e.className = c || ""; if (txt != null) e.textContent = txt; return e; },
    getLatestStockForDrawer: (s) => s,
    renderStockDetailDrawerHeader() {},
    lockBodyScroll() {},
    makeStockKey: (m, s) => `${m}:${s}`,
    getChartPref(_k, kind, fb) { return kind === "interval" ? "1h" : kind === "period" ? "1y" : fb; },
    refreshStockChart(w, p, iv) { refreshCalls.push({ w, p, iv }); },
    ensureStockDetails() {},
    closeStockDetailDrawer() {},
  };
  vm.createContext(drawerCtx);
  vm.runInContext(
    sliceFn(uiSrc, "function openStockDetailDrawer(") + "\n" + sliceFn(uiSrc, "function renderAiTechnicalLinesSummary("),
    drawerCtx,
  );
  drawerCtx.openStockDetailDrawer({ symbol: "NVDA", market: "us" }, cardWrapper);
  assert(
    refreshCalls.length === 1 && refreshCalls[0].p === "1y" && refreshCalls[0].iv === "1h",
    "openStockDetailDrawer must pass period and interval to refreshStockChart: " +
      JSON.stringify(refreshCalls.map((c) => ({ p: c.p, iv: c.iv }))),
  );
  assert(
    drawerChartContent.querySelector(".ai-tech-lines-summary")?.textContent !== undefined &&
      !drawerChartContent.querySelector(".ai-tech-lines-summary").classList.contains("hidden"),
    "openStockDetailDrawer must rehydrate .ai-tech-lines-summary from wrapper.__aiTechnicalLines",
  );
  assert(
    uiSrc.includes("refreshStockChart(wrapper, period, interval)") &&
      uiSrc.includes("refreshStockChart(targetWrapper, period, interval)") &&
      uiSrc.includes("getFreshPrefetchedHistory(stockKey, period, interval)"),
    "ui.js must propagate interval across toggleDetail and triggerAiTechnicalLines",
  );

  // --- UI-011 & UI-018: chart.js createEl button type, fullscreen empty info, stale chartFetchId ---
  const chartSrc = readRel("static/js/chart.js");
  const fsWrapper = makeElement("div", "fs-chart-canvas-wrapper");
  const fsCanvas = makeElement("canvas", "fs-chart-canvas");
  fsWrapper.appendChild(fsCanvas);
  const chartCtx = {
    NO_HISTORY_MSG: "チャート履歴が見つかりません",
    document: {
      createElement: (t) => makeElement(t),
      getElementById(id) { return id === "fs-chart-canvas-wrapper" ? fsWrapper : null; },
      querySelector() { return null; },
    },
    getChartAnimationEnabled() { return false; },
    _safeDestroyChart() {},
    showChartError() { throw new Error("Must not target card wrapper when options.targetCanvas set"); },
  };
  vm.createContext(chartCtx);
  vm.runInContext(
    sliceFn(chartSrc, "function createEl(") + "\n" + sliceFn(chartSrc, "function drawChart("),
    chartCtx,
  );
  const btn = chartCtx.createEl("button", "test-btn", "Go");
  assert(btn.type === "button", "createEl('button') must set type='button'");
  chartCtx.drawChart(null, [], [], { targetCanvas: fsCanvas });
  const fsErr = fsWrapper.querySelector(".chart-error");
  assert(fsErr && fsErr.classList.contains("info") && fsErr.textContent === "チャート履歴が見つかりません",
    "Empty fullscreen history must display info message in #fs-chart-canvas-wrapper");

  // refreshStockChart checks AbortError/signal.aborted/chartFetchId before TimeoutError
  const catchIdx = chartSrc.indexOf("} catch (e) {", chartSrc.indexOf("async function refreshStockChart("));
  const catchSlice = chartSrc.slice(catchIdx, catchIdx + 450);
  const abortCheckPos = catchSlice.indexOf("AbortError");
  const timeoutCheckPos = catchSlice.indexOf("TimeoutError");
  assert(
    abortCheckPos !== -1 && timeoutCheckPos !== -1 && abortCheckPos < timeoutCheckPos &&
      catchSlice.includes("wrapper.dataset.chartFetchId !== currentFetchId"),
    "refreshStockChart catch must check AbortError/signal.aborted/chartFetchId before TimeoutError",
  );

  // --- UI-012: isUsdHolding consistency & holiday backfill in drawPortfolioSummaryChart ---
  const pfNodes = { "pf-summary-canvas": makeElement("canvas", "pf-summary-canvas"), "pf-summary-loading": makeElement("div") };
  let capturedPoints = null;
  const pfCtx = {
    DOM: { get(id) { return pfNodes[id] || null; } },
    portfolioFixedExchangeRate: 150,
    state: { exchangeRate: 150, indices: {} },
    document: {
      getElementById(id) { return pfNodes[id] || null; },
      createElement: (t) => makeElement(t),
    },
    resolvePortfolioChartAnimationControl() { return { animation: false, updateMode: "none" }; },
    makeStockKey(m, s) { return `${m}:${s}`; },
    toFiniteNumber(v, fb) { return typeof v === "number" && Number.isFinite(v) ? v : fb; },
    buildLocalDateKey(d) { return d.toISOString().slice(0, 10); },
    drawSectorPieChart() {},
    pfSummaryChartInstance: null,
    lastPfChartSignature: "",
    Chart: function (_ctx, cfg) {
      capturedPoints = cfg.data.datasets[0].data;
      this.data = cfg.data;
      this.options = cfg.options;
      this.update = () => {};
    },
  };
  vm.createContext(pfCtx);
  vm.runInContext(
    [
      sliceFn(uiSrc, "function isUsdHolding("),
      sliceFn(uiSrc, "function drawPortfolioSummaryChart("),
      sliceFn(uiSrc, "function calculatePortfolioMetrics("),
      sliceFn(uiSrc, "function updatePortfolioHeader("),
    ].join("\n"),
    pfCtx,
  );
  // JP stock closed on Jan 2 (only has Jan 3 = ¥2000); US stock (missing currency field!) has Jan 2=$100 & Jan 3=$100
  const holdings = [
    { symbol: "AAPL", market: "us", shares: 1, avg_price: 90, price: 100, change: 0, chart_data: [
      { x: "2026-01-02T12:00:00Z", price: 100 },
      { x: "2026-01-03T12:00:00Z", price: 100 },
    ] },
    { symbol: "7203.T", market: "jp", currency: "JPY", shares: 10, avg_price: 1800, price: 2000, change: 0, chart_data: [
      { x: "2026-01-03T06:00:00Z", price: 2000 },
    ] },
  ];
  pfCtx.drawPortfolioSummaryChart(holdings);
  assert(capturedPoints && capturedPoints.length === 2, "Expected 2 portfolio summary dates");
  assert(capturedPoints[0].y === 1 * 100 * 150 + 10 * 2000,
    "Jan 2 must backfill JP pre-history holiday with earliest close (35000), got " + capturedPoints[0].y);

  // --- UI-019: ai_portfolio.js error message forwarding ---
  const aiPfSrc = readRel("static/js/ai_portfolio.js");
  assert(
    aiPfSrc.includes('showAiPortfolioFailure(\n        err?.message || "マイポートフォリオへの反映に失敗しました"') ||
      aiPfSrc.includes('showAiPortfolioFailure(err?.message || "マイポートフォリオへの反映に失敗しました"'),
    "copyAiPortfolioToMy must pass err?.message to showAiPortfolioFailure",
  );

  process.stdout.write("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(script)


def test_ui_013_014_015_016_heatmap_settings_alerts_themes() -> None:
    """UI-013,014/API-003,015,016: heatmap tooltip/error persistence, settings/setup UX, FX>0, checkAlerts, CSS theme vars."""
    script = _SHARED_NODE_HELPERS + r"""
(async () => {
  // --- UI-013: heatmap keyboard tooltip positioning & error state across 2D/3D toggle ---
  const hmEls = new Map();
  const canvas2d = makeElement("div", "heatmap-canvas");
  const canvas3d = makeElement("div", "heatmap-3d-canvas");
  const tooltip = makeElement("div", "heatmap-tooltip");
  tooltip._rect = { left: 0, top: 0, width: 180, height: 60, bottom: 60 };
  const btn2d = makeElement("button", "view-2d");
  const btn3d = makeElement("button", "view-3d");
  const controls3d = makeElement("div", "heatmap-3d-controls");
  const toggleUs = makeElement("button", "toggle-us");
  const toggleJp = makeElement("button", "toggle-jp");
  [canvas2d, canvas3d, tooltip, btn2d, btn3d, controls3d, toggleUs, toggleJp].forEach((e) => hmEls.set(e.id, e));

  let domReadyFn = null;
  const hmBody = makeElement("body");
  const hmCtx = {
    document: {
      body: hmBody,
      addEventListener(t, fn) { if (t === "DOMContentLoaded") domReadyFn = fn; },
      getElementById(id) { return hmEls.get(id) || null; },
      createElement: (t) => makeElement(t),
    },
    window: {
      innerWidth: 1280,
      innerHeight: 800,
      devicePixelRatio: 1,
      addEventListener() {},
      location: { href: "" },
    },
    apiFetch: async () => ({ data: { stocks: [] } }),
    AbortController,
    setTimeout,
    clearTimeout,
    requestAnimationFrame() {},
    cancelAnimationFrame() {},
    showToast() {},
    logger: { error() {}, warn() {}, info() {} },
    toFiniteNumber: (v, fb = 0) => { const n = Number(v); return Number.isFinite(n) ? n : fb; },
    formatNumber: (v, m) => (m === "jp" ? "¥" : "$") + Number(v || 0).toFixed(2),
    formatCompact: (v) => String(v || 0),
    getColorSchemePreference: () => "us_standard",
    THREE: {
      PCFSoftShadowMap: 1,
      Scene: function () { this.add = () => {}; },
      Color: function () {},
      FogExp2: function () {},
      PerspectiveCamera: function () { this.position = { set() {} }; this.updateProjectionMatrix = () => {}; },
      WebGLRenderer: function () {
        this.domElement = makeElement("canvas");
        this.shadowMap = {};
        this.setSize = () => {};
        this.setPixelRatio = () => {};
        this.render = () => {};
      },
      OrbitControls: function () { this.update = () => {}; },
      AmbientLight: function () {},
      DirectionalLight: function () { this.position = { set() {} }; },
      GridHelper: function () { this.position = { y: 0 }; },
      Raycaster: function () { this.setFromCamera = () => {}; this.intersectObjects = () => []; },
      Vector2: function () {},
    },
  };
  hmCtx.window.window = hmCtx.window;
  vm.createContext(hmCtx);
  vm.runInContext(readRel("static/js/heatmap.js"), hmCtx);
  domReadyFn();

  // Wait for empty stocks apiFetch to resolve and call showError
  await new Promise((r) => setTimeout(r, 20));
  assert(canvas2d.querySelector(".heatmap-error-state") !== null, "2D canvas shows initial error state");

  // Toggle to 3D and verify error state persists in 3D container, then back to 2D
  btn3d.click();
  assert(canvas3d.querySelector(".heatmap-error-state") !== null, "Error must re-render on switchViewMode('3d')");
  btn2d.click();
  assert(canvas2d.querySelector(".heatmap-error-state") !== null, "Error must re-render on switchViewMode('2d')");

  // Keyboard focus on a 2D heatmap node positions #heatmap-tooltip using getBoundingClientRect
  let mockFetchMode = "empty";
  hmCtx.apiFetch = async () => mockFetchMode === "empty"
    ? { data: { stocks: [] } }
    : { data: { stocks: [{ symbol: "NVDA", name: "NVIDIA", market: "us", price: 140, change_percent: 2.5, market_cap: 3e12, sector: "Tech" }] } };
  mockFetchMode = "populated";
  // Trigger reload via market toggle click (us -> jp)
  toggleJp.click();
  await new Promise((r) => setTimeout(r, 25));
  const firstNode = canvas2d.querySelector(".heatmap-node");
  assert(firstNode !== null, "Expected rendered .heatmap-node");
  firstNode._rect = { left: 200, top: 150, width: 120, height: 80, bottom: 230 };
  tooltip.style.left = "";
  tooltip.style.top = "";
  firstNode.dispatchEvent({ type: "focus" });
  assert(tooltip.classList.contains("show"), "Keyboard focus must show tooltip");
  assert(tooltip.style.left.endsWith("px") && tooltip.style.top.endsWith("px"),
    "Keyboard focus must set tooltip coordinates (got left=" + tooltip.style.left + ", top=" + tooltip.style.top + ")");

  // Check tooltip positioning helpers and 3D blur in source
  const hmSrc = readRel("static/js/heatmap.js");
  assert(hmSrc.includes("function positionTooltipAt("), "positionTooltipAt helper expected");
  assert(hmSrc.includes('renderer.domElement.addEventListener("blur", hideTooltip)'), "3D canvas must hide tooltip on blur");

  // --- UI-014 (settings.js & setup.js password toggle, moveStock focus, modelGrid refresh on save) ---
  const setupHtml = readRel("templates/setup.html");
  const settingsHtml = readRel("templates/settings.html");
  assert(setupHtml.includes("&#128065;") && settingsHtml.includes("&#128065;"), "Initial password toggle HTML icon is 👁");

  const setupSrc = readRel("static/js/setup.js");
  const settingsSrc = readRel("static/js/settings.js");
  // Load togglePasswordVisibility and moveStock/renderList from settings.js
  const pwInput = makeElement("input", "mistral-api-key-input"); pwInput.type = "password";
  const pwBtn = makeElement("button"); pwBtn.classList.add("password-toggle"); pwBtn.dataset.target = "mistral-api-key-input";
  const pwIcon = makeElement("span"); pwIcon.classList.add("toggle-icon"); pwIcon.textContent = "\u{1F441}";
  pwBtn.appendChild(pwIcon);
  const usListEl = makeElement("ul", "us-list");
  let savedOrder = ["AAPL", "NVDA"];
  const settingsMoveCtx = {
    DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
    stocksMoveInFlight: new Set(),
    document: {
      getElementById(id) {
        if (id === "mistral-api-key-input") return pwInput;
        if (id === "us-list") return usListEl;
        return null;
      },
      querySelector(sel) { return sel.includes("mistral-api-key-input") ? pwBtn : null; },
      createElement: (t) => makeElement(t),
    },
    getSortOrder: () => savedOrder,
    saveSortOrder: (_m, ord) => { savedOrder = ord; },
    sortIndex: (ord, sym) => { const idx = ord.indexOf(sym); return idx === -1 ? 999 : idx; },
    ensureDragContainer() {},
    addDragEvents() {},
    attachInlineDeleteConfirm() {},
    executeDeleteStock() {},
    showSettingsMessage() {},
    fetchStocksForMarket: async () => [{ symbol: "AAPL", name: "Apple" }, { symbol: "NVDA", name: "NVIDIA" }],
  };
  vm.createContext(settingsMoveCtx);
  const renderStart = settingsSrc.indexOf("function renderList(");
  const renderEnd = settingsSrc.indexOf("async function fetchStocksForMarket(");
  const fnStart = settingsSrc.indexOf("function togglePasswordVisibility(");
  const fnEnd = settingsSrc.indexOf("function initSettingsTabs(");
  vm.runInContext(
    settingsSrc.slice(renderStart, renderEnd) + "\n" + settingsSrc.slice(fnStart, fnEnd),
    settingsMoveCtx,
  );

  settingsMoveCtx.togglePasswordVisibility("mistral-api-key-input");
  assert(pwInput.type === "text" && pwIcon.textContent === "\u{1F576}", "Revealing password must show 🕶");
  settingsMoveCtx.togglePasswordVisibility("mistral-api-key-input");
  assert(pwInput.type === "password" && pwIcon.textContent === "\u{1F441}", "Masking password must show 👁");

  await settingsMoveCtx.moveStock("us", "NVDA", -1);
  assert(JSON.stringify(savedOrder) === JSON.stringify(["NVDA", "AAPL"]), "moveStock must swap order");
  const movedNvdaUpBtn = usListEl.querySelectorAll(".stock-item")
    .find((el) => el.dataset.symbol === "NVDA")
    ?.querySelector(".stock-move-up");
  assert(movedNvdaUpBtn && movedNvdaUpBtn.focused === true, "moveStock must restore focus to moved button");

  assert(
    setupSrc.includes('icon.textContent = isPassword ? "\\u{1F576}" : "\\u{1F441}"'),
    "setup.js togglePasswordVisibility must match fixed icon mapping",
  );
  assert(
    settingsSrc.includes("renderModelOptions(\n            modelGrid,\n            data.available_models") ||
      settingsSrc.includes("renderModelOptions(modelGrid, data.available_models"),
    "settings.js must re-render modelGrid immediately after saving credentials",
  );

  // --- API-003 (index_main.js rejects avg_fx_rate <= 0 & index.html min="0.01") ---
  const indexHtml = readRel("templates/index.html");
  assert(indexHtml.includes('id="pf-fx-rate-input"') && indexHtml.includes('min="0.01"'),
    "index.html pf-fx-rate-input should require min='0.01'");
  const indexMainSrc = readRel("static/js/index_main.js");
  assert(indexMainSrc.includes("fxRateParsed.value <= 0"), "index_main.js must reject avg_fx_rate <= 0");

  // --- UI-015 (checkAlerts ignores non-finite price) ---
  let alertTriggered = false;
  const alertStart = indexMainSrc.indexOf("function checkAlerts(");
  let alertDepth = 0, alertEnd = -1;
  for (let i = indexMainSrc.indexOf("{", alertStart); i < indexMainSrc.length; i++) {
    if (indexMainSrc[i] === "{") alertDepth++;
    else if (indexMainSrc[i] === "}") { alertDepth--; if (alertDepth === 0) { alertEnd = i + 1; break; } }
  }
  const alertCtx = {
    makeStockKey: (m, s) => `${m}:${s}`,
    getAlertsConfig: () => ({ "us:NVDA": { priceDown: 100, triggeredDown: false } }),
    showToast() { alertTriggered = true; },
    showBrowserNotification() { alertTriggered = true; },
    saveAlertsConfig() {},
  };
  vm.createContext(alertCtx);
  vm.runInContext(indexMainSrc.slice(alertStart, alertEnd), alertCtx);
  alertCtx.checkAlerts({ symbol: "NVDA", market: "us", price: null }, 120);
  alertCtx.checkAlerts({ symbol: "NVDA", market: "us", price: NaN }, 120);
  assert(alertTriggered === false, "checkAlerts must ignore null/NaN stock.price");

  // --- UI-016 (colors.css tokens + state.js reads document.body) ---
  const colorsCss = readRel("static/css/colors.css");
  ["--success:", "--warning:", "--purple:", "--gold:"].forEach((tok) => {
    assert(colorsCss.includes(tok), "colors.css missing " + tok);
  });
  const jpBlock = colorsCss.slice(colorsCss.indexOf("body.theme-color-jp {"));
  assert(
    jpBlock.includes("--danger: var(--color-neg-jp)") && jpBlock.includes("--success: var(--color-pos-jp)"),
    "body.theme-color-jp must override --danger and --success",
  );
  let isJpTheme = false;
  const stateCtx = {
    APP_CONFIG: {},
    DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
    document: { body: { id: "body" }, documentElement: { id: "html" } },
    window: {
      getComputedStyle(el) {
        return {
          getPropertyValue(name) {
            if (el !== stateCtx.document.body) return "";
            if (name === "--danger") return isJpTheme ? "#10b981" : "#f43f5e";
            if (name === "--success") return isJpTheme ? "#f43f5e" : "#10b981";
            return "";
          },
        };
      },
    },
  };
  vm.createContext(stateCtx);
  vm.runInContext(readRel("static/js/state.js"), stateCtx);
  assert(stateCtx.window.CONSTANTS.COLORS.DANGER === "#f43f5e", "Initial US --danger read from body");
  isJpTheme = true;
  stateCtx.window.CONSTANTS.refreshColors();
  assert(stateCtx.window.CONSTANTS.COLORS.DANGER === "#10b981", "state.js refreshColors must read JP --danger from document.body");
  assert(stateCtx.window.CONSTANTS.COLORS.SUCCESS === "#f43f5e", "state.js refreshColors must read JP --success from document.body");

  process.stdout.write("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(script)


def test_ui_018_all_core_template_buttons_have_explicit_type() -> None:
    """UI-018: Verify every <button> in core templates and ui.js retry button specifies type."""
    for rel in (
        "templates/base.html",
        "templates/index.html",
        "templates/setup.html",
        "templates/settings.html",
        "templates/screener.html",
        "templates/heatmap.html",
    ):
        html = (ROOT_DIR / rel).read_text(encoding="utf-8")
        for match in re.finditer(r"<button\b([^>]*)>", html, re.IGNORECASE | re.DOTALL):
            assert re.search(r"\btype\s*=", match.group(1), re.IGNORECASE), (
                f"{rel}: <button> missing explicit type attribute: {match.group(0)}"
            )

    ui_src = (ROOT_DIR / "static/js/ui.js").read_text(encoding="utf-8")
    assert 'retryBtn.type = "button"' in ui_src
