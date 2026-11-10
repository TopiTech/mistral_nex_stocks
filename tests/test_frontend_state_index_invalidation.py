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

  const third = { symbol: "AAPL", market: "us", shares: 9, avg_price: 101 };
  const replaced = state.replaceStock("us", "AAPL", third);
  const afterReplace = getStockByKey("us:AAPL");

  ({
    beforeIsFirst: before === first,
    afterIsSecond: after === second,
    shares: after ? after.shares : null,
    avgFxRate: after ? after.avg_fx_rate : null,
    replaced,
    afterReplaceIsThird: afterReplace === third,
    replaceShares: afterReplace ? afterReplace.shares : null,
  });
`,
  context,
);

if (!result.beforeIsFirst) throw new Error("initial lookup did not index the object");
if (!result.afterIsSecond) throw new Error("getStockByKey returned the pre-update object");
if (result.shares !== 7) throw new Error(`stale shares: ${result.shares}`);
if (result.avgFxRate !== 150) throw new Error(`stale fx rate: ${result.avgFxRate}`);
if (!result.replaced) throw new Error("replaceStock did not find the row");
if (!result.afterReplaceIsThird) throw new Error("replaceStock left the index stale");
if (result.replaceShares !== 9) throw new Error(`stale replace shares: ${result.replaceShares}`);
process.stdout.write("ok");
"""

    assert _run_node(script) == "ok"


def test_portfolio_save_uses_replace_stock_instead_of_direct_assignment() -> None:
    source = (ROOT / "static" / "js" / "index_main.js").read_text(encoding="utf-8")
    assert "state.replaceStock(" in source
    assert "list[idx] = updated" not in source


def test_realtime_dom_cache_refreshes_when_a_wrapper_is_added() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const noop = () => {};
const wrappers = [{ isConnected: true, id: 1 }];
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
  CSS: { escape: (s) => String(s) },
  document: {
    readyState: "complete",
    addEventListener: noop,
    querySelectorAll: () => wrappers,
  },
  window: {},
};
context.window.document = context.document;
context.globalThis = context;
let registryVersion = 0;
context.window.getWrapperRegistryVersion = () => registryVersion;

vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "realtime_client.js"), "utf8"),
  context,
  { filename: "realtime_client.js" },
);

const client = context.window.realtimeClient;
if (!client) throw new Error("realtime client was not initialised");
const first = client._getElements("AAPL");
if (first.wrappers.length !== 1) throw new Error("initial DOM cache missed the wrapper");

wrappers.push({ isConnected: true, id: 2 });
registryVersion++;
const second = client._getElements("AAPL");
if (second.wrappers.length !== 2) {
  throw new Error("DOM cache did not refresh after a wrapper was registered");
}
process.stdout.write("ok");
"""

    assert _run_node(script) == "ok"
