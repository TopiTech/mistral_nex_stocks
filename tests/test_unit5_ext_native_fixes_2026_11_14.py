"""Unit 5 regression tests for extension, native host, PowerShell, and CI/test isolation fixes."""

from __future__ import annotations

import ctypes
import json
import re
import shutil
import subprocess
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_u5_01_powershell_scripts_syntax_and_safety_guards() -> None:
    """[U5-01][U5-03] PowerShell scripts fix trailing slash regex, StrictMode, handle close, and UTF8."""
    install_ps1 = _read("native_host/install_host_windows.ps1")
    diagnose_ps1 = _read("native_host/diagnose_native_host_windows.ps1")

    assert "'[\\\\/]$'" in install_ps1
    assert "'[\\/]$'" not in install_ps1

    assert "Set-StrictMode -Version Latest" in diagnose_ps1
    assert "$key.Close()" in diagnose_ps1
    assert "-Encoding UTF8" in diagnose_ps1

    ps_exe = shutil.which("pwsh") or shutil.which("powershell")
    if ps_exe is not None:
        parse_cmd = (
            "$errors = $null; $tokens = $null; "
            "foreach ($p in @('native_host/install_host_windows.ps1', 'native_host/diagnose_native_host_windows.ps1')) { "
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $p), [ref]$tokens, [ref]$errors); "
            "if ($errors -and $errors.Count -gt 0) { Write-Error ($errors | Out-String); exit 1 } "
            "}"
        )
        proc = subprocess.run(
            [ps_exe, "-NoProfile", "-Command", parse_cmd],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout


def test_u5_02_posix_ancestor_prefers_exe_over_spoofed_cmdline(tmp_path: Path) -> None:
    """[U5-02] /proc/<ppid>/exe takes precedence over spoofed cmdline argv[0]."""
    import native_host.native_host as nh

    pid_dir = tmp_path / "100"
    ppid_dir = tmp_path / "200"
    pid_dir.mkdir()
    ppid_dir.mkdir()
    (pid_dir / "status").write_text("Name:\tpython\nPPid:\t200\n", encoding="utf-8")
    (ppid_dir / "status").write_text("Name:\tchrome\nPPid:\t1\n", encoding="utf-8")
    (ppid_dir / "cmdline").write_bytes(b"evil-spoofed\x00--flag\x00")

    def _fake_readlink(path: Path | str) -> str:
        if Path(path) == ppid_dir / "exe":
            return "/opt/google/chrome/chrome (deleted)"
        raise OSError("no exe symlink")

    with patch("native_host.native_host.os.readlink", side_effect=_fake_readlink):
        ancestors = nh._get_posix_ancestor_process_names(
            max_depth=3, proc_dir=tmp_path, start_pid=100
        )
    assert ancestors == ["chrome"]

    # When exe readlink fails, falls back gracefully to cmdline argv[0].
    with patch("native_host.native_host.os.readlink", side_effect=OSError("EACCES")):
        fallback = nh._get_posix_ancestor_process_names(
            max_depth=3, proc_dir=tmp_path, start_pid=100
        )
    assert fallback == ["evil-spoofed"]


def test_u5_02_posix_ancestor_psutil_fallback_when_proc_missing() -> None:
    """[U5-02] Falls back to psutil.Process.parents() when /proc directory does not exist."""
    import native_host.native_host as nh

    fake_parent = SimpleNamespace(name=lambda: "Chrome.exe")
    fake_proc = SimpleNamespace(parents=lambda: [fake_parent])
    fake_psutil = SimpleNamespace(Process=lambda _pid: fake_proc)

    orig_is_dir = Path.is_dir

    def _mock_is_dir(self: Path) -> bool:
        if str(self).replace("\\", "/") == "/proc":
            return False
        return orig_is_dir(self)

    with (
        patch.object(Path, "is_dir", _mock_is_dir),
        patch("native_host.native_host.importlib.import_module", return_value=fake_psutil),
    ):
        ancestors = nh._get_posix_ancestor_process_names(
            max_depth=2, proc_dir="/proc", start_pid=None
        )
    assert ancestors == ["chrome.exe"]


def test_u5_02_sliding_window_recomputes_timestamp_after_ledger_lock(tmp_path: Path) -> None:
    """[U5-02] _sliding_window_allow recomputes now/cutoff after acquiring inter-process ledger lock."""
    import native_host.native_host as nh

    # First time.time() under _rate_limit_lock returns 100.0; subsequent calls after
    # _ledger_lock return 120.0. Window=10.0 -> post-lock cutoff=110.0, so 105.0 expires.
    times = iter([100.0])
    timestamps: list[float] = [95.0]
    ledger_path = tmp_path / "rate_limit_ledger.json"
    ledger_path.write_text(json.dumps({"ipc": [105.0]}), encoding="utf-8")

    with (
        patch.object(nh, "_RATE_LIMIT_LEDGER_PATH", ledger_path),
        patch.object(nh, "_rate_limit_ledger_disabled", False),
        patch("native_host.native_host.time.time", side_effect=lambda: next(times, 120.0)),
        patch("native_host.native_host._ledger_lock", return_value=True),
        patch("native_host.native_host._ledger_unlock"),
    ):
        allowed = nh._sliding_window_allow("ipc", timestamps, max_events=1, window=10.0)

    assert allowed is True
    assert timestamps == [120.0]
    persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert persisted["ipc"] == [120.0]


def test_u5_02_get_backend_port_clamps_out_of_range_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """[U5-02] Out-of-range MNS_BACKEND_PORT falls back to 5000."""
    import native_host.native_host as nh

    messages = iter([{"action": "get_backend_port", "extension_id": "a" * 32}, None])
    sent: list[dict[str, object]] = []
    monkeypatch.setenv("MNS_BACKEND_PORT", "70000")

    with (
        patch.object(nh, "read_message", side_effect=lambda: next(messages)),
        patch.object(nh, "send_message", side_effect=sent.append),
        patch.object(nh, "_check_rate_limit", return_value=True),
        patch.object(nh, "_require_valid_extension_id", return_value=True),
        patch.object(nh, "get_backend_port", None),
    ):
        nh.main()

    assert sent == [{"ok": True, "port": 5000}]


def test_u5_02_win32_signature_frees_cert_context_on_exception() -> None:
    """[U5-02] Non-NULL p_cert context is freed in finally when enumeration fails mid-loop."""
    import native_host.native_host as nh

    class _ByRefHandle(Protocol):
        _obj: wintypes.HANDLE

    def _fake_crypt_query(
        _o: object,
        _p: object,
        _c: object,
        _f: object,
        _dw: object,
        _enc: object,
        _ct: object,
        _ft: object,
        ph_store: _ByRefHandle,
        ph_msg: _ByRefHandle,
        _ctx: object,
    ) -> int:
        ph_store._obj.value = 0x5678
        ph_msg._obj.value = 0x9ABC
        return 1

    mock_wintrust = MagicMock()
    mock_wintrust.WinVerifyTrust.return_value = 0
    mock_crypt32 = MagicMock()
    mock_crypt32.CryptQueryObject.side_effect = _fake_crypt_query
    mock_crypt32.CertEnumCertificatesInStore.return_value = 0x1234
    mock_crypt32.CertGetNameStringW.side_effect = RuntimeError("boom during cert inspect")

    fake_windll = SimpleNamespace(wintrust=mock_wintrust, crypt32=mock_crypt32)

    with (
        patch.object(nh.os, "name", "nt"),
        patch.object(ctypes, "windll", fake_windll, create=True),
    ):
        res = nh._get_windows_win32_signature("C:\\Windows\\notepad.exe")

    assert res is None
    assert mock_crypt32.CertFreeCertificateContext.argtypes == [ctypes.c_void_p]
    mock_crypt32.CertFreeCertificateContext.assert_called_once_with(0x1234)
    mock_crypt32.CertCloseStore.assert_called_once()
    assert ctypes.c_void_p is not None and wintypes.HANDLE is not None


def test_u5_03_start_backend_oserror_corrupt_pid_and_returncode(tmp_path: Path) -> None:
    """[U5-03] is_port_in_use tolerates OSError, start() cleans corrupt PID & sets returncode=0."""
    import native_host.start_backend as sb

    with patch.object(sb.socket, "socket", side_effect=OSError("EAFNOSUPPORT")):
        assert sb.is_port_in_use(5000) is False

    pid_file = tmp_path / ".backend.pid"
    log_file = tmp_path / "backend.log"
    app_file = tmp_path / "app.py"
    lock_file = tmp_path / ".backend.start.lock"
    pid_file.write_text("not-a-pid", encoding="utf-8")
    log_file.write_bytes(b"")
    app_file.write_text("print('ok')\n", encoding="utf-8")

    fake_proc = MagicMock()
    fake_proc.pid = 42424
    fake_proc.returncode = None

    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "LOG", log_file),
        patch.object(sb, "APP", app_file),
        patch.object(sb, "STARTUP_LOCK_FILE", lock_file),
        patch.object(sb, "is_port_in_use", return_value=False),
        patch.object(sb, "get_backend_port", return_value=5000),
        patch.object(sb, "wait_for_backend_ready", return_value=True),
        patch.object(sb.subprocess, "Popen", return_value=fake_proc),
    ):
        result = sb.start()

    assert result["ok"] is True
    assert result["pid"] == 42424
    assert fake_proc.returncode == 0
    # Corrupt PID file was cleaned up before/during start, then overwritten cleanly with new pid.
    assert pid_file.read_text(encoding="utf-8") == "42424"


def test_u5_04_extension_popup_and_background_guards() -> None:
    """[U5-04] popup.refresh adopts ctx.backendPort and background.stopBackend aborts on failed token."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for extension runtime tests")

    script = r"""
const fs = require("node:fs");
const vm = require("node:vm");

function extractBlock(src, anchor) {
  const start = src.indexOf(anchor);
  if (start < 0) throw new Error("Anchor not found: " + anchor);
  let i = src.indexOf("{", start);
  let depth = 0;
  let inStr = null;
  let escaped = false;
  for (; i < src.length; i++) {
    const ch = src[i];
    if (escaped) { escaped = false; continue; }
    if (inStr) {
      if (ch === "\\") escaped = true;
      else if (ch === inStr) inStr = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") { inStr = ch; continue; }
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error("Unbalanced braces for: " + anchor);
}

(async () => {
  // 1. Test popup.js backendPortHydrated (lastError resilience) + refresh(ctx.backendPort)
  const popupSrc = fs.readFileSync("chrome_extension/popup.js", "utf8");
  const hydratedSnippet =
    extractBlock(popupSrc, "const backendPortHydrated = new Promise((resolve) => {") + ");";
  const refreshSnippet = extractBlock(popupSrc, "async function refresh()");

  const popupCtx = {
    chrome: {
      runtime: { lastError: { message: "storage failed" } },
      storage: { local: { get: (_k, cb) => cb(undefined) } },
    },
    normalizeBackendPort: (v) => Number(v),
    fallbackBackendBase: () => `http://127.0.0.1:${popupCtx.backendPort}`,
    backendPort: 5000,
    currentBackendBase: "http://127.0.0.1:5000",
    send: async () => ({
      ok: true,
      backendPort: 5432,
      browserName: "Chrome",
      health: { ok: true },
    }),
    setSafeText: () => {},
    setHealth: () => {},
    buildDiagnostics: () => "ok",
    browserPill: {},
    diagBox: {},
    Promise, Error,
  };
  vm.runInNewContext(`${hydratedSnippet}\n${refreshSnippet}`, popupCtx);
  await popupCtx.backendPortHydrated;
  await popupCtx.refresh();

  // 2. Test background.js stopBackend fast-fail when acquireShutdownToken fails without cached token
  const bgSrc = fs.readFileSync("chrome_extension/background.js", "utf8");
  const stopSnippet = extractBlock(bgSrc, 'if (message.action === "stopBackend")');
  let fetchCalled = false;
  let capturedResp = null;
  const bgCtx = {
    message: { action: "stopBackend" },
    checkHealth: async () => ({ ok: true }),
    sendNativeMessage: async () => ({ ok: false, error: "host unavailable" }),
    fetchWithTimeout: async () => { fetchCalled = true; return { ok: true }; },
    safeSendResponse: (r) => { capturedResp = r; return true; },
    setMnsShutdownToken: (t) => { bgCtx.mnsShutdownToken = t; },
    mnsShutdownToken: null,
    console, String, Promise, Error,
  };
  const wrappedStop = `(async () => { ${stopSnippet} })()`;
  await vm.runInNewContext(wrappedStop, bgCtx);

  console.log(JSON.stringify({
    hydratedAndRefreshedPort: popupCtx.backendPort,
    hydratedAndRefreshedBase: popupCtx.currentBackendBase,
    fetchCalled,
    capturedResp,
  }));
})();
"""
    proc = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    data = json.loads(proc.stdout.strip())
    assert data["hydratedAndRefreshedPort"] == 5432
    assert data["hydratedAndRefreshedBase"] == "http://127.0.0.1:5432"
    assert data["fetchCalled"] is False
    assert data["capturedResp"] == {"ok": False, "error": "host unavailable"}


def test_u5_05_test_isolation_and_ci_coverage(tmp_path: Path) -> None:
    """[U5-05] patches.py redirects legacy paths, client fixture restores CSRF, CI pylint/SRI updated."""
    import config_store
    import utils.storage
    from app import app
    from app_state import app_state
    from tests.conftest import client as client_fixture
    from tests.test_support.patches import apply_storage_and_config_patches

    # 1. Verify legacy paths redirected by apply_storage_and_config_patches
    sm = app_state.shutdown_manager
    orig_sm = (
        sm.runtime_state_dir,
        sm.token_file,
        sm.used_marker,
        sm._legacy_token_file,
        sm._legacy_used_marker,
    )
    orig_storage = (utils.storage.USER_STOCKS_FILE, utils.storage.LEGACY_USER_STOCKS_FILE)
    orig_cfg = (
        config_store.APP_DATA_DIR,
        config_store.CONFIG_FILE,
        config_store.USER_STOCKS_FILE,
        config_store.LEGACY_CONFIG_FILE,
    )
    orig_caches = (app_state.stock_disk_cache, app_state.payload_disk_cache)
    try:
        apply_storage_and_config_patches(tmp_path)
        assert sm.runtime_state_dir == tmp_path
        assert sm._legacy_token_file == tmp_path / "_legacy" / ".mns_shutdown_token"
        assert sm._legacy_used_marker == tmp_path / "_legacy" / ".mns_shutdown_token.used"
        assert utils.storage.LEGACY_USER_STOCKS_FILE == str(
            tmp_path / "_legacy" / "user_stocks.json"
        )
        assert config_store.LEGACY_CONFIG_FILE == tmp_path / "_legacy" / "config.json"
    finally:
        (
            sm.runtime_state_dir,
            sm.token_file,
            sm.used_marker,
            sm._legacy_token_file,
            sm._legacy_used_marker,
        ) = orig_sm
        utils.storage.USER_STOCKS_FILE, utils.storage.LEGACY_USER_STOCKS_FILE = orig_storage
        (
            config_store.APP_DATA_DIR,
            config_store.CONFIG_FILE,
            config_store.USER_STOCKS_FILE,
            config_store.LEGACY_CONFIG_FILE,
        ) = orig_cfg
        app_state.stock_disk_cache, app_state.payload_disk_cache = orig_caches

    # 2. Verify client fixture restores absent WTF_CSRF_ENABLED cleanly even when body raises
    had_before = "WTF_CSRF_ENABLED" in app.config
    prev_val = app.config.get("WTF_CSRF_ENABLED")
    try:
        app.config.pop("WTF_CSRF_ENABLED", None)
        gen = client_fixture.__wrapped__()
        next(gen)
        assert app.config["WTF_CSRF_ENABLED"] is False
        with pytest.raises(RuntimeError):
            gen.throw(RuntimeError("simulated test error"))
        assert "WTF_CSRF_ENABLED" not in app.config
    finally:
        if had_before:
            app.config["WTF_CSRF_ENABLED"] = prev_val
        else:
            app.config.pop("WTF_CSRF_ENABLED", None)

    # 3. Verify CI pylint target list & SRI regex
    ci_yml = _read(".github/workflows/ci.yml")
    pylint_line = next(line for line in ci_yml.splitlines() if "pylint --errors-only" in line)
    assert "native_host/" in pylint_line
    assert "schemas/" in pylint_line
    assert "scripts/" not in pylint_line
    for py_file in ROOT.glob("*.py"):
        assert py_file.name in pylint_line, f"{py_file.name} missing from CI pylint command"

    assert '["\']?https:\\/\\/' in ci_yml
    assert '["\']?http:\\/\\/' in ci_yml
    assert '["\']?\\/\\/' in ci_yml
    assert '["\']?sha384-' in ci_yml
    assert '["\']?sha256-' in ci_yml and '["\']?sha512-' in ci_yml
    assert "<script\\b([^>]*)>/gi" in ci_yml

    # Validate the exact SRI regex behavior against edge cases
    src_re = re.compile(
        r"""\bsrc\s*=\s*(?:["']?https://|["']?http://|["']?//)""",
        re.IGNORECASE,
    )
    integrity_re = re.compile(
        r"""\bintegrity\s*=\s*(?:["']?sha384-|["']?sha256-|["']?sha512-)""",
        re.IGNORECASE,
    )
    assert src_re.search("SRC=//cdn.example.com/lib.js")
    assert not src_re.search('src="/static/js/app.js"')
    assert integrity_re.search("INTEGRITY=sha256-deadbeef")
    assert integrity_re.search("integrity='sha512-deadbeef'")
