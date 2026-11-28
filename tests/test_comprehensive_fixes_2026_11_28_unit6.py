"""Unit 6 comprehensive review fixes regression tests (2026-11-28)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_host_launcher_template_sets_utf8_codepage_silently() -> None:
    template = (ROOT / "native_host" / "host_launcher.cmd.template").read_text(encoding="utf-8")
    lines = [line.strip() for line in template.splitlines() if line.strip()]
    assert lines[0].lower() == "@echo off"
    assert lines[1].lower() == "chcp 65001 >nul 2>nul"


def test_native_host_posix_temp_dir_scoping_and_ledger_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from native_host import native_host as nh
    from native_host import start_backend as sb

    # Verify UID-scoped directory name on POSIX vs default on Windows
    monkeypatch.setattr(os, "getuid", lambda: 1042, raising=False)
    assert nh._temp_fallback_dir_name() == "mistral_nex_stocks-1042"
    assert sb._temp_fallback_dir_name() == "mistral_nex_stocks-1042"

    # Reject foreign-owned fallback directory on POSIX
    foreign_dir = tmp_path / "mistral_nex_stocks-1042"
    foreign_dir.mkdir(parents=True)
    monkeypatch.setattr(
        nh.os,
        "lstat",
        lambda _p: SimpleNamespace(st_mode=0o040755, st_uid=9999),
    )
    assert nh._ensure_secure_temp_dir(foreign_dir) is False

    monkeypatch.setattr(
        sb.os,
        "lstat",
        lambda _p: SimpleNamespace(st_mode=0o040755, st_uid=9999),
    )
    with pytest.raises(OSError, match="not owned by current user"):
        sb._ensure_secure_fallback_dir(foreign_dir)

    # _sliding_window_allow uses O_NOFOLLOW when available
    captured_flags: list[int] = []
    ledger_file = tmp_path / "ledger.json"
    monkeypatch.setattr(nh, "_RATE_LIMIT_LEDGER_PATH", ledger_file)
    monkeypatch.setattr(nh, "_rate_limit_ledger_disabled", False)
    monkeypatch.setattr(nh.os, "O_NOFOLLOW", 0x20000, raising=False)
    monkeypatch.setattr(nh, "_ledger_lock", lambda _fd: False)
    monkeypatch.delattr(os, "getuid", raising=False)

    orig_open = os.open

    def _spy_open(path, flags, mode=0o777):
        if Path(path) == ledger_file:
            captured_flags.append(flags)
            flags = flags & ~0x20000
        return orig_open(path, flags, mode)

    monkeypatch.setattr(nh.os, "open", _spy_open)
    nh._sliding_window_allow("test_bucket", [], 10, 1.0)
    assert len(captured_flags) == 1
    assert captured_flags[0] & 0x20000


def test_background_js_startup_session_hydration_does_not_resurrect_rejected_token() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for extension tests"

    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("vm");

let storedSessionCallback = null;
const source = fs.readFileSync("chrome_extension/background.js", "utf8");
const prefix = source.split("let mnsExtensionTokenInflight = null;")[0];

const context = {
  chrome: {
    storage: {
      local: {
        get: (_keys, cb) => cb({}),
        remove: () => undefined,
      },
      session: {
        get: (_keys, cb) => {
          storedSessionCallback = cb;
          return undefined;
        },
      },
    },
  },
  console: { log() {}, warn() {}, error() {} },
};

vm.runInNewContext(
  `${prefix}
   // Simulate forced refresh starting before initial storage.session.get callback fires:
   mnsExtensionTokenGeneration = 1;
   mnsExtensionTokenForceInflight = Promise.resolve("fresh-token");
   this._getToken = () => mnsExtensionToken;
  `,
  context,
);

assert.equal(typeof storedSessionCallback, "function");
storedSessionCallback({ mnsExtensionToken: "stale-rejected-token" });
assert.equal(context._getToken(), null);
"""
    res = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Node check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
