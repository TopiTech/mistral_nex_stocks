"""Regression test for the AI technical-lines request race in ``ui.js``.

``triggerAiTechnicalLines`` aborts the previous request when the button is
pressed again, but its ``finally`` block restored the buttons and removed the
loading overlay unconditionally. The superseded request therefore re-enabled
the button and hid the overlay while the newer request was still in flight,
allowing a third request to stack up.

The test runs the real ``ui.js`` in a Node ``vm``, drives two overlapping
calls through a controllable ``csrfFetch`` stub, and asserts the busy state
survives until the current request settles.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function makeEl(tag = "div", className = "", text = "") {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: className || "",
    textContent: text || "",
    disabled: false,
    children: [],
    style: {},
    dataset: {},
    parentNode: null,
    _classes: new Set(String(className || "").split(/\s+/).filter(Boolean)),
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      if (String(child.className).includes("ai-tech-loading-overlay")) {
        this._overlay = child;
      }
      return child;
    },
    append(child) { return this.appendChild(child); },
    remove() {
      const parent = this.parentNode;
      if (!parent) return;
      const i = parent.children.indexOf(this);
      if (i >= 0) parent.children.splice(i, 1);
      if (parent._overlay === this) parent._overlay = null;
    },
    querySelector(sel) {
      if (String(sel).includes("ai-tech-loading-overlay")) {
        return this._overlay || null;
      }
      return null;
    },
    querySelectorAll() { return []; },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    classList: {
      add: (...cs) => cs.forEach((c) => el._classes.add(c)),
      remove: (...cs) => cs.forEach((c) => el._classes.delete(c)),
      contains: (c) => el._classes.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !el._classes.has(c) : !!force;
        if (on) el._classes.add(c); else el._classes.delete(c);
        return on;
      },
    },
  };
  return el;
}

const document = {
  readyState: "loading",
  activeElement: null,
  _ready: null,
  addEventListener(type, fn) {
    if (type === "DOMContentLoaded") this._ready = fn;
  },
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
};

const window = {
  APP_CONFIG: {
    is_ai_technical_lines_eligible: true,
    ai_provider: "mistral",
  },
  addEventListener() {},
  removeEventListener() {},
};

const state = { subscribe() {} };
const logger = { error() {}, warn() {}, info() {} };
const showToast = () => {};
const getChartPref = (_key, _name, fallback) => fallback;
const getFreshPrefetchedHistory = () => null;
const refreshStockChart = async () => {};
const drawChart = () => {};
const getStockByKey = () => null;

const requests = [];
function csrfFetch(_url, opts = {}) {
  const req = { opts, aborted: false };
  requests.push(req);
  return new Promise((resolve, reject) => {
    req.resolve = resolve;
    req.reject = reject;
    if (opts.signal) {
      opts.signal.addEventListener("abort", () => {
        req.aborted = true;
        const err = new Error("aborted");
        err.name = "AbortError";
        reject(err);
      });
    }
  });
}

const context = {
  console,
  document,
  window,
  state,
  logger,
  showToast,
  getChartPref,
  getFreshPrefetchedHistory,
  refreshStockChart,
  drawChart,
  getStockByKey,
  csrfFetch,
  createEl: (tag, className, text) => makeEl(tag, className, text),
  AbortController,
  Map,
  Set,
  Math,
  Number,
  JSON,
  Date,
  Promise,
  Error,
  setTimeout,
  clearTimeout,
  setImmediate,
  requestAnimationFrame: () => 1,
  cancelAnimationFrame: () => {},
};
context.globalThis = context;
context.self = context;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "ui.js"), "utf8"),
  context,
  { filename: "ui.js" },
);

const tick = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  if (typeof context.triggerAiTechnicalLines !== "function") {
    throw new Error("ui.js did not expose triggerAiTechnicalLines");
  }

  const btn = makeEl("button", "ai-tech-lines-btn", "✨ AIテクニカル描画");
  const chartContainer = makeEl("div", "chart-container");
  chartContainer.parentNode = { insertBefore() {} };
  const wrapper = {
    dataset: { stockKey: "us:AAPL" },
    __stockData: { symbol: "AAPL", market: "us" },
    querySelectorAll(sel) {
      return sel === ".ai-tech-lines-btn" ? [btn] : [];
    },
    querySelector(sel) {
      return sel === ".chart-container" ? chartContainer : null;
    },
  };

  const first = context.triggerAiTechnicalLines(wrapper);
  const second = context.triggerAiTechnicalLines(wrapper);
  await tick();
  await tick();

  if (requests.length !== 2) {
    throw new Error(`expected 2 requests, got ${requests.length}`);
  }
  if (!requests[0].aborted) {
    throw new Error("first request was not aborted");
  }
  if (btn.disabled !== true) {
    throw new Error("button was re-enabled while the current request is in flight");
  }
  if (btn.textContent !== "⏳ AI分析中...") {
    throw new Error(`button text changed to ${JSON.stringify(btn.textContent)}`);
  }
  if (!chartContainer._overlay) {
    throw new Error("loading overlay was removed while the current request is in flight");
  }

  requests[1].resolve({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, summary: "", lines: [] }),
  });
  await second;
  await first;
  await tick();

  if (btn.disabled !== false) {
    throw new Error("button stayed disabled after the current request settled");
  }
  if (btn.textContent !== "✨ AIテクニカル描画") {
    throw new Error(`button text was not restored: ${JSON.stringify(btn.textContent)}`);
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the AI technical-lines race test")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_superseded_ai_lines_request_keeps_current_busy_state() -> None:
    assert _run_node(_SCRIPT) == "ok"
