"""Regression tests for the 2026-11-15 extension/native-host review fixes.

Covers:
- fetchWithTimeout keeps its budget armed while the response body is read.
- Concurrent forced extension-token refreshes share one native acquisition.
- A 403 that is not a rejected extension credential does not spend a token read.
- A content-script error is not displayed as "no tickers".
- start_backend distinguishes a warming-up backend from a foreign listener and
  from a reused PID.
- POSIX executable-path trust rejects world-writable locations.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node(script: str, expected_marker: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for JavaScript unit tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert result.stdout.strip().endswith(expected_marker), (
        f"Expected {expected_marker}, got STDOUT={result.stdout!r} STDERR={result.stderr!r}"
    )


_NODE_PRELUDE = r"""
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let __finished = false;
process.on("beforeExit", () => {
  if (!__finished) {
    console.error("Node exited before async test completed (dangling promise)");
    process.exit(1);
  }
});

function readRel(relPath) {
  return fs.readFileSync(path.join(process.cwd(), relPath), "utf8");
}

function extractNamedBlock(src, marker) {
  const start = src.indexOf(marker);
  assert(start !== -1, "Missing marker: " + marker);
  const declStart = src.lastIndexOf("\n", start) + 1;
  let i = src.indexOf("(", start);
  let parens = 0;
  let inParen = false;
  for (; i < src.length; i++) {
    if (src[i] === "(") { parens++; inParen = true; }
    else if (src[i] === ")") { parens--; if (inParen && parens === 0) break; }
  }
  const open = src.indexOf("{", i);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") {
      depth--;
      if (depth === 0) return src.slice(declStart, j + 1);
    }
  }
  throw new Error("Unbalanced block for " + marker);
}

function makeElement(tag = "div") {
  const listeners = new Map();
  const classes = new Set();
  const children = [];
  const attrs = {};
  let _className = "";
  const el = {
    tagName: String(tag).toUpperCase(),
    type: "",
    id: "",
    title: "",
    hidden: false,
    textContent: "",
    value: "",
    dataset: {},
    style: {},
    children,
    classList: {
      add(...items) { items.forEach((c) => c && classes.add(c)); _className = Array.from(classes).join(" "); },
      remove(...items) { items.forEach((c) => classes.delete(c)); _className = Array.from(classes).join(" "); },
      contains(c) { return classes.has(c); },
      toggle(c, force) {
        const next = force === undefined ? !classes.has(c) : !!force;
        if (next) classes.add(c); else classes.delete(c);
        _className = Array.from(classes).join(" ");
        return next;
      },
    },
    get className() { return _className; },
    set className(val) {
      _className = String(val || "");
      classes.clear();
      _className.split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    appendChild(child) { children.push(child); return child; },
    replaceChildren(...nodes) { children.length = 0; nodes.forEach((n) => children.push(n)); },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatchEvent(type, eventObj = {}) {
      const fns = listeners.get(type) || [];
      return Promise.all(fns.map((fn) => fn({ stopPropagation() {}, preventDefault() {}, ...eventObj })));
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  return el;
}

function findAll(root, predicate, out = []) {
  if (!root || typeof root !== "object") return out;
  if (predicate(root)) out.push(root);
  if (Array.isArray(root.children)) {
    for (const c of root.children) findAll(c, predicate, out);
  }
  return out;
}
"""


def test_fetch_with_timeout_covers_body_consumption():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  for (const sourcePath of ["chrome_extension/background.js", "chrome_extension/popup.js"]) {
    const src = readRel(sourcePath);
    const block = extractNamedBlock(src, "function fetchWithTimeout(");
    const ctx = {
      AbortController,
      setTimeout,
      clearTimeout,
      console,
      fetch: async (_url, options) => ({
        ok: true,
        status: 200,
        json: () =>
          new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () => {
              const err = new Error("aborted");
              err.name = "AbortError";
              reject(err);
            }, { once: true });
          }),
      }),
    };
    vm.runInNewContext(block + "; this.fetchWithTimeout = fetchWithTimeout;", ctx);
    const started = Date.now();
    let caught = null;
    try {
      const res = await ctx.fetchWithTimeout("http://127.0.0.1:1/api/health", {}, 60);
      await res.json();
    } catch (err) {
      caught = err;
    }
    assert(caught && caught.name === "TimeoutError", sourcePath + ": " + (caught && caught.name));
    assert(Date.now() - started < 2000, sourcePath + ": timeout must be bounded");
  }
  __finished = true;
  console.log("FETCH BODY TIMEOUT PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "FETCH BODY TIMEOUT PASS")


def test_concurrent_forced_token_refresh_is_single_flight():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  const src = readRel("chrome_extension/background.js");
  const acquireBlock = extractNamedBlock(src, "async function _acquireExtensionToken(");
  const getBlock = extractNamedBlock(src, "async function getOrFetchExtensionToken(");
  let nativeReads = 0;
  const sessionStore = {
    async get() { return { mnsExtensionToken: "stale-token" }; },
    async remove() { return undefined; },
  };
  const ctx = {
    console,
    setTimeout,
    AbortController,
    mnsExtensionToken: "stale-token",
    mnsExtensionTokenInflight: null,
    mnsExtensionTokenForceInflight: null,
    mnsExtensionTokenGeneration: 0,
    getMnsSessionStorage: () => sessionStore,
    setMnsExtensionToken: (value) => { ctx.mnsExtensionToken = value; },
    sendNativeMessage: async () => {
      nativeReads += 1;
      await new Promise((resolve) => setTimeout(resolve, 15));
      return { ok: true, token: "fresh-token" };
    },
  };
  ctx.globalThis = ctx;
  vm.runInNewContext(
    acquireBlock + "\n" + getBlock +
      "\nthis.getOrFetchExtensionToken = getOrFetchExtensionToken;",
    ctx,
  );
  const results = await Promise.all([
    ctx.getOrFetchExtensionToken(true),
    ctx.getOrFetchExtensionToken(true),
    ctx.getOrFetchExtensionToken(true),
    ctx.getOrFetchExtensionToken(true),
  ]);
  assert.deepStrictEqual(results, ["fresh-token", "fresh-token", "fresh-token", "fresh-token"]);
  assert.strictEqual(nativeReads, 1, "concurrent forced refreshes must share one native read");
  __finished = true;
  console.log("TOKEN SINGLE FLIGHT PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "TOKEN SINGLE FLIGHT PASS")


def test_unverified_backend_never_receives_extension_token():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  const src = readRel("chrome_extension/background.js");
  const block = extractNamedBlock(src, "async function addStockViaExtension(");
  let tokenCalls = 0;
  let fetchCalls = 0;
  const ctx = {
    console,
    backendVerified: false,
    ADD_STOCK_TIMEOUT_MS: 1000,
    getOrFetchExtensionToken: async () => { tokenCalls += 1; return "token"; },
    fetchWithTimeout: async () => { fetchCalls += 1; return { ok: true, status: 200, json: async () => ({}) }; },
  };
  vm.runInNewContext(block + "; this.addStockViaExtension = addStockViaExtension;", ctx);
  let caught = null;
  try {
    await ctx.addStockViaExtension("http://127.0.0.1:5000", "AAPL", "us");
  } catch (err) {
    caught = err;
  }
  assert(caught && /正当性/.test(caught.message), "unverified backend must be rejected");
  assert.strictEqual(tokenCalls, 0, "token must not be acquired for an unverified backend");
  assert.strictEqual(fetchCalls, 0, "no request may be sent to an unverified backend");
  __finished = true;
  console.log("UNVERIFIED BACKEND PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "UNVERIFIED BACKEND PASS")


def test_non_token_403_does_not_refresh_extension_token():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  const src = readRel("chrome_extension/background.js");
  const block = extractNamedBlock(src, "async function addStockViaExtension(");
  let tokenCalls = 0;
  let fetchCalls = 0;
  const ctx = {
    console,
    ADD_STOCK_TIMEOUT_MS: 1000,
    getOrFetchExtensionToken: async () => { tokenCalls += 1; return "token"; },
    fetchWithTimeout: async () => {
      fetchCalls += 1;
      return {
        ok: false,
        status: 403,
        json: async () => ({ ok: false, details: { reason: "forbidden in remote API mode" } }),
      };
    },
  };
  vm.runInNewContext(block + "; this.addStockViaExtension = addStockViaExtension;", ctx);
  const result = await ctx.addStockViaExtension("http://127.0.0.1:5000", "AAPL", "us");
  assert.strictEqual(result.res.status, 403);
  assert.strictEqual(fetchCalls, 1, "non-token 403 must not retry");
  assert.strictEqual(tokenCalls, 1, "non-token 403 must not refresh the token");

  // A rejected extension credential still refreshes exactly once.
  let tokenCalls2 = 0;
  let fetchCalls2 = 0;
  const ctx2 = {
    console,
    ADD_STOCK_TIMEOUT_MS: 1000,
    getOrFetchExtensionToken: async () => { tokenCalls2 += 1; return "token"; },
    fetchWithTimeout: async () => {
      fetchCalls2 += 1;
      return {
        ok: false,
        status: 403,
        json: async () => ({ ok: false, details: { reason: "invalid or missing extension token" } }),
      };
    },
  };
  vm.runInNewContext(block + "; this.addStockViaExtension = addStockViaExtension;", ctx2);
  const result2 = await ctx2.addStockViaExtension("http://127.0.0.1:5000", "AAPL", "us");
  assert.strictEqual(result2.res.status, 403);
  assert.strictEqual(fetchCalls2, 2, "rejected token must retry once");
  assert.strictEqual(tokenCalls2, 2, "rejected token must refresh once");
  __finished = true;
  console.log("TOKEN 403 PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "TOKEN 403 PASS")


def test_content_script_error_is_not_reported_as_no_tickers():
    node_script = (
        _NODE_PRELUDE
        + r"""
(async () => {
  const src = readRel("chrome_extension/popup.js");
  const block = extractNamedBlock(src, "async function loadDetectedTickers(");
  const container = makeElement("div");
  const titleEl = makeElement("div");
  const ctx = {
    console,
    detectorGeneration: 0,
    document: {
      createElement: (tag) => makeElement(tag),
      getElementById: (id) => ({ detectedListContainer: container, detectorPageTitle: titleEl }[id] || null),
    },
    $: (id) => ({ detectedListContainer: container, detectorPageTitle: titleEl }[id] || null),
    setSafeText: (el, text) => { el.textContent = String(text ?? ""); },
    isInjectableUrl: () => true,
    chrome: {
      tabs: {
        query: async () => [{ id: 1, url: "https://finance.yahoo.com/quote/AAPL", title: "Yahoo" }],
        sendMessage: async () => ({ ok: false, error: "検出に失敗しました" }),
      },
      scripting: null,
    },
  };
  vm.runInNewContext(block + "; this.loadDetectedTickers = loadDetectedTickers;", ctx);
  await ctx.loadDetectedTickers();
  const text = findAll(container, (el) => typeof el.textContent === "string")
    .map((el) => el.textContent)
    .join(" ");
  assert(text.includes("検出に失敗しました"), "content-script error must be shown: " + text);
  assert(!text.includes("見つかりませんでした"), "error must not masquerade as no tickers: " + text);
  __finished = true;
  console.log("CONTENT ERROR PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    _run_node(node_script, "CONTENT ERROR PASS")


def _fresh_pid_file(tmp: Path, pid: int = 12345) -> Path:
    pid_file = tmp / ".backend.pid"
    pid_file.write_text(str(pid), encoding="utf-8")
    return pid_file


def test_fresh_pid_with_bound_port_is_warming_up_not_unhealthy():
    import native_host.start_backend as sb

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpdir = Path(tmp)
        pid_file = _fresh_pid_file(tmpdir)
        with (
            patch.object(sb, "PID_FILE", pid_file),
            patch.object(sb, "STARTUP_LOCK_FILE", tmpdir / ".backend.start.lock"),
            patch.object(sb, "is_port_in_use", return_value=True),
            patch.object(sb, "is_running", return_value=True),
            patch.object(sb, "_pid_is_backend_process", return_value=True),
            patch.object(sb, "is_backend_healthy_once", return_value=False),
            patch.object(sb.subprocess, "Popen") as popen,
        ):
            result = sb.start()

        assert result.get("ok") is True
        assert result.get("warming_up") is True
        assert pid_file.exists(), "a warming-up backend must keep its PID file"
        popen.assert_not_called()


def test_stale_pid_with_bound_port_is_reported_as_unhealthy():
    import native_host.start_backend as sb

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpdir = Path(tmp)
        pid_file = _fresh_pid_file(tmpdir)
        stale = 1_600_000_000
        os.utime(pid_file, (stale, stale))
        with (
            patch.object(sb, "PID_FILE", pid_file),
            patch.object(sb, "STARTUP_LOCK_FILE", tmpdir / ".backend.start.lock"),
            patch.object(sb, "is_port_in_use", return_value=True),
            patch.object(sb, "is_running", return_value=True),
            patch.object(sb, "_pid_is_backend_process", return_value=True),
            patch.object(sb, "is_backend_healthy_once", return_value=False),
            patch.object(sb.time, "time", return_value=stale + sb.PID_WARMUP_GRACE_SEC + 5),
            patch.object(sb.subprocess, "Popen") as popen,
        ):
            result = sb.start()

        assert result.get("ok") is False
        assert "unhealthy" in result.get("error", "")
        assert not pid_file.exists()
        popen.assert_not_called()


def test_reused_pid_is_not_reported_as_warming_up():
    import native_host.start_backend as sb

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpdir = Path(tmp)
        pid_file = _fresh_pid_file(tmpdir)
        log_file = tmpdir / "backend.log"
        app_file = tmpdir / "app.py"
        log_file.write_bytes(b"")
        app_file.write_text("print('ok')\n", encoding="utf-8")
        fake_proc = MagicMock()
        fake_proc.pid = 98765
        with (
            patch.object(sb, "PID_FILE", pid_file),
            patch.object(sb, "STARTUP_LOCK_FILE", tmpdir / ".backend.start.lock"),
            patch.object(sb, "LOG", log_file),
            patch.object(sb, "APP", app_file),
            patch.object(sb, "is_port_in_use", return_value=False),
            patch.object(sb, "is_running", return_value=True),
            patch.object(sb, "_pid_is_backend_process", return_value=False),
            patch.object(sb, "is_backend_healthy_once", return_value=False),
            patch.object(sb, "wait_for_backend_ready", return_value=True),
            patch.object(sb.subprocess, "Popen", return_value=fake_proc) as popen,
        ):
            result = sb.start()

        assert result.get("ok") is True
        assert result.get("pid") == 98765
        popen.assert_called_once()
        assert pid_file.read_text(encoding="utf-8") == "98765"


def test_backend_verification_requires_pid_to_own_listening_port(tmp_path):
    import native_host.start_backend as sb

    pid_file = tmp_path / ".backend.pid"
    pid_file.write_text("12345", encoding="utf-8")
    conn = types.SimpleNamespace(
        pid=12345,
        laddr=types.SimpleNamespace(port=5000),
    )
    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        patch.object(sb, "_pid_is_backend_process", return_value=True),
        patch.object(sb.psutil, "net_connections", return_value=[conn]),
    ):
        assert sb.get_backend_verification(5000) is True

    foreign = types.SimpleNamespace(
        pid=99999,
        laddr=types.SimpleNamespace(port=5000),
    )
    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        patch.object(sb, "_pid_is_backend_process", return_value=True),
        patch.object(sb.psutil, "net_connections", return_value=[foreign]),
    ):
        assert sb.get_backend_verification(5000) is False

    missing = tmp_path / "missing.pid"
    with (
        patch.object(sb, "PID_FILE", missing),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
    ):
        assert sb.get_backend_verification(5000) is None


def test_backend_verification_accepts_launcher_child_owning_port(tmp_path):
    """Windows venv pythonw.exe redirectors run app.py in a child process."""
    import native_host.start_backend as sb

    pid_file = tmp_path / ".backend.pid"
    pid_file.write_text("12345", encoding="utf-8")
    child_listener = types.SimpleNamespace(
        pid=54321,
        laddr=types.SimpleNamespace(port=5000),
    )
    # The recorded launcher PID must still be recognised as app.py.
    identity = {12345: True, 54321: True}.get

    class _FakeChild:
        def __init__(self, parents):
            self._parents = parents

        def parents(self):
            return [types.SimpleNamespace(pid=parent) for parent in self._parents]

    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        patch.object(sb, "_pid_is_backend_process", side_effect=identity),
        patch.object(sb.psutil, "Process", return_value=_FakeChild([12345])),
        patch.object(sb.psutil, "net_connections", return_value=[child_listener]),
    ):
        assert sb.get_backend_verification(5000) is True

    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        patch.object(sb, "_pid_is_backend_process", side_effect=identity),
        patch.object(sb.psutil, "Process", return_value=_FakeChild([777])),
        patch.object(sb.psutil, "net_connections", return_value=[child_listener]),
    ):
        assert sb.get_backend_verification(5000) is False

    # A descendant that is not an app.py process is not accepted either.
    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        patch.object(sb, "_pid_is_backend_process", side_effect={12345: True}.get),
        patch.object(sb.psutil, "Process", return_value=_FakeChild([12345])),
        patch.object(sb.psutil, "net_connections", return_value=[child_listener]),
    ):
        assert sb.get_backend_verification(5000) is False


def test_posix_executable_path_trust_rejects_world_writable_locations():
    from native_host import native_host

    def fake_stat(mode: int):
        return types.SimpleNamespace(st_mode=mode)

    with patch.object(native_host.os, "stat", return_value=fake_stat(0o755)):
        assert native_host._posix_executable_path_is_trusted("/usr/bin/chrome") is True

    with patch.object(native_host.os, "stat", return_value=fake_stat(0o777)):
        assert native_host._posix_executable_path_is_trusted("/tmp/chrome") is False

    def fake_world_writable_parent(path):
        # The executable itself is 0755; its parent (platform-specific
        # absolute form of /tmp) is 0777.
        return fake_stat(0o755 if str(path).endswith("chrome") else 0o777)

    with patch.object(native_host.os, "stat", side_effect=fake_world_writable_parent):
        assert native_host._posix_executable_path_is_trusted("/tmp/chrome") is False

    with patch.object(native_host.os, "stat", side_effect=OSError("missing")):
        assert native_host._posix_executable_path_is_trusted("/missing/chrome") is False
