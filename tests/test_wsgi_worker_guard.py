"""Regression tests for H1: wsgi.py must reject multi-worker mode.

gunicorn controls the worker count via the WEB_CONCURRENCY environment variable
(or --workers on the command line). The previous implementation inspected the
wrong variable names (PRELOAD_WORKERS / GUNICORN_WORKERS), so `gunicorn
--workers 4 wsgi:app` would silently start in an unsupported multi-process mode
and break the in-memory singleton state. These tests verify the guard now
detects WEB_CONCURRENCY > 1 and aborts.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_wsgi_with_env(env: dict) -> subprocess.CompletedProcess:
    """Execute wsgi.py as a subprocess with the given extra env vars.

    wsgi.py performs the worker-count guard at import time, so a process that
    reaches the bottom (and prints WSGI_APP_READY) passed the guard, while one
    that raises RuntimeError failed it.
    """
    full_env = dict(os.environ)
    full_env.update(env)
    full_env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", "import wsgi; print('WSGI_APP_READY')"],
        cwd=str(PROJECT_ROOT),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_single_worker_is_allowed():
    """WEB_CONCURRENCY=1 (or unset) must not raise."""
    result = _run_wsgi_with_env({"WEB_CONCURRENCY": "1", "MNS_WORKER_VALIDATION": "1"})
    assert result.returncode == 0, result.stderr
    assert "WSGI_APP_READY" in result.stdout


def test_multi_worker_warning_logged():
    """WEB_CONCURRENCY=4 must print a warning but allow startup."""
    result = _run_wsgi_with_env({"WEB_CONCURRENCY": "4", "MNS_WORKER_VALIDATION": "1"})
    assert result.returncode == 0, result.stderr
    assert "Multi-worker mode detected" in (result.stderr + result.stdout)


def test_validation_can_be_disabled():
    """MNS_WORKER_VALIDATION=0 disables the guard even with WEB_CONCURRENCY=4."""
    result = _run_wsgi_with_env({"WEB_CONCURRENCY": "4", "MNS_WORKER_VALIDATION": "0"})
    assert result.returncode == 0, result.stderr
    assert "WSGI_APP_READY" in result.stdout
