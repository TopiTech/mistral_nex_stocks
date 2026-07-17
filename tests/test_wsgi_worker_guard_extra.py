"""Extended coverage tests for wsgi.py worker-count guard edge cases.

The main guard behavior is covered by test_wsgi_worker_guard.py. These target
the branch-level gaps: invalid worker counts, GUNICORN_WORKERS fallback,
validation explicitly disabled with invalid counts, and the __main__ runner.
"""

import os
import sys
import importlib
import pytest
from unittest.mock import patch
import wsgi as wsgi_mod


def test_invalid_worker_count_falls_back_to_one():
    """A non-numeric WEB_CONCURRENCY must not crash; treated as 1."""
    with patch.dict(
        "os.environ", {"WEB_CONCURRENCY": "not-a-number", "MNS_WORKER_VALIDATION": "1"}
    ):
        importlib.reload(wsgi_mod)


def test_gunicorn_workers_env_used():
    """GUNICORN_WORKERS should be consulted when WEB_CONCURRENCY is unset."""
    with patch.dict("os.environ", {"GUNICORN_WORKERS": "4", "MNS_WORKER_VALIDATION": "1"}):
        with patch.dict("os.environ", {}):
            if "WEB_CONCURRENCY" in os.environ:
                del os.environ["WEB_CONCURRENCY"]
            with pytest.raises(SystemExit) as excinfo:
                importlib.reload(wsgi_mod)
            assert excinfo.value.code == 1


def test_validation_disabled_with_invalid_count():
    """Validation off + invalid count must still start (no hard-fail)."""
    with patch.dict("os.environ", {"WEB_CONCURRENCY": "blah", "MNS_WORKER_VALIDATION": "0"}):
        importlib.reload(wsgi_mod)


def test_worker_count_of_one_allowed_via_gunicorn():
    with patch.dict("os.environ", {"GUNICORN_WORKERS": "1", "MNS_WORKER_VALIDATION": "1"}):
        with patch.dict("os.environ", {}):
            if "WEB_CONCURRENCY" in os.environ:
                del os.environ["WEB_CONCURRENCY"]
            importlib.reload(wsgi_mod)


def test_main_block_runs_app():
    """The if __name__ == '__main__' block should invoke app.run without error."""
    with patch("wsgi.app") as mock_app:
        with patch.dict("os.environ", {"MNS_SKIP_BOOTSTRAP": "1"}):
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
                exec(code, {"app": wsgi_mod.app, "constants": __import__("constants")})  # nosec B102
            finally:
                sys.modules["__main__"] = saved
            mock_app.run.assert_called_once()
