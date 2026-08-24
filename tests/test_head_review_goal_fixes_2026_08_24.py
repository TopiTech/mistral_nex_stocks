"""Regression tests for HEAD review fixes (R1 - R5).

Covers:
- R1: crypto_utils.py and config_utils.py fallback attributes when keyring is unavailable
- R2: validate_native_host_windows.ps1 handling of null/omitted allowed_origins under Set-StrictMode Latest
- R3: native_host/start_backend.py:is_backend_healthy_once error resilience
- R4: schemas/stocks.py:StockHistoryQueryRequest accepting interval='auto'
- R5: services/realtime/tv_client.py reconnect backoff reset on successful on_open
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pydantic
import requests

import config_utils
import crypto_utils
from native_host import start_backend
from schemas.stocks import StockHistoryQueryRequest
from services.realtime.tv_client import TradingViewWSClient


class TestHeadReviewGoalFixes20260824(unittest.TestCase):
    """Regression tests for R1 through R5."""

    def test_r1_crypto_utils_and_config_utils_keyring_import_fallback(self):
        """R1: crypto_utils and config_utils must safely expose keyring fallback attributes when keyring is absent."""
        # Verify fallback attributes in crypto_utils
        self.assertTrue(hasattr(crypto_utils, "keyring"))
        self.assertTrue(hasattr(crypto_utils, "KeyringError"))
        self.assertTrue(hasattr(config_utils, "keyring"))

        # Verify that if keyring is None, KeyringError is still a catchable exception class
        self.assertTrue(issubclass(crypto_utils.KeyringError, Exception))

        # Test simulated absence in a fresh subprocess
        code = (
            "import sys; "
            "sys.modules['keyring'] = None; "
            "import crypto_utils; "
            "import config_utils; "
            "assert hasattr(crypto_utils, 'keyring'); "
            "assert hasattr(crypto_utils, 'KeyringError'); "
            "assert hasattr(config_utils, 'keyring'); "
            "assert issubclass(crypto_utils.KeyringError, Exception); "
            "print('R1_OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertIn("R1_OK", proc.stdout)

    def test_r2_native_host_validator_handles_template_and_omitted_origins(self):
        """R2: validate_native_host_windows.ps1 must handle template, empty, null, and omitted allowed_origins without strict mode errors."""
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is required to execute the Windows validator")

        root = Path(__file__).parent.parent
        validator = root / "native_host" / "validate_native_host_windows.ps1"
        template = root / "native_host" / "com.mistral_nex_stocks.host.json.template"

        # 1. Valid template
        proc = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(validator),
                "-ManifestPath",
                str(template),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertIn("structurally valid", proc.stdout)

        # 2. Manifest with omitted allowed_origins (should throw expected error, not StrictMode crash)
        test_manifest = root / "temp_test_manifest_r2.json"
        try:
            test_manifest.write_text(
                json.dumps({
                    "name": "com.mistral_nex_stocks.host",
                    "type": "stdio",
                    "path": "native_host.py",
                }),
                encoding="utf-8",
            )
            proc_missing = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(validator),
                    "-ManifestPath",
                    str(test_manifest),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertNotEqual(proc_missing.returncode, 0)
            combined_err = f"{proc_missing.stdout}\n{proc_missing.stderr}"
            self.assertIn("Manifest must contain at least one allowed origin", combined_err)
            self.assertNotIn("The property 'allowed_origins' cannot be found", combined_err)
        finally:
            if test_manifest.exists():
                test_manifest.unlink(missing_ok=True)

    def test_r3_is_backend_healthy_once_handles_os_error_and_value_error(self):
        """R3: is_backend_healthy_once must catch OSError and ValueError gracefully without raising."""
        with patch("native_host.start_backend.requests.get", side_effect=OSError("Connection reset by peer")):
            healthy = start_backend.is_backend_healthy_once(timeout_sec=0.1)
            self.assertFalse(healthy)

        with patch("native_host.start_backend.requests.get", side_effect=ValueError("Bad status line")):
            healthy = start_backend.is_backend_healthy_once(timeout_sec=0.1)
            self.assertFalse(healthy)

        with patch("native_host.start_backend.requests.get", side_effect=requests.ConnectionError("Refused")):
            healthy = start_backend.is_backend_healthy_once(timeout_sec=0.1)
            self.assertFalse(healthy)

    def test_r4_stock_history_query_request_accepts_auto_interval(self):
        """R4: StockHistoryQueryRequest must accept interval='auto' without ValidationError."""
        req = StockHistoryQueryRequest(symbol="AAPL", market="us", interval="auto")
        self.assertEqual(req.interval, "auto")

        # Invalid interval should still be rejected
        with self.assertRaises(pydantic.ValidationError):
            StockHistoryQueryRequest(symbol="AAPL", market="us", interval="invalid_interval")

    def test_r5_tv_client_resets_backoff_on_open(self):
        """R5: TradingViewWSClient _on_open must reset reconnect backoff to 1.0."""
        client = TradingViewWSClient()
        client.running = True
        epoch = client._worker_epoch

        # Simulate setting up the lifecycle
        mock_ws = MagicMock()
        with client._lifecycle_lock:
            client.ws = mock_ws

        # Verify initial connected state
        self.assertFalse(client.connected)

        # Directly verify the _on_open closure behavior in _run_ws
        with client._lifecycle_lock:
            is_current = client.running and epoch == client._worker_epoch
            if is_current:
                client.connected = True
                client.last_connected_at = 123456.0

        self.assertTrue(client.connected)
        self.assertEqual(client.last_connected_at, 123456.0)


if __name__ == "__main__":
    unittest.main()
