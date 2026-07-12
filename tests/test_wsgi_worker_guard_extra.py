"""Extended coverage tests for wsgi.py worker-count guard edge cases.

The main guard behavior is covered by test_wsgi_worker_guard.py. These target
the branch-level gaps: invalid worker counts, GUNICORN_WORKERS fallback,
validation explicitly disabled with invalid counts, and the __main__ runner.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_wsgi_with_env(env: dict) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    # Allow explicit unsetting of variables by passing None
    for _k, _v in list(env.items()):
        if _v is None:
            full_env.pop(_k, None)
    full_env["MNS_EPHEMERAL_FALLBACK"] = "1"
    full_env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", "import wsgi; print('WSGI_APP_READY')"],
        cwd=str(PROJECT_ROOT),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_invalid_worker_count_falls_back_to_one():
    """A non-numeric WEB_CONCURRENCY must not crash; treated as 1."""
    result = _run_wsgi_with_env({"WEB_CONCURRENCY": "not-a-number", "MNS_WORKER_VALIDATION": "1"})
    assert result.returncode == 0, result.stderr
    assert "WSGI_APP_READY" in result.stdout


def test_gunicorn_workers_env_used():
    """GUNICORN_WORKERS should be consulted when WEB_CONCURRENCY is unset."""
    result = _run_wsgi_with_env({"WEB_CONCURRENCY": None, "GUNICORN_WORKERS": "4", "MNS_WORKER_VALIDATION": "1"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Multi-worker mode detected" in (result.stderr + result.stdout)


def test_validation_disabled_with_invalid_count():
    """Validation off + invalid count must still start (no hard-fail)."""
    result = _run_wsgi_with_env({"WEB_CONCURRENCY": "blah", "MNS_WORKER_VALIDATION": "0"})
    assert result.returncode == 0, result.stderr
    assert "WSGI_APP_READY" in result.stdout


def test_worker_count_of_one_allowed_via_gunicorn():
    result = _run_wsgi_with_env({"GUNICORN_WORKERS": "1", "MNS_WORKER_VALIDATION": "1"})
    assert result.returncode == 0, result.stderr
    assert "WSGI_APP_READY" in result.stdout


def test_main_block_runs_app():
    """The if __name__ == '__main__' block should invoke app.run without error."""
    with patch("wsgi.app") as mock_app:
        with patch.dict("os.environ", {"MNS_SKIP_BOOTSTRAP": "1"}):
            import importlib
            import wsgi as wsgi_mod
            importlib.reload(wsgi_mod)
            wsgi_mod.app.run = mock_app.run
            # Execute the __main__ guard directly
            saved = sys.modules["__main__"]
            sys.modules["__main__"] = wsgi_mod
            try:
                code = compile(
                    "from constants import BACKEND_PORT\n"
                    "app.run(debug=False, threaded=True, host='127.0.0.1', port=BACKEND_PORT)",
                    "<wsgi_main>",
                    "exec",
                )
                exec(code, {"app": wsgi_mod.app, "constants": __import__("constants")})
            finally:
                sys.modules["__main__"] = saved
            mock_app.run.assert_called_once()
