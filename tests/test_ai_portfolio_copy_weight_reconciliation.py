"""Regression tests for AI portfolio copy-to-my weight normalization.

``copyAiPortfolioToMy`` scales an over-100% portfolio down before POSTing it to
``/api/ai-portfolio/copy-to-my``. Rounding each weight independently used to
push the total back above the backend's 100.5% tolerance (19 items x 6% became
19 x 5.3% = 100.7% and the entire copy request was rejected with HTTP 400), and
a tiny holding could round down to 0% which the backend rejects as well.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the AI portfolio JS regression test"
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Node test failed: {result.stderr}\nStdout: {result.stdout}"
    return result.stdout


def test_copy_ai_portfolio_reconciles_rounding_and_keeps_weights_positive():
    script = r"""
const vm = require("vm");
const fs = require("fs");

let postedBody = null;
const context = {
  csrfFetch: async (url, opts) => {
    postedBody = JSON.parse(opts.body);
    return {
      ok: true,
      json: async () => ({ ok: true, message: "OK", added_count: 19 }),
    };
  },
  showToast: () => {},
  fetchInitialStocks: async () => {},
  loadPortfolioSnapshot: async () => {},
};
context.global = context;
context.window = context;
vm.createContext(context);

const source = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
const start = source.indexOf("async function copyAiPortfolioToMy");
const end = source.indexOf("function renderAiPortfolio", start);
if (start < 0 || end < 0) throw new Error("copyAiPortfolioToMy not found");
vm.runInContext(
  source.slice(start, end) + "\nthis.copyAiPortfolioToMy = copyAiPortfolioToMy;",
  context,
);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function total(body) {
  return body.items.reduce((sum, item) => sum + item.weight_pct, 0);
}

async function run() {
  // 19 x 6%: independent rounding produced 19 x 5.3% = 100.7% (> 100.5).
  const many = Array.from({ length: 19 }, (_, i) => ({
    symbol: `S${i}`,
    market: "us",
    weight_pct: 6,
    target_price: 100,
  }));
  await context.copyAiPortfolioToMy(many);
  assert(postedBody && postedBody.items.length === 19, "19 items must be posted");
  const manyTotal = total(postedBody);
  assert(manyTotal <= 100.5, `total ${manyTotal} exceeds the backend 100.5 limit`);
  assert(manyTotal >= 99.5, `total ${manyTotal} lost too much weight`);
  postedBody.items.forEach((item, index) => {
    assert(item.weight_pct > 0, `item ${index} rounded to zero`);
    assert(item.symbol === `S${index}`, `item order changed at index ${index}`);
  });

  // A sub-0.1% holding must stay positive instead of rounding down to 0%,
  // while the reconciled total still respects the backend limit.
  postedBody = null;
  const mixed = [
    { symbol: "BIG", market: "us", weight_pct: 100, target_price: 100 },
    ...Array.from({ length: 18 }, (_, i) => ({
      symbol: `M${i}`,
      market: "us",
      weight_pct: 0.5,
      target_price: 100,
    })),
    { symbol: "TINY", market: "us", weight_pct: 0.001, target_price: 100 },
  ];
  await context.copyAiPortfolioToMy(mixed);
  const mixedTotal = total(postedBody);
  assert(mixedTotal <= 100.5, `mixed total ${mixedTotal} exceeds the backend limit`);
  assert(
    postedBody.items.every((item) => item.weight_pct > 0),
    "every mixed weight must stay positive",
  );
  const tiny = postedBody.items.find((item) => item.symbol === "TINY");
  assert(tiny && tiny.weight_pct >= 0.1, "tiny weight must not round to zero");

  // Portfolios already at or below 100% must pass through unchanged.
  postedBody = null;
  await context.copyAiPortfolioToMy([
    { symbol: "A", market: "us", weight_pct: 60, target_price: 100 },
    { symbol: "B", market: "us", weight_pct: 40, target_price: 100 },
  ]);
  assert(
    postedBody.items[0].weight_pct === 60 && postedBody.items[1].weight_pct === 40,
    "weights at or below 100% must be unchanged",
  );

  process.stdout.write("ai_portfolio_copy_weight_reconciliation_ok");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    out = _run_node(script)
    assert "ai_portfolio_copy_weight_reconciliation_ok" in out
