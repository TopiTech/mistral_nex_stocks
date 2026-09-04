"""Regression coverage for the 2026-09-03 repository review."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from services.stock_provider import sanitize_fundamental_dict

ROOT = Path(__file__).resolve().parents[1]


def test_fundamental_sanitizer_handles_nested_and_numpy_values() -> None:
    raw = {
        "nested": [
            {"valid": np.float64(12.5), "nan": float("nan"), "flag": True},
            [np.int64(7), pd.NA, float("inf")],
        ],
        "nat": pd.NaT,
        "plain": "Technology",
    }

    clean = sanitize_fundamental_dict(raw)

    assert clean == {
        "nested": [{"valid": 12.5}, [7]],
        "plain": "Technology",
    }
    assert json.loads(json.dumps(clean, allow_nan=False)) == clean


def test_chart_indicators_do_not_emit_nan_or_use_partial_windows() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend indicator regression coverage")

    script = r"""
const fs = require("fs");
const vm = require("vm");
const ctx = {
  APP_CONFIG: { has_mistral_api_key: false },
  document: {
    documentElement: {},
    addEventListener: () => {},
    removeEventListener: () => {},
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ setAttribute: () => {}, addEventListener: () => {} }),
  },
  addEventListener: () => {},
  removeEventListener: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
};
ctx.window = ctx;
ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/chart.js", "utf8"), ctx);

const ema = ctx.calculateEMA([1, 2, NaN, 4, 5, 6, 7], 3);
if (ema.some(Number.isNaN)) throw new Error(`EMA emitted NaN: ${JSON.stringify(ema)}`);
if (ema[2] !== null || ema[3] !== null || ema[4] !== null || ema[5] !== 5) {
  throw new Error(`EMA used an incomplete seed window: ${JSON.stringify(ema)}`);
}

const partialRsi = ctx.calculateRSI([1, null, 3, 4, 5], 3);
if (partialRsi.some((value) => value !== null)) {
  throw new Error(`RSI used fewer than three consecutive changes: ${JSON.stringify(partialRsi)}`);
}
const recoveredRsi = ctx.calculateRSI([1, null, 3, 4, 5, 6], 3);
if (recoveredRsi[5] !== 100) {
  throw new Error(`RSI did not recover after a complete window: ${JSON.stringify(recoveredRsi)}`);
}

const gappedSeries = Array.from({ length: 45 }, (_, index) => index === 15 ? NaN : 100 + index);
const macd = ctx.calculateMACD(gappedSeries, 3, 5, 2);
for (const values of [macd.macdLine, macd.signalLine, macd.histogram]) {
  if (values.some(Number.isNaN)) throw new Error(`MACD emitted NaN: ${JSON.stringify(values)}`);
}

const candles = ctx.calculateHeikinAshi([
  { x: 1, o: NaN, h: 10, l: 8, c: 9, v: NaN },
  { x: 2, o: null, h: null, l: null, c: null, price: 11 },
  { x: 3, o: NaN, h: NaN, l: NaN, c: NaN },
]);
if (candles.length !== 2) throw new Error(`Invalid candle was not dropped: ${JSON.stringify(candles)}`);
for (const candle of candles) {
  for (const key of ["o", "h", "l", "c", "v"]) {
    if (!Number.isFinite(candle[key])) throw new Error(`Heikin-Ashi emitted invalid ${key}`);
  }
}
process.stdout.write("ok");
"""
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
    assert result.stdout == "ok"
