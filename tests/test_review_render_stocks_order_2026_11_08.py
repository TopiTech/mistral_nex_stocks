"""Regression test for stock-card ordering after a card is added.

``renderStocks`` reorders wrappers in place to preserve Chart.js canvases.
The previous index-comparison loop appended a newly-created card at the end
and left it there whenever the following positions already matched, so a new
stock requested first in the sorted list rendered last until the next refresh.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");

class FakeNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.textContent = "";
    this.hidden = false;
    this.classList = {
      add() {},
      remove() {},
      toggle() {},
      contains() { return false; },
    };
  }
  get firstElementChild() { return this.children[0] || null; }
  get nextElementSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return this.parentNode.children[idx + 1] || null;
  }
  _detach() {
    if (!this.parentNode) return;
    const idx = this.parentNode.children.indexOf(this);
    if (idx !== -1) this.parentNode.children.splice(idx, 1);
    this.parentNode = null;
  }
  appendChild(child) {
    child._detach();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, reference) {
    if (child === reference) return child;
    child._detach();
    if (reference == null) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    }
    const idx = this.children.indexOf(reference);
    child.parentNode = this;
    if (idx === -1) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }
  remove() { this._detach(); }
  setAttribute() {}
  getAttribute() { return null; }
  removeAttribute() {}
  addEventListener() {}
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

let uiContainer = null;
const document = {
  readyState: "complete",
  hidden: false,
  body: new FakeNode("body"),
  activeElement: null,
  addEventListener() {},
  getElementById(id) { return id === "us-stocks" ? uiContainer : null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return new FakeNode(tag); },
  createTextNode(text) { return { textContent: String(text) }; },
  contains() { return false; },
};
const window = {
  addEventListener() {},
  matchMedia: () => ({ matches: false }),
  location: { origin: "http://localhost" },
  devicePixelRatio: 1,
  getComputedStyle: () => ({ display: "block", visibility: "visible" }),
  APP_CONFIG: {},
  updateStockUI: null,
  updateExistingCard: null,
};
const context = {
  window,
  document,
  console,
  setTimeout,
  clearTimeout,
  Map,
  WeakMap,
  Set,
  state: {
    stocks: { us: [], jp: [], idx: [] },
    subscribe() {},
    isFavorite: () => false,
    exchangeRate: null,
    indices: {},
  },
  stockRealtimeUpdateAt: new Map(),
  stockDetailsCache: new Map(),
  sparklineSignatureMap: new Map(),
  chartInstances: new WeakMap(),
  checkAlerts() {},
  hasFreshSparklineData: () => false,
  shouldUpdateSparkline: () => false,
  formatPrice: (value) => String(value),
  setSparklineVisibility() {},
  makeStockKey: (market, symbol) => `${market}:${symbol}`,
  getStockByKey: () => null,
  CONSTANTS: { PERIODS: [] },
  Chart: function Chart() {},
};
context.window.window = context.window;
context.window.document = document;
vm.createContext(context);

// ui.js の updateStockUI は api.js の SSE 差分ゲートと同じ描画ハッシュ
// (utils.js の stockRenderHash)を使う。ここでスタブを置くと本物の計算量が
// 検証されないため、utils.js から関数本体を抽出して評価する。
function extractFunctionSource(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) throw new Error(`utils.js no longer defines ${name}()`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    else if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`utils.js has unbalanced braces in ${name}()`);
}

const utilsSource = fs.readFileSync("static/js/utils.js", "utf8");
vm.runInContext(
  [
    extractFunctionSource(utilsSource, "stockRenderHash"),
    extractFunctionSource(utilsSource, "stockUpdateSignature"),
  ].join("\n"),
  context,
);
vm.runInContext(fs.readFileSync("static/js/ui.js", "utf8"), context);

context.applySortOrder = (market, stocks) => stocks;
context.createStockCard = (stock, market) => {
  const node = new FakeNode("div");
  node.dataset.stockKey = `${market}:${stock.symbol}`;
  node.dataset.symbol = stock.symbol;
  node.dataset.market = market;
  node.__stockData = { ...stock, market };
  return node;
};

const container = new FakeNode("div");
uiContainer = container;
container.querySelectorAll = (selector) =>
  selector === ".stock-wrapper"
    ? container.children.filter((child) => child.dataset.stockKey)
    : [];

function card(symbol) {
  const node = new FakeNode("div");
  node.dataset.stockKey = `us:${symbol}`;
  node.dataset.symbol = symbol;
  node.dataset.market = "us";
  node.dataset.marketContext = "us";
  node.__stockData = { symbol, market: "us", price: 1 };
  return node;
}

const existingA = card("AAA");
const existingB = card("BBB");
container.appendChild(existingA);
container.appendChild(existingB);

context.renderStocks("us", [
  { symbol: "CCC", market: "us", price: 1 },
  { symbol: "AAA", market: "us", price: 1 },
  { symbol: "BBB", market: "us", price: 1 },
]);

const order = container.children.map((node) => node.dataset.symbol).join(",");
if (order !== "CCC,AAA,BBB") {
  throw new Error(`new card rendered out of order: ${order}`);
}
process.stdout.write(order);
process.exit(0);
"""


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the stock-card ordering regression test")
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


def test_new_stock_card_renders_at_its_sorted_position():
    assert _run_node(_SCRIPT) == "CCC,AAA,BBB"
