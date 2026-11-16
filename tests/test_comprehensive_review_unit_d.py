"""Node VM regression tests for Unit D frontend fixes ([D-1] through [D-7])."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_node_check(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.fail("Node.js is required for frontend runtime tests")
    res = subprocess.run(
        [node, "-"],
        input=script,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(ROOT),
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, f"Node check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"


def test_d1_api_client_server_error_backoff_and_single_onerror_callback():
    _run_node_check(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const apiClientCode = fs.readFileSync("static/js/api_client.js", "utf8");
const instances = [];

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.listeners = new Map();
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    instances.push(this);
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const arr = this.listeners.get(type);
    if (arr) this.listeners.set(type, arr.filter((f) => f !== fn));
  }
  dispatchServerError(msg) {
    const evt = { data: JSON.stringify({ message: msg }) };
    if (typeof this.onerror === "function") this.onerror(evt);
    for (const fn of this.listeners.get("error") || []) fn(evt);
  }
  dispatchTransportError() {
    const evt = new Error("transport error");
    if (typeof this.onerror === "function") this.onerror(evt);
    for (const fn of this.listeners.get("error") || []) fn(evt);
  }
  dispatchHeartbeat() {
    const evt = { lastEventId: "1" };
    for (const fn of this.listeners.get("heartbeat") || []) fn(evt);
  }
  close() {
    this.readyState = 2;
  }
}

const context = {
  window: { addEventListener() {}, removeEventListener() {} },
  document: { hidden: false, addEventListener() {} },
  EventSource: FakeEventSource,
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval() {},
  console,
  process,
  Error,
  Map,
  Set,
  Math,
  Date,
  Number,
  String,
  JSON,
  MNS_DEBUG_LOGS: false,
};
vm.createContext(context);
vm.runInContext(apiClientCode, context);

const client = new context.window.APIClient("", {
  sseReconnectBaseDelay: 100,
  sseReconnectMaxDelay: 5000,
});
const errors = [];
const es1 = client.openSSE("/api/stocks/stream", () => {}, (e) => errors.push(e.message), {
  maxReconnectAttempts: 5,
});

// 1) onopen alone must NOT wipe backoff when the server immediately emits `event: error`.
client.sseReconnectAttempt = 2;
es1.onopen();
assert.strictEqual(client.sseReconnectAttempt, 2, "onopen must not reset backoff before real data");

es1.dispatchServerError("too many SSE connections");
assert.deepStrictEqual(errors, ["too many SSE connections"], "server error must fire onError once");
assert.strictEqual(client.sseReconnectAttempt, 3, "server error must preserve and increment backoff");

// Cancel pending timer cleanly before heartbeat verification.
clearTimeout(client.ssePendingReconnectTimeout);
client.ssePendingReconnectTimeout = null;
client._reconnecting = false;

// 2) Real heartbeat/message resets sseReconnectAttempt back to 0.
const es2 = client.openSSE("/api/stocks/stream", () => {}, (e) => errors.push(e.message));
client.sseReconnectAttempt = 4;
es2.dispatchHeartbeat();
assert.strictEqual(client.sseReconnectAttempt, 0, "heartbeat must reset sseReconnectAttempt");

// 3) Transport error without .data triggers reconnect cleanly without throwing.
es2.dispatchTransportError();
assert.strictEqual(client.sseReconnectAttempt, 1);

client.closeSSE();
"""
    )


def test_d2_d3_d4_frontend_api_utils_and_state_contracts():
    _run_node_check(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// D-3: utils.js showToast("error") bypasses 3s duplicate suppression.
const toastContainer = {
  children: [],
  appendChild(el) { this.children.push(el); },
};
const utilsCtx = {
  window: {
    matchMedia: () => ({ matches: false }),
    location: { search: "", origin: "http://127.0.0.1" },
    addEventListener() {},
    removeEventListener() {},
  },
  document: {
    body: { classList: { contains: () => false }, appendChild() {} },
    addEventListener() {},
    removeEventListener() {},
    getElementById(id) { return id === "toast-container" ? toastContainer : null; },
    createElement(tag) {
      return {
        tagName: tag,
        className: "",
        style: { setProperty() {} },
        textContent: "",
        dataset: {},
        classList: { add() {}, remove() {}, contains: () => false },
        appendChild() {},
        addEventListener() {},
        removeEventListener() {},
        remove() {},
        isConnected: true,
      };
    },
  },
  requestAnimationFrame: (fn) => fn(),
  setTimeout: () => 0,
  clearTimeout() {},
  console,
  Map,
  Set,
  Date,
  Math,
};
vm.createContext(utilsCtx);
vm.runInContext(fs.readFileSync("static/js/utils.js", "utf8"), utilsCtx);

utilsCtx.showToast("same-info", "info", 5000);
utilsCtx.showToast("same-info", "info", 5000);
assert.strictEqual(toastContainer.children.length, 1, "non-error duplicates suppressed");

utilsCtx.showToast("same-err", "error", 5000);
utilsCtx.showToast("same-err", "error", 5000);
assert.strictEqual(toastContainer.children.length, 3, "semantic 'error' toasts must not be suppressed");

// D-4: state.js cache limits use while loop & refreshCredentialState syncs APP_CONFIG.
const stateCtx = {
  window: {
    APP_CONFIG: { ai_provider: "mistral", preview_mode: true, has_ai_access: false },
    MNS_CONFIG: { isPreviewMode: true },
    apiClient: null,
  },
  APP_CONFIG: { ai_provider: "mistral", preview_mode: true, has_ai_access: false },
  document: { querySelectorAll: () => [], getElementById: () => null },
  fetchWithTimeout: async () => ({
    ok: true,
    json: async () => ({
      ok: true,
      ai_provider: "openai_compatible",
      preview_mode: false,
      has_ai_access: true,
      has_mistral_api_key: false,
      is_ai_technical_lines_eligible: true,
      mistral_model: "mistral-large-latest",
      api_mode: "chat",
      openai_model: "gpt-4o",
    }),
  }),
  FAST_FETCH_TIMEOUT_MS: 3000,
  console,
  process,
  Map,
  Set,
  Date,
  Object,
};
stateCtx.APP_CONFIG = stateCtx.window.APP_CONFIG;
vm.createContext(stateCtx);
vm.runInContext(
  fs.readFileSync("static/js/state.js", "utf8") +
    `
for (let i = 0; i < 65; i++) historyPrefetchCache.set("k" + i, i);
_enforcePrefetchCacheLimit();
if (historyPrefetchCache.size !== PREFETCH_CACHE_MAX_SIZE) {
  throw new Error("historyPrefetchCache not trimmed to max size");
}
_enforcePrefetchCacheLimit(1);
if (historyPrefetchCache.size !== PREFETCH_CACHE_MAX_SIZE - 1) {
  throw new Error("historyPrefetchCache did not reserve space before insert");
}
for (let i = 0; i < 130; i++) stockDetailsCache.set("s" + i, i);
_enforceStockDetailsCacheLimit();
if (stockDetailsCache.size !== STOCK_DETAILS_CACHE_MAX_SIZE) {
  throw new Error("stockDetailsCache not trimmed to max size");
}
_enforceStockDetailsCacheLimit(1);
if (stockDetailsCache.size !== STOCK_DETAILS_CACHE_MAX_SIZE - 1) {
  throw new Error("stockDetailsCache did not reserve space before insert");
}
`,
  stateCtx,
);

(async () => {
  await stateCtx.refreshCredentialState();
  assert.strictEqual(stateCtx.APP_CONFIG.has_ai_access, true);
  assert.strictEqual(stateCtx.APP_CONFIG.is_ai_technical_lines_eligible, true);
  assert.strictEqual(stateCtx.APP_CONFIG.openai_model, "gpt-4o");
  assert.strictEqual(stateCtx.APP_CONFIG.api_mode, "chat");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});

// D-2 structural & runtime checks in api.js (done-with-empty-reply & poll fetching reset).
const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
assert(apiSrc.includes("delete data.fetching;"), "pollData must clear data.fetching");
assert(!apiSrc.includes("await sleep("), "api.js must use abortableSleep instead of sleep()");
assert(
  apiSrc.includes("if (!reply || !String(reply).trim())"),
  "streamChatReply must reject empty done frames",
);
"""
    )


def test_d5_d6_d7_ui_tradingview_and_settings_fixes():
    _run_node_check(
        r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

// D-6: tradingview_manager.js clearContainer cancels async mountWidget & verifies innerId presence.
let widgetMounts = 0;
let onLoadCallback = null;
const tvDom = new Map();

function makeEl(id) {
  const el = {
    id,
    className: "",
    style: {},
    innerHTML: "",
    children: [],
    isConnected: true,
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    removeEventListener() {},
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild(child) {
      this.children.push(child);
      if (child.id) tvDom.set(child.id, child);
      if (child.tagName === "script" && typeof child.onload === "function") {
        onLoadCallback = child.onload;
      }
    },
    replaceChildren() {
      for (const c of this.children) {
        if (c.id) tvDom.delete(c.id);
      }
      this.children = [];
    },
  };
  return el;
}

const chartContainer = makeEl("tradingview-chart-container");
tvDom.set("tradingview-chart-container", chartContainer);

const tvCtx = {
  window: {
    TradingView: { widget: function () { widgetMounts++; } },
    addEventListener() {},
    removeEventListener() {},
  },
  document: {
    body: { classList: { contains: () => false } },
    head: {
      appendChild(s) {
        if (typeof s.onload === "function") onLoadCallback = s.onload;
      },
    },
    querySelector: () => null,
    getElementById: (id) => tvDom.get(id) || null,
    createElement(tag) {
      const el = makeEl("");
      el.tagName = tag;
      return el;
    },
  },
  setTimeout: () => 0,
  clearTimeout() {},
  console,
  String,
  Map,
};
vm.createContext(tvCtx);
vm.runInContext(fs.readFileSync("static/js/tradingview_manager.js", "utf8"), tvCtx);

tvCtx.window.TradingView = {}; // Delay tv.js readiness so script onload runs async.
tvCtx.window.TradingViewManager.renderAdvancedChart("tradingview-chart-container", "AAPL", "NASDAQ");
assert(onLoadCallback, "tv.js onload handler should be registered");
// Close modal before tv.js finishes loading.
tvCtx.window.TradingViewManager.clearContainer(chartContainer);
tvCtx.window.TradingView.widget = function () { widgetMounts++; };
onLoadCallback();
assert.strictEqual(widgetMounts, 0, "stale async onload after clearContainer must not mount widget");

// D-5 & D-7 structural verification on ui.js and settings.js.
const uiSrc = fs.readFileSync("static/js/ui.js", "utf8");
assert(uiSrc.includes("function _destroyPortfolioCharts()"), "ui.js must clean up portfolio Chart instances");
assert(uiSrc.includes("drawSectorPieChart([], null, chartAnimationControl)"), "missing FX must clear stale sector pie");
assert(uiSrc.includes("chatSection.dataset.drawerPrevShow"), "drawer open/close must restore prior chatSection visibility");

const settingsSrc = fs.readFileSync("static/js/settings.js", "utf8");
assert(settingsSrc.includes('agentIdDisplay.style.display = "none";\n      agentIdDisplay.textContent = "";'), "updateConnectionModeDisplay must reset agentIdDisplay");
assert(settingsSrc.includes("\n        payload.openai_embeddings_model = oaEmb;"), "embeddings model must remain clearable when empty string");
"""
    )
