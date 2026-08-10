"""Regression tests for H1: wsgi.py must reject multi-worker mode.

gunicorn controls the worker count via the WEB_CONCURRENCY environment variable.
These tests verify that wsgi.py detects WEB_CONCURRENCY > 1 and aborts.
"""

import importlib
from unittest.mock import patch

import pytest

import wsgi as wsgi_mod


def test_single_worker_is_allowed():
    """WEB_CONCURRENCY=1 (or unset) must not raise."""
    with patch.dict("os.environ", {"WEB_CONCURRENCY": "1", "MNS_WORKER_VALIDATION": "1"}):
        importlib.reload(wsgi_mod)


def test_multi_worker_is_rejected():
    """WEB_CONCURRENCY=4 must abort startup (single-worker only)."""
    with patch.dict("os.environ", {"WEB_CONCURRENCY": "4", "MNS_WORKER_VALIDATION": "1"}):
        with pytest.raises(SystemExit) as excinfo:
            importlib.reload(wsgi_mod)
        assert excinfo.value.code == 1


def test_validation_can_be_disabled():
    """MNS_WORKER_VALIDATION=0 disables the guard even with WEB_CONCURRENCY=4."""
    with patch.dict("os.environ", {"WEB_CONCURRENCY": "4", "MNS_WORKER_VALIDATION": "0"}):
        importlib.reload(wsgi_mod)


def test_multi_worker_cli_arg_is_rejected():
    """`gunicorn --workers 4 wsgi:app` (no env vars set) must abort startup.

    Gunicorn does not export WEB_CONCURRENCY/GUNICORN_WORKERS itself, so the
    guard has to inspect the raw CLI arguments (R14).
    """
    import sys

    with (
        patch.dict(
            "os.environ",
            {
                "MNS_WORKER_VALIDATION": "1",
                "WEB_CONCURRENCY": "",
                "GUNICORN_WORKERS": "",
                "GUNICORN_CMD_ARGS": "",
            },
        ),
        patch.object(sys, "argv", ["gunicorn", "--workers", "4", "wsgi:app"]),
    ):
        with pytest.raises(SystemExit) as excinfo:
            importlib.reload(wsgi_mod)
        assert excinfo.value.code == 1


def test_multi_worker_short_cli_arg_is_rejected():
    """The short form `-w 4` is detected as well."""
    import sys

    with (
        patch.dict(
            "os.environ",
            {
                "MNS_WORKER_VALIDATION": "1",
                "WEB_CONCURRENCY": "",
                "GUNICORN_WORKERS": "",
                "GUNICORN_CMD_ARGS": "",
            },
        ),
        patch.object(sys, "argv", ["gunicorn", "-w", "4", "wsgi:app"]),
    ):
        with pytest.raises(SystemExit) as excinfo:
            importlib.reload(wsgi_mod)
        assert excinfo.value.code == 1


def test_multi_worker_gunicorn_cmd_args_is_rejected():
    """GUNICORN_CMD_ARGS (systemd/PaaS convention) is detected as well."""
    with patch.dict(
        "os.environ",
        {
            "MNS_WORKER_VALIDATION": "1",
            "WEB_CONCURRENCY": "",
            "GUNICORN_WORKERS": "",
            "GUNICORN_CMD_ARGS": "--workers 4",
        },
    ):
        with pytest.raises(SystemExit) as excinfo:
            importlib.reload(wsgi_mod)
        assert excinfo.value.code == 1


def test_single_worker_cli_is_allowed():
    """`--workers 1` on the CLI is allowed (no env vars set)."""
    import sys

    with (
        patch.dict(
            "os.environ",
            {
                "MNS_WORKER_VALIDATION": "1",
                "WEB_CONCURRENCY": "",
                "GUNICORN_WORKERS": "",
                "GUNICORN_CMD_ARGS": "",
            },
        ),
        patch.object(sys, "argv", ["gunicorn", "--workers", "1", "wsgi:app"]),
    ):
        importlib.reload(wsgi_mod)
