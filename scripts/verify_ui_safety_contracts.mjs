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
  return fs.readFileSync(path.join(ROOT, rel), "utf8").replace(/\r\n/g, "\n");
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
// 1. No HTML-string or dynamic code-execution sinks (comment-stripped lexer).
// ---------------------------------------------------------------------------
function stripJsComments(code) {
  let out = "";
  let inLineComment = false;
  let inBlockComment = false;
  let inString = null;
  let escaped = false;
  for (let i = 0; i < code.length; i += 1) {
    const ch = code[i];
    const next = code[i + 1] || "";
    if (inLineComment) {
      if (ch === "\n") {
        inLineComment = false;
        out += "\n";
      }
      continue;
    }
    if (inBlockComment) {
      if (ch === "*" && next === "/") {
        inBlockComment = false;
        out += " ";
        i += 1;
      } else if (ch === "\n") {
        out += "\n";
      }
      continue;
    }
    if (escaped) {
      escaped = false;
      out += ch;
      continue;
    }
    if (inString) {
      out += ch;
      if (ch === "\\") escaped = true;
      else if (ch === inString) inString = null;
      continue;
    }
    if (ch === "/" && next === "/") {
      inLineComment = true;
      i += 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      inBlockComment = true;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
    }
    out += ch;
  }
  return out;
}

const HTML_SINK_PATTERNS = [
  /\.(?:inner|outer)HTML\s*(?:\+?=)/,
  /insertAdjacentHTML\s*\(/,
  /document\s*\.\s*write(?:ln)?\s*\(/,
  /(?:^|[^\w.$])eval\s*\(/,
  /\bnew\s+Function\s*\(/,
  /\.srcdoc\s*(?:\+?=)/,
  /^\s*document\.open\s*\(/,
  /(?:^|[\s;.(])location\.(?:assign|replace)\s*\(\s*["'`]\s*javascript:/i,
];
for (const file of jsFiles) {
  const cleanLines = stripJsComments(read(file)).split(/\r?\n/);
  cleanLines.forEach((line, index) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    for (const pattern of HTML_SINK_PATTERNS) {
      checks += 1;
      if (pattern.test(line)) {
        fail(
          `${file}:${index + 1}: HTML-string or dynamic code sink is forbidden -> ${trimmed}`,
        );
      }
    }
    checks += 1;
    if (
      /(?:\.href\s*=\s*["'`]\s*javascript:|setAttribute\s*\(\s*["']href["']\s*,\s*["'`]\s*javascript:)/i.test(
        line,
      )
    ) {
      fail(
        `${file}:${index + 1}: javascript: URL assignment is forbidden -> ${trimmed}`,
      );
    }
    if (/\.target\s*=\s*["']_blank["']/.test(line)) {
      checks += 1;
      const nearby = cleanLines
        .slice(Math.max(0, index - 3), index + 5)
        .join("\n");
      if (!/noopener/.test(nearby) || !/noreferrer/.test(nearby)) {
        fail(
          `${file}:${index + 1}: target="_blank" link must include rel="noopener noreferrer"`,
        );
      }
    }
  });
}

// ---------------------------------------------------------------------------
// 2. Templates: no |safe, no inline handlers (quoted or unquoted), nonce on <script>,
//    safe target="_blank" rel, and no javascript: URLs.
// ---------------------------------------------------------------------------
for (const file of listFiles("templates")) {
  if (!file.endsWith(".html")) continue;
  const source = read(file);
  checks += 1;
  if (/\|\s*safe\b/.test(source))
    fail(`${file}: dynamic data must not be marked |safe`);
  checks += 1;
  if (/<[a-zA-Z][^>]*\son[a-z]+\s*=\s*(?:["']|[^\s>])/i.test(source)) {
    fail(`${file}: inline on* handler bypasses the script-src nonce policy`);
  }
  checks += 1;
  if (
    /<[a-zA-Z][^>]*\b(?:href|src|action)\s*=\s*["']?\s*javascript:/i.test(
      source,
    )
  ) {
    fail(`${file}: javascript: URL attribute is forbidden`);
  }
  const blankLinks =
    source.match(/<a\b[^>]*\btarget\s*=\s*["']?_blank\b[^>]*>/gi) || [];
  for (const anchor of blankLinks) {
    checks += 1;
    if (
      !/\brel\s*=\s*["'][^"']*\bnoopener\b[^"']*\bnoreferrer\b/i.test(anchor) &&
      !/\brel\s*=\s*["'][^"']*\bnoreferrer\b[^"']*\bnoopener\b/i.test(anchor)
    ) {
      fail(
        `${file}: target="_blank" without rel="noopener noreferrer" -> ${anchor.trim()}`,
      );
    }
  }
  const scriptTags = source.match(/<script\b[^>]*>/gi) || [];
  for (const tag of scriptTags) {
    checks += 1;
    if (!/\bnonce\s*=/.test(tag))
      fail(`${file}: <script> without nonce -> ${tag.trim()}`);
  }
  // Only values serialized with |tojson are safe inside an inline <script> body.
  const scriptBlocks = Array.from(
    source.matchAll(/<script\b(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi),
    (m) => m[1],
  );
  for (const block of scriptBlocks) {
    const exprs = block.match(/{{[\s\S]*?}}/g) || [];
    for (const expr of exprs) {
      checks += 1;
      if (!/\|\s*tojson\b/.test(expr)) {
        fail(
          `${file}: expression interpolated into a script block without |tojson -> ${expr.trim()}`,
        );
      }
    }
  }
}

function extractBalancedFromOffset(source, openBraceOffset) {
  if (openBraceOffset < 0 || source[openBraceOffset] !== "{") return "";
  let depth = 0;
  let inLineCmt = false;
  let inBlkCmt = false;
  let inStr = null;
  let esc = false;
  for (let i = openBraceOffset; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1] || "";
    if (inLineCmt) {
      if (ch === "\n") inLineCmt = false;
      continue;
    }
    if (inBlkCmt) {
      if (ch === "*" && next === "/") {
        inBlkCmt = false;
        i += 1;
      }
      continue;
    }
    if (esc) {
      esc = false;
      continue;
    }
    if (inStr) {
      if (ch === "\\") esc = true;
      else if (ch === inStr) inStr = null;
      continue;
    }
    if (ch === "/" && next === "/") {
      inLineCmt = true;
      i += 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      inBlkCmt = true;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inStr = ch;
      continue;
    }
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(openBraceOffset, i + 1);
    }
  }
  return "";
}

// ---------------------------------------------------------------------------
// 3. `message` listeners validate event.origin strictly; no global onmessage.
// ---------------------------------------------------------------------------
for (const file of jsFiles) {
  const source = read(file);
  const cleanSource = stripJsComments(source);
  checks += 1;
  if (
    /(?:^|[^\w.])(?:window|self|globalThis)\.onmessage\s*=/.test(cleanSource)
  ) {
    fail(
      `${file}: direct window.onmessage assignment is forbidden; use addEventListener("message") with origin validation`,
    );
  }
  const msgRe = /addEventListener\(\s*["']message["']\s*,\s*([^,)]+)/g;
  for (const match of source.matchAll(msgRe)) {
    checks += 1;
    const matchOffset = match.index ?? 0;
    const lineNum = source.slice(0, matchOffset).split(/\r?\n/).length;
    const handler = match[1].trim();
    let body = "";
    if (/^[A-Za-z_$][\w$]*$/.test(handler)) {
      const defMatch = source.match(
        new RegExp(`(?:const|let|var|function)\\s+${handler}\\b`),
      );
      if (defMatch && defMatch.index !== undefined) {
        const braceIdx = source.indexOf("{", defMatch.index);
        body = extractBalancedFromOffset(source, braceIdx);
      }
    } else {
      const braceIdx = source.indexOf("{", matchOffset);
      body = extractBalancedFromOffset(source, braceIdx);
    }
    const cleanBody = stripJsComments(body || "");
    const checksOrigin =
      /\borigin\b/.test(cleanBody) &&
      (/(?:!==?|===?)\s*(?:window\.|globalThis\.)?(?:location\.origin|expectedOrigin|selfOrigin|[A-Z_]*ORIGIN)/.test(
        cleanBody,
      ) ||
        /(?:location\.origin|expectedOrigin|[A-Z_]*ORIGIN)\s*(?:!==?|===?)/.test(
          cleanBody,
        ) ||
        /\.(?:has|includes)\([^)]*\.origin\b/.test(cleanBody));
    if (!checksOrigin) {
      fail(
        `${file}:${lineNum}: message listener ${handler} without a verified origin comparison`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// 4. Extension onMessage handlers reject foreign senders.
// ---------------------------------------------------------------------------
for (const file of jsFiles.filter((f) => f.startsWith("chrome_extension/"))) {
  const source = read(file);
  const lines = source.split(/\r?\n/);
  lines.forEach((line, index) => {
    if (!/onMessage\.addListener/.test(line)) return;
    checks += 1;
    const lineOffset = lines.slice(0, index).join("\n").length;
    const braceIdx = source.indexOf("{", lineOffset);
    const extracted =
      extractBalancedFromOffset(source, braceIdx) ||
      lines.slice(index, index + 60).join("\n");
    const cleanWindow = stripJsComments(extracted);
    // Require an actual rejection guard, not merely mentions of `sender` and
    // `runtime.id`: a handler that logs sender.id but continues would pass a
    // proximity-only check.
    const comparesSenderId =
      /sender\s*(?:&&\s*sender\s*)?\??\.\s*id\s*!==?\s*chrome\.runtime\.id/.test(
        cleanWindow,
      );
    const rejectsForeignSender =
      /if\s*\([^)]*sender[^)]*\)\s*\{[\s\S]{0,500}?(?:return|sendResponse|safeSendResponse)/.test(
        cleanWindow,
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
// 6. setInterval owners can clear their timers (comment-stripped).
// ---------------------------------------------------------------------------
for (const file of jsFiles) {
  const cleanSource = stripJsComments(read(file));
  const starts = cleanSource.match(/(?<!clear)setInterval\s*\(/g) || [];
  if (starts.length === 0) continue;
  checks += 1;
  if (!/clearInterval\s*\(/.test(cleanSource)) {
    fail(
      `${file}: creates ${starts.length} interval(s) but never calls clearInterval`,
    );
  }
}

// ---------------------------------------------------------------------------
// 7. Post-build policy posture: no remote script without SRI + crossorigin.
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
    checks += 1;
    if (!/\bcrossorigin\s*=\s*["']?anonymous\b/i.test(tag)) {
      fail(
        `${file}: CDN script without crossorigin="anonymous" -> ${tag.trim()}`,
      );
    }
  }
}

if (failures.length > 0) {
  console.error(`verify_ui_safety_contracts: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}

console.log(`verify_ui_safety_contracts: ${checks} checks passed`);
