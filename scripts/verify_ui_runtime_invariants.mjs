#!/usr/bin/env node
/**
 * Behavioral regression gate for two UI invariants that a source grep cannot
 * express. Both functions are extracted from the real sources and executed, so
 * a rewrite that keeps their names but breaks their behaviour still fails.
 *
 *  1. sanitizeTextInput() must cap by code points, never by UTF-16 code units.
 *     A unit-based cut splits a surrogate pair and emits a lone surrogate,
 *     which is not a Unicode scalar value: JSON.stringify still ships it and
 *     every UTF-8 encoder downstream rejects it. It must also strip control
 *     characters *before* consuming the length budget, otherwise a prefix of
 *     control characters silently discards valid text.
 *
 *  2. isCurrentInitialStocksFetch() is the out-of-order guard for the initial
 *     /api/stocks load. A response that belongs to a superseded generation, or
 *     to an aborted controller, must never be applied.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import vm from "node:vm";
import { extractFunction } from "./_brace_matcher.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");
const UTILS_PATH = path.join(ROOT, "static/js/utils.js");
const INDEX_MAIN_PATH = path.join(ROOT, "static/js/index_main.js");

const failures = [];
let checks = 0;

function fail(message) {
  failures.push(message);
}

function equal(actual, expected, label) {
  checks += 1;
  if (actual !== expected) {
    fail(
      `${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );
  }
}

function ok(condition, label) {
  checks += 1;
  if (!condition) fail(label);
}

/** True when `text` contains a surrogate that is not part of a valid pair. */
function hasLoneSurrogate(text) {
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// 1. sanitizeTextInput
// ---------------------------------------------------------------------------
const utilsSource = readFileSync(UTILS_PATH, "utf8");
const sanitizeBody = extractFunction(
  utilsSource,
  "sanitizeTextInput",
  UTILS_PATH,
);
const utilsSandbox = vm.createContext({});
vm.runInContext(
  `${sanitizeBody}\nglobalThis.__sanitize = sanitizeTextInput;`,
  utilsSandbox,
);
const sanitize = utilsSandbox.__sanitize;

// A cap that lands between the halves of a surrogate pair.
const splitProbe = `a${"\u{1f600}".repeat(3)}`;
equal([...sanitize(splitProbe, 2)].length, 2, "sanitize: code-point cap");
ok(
  !hasLoneSurrogate(sanitize(splitProbe, 2)),
  "sanitize: a code-unit cut must not emit a lone surrogate",
);
equal(
  sanitize(splitProbe, 2),
  `a${"\u{1f600}"}`,
  "sanitize: keeps whole code points only",
);

// Control characters must not consume the length budget.
equal(
  sanitize("\x00\x00abc", 3),
  "abc",
  "sanitize: strip control chars before capping",
);
equal(sanitize("  spaced  "), "spaced", "sanitize: trims");
equal(
  sanitize("keep\ttab\nand newline"),
  "keep\ttab\nand newline",
  "sanitize: keeps tab/newline",
);
equal(sanitize("\x07bell\x7fdel"), "belldel", "sanitize: removes BEL and DEL");
equal(sanitize(null), "", "sanitize: non-string input");
equal(sanitize(123), "", "sanitize: numeric input");
equal(
  sanitize("<script>alert(1)</script>").includes("<"),
  true,
  "sanitize: leaves HTML to textContent",
);
ok(
  sanitize("x".repeat(5000), 1000).length === 1000,
  "sanitize: honours maxLength",
);
ok(
  !hasLoneSurrogate(sanitize(`${"\u{1f600}".repeat(600)}`, 1000)),
  "sanitize: long astral input stays well formed",
);

// ---------------------------------------------------------------------------
// 2. isCurrentInitialStocksFetch (out-of-order guard)
// ---------------------------------------------------------------------------
const indexMainSource = readFileSync(INDEX_MAIN_PATH, "utf8");
const guardBody = extractFunction(
  indexMainSource,
  "isCurrentInitialStocksFetch",
  INDEX_MAIN_PATH,
);
const guardSandbox = vm.createContext({});
vm.runInContext(
  `
  let fetchInitialStocksGeneration = 0;
  let fetchInitialStocksAbortController = null;
  ${guardBody}
  globalThis.__guard = {
    isCurrentInitialStocksFetch,
    setState: (generation, controller) => {
      fetchInitialStocksGeneration = generation;
      fetchInitialStocksAbortController = controller;
    },
    bump: () => {
      fetchInitialStocksGeneration += 1;
    },
  };
  `,
  guardSandbox,
);
const guard = guardSandbox.__guard;
const controller = (aborted) => ({ signal: { aborted } });

const current = controller(false);
guard.setState(5, current);
equal(
  guard.isCurrentInitialStocksFetch(5, current),
  true,
  "guard: current response accepted",
);

// An older request that resolves after a newer one must not publish.
equal(
  guard.isCurrentInitialStocksFetch(4, current),
  false,
  "guard: stale generation rejected",
);

guard.bump();
equal(
  guard.isCurrentInitialStocksFetch(5, current),
  false,
  "guard: superseded generation rejected",
);

const aborted = controller(true);
guard.setState(6, aborted);
equal(
  guard.isCurrentInitialStocksFetch(6, aborted),
  false,
  "guard: aborted request rejected",
);

const replaced = controller(false);
const previous = controller(false);
guard.setState(7, replaced);
equal(
  guard.isCurrentInitialStocksFetch(7, previous),
  false,
  "guard: replaced controller rejected",
);

if (failures.length > 0) {
  console.error(`verify_ui_runtime_invariants: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}

console.log(`verify_ui_runtime_invariants: ${checks} checks passed`);
