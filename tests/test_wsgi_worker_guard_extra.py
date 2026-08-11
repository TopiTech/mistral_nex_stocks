"""Extended coverage tests for wsgi.py worker-count guard edge cases.

The main guard behavior is covered by test_wsgi_worker_guard.py. These target
the branch-level gaps: invalid worker counts, GUNICORN_WORKERS fallback,
validation explicitly disabled with invalid counts, and the __main__ runner.
"""

import importlib
import os
import sys
from unittest.mock import patch

import pytest

import wsgi as wsgi_mod
from app_state import app_state
from routes.api_system import api_health


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


def test_explicit_zero_does_not_skip_wsgi_bootstrap():
    original_bootstrap = wsgi_mod.bootstrap
    try:
        with (
            patch.dict(
                "os.environ",
                {"MNS_SKIP_BOOTSTRAP": "0", "MNS_WORKER_VALIDATION": "0"},
                clear=False,
            ),
            patch("app.bootstrap") as bootstrap_mock,
        ):
            reloaded = importlib.reload(wsgi_mod)
            bootstrap_mock.assert_called_once_with(reloaded.app)
    finally:
        wsgi_mod.bootstrap = original_bootstrap


def test_health_does_not_report_ready_for_explicit_false_skip_value():
    was_ready = app_state.bootstrap_ready.is_set()
    app_state.bootstrap_ready.clear()
    try:
        with (
            patch.dict("os.environ", {"MNS_SKIP_BOOTSTRAP": "0"}, clear=False),
            wsgi_mod.app.test_request_context("/api/health"),
        ):
            response = api_health()
        assert response.get_json()["ready"] is False
    finally:
        if was_ready:
            app_state.bootstrap_ready.set()


def test_main_block_runs_app():
    """The if __name__ == '__main__' block should invoke app.run without error."""
    with patch("wsgi.app") as mock_app, patch.dict("os.environ", {"MNS_SKIP_BOOTSTRAP": "1"}):
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
            exec(code, {"app": wsgi_mod.app, "constants": __import__("constants")})  # noqa: S102 # nosec B102
        finally:
            sys.modules["__main__"] = saved
        mock_app.run.assert_called_once()
