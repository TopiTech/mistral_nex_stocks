"""Regression tests for 2026-11-21 comprehensive code review fixes.

Covers:
  - [AI-01]: Mistral streaming CompletionEvent (chunk.data.usage), OpenAI-compatible
    streaming SSE usage accounting, and Mistral embeddings SimpleNamespace usage normalization.
  - [UI-01]: api.js & ui.js beforeunload abort/cleanup, analyzeStock AbortSignal propagation,
    and sendChat focus-restoration guards across hidden/closed/detached containers.
  - [UI-02]: unregisterWrapper & fetchInitialStocks error cleanup closing open drawer/fullscreen
    modal, fullscreen toolbar sync to card/drawer chart controls, and fs-chart-view-toggle a11y.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app_state import app_state

ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend regression tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return result.stdout.strip()


def test_ai01_stream_mistral_chat_records_usage_from_completion_event_data() -> None:
    """Mistral SDK v2 CompletionEvent exposes chunk.data.usage instead of chunk.usage."""
    from services import ai_service

    class _SlottedUsage:
        __slots__ = ("completion_tokens", "prompt_tokens", "total_tokens")

        def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens
            self.total_tokens = prompt_tokens + completion_tokens

    class _StreamCM:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def __iter__(self):
            yield SimpleNamespace(
                data=SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))],
                    usage=None,
                )
            )
            yield SimpleNamespace(
                data=SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="world"))],
                    usage=_SlottedUsage(24, 12),
                )
            )

        def close(self) -> None:
            return None

    client = MagicMock()
    client.chat.stream.return_value = _StreamCM()
    recorded: list[tuple[object, str]] = []

    with (
        patch.object(app_state.ai, "mistral_stream_semaphore", nullcontext()),
        patch("services.ai_service.is_preview_api_key", return_value=False),
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch("services.ai_service._resolve_agents_target", return_value=(False, "")),
        patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603"),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch(
            "services.ai_service._acquire_mistral_call_slot_with_reservation",
            return_value=(0.0, 0.0),
        ),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch.object(
            app_state.ai,
            "record_mistral_usage",
            side_effect=lambda u, model="": recorded.append((u, model)),
        ),
    ):
        events = list(
            ai_service.stream_mistral_chat("real-key-1234", [{"role": "user", "content": "hi"}])
        )

    assert events[-1] == {"type": "done", "text": "Hello world"}
    assert recorded == [({"prompt_tokens": 24, "completion_tokens": 12}, "mistral-small-2603")]


def test_ai01_stream_openai_chat_records_usage_from_final_chunk() -> None:
    """OpenAI-compatible SSE stream records usage emitted in delta or empty-choices tail chunk."""
    from services import openai_service

    class _FakeStreamResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def iter_lines(self):
            chunks = [
                {"choices": [{"delta": {"content": "Part 1 "}}]},
                {"choices": [{"delta": {"content": "Part 2"}}]},
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 35, "completion_tokens": 15, "total_tokens": 50},
                },
            ]
            for c in chunks:
                yield f"data: {json.dumps(c)}"
            yield "data: [DONE]"

        def close(self) -> None:
            return None

    client = MagicMock()
    client.stream.return_value = _FakeStreamResp()
    recorded: list[tuple[object, str]] = []

    with (
        patch.object(app_state.ai, "openai_stream_semaphore", nullcontext()),
        patch("services.openai_service.is_preview_api_key", return_value=False),
        patch("services.openai_service.get_openai_base_url", return_value="http://example.test/v1"),
        patch("services.openai_service.get_openai_model", return_value="gpt-4o-mini"),
        patch("services.openai_service._get_openai_client", return_value=client),
        patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch.object(
            app_state.ai,
            "record_openai_usage",
            side_effect=lambda u, model="": recorded.append((u, model)),
        ),
    ):
        events = list(
            openai_service.stream_openai_chat("sk-test-1234", [{"role": "user", "content": "hi"}])
        )

    assert events == [
        {"type": "delta", "text": "Part 1 "},
        {"type": "delta", "text": "Part 2"},
        {"type": "done", "text": "Part 1 Part 2"},
    ]
    assert recorded == [
        ({"prompt_tokens": 35, "completion_tokens": 15, "total_tokens": 50}, "gpt-4o-mini")
    ]


def test_ai01_stream_openai_chat_ignores_non_dict_chunk_and_missing_usage() -> None:
    """Malformed JSON chunks or absent usage never break OpenAI-compatible streaming."""
    from services import openai_service

    class _FakeStreamResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def iter_lines(self):
            yield 'data: ["unexpected-array"]'
            yield 'data: {"choices": [{"delta": {"content": "ok"}}], "usage": null}'
            yield "data: [DONE]"

        def close(self) -> None:
            return None

    client = MagicMock()
    client.stream.return_value = _FakeStreamResp()
    before = app_state.ai.openai_usage_stats()["call_count"]

    with (
        patch.object(app_state.ai, "openai_stream_semaphore", nullcontext()),
        patch("services.openai_service.is_preview_api_key", return_value=False),
        patch("services.openai_service.get_openai_base_url", return_value="http://example.test/v1"),
        patch("services.openai_service.get_openai_model", return_value="gpt-4o-mini"),
        patch("services.openai_service._get_openai_client", return_value=client),
        patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
    ):
        events = list(
            openai_service.stream_openai_chat("sk-test-1234", [{"role": "user", "content": "hi"}])
        )

    assert events == [{"type": "delta", "text": "ok"}, {"type": "done", "text": "ok"}]
    assert app_state.ai.openai_usage_stats()["call_count"] == before


def test_ai01_mistral_embeddings_records_namespace_usage() -> None:
    """Mistral embeddings normalize SimpleNamespace usage objects without model_dump."""
    from services import embeddings_service

    client = MagicMock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.5, 0.25])],
        usage=SimpleNamespace(prompt_tokens=19, total_tokens=19),
    )
    recorded: list[tuple[object, str]] = []
    unique_text = f"unique-embedding-{uuid.uuid4()}"

    with (
        patch("credential_manager.is_openai_provider", return_value=False),
        patch("credential_manager.is_preview_api_key", return_value=False),
        patch("services.embeddings_service._get_client", return_value=client),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch(
            "services.ai_service._acquire_mistral_call_slot_with_reservation",
            return_value=(0.0, 0.0),
        ),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch.object(
            app_state.ai,
            "record_mistral_usage",
            side_effect=lambda u, model="": recorded.append((u, model)),
        ),
    ):
        vectors = embeddings_service.get_mistral_embeddings_batch([unique_text], "real-key-1234")

    assert vectors == [[0.5, 0.25]]
    assert recorded == [({"prompt_tokens": 19, "total_tokens": 19}, "mistral-embed")]


def test_ui01_api_beforeunload_and_analyze_stock_abort_cleanup() -> None:
    """api.js beforeunload aborts active SSE/news/analyze requests; analyzeStock passes AbortSignal."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const apiSrc = fs.readFileSync(path.join(process.cwd(), "static/js/api.js"), "utf8");

function extractNamedBlock(source, signature) {
  const start = source.indexOf(signature);
  assert(start !== -1, `Signature not found: ${signature}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`Unclosed block: ${signature}`);
}

const windowListeners = new Map();
let closeSseCalled = 0;
let stopIndicesCalled = 0;
let stopFallbackCalled = 0;
let pollingClearCalled = 0;
let clearedTimerId = null;

class FakeAPIClient {
  closeSSE() { closeSseCalled += 1; }
}

const toasts = [];
const analysisErrors = [];
let capturedSignal = null;
let capturedNewsSignal = null;
let rejectAnalysis = null;
let rejectNews = null;

const ctx = {
  AbortController,
  Error,
  Promise,
  Map,
  Set,
  Date,
  Math,
  JSON,
  console,
  setTimeout,
  clearTimeout: (id) => {
    clearedTimerId = id;
    clearTimeout(id);
  },
  requestAnimationFrame: (fn) => fn(),
  HTMLElement: function () {},
  CustomEvent: function (t) { this.type = t; },
  CONSTANTS: { TIMEOUT: { NEWS_REQUEST: 60000 } },
  APIClient: FakeAPIClient,
  APIError: Error,
  logger: { info() {}, warn() {}, error() {}, debug() {} },
  HAS_MISTRAL_API_KEY: true,
  state: { isLoadingNews: false, isAnalyzing: false },
  DOM: { get: () => null },
  document: {
    getElementById: () => null,
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} } }),
  },
  window: {
    APP_CONFIG: {},
    addEventListener(type, fn) {
      windowListeners.set(type, fn);
    },
  },
  stopLoadIndicesLoop: () => { stopIndicesCalled += 1; },
  stopSseFallbackPolling: () => { stopFallbackCalled += 1; },
  pollingManager: { clearAll: () => { pollingClearCalled += 1; } },
  setButtonLoading() {},
  resetButton() {},
  createRequestToken: () => "tok-1",
  showToast: (m, c) => toasts.push({ m, c }),
  findAllWrappersByStockKey: (k) => [{ dataset: { stockKey: k } }],
  applyAnalysisError: (w, m) => analysisErrors.push({ w, m }),
  applyAnalysisResult() {},
  apiFetch: async (_url, init) => {
    capturedNewsSignal = init?.signal || null;
    return new Promise((_res, rej) => { rejectNews = rej; });
  },
  requestStockAnalysis: (_key, _onProg, signal) => {
    capturedSignal = signal;
    return new Promise((_res, rej) => { rejectAnalysis = rej; });
  },
};

const sseStateSnippet = `
const sseState = {
  stockEventSource: { readyState: 1 },
  ticketAbortController: new AbortController(),
  reconnectTimer: 777,
};
`;
const beforeunloadStart = apiSrc.indexOf("let activeNewsRequestController = null;");
const beforeunloadEnd = apiSrc.indexOf("// #endregion SSE & Real-time Integration");
assert(beforeunloadStart !== -1 && beforeunloadEnd !== -1);

const code = [
  "const $logger = logger;",
  "const sseApiClient = new APIClient();",
  sseStateSnippet,
  "let activeSearchController = new AbortController();",
  "let bulkAnalyzeAbortController = new AbortController();",
  apiSrc.slice(beforeunloadStart, beforeunloadEnd),
  extractNamedBlock(apiSrc, "async function loadNews(forceRefresh = false)"),
  extractNamedBlock(apiSrc, "async function analyzeStock(btnEl, wrapper)"),
].join("\n\n");

vm.createContext(ctx);
vm.runInContext(code, ctx);

const newsPromise = ctx.loadNews(false);
const analyzePromise = ctx.analyzeStock({}, { dataset: { stockKey: "us:AAPL" } });

assert(capturedSignal && capturedSignal.aborted === false, "analyzeStock must pass AbortSignal");
const onBeforeUnload = windowListeners.get("beforeunload");
assert(typeof onBeforeUnload === "function", "beforeunload handler must be registered");

const ticketCtrl = vm.runInContext("sseState.ticketAbortController", ctx);
const searchCtrl = vm.runInContext("activeSearchController", ctx);
const bulkCtrl = vm.runInContext("bulkAnalyzeAbortController", ctx);

onBeforeUnload();

assert.strictEqual(closeSseCalled, 1, "sseApiClient.closeSSE must run");
assert.strictEqual(stopIndicesCalled, 1);
assert.strictEqual(stopFallbackCalled, 1);
assert.strictEqual(pollingClearCalled, 1);
assert.strictEqual(clearedTimerId, 777);
assert.strictEqual(ticketCtrl.signal.aborted, true);
assert.strictEqual(searchCtrl.signal.aborted, true);
assert.strictEqual(bulkCtrl.signal.aborted, true);
assert(capturedNewsSignal && capturedNewsSignal.aborted === true, "loadNews controller must abort on unload");
assert.strictEqual(capturedSignal.aborted, true, "analyzeStock controller must abort on unload");

const abortErr = new Error("Request aborted");
abortErr.name = "AbortError";
rejectNews(abortErr);
rejectAnalysis(abortErr);

Promise.all([newsPromise.catch(() => {}), analyzePromise]).then(() => {
  assert.strictEqual(toasts.length, 0, "unload abort must not flash error toast");
  assert.strictEqual(analysisErrors.length, 0, "unload abort must not render error card");
  process.stdout.write("ui01_api_beforeunload_ok");
}).catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    assert _run_node(script) == "ui01_api_beforeunload_ok"


def test_ui01_send_chat_focus_restoration_guard() -> None:
    """sendChat focuses visible input and refuses to steal focus when hidden/closed/detached."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const apiSrc = fs.readFileSync(path.join(process.cwd(), "static/js/api.js"), "utf8");
const start = apiSrc.indexOf("async function sendChat(");
const open = apiSrc.indexOf("{", start);
let depth = 0;
let end = -1;
for (let i = open; i < apiSrc.length; i += 1) {
  if (apiSrc[i] === "{") depth += 1;
  else if (apiSrc[i] === "}") {
    depth -= 1;
    if (depth === 0) { end = i + 1; break; }
  }
}
const sendChatCode = apiSrc.slice(start, end);

function makeNode(clsList = [], extra = {}) {
  const classes = new Set(clsList);
  const attrs = new Set(extra.attrs || []);
  return {
    classList: { contains: (c) => classes.has(c), add: (c) => classes.add(c) },
    hasAttribute: (a) => attrs.has(a),
    children: [],
    dataset: extra.dataset || {},
    parentNode: null,
    isConnected: extra.isConnected !== undefined ? extra.isConnected : true,
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    querySelector() { return null; },
    closest(sel) {
      let cur = this;
      while (cur) {
        if (sel === "[inert]" && cur.hasAttribute("inert")) return cur;
        if (sel === "[hidden]" && cur.hasAttribute("hidden")) return cur;
        if (sel === ".hidden" && cur.classList.contains("hidden")) return cur;
        if (
          sel === ".detail-panel:not(.open)" &&
          cur.classList.contains("detail-panel") &&
          !cur.classList.contains("open")
        ) return cur;
        if (
          sel === ".chat-section:not(.show)" &&
          cur.classList.contains("chat-section") &&
          !cur.classList.contains("show")
        ) return cur;
        cur = cur.parentNode;
      }
      return null;
    },
  };
}

async function runCase({ isConnected = true, drawerHidden = false, detailClosed = false, chatHidden = false }) {
  let focused = 0;
  const rootOverlay = makeNode(drawerHidden ? ["hidden"] : [], { attrs: drawerHidden ? ["inert"] : [] });
  const detailPanel = makeNode(detailClosed ? ["detail-panel"] : ["detail-panel", "open"]);
  const chatSection = makeNode(chatHidden ? ["chat-section"] : ["chat-section", "show"]);
  const logEl = makeNode(["chat-log"]);
  const sendBtn = makeNode(["chat-send-btn"]);
  sendBtn.disabled = false;
  const input = makeNode(["chat-input"], { isConnected });
  input.value = "Outlook?";
  input.disabled = false;
  input.focus = () => { focused += 1; };

  chatSection.querySelector = (sel) => (sel === ".chat-send-btn" ? sendBtn : sel === ".chat-log" ? logEl : null);
  rootOverlay.appendChild(detailPanel);
  detailPanel.appendChild(chatSection);
  chatSection.appendChild(logEl);
  chatSection.appendChild(sendBtn);
  chatSection.appendChild(input);
  // Allow finding chat-section as nearest ancestor via closest(".chat-section")
  const origClosest = input.closest.bind(input);
  input.closest = (sel) => (sel === ".chat-section" ? chatSection : origClosest(sel));

  const ctx = {
    HAS_MISTRAL_API_KEY: true,
    $logger: { warn() {}, error() {} },
    document: {
      getElementById: () => null,
      createElement: () => makeNode(),
    },
    getStockByKey: () => ({ symbol: "NVDA", market: "us" }),
    ensureChatDisclaimer() {},
    createRequestToken: () => "tok-chat",
    showToast() {},
    streamChatReply: async () => "Bullish outlook",
    chatPollingReply: async () => "Bullish outlook",
  };
  vm.createContext(ctx);
  vm.runInContext(sendChatCode, ctx);
  await ctx.sendChat({ dataset: { stockKey: "us:NVDA" } }, input);
  return focused;
}

(async () => {
  assert.strictEqual(await runCase({}), 1, "visible chat input must regain focus");
  assert.strictEqual(await runCase({ isConnected: false }), 0, "detached input must not focus");
  assert.strictEqual(await runCase({ drawerHidden: true }), 0, "hidden/inert ancestor must not focus");
  assert.strictEqual(await runCase({ detailClosed: true }), 0, "collapsed detail panel must not focus");
  assert.strictEqual(await runCase({ chatHidden: true }), 0, "hidden chat section must not focus");
  process.stdout.write("ui01_send_chat_focus_ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    assert _run_node(script) == "ui01_send_chat_focus_ok"


def test_ui01_and_ui02_lifecycle_sync_and_unregister() -> None:
    """Verify ui.js beforeunload, unregisterWrapper drawer/modal close, index_main cleanup, and fs toolbar sync."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const uiSrc = fs.readFileSync(path.join(process.cwd(), "static/js/ui.js"), "utf8");
const stateSrc = fs.readFileSync(path.join(process.cwd(), "static/js/state.js"), "utf8");
const indexMainSrc = fs.readFileSync(path.join(process.cwd(), "static/js/index_main.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(process.cwd(), "templates/index.html"), "utf8");

function extractNamedBlock(source, signature) {
  const start = source.indexOf(signature);
  assert(start !== -1, `Signature not found: ${signature}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`Unclosed block: ${signature}`);
}

// 1. HTML contract for #fs-chart-view-toggle
assert(indexHtml.includes('id="fs-chart-view-toggle"'), "#fs-chart-view-toggle must exist");
assert(indexHtml.includes('role="group"'), "#fs-chart-view-toggle must have role=group");
assert(indexHtml.includes('aria-pressed="true"'), "builtin fs button must initialize aria-pressed=true");
assert(indexHtml.includes('aria-pressed="false"'), "tradingview fs button must initialize aria-pressed=false");

// 2. index_main.js error cleanup closes open StockDetailDrawer before unregistering cards
assert(
  indexMainSrc.includes("currentDrawerActiveWrapper === w") &&
    indexMainSrc.includes("closeStockDetailDrawer()"),
  "index_main.js cleanup loop must close open drawer before unregistering wrappers",
);

// 3. ui.js beforeunload cleans up timer & aiDrawerAbortController
const cleanupFn = extractNamedBlock(uiSrc, "function cleanupIntersectionObserver()");
let disconnected = false;
let clearedTimer = null;
const uiAbortCtrl = new AbortController();
const cleanupCtx = {
  clearTimeout: (id) => { clearedTimer = id; },
  cardIntersectionObserver: { disconnect: () => { disconnected = true; } },
  stockDetailDrawerRefreshTimer: 42,
  aiDrawerAbortController: uiAbortCtrl,
};
vm.createContext(cleanupCtx);
vm.runInContext(cleanupFn + "; cleanupIntersectionObserver();", cleanupCtx);
assert.strictEqual(disconnected, true);
assert.strictEqual(clearedTimer, 42);
assert.strictEqual(uiAbortCtrl.signal.aborted, true);

// 4. state.js unregisterWrapper closes open drawer & matching fullscreen modal, ignoring other wrappers for same stockKey
const unregisterFn = extractNamedBlock(stateSrc, "function unregisterWrapper(stockKey, wrapper)");
const wrapperA = { id: "wrapA" };
const wrapperB = { id: "wrapB" };
let drawerCloses = 0;
let fsCloses = 0;
const fsModal = {
  classList: { contains: (c) => c === "hidden" ? false : false },
  dataset: { stockKey: "us:AAPL" },
  _activeWrapper: wrapperA,
};
const stateCtx = {
  wrapperRegistryMap: new Map([["us:AAPL", new Set([wrapperA, wrapperB])]]),
  _wrapperRegistryVersion: 0,
  currentDrawerActiveWrapper: wrapperA,
  closeStockDetailDrawer: () => { drawerCloses += 1; },
  closeFsChartModal: () => { fsCloses += 1; },
  document: { getElementById: (id) => (id === "chart-fullscreen-modal" ? fsModal : null) },
};
vm.createContext(stateCtx);
vm.runInContext(unregisterFn, stateCtx);

// Removing wrapperB (not active in drawer/modal) must NOT close wrapperA's drawer or modal
stateCtx.unregisterWrapper("us:AAPL", wrapperB);
assert.strictEqual(drawerCloses, 0);
assert.strictEqual(fsCloses, 0);

// Removing wrapperA must close both active drawer and active fullscreen modal
stateCtx.unregisterWrapper("us:AAPL", wrapperA);
assert.strictEqual(drawerCloses, 1);
assert.strictEqual(fsCloses, 1);

// 5. Fullscreen toolbar click syncs card/drawer .chart-controls + updateIntervalControlsVisibility + applyFsViewMode aria-pressed
function makeFakeBtn(tag = "button", className = "", text = "") {
  const attrs = new Map();
  const classes = new Set(className.split(/\s+/).filter(Boolean));
  const listeners = new Map();
  const el = {
    tagName: tag.toUpperCase(),
    textContent: text,
    style: {},
    dataset: {},
    children: [],
    parentElement: null,
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      toggle: (c, force) => (force === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c)) : (force ? classes.add(c) : classes.delete(c))),
      contains: (c) => classes.has(c),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: (k) => (attrs.has(k) ? attrs.get(k) : null),
    removeAttribute: (k) => attrs.delete(k),
    appendChild(child) { child.parentElement = this; this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
    addEventListener(t, fn) { listeners.set(t, fn); },
    removeEventListener() {},
    focus() {},
    contains: () => false,
    hasAttribute: (k) => attrs.has(k),
    closest: () => null,
    async dispatch(t) { if (listeners.has(t)) return listeners.get(t)(); if (t === "click" && this.onclick) return this.onclick(); },
    querySelectorAll(sel) {
      const out = [];
      const match = (n) => {
        if (sel.includes(".interval-controls") && n.classList.contains("interval-controls")) return true;
        const mAttr = /^\[data-([a-z-]+)\]$/.exec(sel.trim());
        if (mAttr) {
          const camel = mAttr[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
          return camel in n.dataset;
        }
        return false;
      };
      const walk = (node) => {
        for (const ch of node.children) {
          if (sel.includes(",")) {
            for (const part of sel.split(",")) {
              const p = part.trim();
              const ma = /^\[data-([a-z-]+)\]$/.exec(p);
              if (ma && (ma[1] in ch.dataset)) { out.push(ch); break; }
            }
          } else if (match(ch)) {
            out.push(ch);
          }
          walk(ch);
        }
      };
      walk(this);
      return out;
    },
    querySelector(sel) {
      if (sel === ".chart-controls") return this._chartControls || null;
      if (sel === "#fs-ai-tech-lines-btn") return null;
      return null;
    },
  };
  return el;
}

const cardControls = makeFakeBtn("div", "chart-controls");
const cardTypeCandle = makeFakeBtn("button", "control-btn active");
cardTypeCandle.dataset.type = "candlestick";
cardTypeCandle.setAttribute("aria-pressed", "true");
const cardTypeLine = makeFakeBtn("button", "control-btn");
cardTypeLine.dataset.type = "line";
cardTypeLine.setAttribute("aria-pressed", "false");
const cardIndRsi = makeFakeBtn("button", "chip-btn");
cardIndRsi.dataset.ind = "ind_rsi";
cardIndRsi.setAttribute("aria-pressed", "false");
const cardIntervalGroup = makeFakeBtn("div", "control-group interval-controls");
const cardInterval1h = makeFakeBtn("button", "control-btn active");
cardInterval1h.dataset.interval = "1h";
cardInterval1h.setAttribute("aria-pressed", "true");
const cardIntervalAuto = makeFakeBtn("button", "control-btn");
cardIntervalAuto.dataset.interval = "auto";
cardIntervalAuto.setAttribute("aria-pressed", "false");
cardIntervalGroup.appendChild(cardIntervalAuto);
cardIntervalGroup.appendChild(cardInterval1h);
cardControls.appendChild(cardTypeCandle);
cardControls.appendChild(cardTypeLine);
cardControls.appendChild(cardIndRsi);
cardControls.appendChild(cardIntervalGroup);

const cardWrapper = makeFakeBtn("div", "stock-wrapper");
cardWrapper.dataset.stockKey = "us:AAPL";
cardWrapper.__stockData = { symbol: "AAPL", market: "us", name: "Apple", price: 210 };
cardWrapper._chartControls = cardControls;

const modalEl = makeFakeBtn("div", "hidden");
const toolbarEl = makeFakeBtn("div");
const viewToggleEl = makeFakeBtn("div");
const fsBtnBuiltin = makeFakeBtn("button", "fs-view-btn active");
fsBtnBuiltin.dataset.fsView = "builtin";
fsBtnBuiltin.setAttribute("aria-pressed", "true");
const fsBtnTv = makeFakeBtn("button", "fs-view-btn");
fsBtnTv.dataset.fsView = "tradingview";
fsBtnTv.setAttribute("aria-pressed", "false");
viewToggleEl.appendChild(fsBtnBuiltin);
viewToggleEl.appendChild(fsBtnTv);

const prefStore = { type: "candlestick", period: "1mo", interval: "1h", ind_rsi: "off" };
const fsElements = {
  "chart-fullscreen-modal": modalEl,
  "fs-stock-symbol": makeFakeBtn("h3"),
  "fs-stock-name": makeFakeBtn("span"),
  "fs-stock-price": makeFakeBtn("span"),
  "fs-chart-toolbar": toolbarEl,
  "fs-chart-canvas": makeFakeBtn("canvas"),
  "fs-chart-canvas-wrapper": makeFakeBtn("div"),
  "tradingview-chart-container": makeFakeBtn("div"),
  "fs-chart-view-toggle": viewToggleEl,
  "closeFsChartModal": makeFakeBtn("button"),
  "stock-detail-drawer": null,
};

const fsCtx = {
  Number, String, Promise, Set, Map, Array, Object,
  console: { debug() {}, warn() {}, log() {} },
  setTimeout, clearTimeout,
  requestAnimationFrame: (fn) => fn(),
  window: {
    APP_CONFIG: { is_ai_technical_lines_eligible: false },
    TradingViewManager: { renderAdvancedChart() {}, clearContainer() {} },
  },
  document: {
    activeElement: null,
    getElementById: (id) => fsElements[id] || null,
  },
  createEl: makeFakeBtn,
  currentDrawerActiveWrapper: null,
  CONSTANTS: { PERIODS: ["1mo", "3mo", "5y"] },
  getSseMode: () => 2,
  lockBodyScroll() {},
  unlockBodyScroll() {},
  destroyChart() {},
  findAllWrappersByStockKey: () => [cardWrapper],
  getStockByKey: () => cardWrapper.__stockData,
  formatPrice: (p) => `$${p}`,
  getChartPref: (_k, p, def) => (p in prefStore ? prefStore[p] : def),
  setChartPref: (_k, p, v) => { prefStore[p] = v; },
  getFreshPrefetchedHistory: () => ({ formattedData: [{ x: 1, y: 1 }], ohlcData: [] }),
  refreshStockChart: async () => {},
  drawChart() {},
  isUiElementVisible: () => true,
  closeTopmostOverlayOnEscape: () => {},
  triggerAiTechnicalLines: () => {},
};

const fullFsCode = [
  extractNamedBlock(uiSrc, "function getSupportedIntervalsForPeriod(period)"),
  extractNamedBlock(uiSrc, "function updateIntervalControlsVisibility(parentEl, stockKey, currentPeriod)"),
  extractNamedBlock(uiSrc, "function syncChartControlsContainerState(controlsEl, stockKey)"),
  extractNamedBlock(uiSrc, "function syncWrapperChartControls(wrapper, stockKey)"),
  extractNamedBlock(uiSrc, "function openFullscreenChart(wrapper)"),
  extractNamedBlock(uiSrc, "function closeFsChartModal()"),
].join("\n\n");

vm.createContext(fsCtx);
vm.runInContext(fullFsCode, fsCtx);

fsCtx.openFullscreenChart(cardWrapper);
assert.strictEqual(modalEl._activeWrapper, cardWrapper);
assert.strictEqual(fsBtnTv.getAttribute("aria-pressed"), "true", "tradingview button aria-pressed updates on open");
assert.strictEqual(fsBtnBuiltin.getAttribute("aria-pressed"), "false");

// Switch to builtin view, then toggle chart type to 'line' and period to '1y' (unsupported '1h' falls back to 'auto')
fsBtnBuiltin.onclick();
assert.strictEqual(fsBtnBuiltin.getAttribute("aria-pressed"), "true");
assert.strictEqual(fsBtnTv.getAttribute("aria-pressed"), "false");

const fsLineBtn = toolbarEl.querySelectorAll("[data-type]").find((b) => b.dataset.type === "line");
const fs5yBtn = toolbarEl.querySelectorAll("[data-period]").find((b) => b.dataset.period === "5y");
const fsRsiBtn = toolbarEl.querySelectorAll("[data-ind]").find((b) => b.dataset.ind === "ind_rsi");

(async () => {
  await fsLineBtn.dispatch("click");
  await fsRsiBtn.dispatch("click");
  await fs5yBtn.dispatch("click");

  assert.strictEqual(cardTypeLine.classList.contains("active"), true, "card type button synced to active");
  assert.strictEqual(cardTypeLine.getAttribute("aria-pressed"), "true");
  assert.strictEqual(cardTypeCandle.classList.contains("active"), false);
  assert.strictEqual(cardTypeCandle.getAttribute("aria-pressed"), "false");

  assert.strictEqual(cardIndRsi.classList.contains("active"), true, "card indicator button synced");
  assert.strictEqual(cardIndRsi.getAttribute("aria-pressed"), "true");

  assert.strictEqual(cardIntervalAuto.classList.contains("active"), true, "fallback interval auto synced with aria-pressed");
  assert.strictEqual(cardIntervalAuto.getAttribute("aria-pressed"), "true");
  assert.strictEqual(cardInterval1h.getAttribute("aria-pressed"), "false");

  fsCtx.closeFsChartModal();
  assert.strictEqual(modalEl._activeWrapper, null, "closeFsChartModal must clear _activeWrapper");
  process.stdout.write("ui01_ui02_sync_ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    assert _run_node(script) == "ui01_ui02_sync_ok"

