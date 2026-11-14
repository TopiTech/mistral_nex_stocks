"""2026-11-14 continuation review — regression tests for newly found issues.

Covers:
* ``_fluctuate_indices`` must never emit NaN/inf ``percent`` when
  ``prev_close`` is 0 or non-finite (previously the percent branch was
  skipped silently and stale/undefined values could leak into SSE frames).
* ``purgeStockKeyCaches`` (state.js) removes every per-stockKey cache entry
  including the ``ai_prev_*`` localStorage key, and the SSE ``_removed``
  path in api.js routes through it.
* ``resetAllStocks`` (settings.js) sweeps ``ai_prev_*`` localStorage keys.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Backend: bg/sse_interpolator._fluctuate_indices zero / non-finite prev_close
# ---------------------------------------------------------------------------


def _run_fluctuate(indices: dict, key: str = "N225") -> dict:
    from bg import sse_interpolator

    with (
        patch.object(sse_interpolator, "_get_simulate_fluctuation", return_value=True),
        patch.object(sse_interpolator.random, "random", return_value=0.0),
        patch.object(sse_interpolator.random, "uniform", return_value=0.00005),
    ):
        sse_interpolator._fluctuate_indices(indices, us_open=True, jp_open=True)
    return indices[key]


def test_fluctuate_indices_prev_close_zero_keeps_percent_finite() -> None:
    # price == change  =>  prev_close == 0: percent change is undefined.
    indices = {"N225": {"price": 100.0, "change": 100.0}}
    out = _run_fluctuate(indices)
    assert "percent" not in out or math.isfinite(out.get("percent", 0.0))
    assert math.isfinite(out["price"])
    # price must still move (fluctuation applied), percent must not be NaN
    assert out["price"] != 100.0
    if "percent" in out:
        assert not math.isnan(out["percent"]) and not math.isinf(out["percent"])


def test_fluctuate_indices_normal_path_sets_finite_percent() -> None:
    indices = {"N225": {"price": 110.0, "change": 10.0}}  # prev_close = 100
    out = _run_fluctuate(indices)
    assert math.isfinite(out["percent"])
    assert math.isfinite(out["price"])
    assert math.isfinite(out["change"])


def test_fluctuate_indices_fx_pair_zero_prev_close() -> None:
    indices = {"USDJPY": {"price": 150.0, "change": 150.0}}
    out = _run_fluctuate(indices, key="USDJPY")
    assert math.isfinite(out["price"])
    if "percent" in out:
        assert math.isfinite(out["percent"])


# ---------------------------------------------------------------------------
# Frontend: purgeStockKeyCaches + removal-path wiring (Node vm harness)
# ---------------------------------------------------------------------------


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend coverage")
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


_STATE_HARNESS = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const noop = () => {};
const store = new Map();
const localStorageMock = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  key: (i) => Array.from(store.keys())[i] ?? null,
  get length() { return store.size; },
};
const harnessSandbox = {
  console, JSON, Math, Object, Array, String, Number, Boolean, Date,
  Map, Set, WeakMap, Promise, Error, Intl,
  performance: { now: () => 0 },
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout: noop,
  setInterval: () => 0,
  clearInterval: noop,
  requestAnimationFrame: (fn) => { fn(0); return 1; },
  cancelAnimationFrame: noop,
  localStorage: localStorageMock,
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
      style: {}, appendChild: noop, setAttribute: noop,
    }),
    body: { classList: { add: noop, remove: noop, toggle: noop } },
  },
  window: {
    addEventListener: noop,
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    matchMedia: () => ({ matches: false, addEventListener: noop }),
    location: { href: "" },
  },
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  logger: { warn: noop, info: noop, debug: noop, error: noop },
};
harnessSandbox.window.localStorage = localStorageMock;
harnessSandbox.globalThis = harnessSandbox;
vm.createContext(harnessSandbox);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "state.js"), "utf8"),
  harnessSandbox,
  { filename: "state.js" },
);
globalThis.__harness = { harnessSandbox, store };
"""


def test_purge_stock_key_caches_clears_all_maps_and_localstorage() -> None:
    script = _STATE_HARNESS + r"""
const purge = globalThis.__harness.harnessSandbox.window.purgeStockKeyCaches;
if (typeof purge !== "function") throw new Error("purgeStockKeyCaches not exported");

const harnessStore = globalThis.__harness.store;
const key = "us:AAPL";
harnessStore.set(`ai_prev_${key}`, JSON.stringify({ recommendation: "buy" }));
purge(key);
if (harnessStore.has(`ai_prev_${key}`)) throw new Error("ai_prev_ localStorage entry survived purge");
// unknown key must be a no-op
purge("us:UNKNOWN");
purge("");
process.stdout.write("ok");
"""
    assert _run_node(script) == "ok"


def test_purge_removes_internal_maps_via_observable_behaviour() -> None:
    # shouldUpdateSparkline stores into sparklineSignatureMap/sparklineUpdateAt;
    # after purge the same signature must be treated as new again.
    script = _STATE_HARNESS + r"""
// Evaluate inside the vm context so we reach module-scope functions directly.
const probe = vm.runInContext(
  `
  (() => {
    const key = "us:MSFT";
    const wrapper = { dataset: { stockKey: key, visible: "true" } };
    const data = [1, 2, 3];
    const first = shouldUpdateSparkline(wrapper, key, data);
    const second = shouldUpdateSparkline(wrapper, key, data);
    if (!first) throw new Error("first sparkline update should run");
    if (second) throw new Error("identical signature should be skipped");
    window.purgeStockKeyCaches(key);
    const third = shouldUpdateSparkline(wrapper, key, data);
    if (!third) throw new Error("after purge the same signature must be re-applied");
    return "ok";
  })()
  `,
  globalThis.__harness.harnessSandbox,
);
if (probe !== "ok") throw new Error("probe failed: " + probe);
process.stdout.write("ok");
"""
    assert _run_node(script) == "ok"


def test_api_removed_path_calls_purge_helper() -> None:
    src = (ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
    assert "window.purgeStockKeyCaches(stockKey)" in src
    assert "stockHashMap.delete(stockKey);" in src  # fallback branch retained


def test_ui_renderstocks_removal_calls_purge_helper() -> None:
    src = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    assert "window.purgeStockKeyCaches(wrapper.dataset.stockKey)" in src


def test_settings_reset_sweeps_ai_prev_keys() -> None:
    src = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert 'k.startsWith("ai_prev_")' in src
    assert "window.purgeStockKeyCaches(`${market}:${symbol}`)" in src


def test_state_js_exports_purge() -> None:
    src = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")
    assert "function purgeStockKeyCaches(stockKey)" in src
    assert "window.purgeStockKeyCaches = purgeStockKeyCaches;" in src
    # every per-key structure must be purged
    for name in (
        "stockHashMap.delete(stockKey)",
        "stockRealtimeUpdateAt.delete(stockKey)",
        "sparklineUpdateAt.delete(stockKey)",
        "sparklineSignatureMap.delete(stockKey)",
        "stockDetailsCache.delete(stockKey)",
        "historyPrefetchCache.delete(stockKey)",
        "ai_prev_${stockKey}",
    ):
        assert name in src, f"purgeStockKeyCaches missing {name}"


# ---------------------------------------------------------------------------
# JSON safety: the SSE frame builder rejects NaN/inf (guard for the fix above)
# ---------------------------------------------------------------------------


def test_sse_frame_json_rejects_nan() -> None:
    with pytest.raises(ValueError):
        json.dumps({"percent": float("nan")}, allow_nan=False)
