"""getStockByKey must see the objects installed by the latest updateStocks.

The O(1) lookup index cached object references and was only rebuilt on a
missing key. ``updateStocks`` replaced the arrays, so a key that still existed
kept resolving to the old object. In the portfolio flow that meant the modal
read pre-snapshot holdings (0 shares / no FX) and could save that stale state
back over real holdings.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend state coverage")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return result.stdout


def test_update_stocks_invalidates_the_stock_key_index() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "state.js"),
  "utf8",
);

const noop = () => {};
const context = {
  console,
  JSON,
  Math,
  Object,
  Array,
  String,
  Number,
  Boolean,
  Date,
  Map,
  Set,
  WeakMap,
  Promise,
  Error,
  Intl,
  performance: { now: () => 0 },
  setTimeout: (fn) => {
    fn();
    return 0;
  },
  clearTimeout: noop,
  setInterval: () => 0,
  clearInterval: noop,
  requestAnimationFrame: (fn) => {
    fn(0);
    return 1;
  },
  cancelAnimationFrame: noop,
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  APP_CONFIG: {},
  DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
  document: {
    documentElement: {},
    addEventListener: noop,
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: () => ({
      classList: { add: noop, remove: noop, toggle: noop },
      style: {},
      appendChild: noop,
      setAttribute: noop,
    }),
    body: { classList: { add: noop, remove: noop, toggle: noop } },
  },
  window: {
    addEventListener: noop,
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    matchMedia: () => ({ matches: false, addEventListener: noop }),
    location: { href: "" },
  },
  IntersectionObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  logger: { warn: noop, info: noop, debug: noop, error: noop },
};
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: "state.js" });

const result = vm.runInContext(
  `
  const first = { symbol: "AAPL", market: "us", shares: 0, avg_price: 0 };
  state.updateStocks({ us: [first], jp: [], idx: [] });
  const before = getStockByKey("us:AAPL");

  const second = { symbol: "AAPL", market: "us", shares: 7, avg_price: 100, avg_fx_rate: 150 };
  state.updateStocks({ us: [second], jp: [], idx: [] });
  const after = getStockByKey("us:AAPL");

  ({
    beforeIsFirst: before === first,
    afterIsSecond: after === second,
    shares: after ? after.shares : null,
    avgFxRate: after ? after.avg_fx_rate : null,
  });
`,
  context,
);

if (!result.beforeIsFirst) throw new Error("initial lookup did not index the object");
if (!result.afterIsSecond) throw new Error("getStockByKey returned the pre-update object");
if (result.shares !== 7) throw new Error(`stale shares: ${result.shares}`);
if (result.avgFxRate !== 150) throw new Error(`stale fx rate: ${result.avgFxRate}`);
process.stdout.write("ok");
"""

    assert _run_node(script) == "ok"
