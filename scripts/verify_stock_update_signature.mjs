import { readFileSync } from "node:fs";
import vm from "node:vm";

// 銘柄カード更新の差分ゲート契約の回帰ゲート。
//
// SSE 差分ゲート(api.js)は以前 `stock.snapshot_ts_ms || price|change|len` を
// 使っていた。リアルタイム delta フレームは snapshot_ts_ms を含まないため、
// 価格と変化幅とチャート点数が同じまま出来高・高値・安値・保有株数だけが
// 動いたフレームが「変化なし」と誤判定され、描画から落ちていた。
// utils.js の stockUpdateSignature / stockRenderHash が唯一の組成元である
// ことを、実ソースから関数を取り出して検証する。

const UTILS_PATH = "static/js/utils.js";
const API_PATH = "static/js/api.js";
const UI_PATH = "static/js/ui.js";

function fail(message) {
  throw new Error(message);
}

function extractFunction(source, name, sourcePath) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) fail(`${sourcePath} no longer defines ${name}()`);
  const open = source.indexOf("{", start);
  if (open === -1) fail(`${sourcePath} has no body for ${name}()`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  return fail(`${sourcePath} has unbalanced braces in ${name}()`);
}

function loadSignatureHelpers() {
  const source = readFileSync(UTILS_PATH, "utf8");
  const bodies = [
    extractFunction(source, "stockRenderHash", UTILS_PATH),
    extractFunction(source, "stockUpdateSignature", UTILS_PATH),
  ];
  // ブラウザ専用グローバルを一切与えず、純粋関数であることを担保する。
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(
    `${bodies.join("\n")}\nglobalThis.__helpers = { stockRenderHash, stockUpdateSignature };`,
    sandbox,
  );
  return sandbox.__helpers;
}

function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    fail(
      `${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );
  }
}

function assertNotEqual(actual, other, label) {
  if (actual === other) {
    fail(
      `${label}: expected a different value, both were ${JSON.stringify(actual)}`,
    );
  }
}

// 修正前のゲート組成。これが「変化なし」と言ってしまうフレームで新実装が
// 変化を検出できることを示すために使う。
function legacyGateKey(stock) {
  return (
    stock.snapshot_ts_ms ||
    stock.price + "|" + stock.change + "|" + (stock.chart_data || []).length
  );
}

function verifyHelpersArePure() {
  const { stockRenderHash, stockUpdateSignature } = loadSignatureHelpers();
  assertEqual(stockRenderHash(null), "", "stockRenderHash(null)");
  assertEqual(stockRenderHash(undefined), "", "stockRenderHash(undefined)");
  assertEqual(stockUpdateSignature(null), "", "stockUpdateSignature(null)");
  assertEqual(stockUpdateSignature(42), "", "stockUpdateSignature(primitive)");
}

function verifyRenderHashMatchesRenderedFields() {
  const { stockRenderHash } = loadSignatureHelpers();
  const stock = {
    price: 1234.5,
    change: 12.5,
    change_percent: 1.02,
    shares: 100,
    avg_price: 1200,
    chart_data: [
      { x: 1, y: 1200 },
      { x: 2, y: 1234.5 },
    ],
    volume: 987654,
    high: 1240,
    low: 1195,
    pts_price: 1234.5,
  };
  // ui.js が以前インラインで組み立てていた文字列と一致しなければ、
  // リファクタで描画判定の意味が変わってしまう。
  const inline = `${stock.price}|${stock.change}|${stock.change_percent}|${stock.shares}|${stock.avg_price}|${stock.chart_data[stock.chart_data.length - 1].x}|${stock.volume}|${stock.high}|${stock.low}|${stock.pts_price}`;
  assertEqual(stockRenderHash(stock), inline, "render hash composition");
}

function verifyUnchangedFrameIsSuppressed() {
  const { stockUpdateSignature } = loadSignatureHelpers();
  const frame = {
    price: 100,
    change: 1,
    change_percent: 1,
    shares: 10,
    avg_price: 90,
    chart_data: [{ x: 1700000000000, y: 100 }],
    volume: 1000,
    high: 101,
    low: 99,
    pts_price: 100,
    snapshot_ts_ms: 1700000000000,
  };
  assertEqual(
    stockUpdateSignature({ ...frame }),
    stockUpdateSignature({ ...frame }),
    "identical frames must share a signature",
  );
}

function verifyDeltaFrameWithoutSnapshotTsIsDetected() {
  const { stockUpdateSignature } = loadSignatureHelpers();
  const base = {
    price: 100,
    change: 1,
    change_percent: 1,
    shares: 10,
    avg_price: 90,
    chart_data: [{ x: 1700000000000, y: 100 }],
    volume: 1000,
    high: 101,
    low: 99,
    pts_price: 100,
  };
  // リアルタイム delta フレーム: snapshot_ts_ms なし、価格・変化幅・点数は同じ。
  const next = { ...base, volume: 2500, high: 102, low: 98 };
  if (legacyGateKey(base) !== legacyGateKey(next)) {
    fail("fixture is wrong: the legacy gate already detected this delta frame");
  }
  assertNotEqual(
    stockUpdateSignature(base),
    stockUpdateSignature(next),
    "volume/high/low change without snapshot_ts_ms",
  );

  const sharesOnly = { ...base, shares: 25 };
  assertNotEqual(
    stockUpdateSignature(base),
    stockUpdateSignature(sharesOnly),
    "shares change without snapshot_ts_ms",
  );

  const ptsOnly = { ...base, pts_price: 100.5 };
  assertNotEqual(
    stockUpdateSignature(base),
    stockUpdateSignature(ptsOnly),
    "pts_price change without snapshot_ts_ms",
  );
}

function verifySnapshotTsRemainsATrigger() {
  const { stockUpdateSignature } = loadSignatureHelpers();
  const frame = {
    price: 100,
    change: 1,
    change_percent: 1,
    shares: 10,
    avg_price: 90,
    chart_data: [{ x: 1700000000000, y: 100 }],
    volume: 1000,
    high: 101,
    low: 99,
    pts_price: 100,
    snapshot_ts_ms: 1700000000000,
  };
  const advanced = { ...frame, snapshot_ts_ms: 1700000005000 };
  if (legacyGateKey(frame) === legacyGateKey(advanced)) {
    fail("fixture is wrong: snapshot_ts_ms must move the legacy gate");
  }
  assertNotEqual(
    stockUpdateSignature(frame),
    stockUpdateSignature(advanced),
    "new snapshot timestamp with identical values",
  );
}

function verifySnapshotTsTypeIsNormalised() {
  const { stockUpdateSignature } = loadSignatureHelpers();
  const base = { price: 100, change: 1, chart_data: [] };
  // SSE の JSON 経路と数値化経路が混在しても、同じ時刻なら再描画しない。
  assertEqual(
    stockUpdateSignature({ ...base, snapshot_ts_ms: 1700000000000 }),
    stockUpdateSignature({ ...base, snapshot_ts_ms: "1700000000000" }),
    "numeric and string snapshot_ts_ms of the same instant",
  );
  assertNotEqual(
    stockUpdateSignature({ ...base, snapshot_ts_ms: 0 }),
    stockUpdateSignature({ ...base }),
    "snapshot_ts_ms=0 is a real timestamp, not an absent one",
  );
}

function verifyMalformedChartDataIsSafe() {
  const { stockRenderHash } = loadSignatureHelpers();
  const base = { price: 100, change: 1 };
  assertEqual(
    stockRenderHash(base),
    stockRenderHash({ ...base, chart_data: [] }),
    "missing chart_data equals empty chart_data",
  );
  assertEqual(
    stockRenderHash(base),
    stockRenderHash({ ...base, chart_data: [null] }),
    "null chart point must not throw",
  );
  assertEqual(
    stockRenderHash(base),
    stockRenderHash({ ...base, chart_data: [{ y: 1 }] }),
    "chart point without x must not throw",
  );
  assertEqual(
    stockRenderHash(base),
    stockRenderHash({ ...base, chart_data: "not-an-array" }),
    "non-array chart_data must not throw",
  );
}

function verifyCallSitesUseSharedHelpers() {
  const apiSource = readFileSync(API_PATH, "utf8");
  const uiSource = readFileSync(UI_PATH, "utf8");

  if (!apiSource.includes("const currentTs = stockUpdateSignature(stock);")) {
    fail(`${API_PATH} delta gate no longer uses stockUpdateSignature()`);
  }
  if (apiSource.includes("stock.snapshot_ts_ms ||")) {
    fail(`${API_PATH} re-inlined the narrow snapshot_ts_ms gate`);
  }
  if (!uiSource.includes("const newDataStr = stockRenderHash(stock);")) {
    fail(`${UI_PATH} render throttle no longer uses stockRenderHash()`);
  }
  // 片方だけ組成を書き戻す再発を検出する。
  const inlineComposition =
    "${stock.price}|${stock.change}|${stock.change_percent}";
  if (
    apiSource.includes(inlineComposition) ||
    uiSource.includes(inlineComposition)
  ) {
    fail("render hash composition was inlined again; use stockRenderHash()");
  }
}

const checks = [
  ["helpers are pure", verifyHelpersArePure],
  ["render hash covers rendered fields", verifyRenderHashMatchesRenderedFields],
  ["unchanged frame is suppressed", verifyUnchangedFrameIsSuppressed],
  [
    "delta frame without snapshot_ts_ms is detected",
    verifyDeltaFrameWithoutSnapshotTsIsDetected,
  ],
  ["snapshot_ts_ms stays a trigger", verifySnapshotTsRemainsATrigger],
  ["snapshot_ts_ms type is normalised", verifySnapshotTsTypeIsNormalised],
  ["malformed chart_data is safe", verifyMalformedChartDataIsSafe],
  ["call sites use the shared helpers", verifyCallSitesUseSharedHelpers],
];

for (const [label, check] of checks) {
  check();
  console.log(`ok - ${label}`);
}

console.log(`verify_stock_update_signature: ${checks.length} checks passed`);
