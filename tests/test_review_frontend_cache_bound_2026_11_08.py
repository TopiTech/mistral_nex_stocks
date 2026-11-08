"""Regression test for the chart-preference cache bound on the read path.

``getChartPref`` lazily inserts an entry for every stock key it is asked
about, but eviction originally ran only from ``setChartPref``. A long
session that merely *reads* preferences for more than 200 distinct stocks
grew ``_chartPrefCache`` without limit, defeating the documented
200-stocks x 12-preferences ceiling.
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
const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "chart.js"),
  "utf8",
);

const storage = {};
const context = {
  console, AbortController, Promise, Error, TypeError,
  setTimeout: () => 0, clearTimeout: () => {},
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
    removeItem: (k) => { delete storage[k]; },
  },
  DOM: { get: () => null },
  document: { getElementById: () => null, querySelector: () => null },
  window: { addEventListener() {} },
  logger: { warn() {}, info() {}, debug() {}, error() {} },
  getStockByKey: () => null,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "chart.js" });

// Read-only access for 2500 distinct stocks (each lazily loads one pref).
vm.runInContext(`
  for (let i = 0; i < 2500; i++) {
    getChartPref("R" + i, "period", "3mo");
  }
`, context);

const size = vm.runInContext("_chartPrefCache.size", context);
if (size > 2400) {
  throw new Error(`read path grew pref cache past the cap: ${size}`);
}

// Defaults must remain readable after eviction; they are re-derived lazily
// from localStorage and are not authoritative state.
const first = vm.runInContext('getChartPref("R0", "period", "3mo")', context);
if (first !== "3mo") throw new Error(`lost default pref: ${first}`);
process.stdout.write("ok");
"""


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the chart pref cache regression test")
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


def test_chart_pref_cache_read_path_is_bounded():
    assert _run_node(_SCRIPT) == "ok"
