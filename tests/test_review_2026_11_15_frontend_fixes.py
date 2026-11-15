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
