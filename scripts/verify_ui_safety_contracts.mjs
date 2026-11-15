#!/usr/bin/env node
/**
 * UI safety contract verification.
 *
 * The UI layer must keep these invariants; they were established by review but
 * are easy to regress silently because nothing else in the pipeline reads the
 * client sources for them:
 *
 *  1. No HTML-string sinks. Server- and model-derived text reaches the DOM only
 *     through textContent / createElement, never through innerHTML-class sinks.
 *  2. Templates never mark dynamic data safe, and every <script> carries the CSP
 *     nonce, so a new inline block cannot slip past the policy.
 *  3. Every `message` listener validates `event.origin` against an allowlist.
 *  4. Every extension `runtime.onMessage` handler rejects foreign senders.
 *  5. Every JSON.parse of localStorage/extension storage sits in a try block, so
 *     corrupt storage cannot abort page init.
 *  6. Every setInterval has an owner that can clear it in the same module.
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const ROOT = path.resolve(import.meta.dirname, "..");
const failures = [];
let checks = 0;

function fail(message) {
  failures.push(message);
}

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), "utf8");
}

function listFiles(dir) {
  const out = [];
  const walk = (current) => {
    for (const entry of fs.readdirSync(path.join(ROOT, current), {
      withFileTypes: true,
    })) {
      const rel = path.posix.join(current, entry.name);
      if (entry.isDirectory()) walk(rel);
      else out.push(rel);
    }
  };
  walk(dir);
  return out.sort();
}

const jsFiles = [
  ...listFiles("static/js"),
  ...listFiles("chrome_extension"),
].filter((f) => f.endsWith(".js"));

// ---------------------------------------------------------------------------
// 1. No HTML-string sinks.
// ---------------------------------------------------------------------------
const HTML_SINK_PATTERNS = [
  /\.(?:inner|outer)HTML\s*(?:\+?=)/,
  /insertAdjacentHTML\s*\(/,
  /document\s*\.\s*write(?:ln)?\s*\(/,
];
for (const file of jsFiles) {
  const lines = read(file).split(/\r?\n/);
  lines.forEach((line, index) => {
    // Do not strip `//` with a regex: it also removes the tail of a string
    // literal such as "http://x", which could hide a real sink on that line.
    // Skip only whole-line comments; a sink inside an inline comment is
    // vanishingly rare and a false positive is safer than a missed sink.
    const trimmed = line.trim();
    if (
      !trimmed ||
      trimmed.startsWith("//") ||
      trimmed.startsWith("*") ||
      trimmed.startsWith("/*")
    ) {
      return;
    }
    for (const pattern of HTML_SINK_PATTERNS) {
      checks += 1;
      if (pattern.test(line)) {
        fail(
          `${file}:${index + 1}: HTML-string sink is forbidden -> ${trimmed}`,
        );
      }
    }
  });
}

// ---------------------------------------------------------------------------
// 2. Templates: no |safe, no inline handlers, nonce on every <script>.
// ---------------------------------------------------------------------------
for (const file of listFiles("templates")) {
  if (!file.endsWith(".html")) continue;
  const source = read(file);
  checks += 1;
  if (/\|\s*safe\b/.test(source))
    fail(`${file}: dynamic data must not be marked |safe`);
  checks += 1;
  if (/\son[a-z]+\s*=\s*["']/i.test(source)) {
    fail(`${file}: inline on* handler bypasses the script-src nonce policy`);
  }
  const scriptTags = source.match(/<script\b[^>]*>/gi) || [];
  for (const tag of scriptTags) {
    checks += 1;
    if (!/\bnonce\s*=/.test(tag))
      fail(`${file}: <script> without nonce -> ${tag.trim()}`);
  }
  // ``app_config`` in HTML text/attribute position is autoescaped by Jinja.
  // Only a value interpolated *inside* an executable script block needs the
  // JSON filter (which additionally escapes </script> breakouts).
  const scriptBlocks =
    source.match(/<script\b(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi) || [];
  for (const block of scriptBlocks) {
    if (!/{{[^}]*\bapp_config\b[^}]*}}/.test(block)) continue;
    checks += 1;
    if (!/\|\s*tojson/.test(block)) {
      fail(
        `${file}: app_config interpolated into a script block without |tojson`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// 3. `message` listeners validate event.origin.
// ---------------------------------------------------------------------------
for (const file of jsFiles) {
  const source = read(file);
  const lines = source.split(/\r?\n/);
  lines.forEach((line, index) => {
    const match = line.match(
      /addEventListener\(\s*["']message["']\s*,\s*([^)]+)/,
    );
    if (!match) return;
    checks += 1;
    const handler = match[1].trim();
    // Inline handlers are checked in place; a registered function is checked
    // wherever it is defined (the origin guard lives in its body).
    let body = lines.slice(index, index + 60).join("\n");
    if (/^[A-Za-z_$][\w$]*$/.test(handler)) {
      const definition = source.match(
        new RegExp(
          `(?:const|let|var|function)\\s+${handler}\\b[\\s\\S]{0,2000}`,
        ),
      );
      body = definition ? definition[0] : body;
    }
    if (!/\borigin\b/.test(body)) {
      fail(
        `${file}:${index + 1}: message listener ${handler} without an origin check`,
      );
    }
  });
}

// ---------------------------------------------------------------------------
// 4. Extension onMessage handlers reject foreign senders.
// ---------------------------------------------------------------------------
for (const file of jsFiles.filter((f) => f.startsWith("chrome_extension/"))) {
  const lines = read(file).split(/\r?\n/);
  lines.forEach((line, index) => {
    if (!/onMessage\.addListener/.test(line)) return;
    checks += 1;
    const window = lines.slice(index, index + 40).join("\n");
    // Require an actual rejection guard, not merely mentions of `sender` and
    // `runtime.id`: a handler that logs sender.id but continues would pass a
    // proximity-only check.
    const comparesSenderId =
      /sender\s*(?:&&\s*sender\s*)?\??\.\s*id\s*!==?\s*chrome\.runtime\.id/.test(
        window,
      );
    const rejectsForeignSender =
      /if\s*\([^)]*sender[^)]*\)\s*\{[\s\S]{0,500}?(?:return|sendResponse|safeSendResponse)/.test(
        window,
      );
    if (!comparesSenderId || !rejectsForeignSender) {
      fail(
        `${file}:${index + 1}: onMessage handler must reject a foreign sender.id`,
      );
    }
  });
}

// ---------------------------------------------------------------------------
// 5. Storage JSON.parse is guarded by try/catch.
// ---------------------------------------------------------------------------
const STORAGE_PARSE =
  /JSON\.parse\(\s*(?:localStorage|sessionStorage|chrome\.storage|window\.localStorage)/;
for (const file of jsFiles) {
  const lines = read(file).split(/\r?\n/);
  lines.forEach((line, index) => {
    if (!STORAGE_PARSE.test(line)) return;
    checks += 1;
    const window = lines.slice(Math.max(0, index - 3), index + 1).join("\n");
    if (!/\btry\s*\{/.test(window)) {
      fail(`${file}:${index + 1}: storage JSON.parse outside try/catch`);
    }
  });
}

// ---------------------------------------------------------------------------
// 6. setInterval owners can clear their timers.
// ---------------------------------------------------------------------------
for (const file of jsFiles) {
  const source = read(file);
  const starts = source.match(/(?<!clear)setInterval\s*\(/g) || [];
  if (starts.length === 0) continue;
  checks += 1;
  if (!/clearInterval\s*\(/.test(source)) {
    fail(
      `${file}: creates ${starts.length} interval(s) but never calls clearInterval`,
    );
  }
}

// ---------------------------------------------------------------------------
// 7. Post-build policy posture: no remote script without SRI.
// ---------------------------------------------------------------------------
for (const file of listFiles("templates")) {
  if (!file.endsWith(".html")) continue;
  const source = read(file);
  const tags = source.match(/<script\b[^>]*>/gi) || [];
  for (const tag of tags) {
    if (!/\bsrc\s*=\s*["']?(?:https?:|\/\/)/i.test(tag)) continue;
    checks += 1;
    if (!/\bintegrity\s*=\s*["']?sha(?:256|384|512)-/i.test(tag)) {
      fail(`${file}: CDN script without SRI -> ${tag.trim()}`);
    }
  }
}

if (failures.length > 0) {
  console.error(`verify_ui_safety_contracts: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}

console.log(`verify_ui_safety_contracts: ${checks} checks passed`);
