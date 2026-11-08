"""Regression tests for ObservatoryState change-notification semantics.

Every background ``/api/stocks`` refresh calls ``setStocks`` with the same
selected symbol. The store used to notify ``selectedSymbol`` unconditionally,
so the TemporalController stopped an in-progress timelapse and reapplied
history on every refresh. Notifications must follow the store's change-only
contract.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Observatory JavaScript regression test")
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


def test_set_stocks_notifies_selected_symbol_only_when_it_changes():
    output = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const context = {
  console,
  URLSearchParams,
  URL,
  localStorage: { getItem: () => null, setItem: () => {} },
};
context.window = {
  location: { search: "", href: "http://localhost/experimental/orbit" },
  matchMedia: () => ({ matches: false }),
  history: { replaceState: () => {} },
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync("static/js/experimental/orbit-state.js", "utf8"),
  context,
);

const state = new context.window.ObservatoryState();
const events = [];
state.subscribe((key, value) => events.push([key, value]));

state.setStocks([{ symbol: "AAPL", market: "us", price: 100 }]);
state.setStocks([{ symbol: "AAPL", market: "us", price: 101 }]);

const selectedEvents = events.filter(([key]) => key === "selectedSymbol");
const stockEvents = events.filter(([key]) => key === "stocks");
if (selectedEvents.length !== 1) {
  throw new Error(
    `selectedSymbol must notify once for an unchanged selection, got ${selectedEvents.length}`,
  );
}
if (selectedEvents[0][1] !== "AAPL") {
  throw new Error(`unexpected selectedSymbol notification: ${selectedEvents[0][1]}`);
}
if (stockEvents.length !== 2) {
  throw new Error(`stocks must notify on every replacement, got ${stockEvents.length}`);
}

// A replacement that drops the selection must still auto-select and notify.
state.setStocks([{ symbol: "MSFT", market: "us", price: 200 }]);
const afterRemoval = events.filter(([key]) => key === "selectedSymbol");
if (afterRemoval.length !== 2 || state.state.selectedSymbol !== "MSFT") {
  throw new Error(
    `auto-selection must notify once, got ${JSON.stringify(afterRemoval)}`,
  );
}

process.stdout.write(JSON.stringify({ selected: afterRemoval.length, stocks: stockEvents.length }));
process.exit(0);
"""
    )
    payload = json.loads(output)
    assert payload == {"selected": 2, "stocks": 2}
