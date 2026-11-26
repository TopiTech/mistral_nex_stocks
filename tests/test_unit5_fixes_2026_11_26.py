"""Regression tests for Unit 5 (extension, native host, verification scripts, hooks) fixes."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_u5_1_start_backend_uses_correct_create_breakaway_from_job_constant() -> None:
    from native_host import start_backend

    captured_kwargs: list[dict[str, Any]] = []
    mock_proc = MagicMock()
    mock_proc.pid = 54321

    def _fake_popen(_cmd: Any, **kwargs: Any) -> MagicMock:
        captured_kwargs.append(dict(kwargs))
        return mock_proc

    mock_pid = MagicMock()
    mock_pid.exists.return_value = False
    mock_pid.with_suffix.return_value = MagicMock()
    mock_legacy = MagicMock()
    mock_legacy.exists.return_value = False

    with (
        patch("subprocess.Popen", side_effect=_fake_popen),
        patch("native_host.start_backend.is_port_in_use", return_value=False),
        patch("native_host.start_backend.is_running", return_value=True),
        patch("native_host.start_backend.PID_FILE", mock_pid),
        patch("native_host.start_backend._LEGACY_PID_FILE", mock_legacy),
        patch("native_host.start_backend.APP") as mock_app,
        patch("native_host.start_backend.os.name", "nt"),
        patch("os.replace"),
        patch("native_host.start_backend.wait_for_backend_ready", return_value=False),
    ):
        mock_app.exists.return_value = True
        res = start_backend._start()

    assert res["ok"] is True
    assert len(captured_kwargs) == 1
    expected_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    assert expected_flag == 0x01000000
    assert (captured_kwargs[0]["creationflags"] & 0x01000000) == 0x01000000
    assert (captured_kwargs[0]["creationflags"] & 0x00000100) == 0


def test_u5_2_hooks_json_points_to_existing_guard_script() -> None:
    hooks_path = ROOT_DIR / ".agents" / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    cmd = data["command-guard"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd == "python scripts/pre_command_guard.py"
    script_rel = cmd.split()[1]
    assert (hooks_path.parent / script_rel).is_file()


def test_u5_4_validate_native_host_script_rejects_missing_explicit_manifest() -> None:
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh is None:
        pytest.skip("PowerShell is required to verify validate_native_host_windows.ps1")

    script_path = ROOT_DIR / "native_host" / "validate_native_host_windows.ps1"
    missing_path = ROOT_DIR / "native_host" / "definitely_missing_manifest_2026_11_26.json"
    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script_path),
            "-ManifestPath",
            str(missing_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode != 0
    assert "Manifest file not found" in (proc.stdout + proc.stderr)


def test_u5_5_verify_ui_safety_contracts_catches_per_expr_tojson_and_multiline_listener(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to test verify_ui_safety_contracts.mjs")

    script_src = (ROOT_DIR / "scripts" / "verify_ui_safety_contracts.mjs").read_text(
        encoding="utf-8"
    )
    assert "exprs = block.match(/{{[\\s\\S]*?}}/g)" in script_src
    assert "source.matchAll(msgRe)" in script_src

    # Negative check on a synthetic fixture copied into a temp workspace
    (tmp_path / "templates").mkdir()
    (tmp_path / "static" / "js").mkdir(parents=True)
    (tmp_path / "chrome_extension").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "verify_ui_safety_contracts.mjs").write_text(
        script_src, encoding="utf-8"
    )

    (tmp_path / "templates" / "bad.html").write_text(
        '<script nonce="x">const a = {{ app_config.a | tojson }}; const b = {{ app_config.b }};</script>',
        encoding="utf-8",
    )
    bad_tpl_proc = subprocess.run(
        [node, str(tmp_path / "scripts" / "verify_ui_safety_contracts.mjs")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert bad_tpl_proc.returncode != 0

    (tmp_path / "templates" / "bad.html").unlink()
    (tmp_path / "static" / "js" / "bad.js").write_text(
        'window.addEventListener(\n  "message",\n  (ev) => {\n    console.log(ev.data);\n  },\n);\n',
        encoding="utf-8",
    )
    bad_js_proc = subprocess.run(
        [node, str(tmp_path / "scripts" / "verify_ui_safety_contracts.mjs")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert bad_js_proc.returncode != 0


def test_u5_6_pre_command_guard_handles_escaped_quotes_equals_flags_and_chains() -> None:
    import importlib.util

    guard_path = ROOT_DIR / ".agents" / "scripts" / "pre_command_guard.py"
    spec = importlib.util.spec_from_file_location("pre_command_guard", guard_path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    # 1. --regexp=foo src/ must NOT append "."
    out1 = guard.process_command("rg --regexp=foo src/")
    assert "src/ ." not in out1
    assert out1.rstrip().endswith("src/")

    # 2. Escaped quotes inside rg pattern before a pipe must NOT corrupt the pipe
    out2 = guard.process_command('rg "foo\\"bar" src/ | head')
    assert out2.endswith("src/ | head")
    assert "| head ." not in out2

    # 3. Chained rg invocations must BOTH be rewritten
    out3 = guard.process_command("rg foo && rg bar")
    assert out3.count("--max-filesize 2M") == 2
    assert out3.endswith("bar .")

    # 4. --type-list should pass through untouched
    assert guard.process_command("rg --type-list") == "rg --type-list"


def test_u5_7_popup_js_clears_stale_aria_label_and_prevents_late_storage_overwrite() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for popup.js tests")

    out = subprocess.run(
        [
            node,
            "-e",
            r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const source = fs.readFileSync("chrome_extension/popup.js", "utf8");
const attrs = new Map();
const stockListEl = {
  id: "stockListContainer",
  className: "",
  textContent: "",
  scrollTop: 0,
  children: [],
  classList: {
    set: new Set(),
    add(c) { this.set.add(c); },
    remove(c) { this.set.delete(c); },
    contains(c) { return this.set.has(c); },
  },
  setAttribute: (k, v) => attrs.set(k, String(v)),
  getAttribute: (k) => attrs.get(k) ?? null,
  removeAttribute: (k) => attrs.delete(k),
  querySelector: () => null,
  querySelectorAll: () => [],
  appendChild(c) { this.children.push(c); return c; },
};

const els = new Map([["stockListContainer", stockListEl]]);
const makeNode = (id) => ({
  id,
  value: "",
  textContent: "",
  className: "",
  classList: { add() {}, remove() {}, toggle() {} },
  setAttribute() {},
  removeAttribute() {},
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  appendChild() {},
});
const getEl = (id) => {
  if (!els.has(id)) els.set(id, makeNode(id));
  return els.get(id);
};

let storageCb = null;
const ctx = {
  console,
  Promise,
  Map,
  Set,
  URL,
  Date,
  Math,
  Number,
  String,
  Array,
  Object,
  AbortController,
  setTimeout: () => 1,
  clearTimeout: () => {},
  document: {
    activeElement: null,
    getElementById: getEl,
    querySelectorAll: () => [],
    createElement: () => makeNode("dyn"),
    createDocumentFragment: () => {
      const frag = {
        childNodes: [],
        appendChild(c) {
          frag.childNodes.push(c);
          return c;
        },
      };
      return frag;
    },
    createTextNode: (text) => ({ textContent: text }),
    addEventListener: () => {},
  },
  window: { addEventListener: () => {} },
  chrome: {
    storage: {
      local: {
        get: (_keys, cb) => { storageCb = cb; },
      },
    },
    runtime: {
      lastError: null,
      sendMessage: async () => ({ ok: true, backendPort: 5055, health: { ok: true, base: "http://127.0.0.1:5055", data: {} }, backendUrls: [] }),
    },
  },
  fetch: async () => ({
    ok: true,
    json: async () => ({ stocks: { us: [], jp: [], idx: [] } }),
  }),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(source, ctx);

(async () => {
  // Simulate live refresh resolving port 5055 before late storage.local.get returns 5000
  await ctx.refresh();
  assert.strictEqual(vm.runInContext("backendPort", ctx), 5055);
  storageCb({ backendPort: 5000 });
  assert.strictEqual(vm.runInContext("backendPort", ctx), 5055, "late storage callback must not overwrite live port");

  // Simulate offline aria-label followed by successful fetchAndRenderStocks recovery
  ctx.setHealth({ ok: false });
  assert.ok(stockListEl.getAttribute("aria-label"));
  await ctx.fetchAndRenderStocks("http://127.0.0.1:5055");
  assert.strictEqual(stockListEl.getAttribute("aria-label"), null, "aria-label must clear after successful render");
  assert.strictEqual(stockListEl.classList.contains("stale"), false, "stale class must clear after successful render");

  process.stdout.write("OK");
})();
""",
        ],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert out.returncode == 0, f"Node failed:\nSTDOUT:{out.stdout}\nSTDERR:{out.stderr}"
    assert out.stdout == "OK"


def test_u5_8_native_host_sleeps_on_empty_shutdown_token_file(tmp_path: Path) -> None:
    from native_host import native_host

    valid_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    token_file = tmp_path / ".mns_shutdown_token"
    token_file.write_text("", encoding="utf-8")
    sleeps: list[float] = []
    sent: list[dict[str, Any]] = []
    req = {"extensionId": valid_id, "action": "get_shutdown_token"}

    native_host._reset_caller_ancestry_cache()
    try:
        with (
            patch("config_store.APP_DATA_DIR", tmp_path),
            patch("sys.argv", ["native_host.py", f"chrome-extension://{valid_id}/"]),
            patch.object(native_host, "_load_allowed_manifest_origins", return_value={valid_id}),
            patch.object(
                native_host,
                "_get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
            patch.object(native_host, "_token_action_allowed", return_value=True),
            patch.object(native_host, "is_backend_healthy_once", return_value=True),
            patch.object(native_host, "read_message", side_effect=[req, None]),
            patch.object(native_host, "send_message", side_effect=sent.append),
            patch.object(native_host.time, "sleep", side_effect=sleeps.append),
        ):
            native_host.main()
    finally:
        native_host._reset_caller_ancestry_cache()

    assert len(sleeps) == 9
    assert sent and sent[0]["ok"] is False
