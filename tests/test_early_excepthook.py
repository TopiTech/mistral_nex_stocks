"""Unit tests for early startup excepthook behavior in app.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import _early_excepthook


def test_early_excepthook_bypasses_keyboard_interrupt(tmp_path: Path):
    with patch.dict("os.environ", {"MNS_DATA_DIR": str(tmp_path)}):
        mock_orig_hook = MagicMock()
        with patch.object(sys, "__excepthook__", mock_orig_hook):
            try:
                raise KeyboardInterrupt("Interrupted by user")
            except KeyboardInterrupt:
                exc_type, exc_value, exc_tb = sys.exc_info()
                _early_excepthook(exc_type, exc_value, exc_tb)

        mock_orig_hook.assert_called_once_with(exc_type, exc_value, exc_tb)
        # Verify no log files were written
        assert not (tmp_path / "backend.log").exists()
        assert not (tmp_path / "error.log").exists()


def test_early_excepthook_bypasses_system_exit(tmp_path: Path):
    with patch.dict("os.environ", {"MNS_DATA_DIR": str(tmp_path)}):
        mock_orig_hook = MagicMock()
        with patch.object(sys, "__excepthook__", mock_orig_hook):
            try:
                sys.exit(0)
            except SystemExit:
                exc_type, exc_value, exc_tb = sys.exc_info()
                _early_excepthook(exc_type, exc_value, exc_tb)

        mock_orig_hook.assert_called_once_with(exc_type, exc_value, exc_tb)
        # Verify no log files were written
        assert not (tmp_path / "backend.log").exists()
        assert not (tmp_path / "error.log").exists()


def test_early_excepthook_logs_startup_exception(tmp_path: Path):
    with patch.dict("os.environ", {"MNS_DATA_DIR": str(tmp_path)}):
        mock_orig_hook = MagicMock()
        with patch.object(sys, "__excepthook__", mock_orig_hook):
            try:
                raise RuntimeError("Critical dependency initialization failure")
            except RuntimeError:
                exc_type, exc_value, exc_tb = sys.exc_info()
                _early_excepthook(exc_type, exc_value, exc_tb)

        mock_orig_hook.assert_called_once_with(exc_type, exc_value, exc_tb)
        backend_log = tmp_path / "backend.log"
        error_log = tmp_path / "error.log"
        assert backend_log.exists()
        assert error_log.exists()
        backend_content = backend_log.read_text(encoding="utf-8")
        assert "CRITICAL startup exception:" in backend_content
        assert "Critical dependency initialization failure" in backend_content


def test_early_excepthook_handles_non_type_gracefully():
    mock_orig_hook = MagicMock()
    with patch.object(sys, "__excepthook__", mock_orig_hook):
        # Must not raise TypeError: issubclass() arg 1 must be a class
        _early_excepthook(None, None, None)
    mock_orig_hook.assert_called_once_with(None, None, None)


def test_early_excepthook_resolves_posix_data_dir(tmp_path: Path):
    with (
        patch.dict(
            "os.environ",
            {"MNS_DATA_DIR": "", "MNS_APP_DATA_DIR": "", "XDG_DATA_HOME": str(tmp_path)},
            clear=False,
        ),
        patch("app._is_windows_runtime", return_value=False),
    ):
        mock_orig_hook = MagicMock()
        with patch.object(sys, "__excepthook__", mock_orig_hook):
            try:
                raise ValueError("Posix startup error")
            except ValueError:
                exc_type, exc_value, exc_tb = sys.exc_info()
                _early_excepthook(exc_type, exc_value, exc_tb)

        posix_dir = tmp_path / "mistral_nex_stocks"
        assert (posix_dir / "backend.log").exists()
        assert (posix_dir / "error.log").exists()
        content = (posix_dir / "backend.log").read_text(encoding="utf-8")
        assert "Posix startup error" in content
