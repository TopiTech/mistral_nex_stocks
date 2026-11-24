"""Regression and edge-case tests for Unit 5 extension, native host & tooling fixes (2026-11-25)."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_ext_01_win32_signature_queries_signer_info_not_enum() -> None:
    """EXT-01: _get_windows_win32_signature must resolve only the CMSG_SIGNER_INFO certificate."""
    import native_host.native_host as nh

    if os.name == "nt":
        real_sig = nh._get_windows_win32_signature(sys.executable)
        assert real_sig is not None
        assert real_sig[0] == "Valid"
        assert "Python Software Foundation" in real_sig[1]

    # Mock path: verify CertEnumCertificatesInStore is NOT used and missing h_msg fails closed
    def _fake_query_no_msg(
        _o, _p, _c, _f, _dw, _enc, _ct, _ft, ph_store, ph_msg, _ctx
    ) -> int:
        ph_store._obj.value = 0x1111
        ph_msg._obj.value = 0
        return 1

    mock_wintrust = MagicMock()
    mock_wintrust.WinVerifyTrust.return_value = 0
    mock_crypt32 = MagicMock()
    mock_crypt32.CryptQueryObject.side_effect = _fake_query_no_msg
    fake_windll = SimpleNamespace(wintrust=mock_wintrust, crypt32=mock_crypt32)

    with (
        patch.object(nh.os, "name", "nt"),
        patch.object(ctypes, "windll", fake_windll, create=True),
    ):
        assert nh._get_windows_win32_signature("C:\\Windows\\notepad.exe") is None
        mock_crypt32.CertCloseStore.assert_called_once()
        assert mock_crypt32.CertCloseStore.call_args[0][0].value == 0x1111
        mock_crypt32.CertEnumCertificatesInStore.assert_not_called()


def test_ext_02_posix_path_trust_walks_ancestors() -> None:
    """EXT-02: World-writable ancestor (/tmp/x/chrome with 0700 subdir) must be rejected."""
    import native_host.native_host as nh

    def _fake_stat(p: str):
        norm = str(p).replace("\\", "/")
        if norm.endswith(("/tmp", ":/tmp")) or norm == "/tmp":
            return SimpleNamespace(st_mode=0o1777)
        return SimpleNamespace(st_mode=0o755)

    with patch.object(nh.os, "stat", side_effect=_fake_stat):
        assert nh._posix_executable_path_is_trusted("/tmp/x/chrome") is False
        assert nh._posix_executable_path_is_trusted("/usr/local/bin/chrome") is True


def test_ext_03_install_ps1_checks_generic_and_acl_modify_rights() -> None:
    """EXT-03: Test-DirectoryUserWritable must inspect GenericWrite/GenericAll/ChangePermissions/TakeOwnership."""
    ps1 = (ROOT / "native_host/install_host_windows.ps1").read_text(encoding="utf-8")
    assert "0x500D0046" in ps1
    # Verify exact bitmask covers all expected rights and excludes read/execute (0x200A9)
    mask = 0x500D0046
    for dangerous in (0x2, 0x4, 0x40, 0x10000, 0x40000, 0x80000, 0x10000000, 0x40000000):
        assert (mask & dangerous) == dangerous
    read_and_execute = 0x200A9
    assert (mask & read_and_execute) == 0


def test_ext_04_safety_contracts_normalizes_crlf() -> None:
    """EXT-04: verify_ui_safety_contracts.mjs must normalize CRLF before lineOffset math."""
    mjs = (ROOT / "scripts/verify_ui_safety_contracts.mjs").read_text(encoding="utf-8")
    assert '.replace(/\\r\\n/g, "\\n")' in mjs

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run(
        [node, "scripts/verify_ui_safety_contracts.mjs"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_ext_06_dependabot_sync_workflow_least_privilege_and_timeout() -> None:
    """EXT-06: dependabot-sync.yml must omit pull-requests: write, set timeout-minutes, and verify PR author."""
    yml = (ROOT / ".github/workflows/dependabot-sync.yml").read_text(encoding="utf-8")
    assert "pull-requests: write" not in yml
    assert "contents: write" in yml
    assert "timeout-minutes: 10" in yml
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in yml
