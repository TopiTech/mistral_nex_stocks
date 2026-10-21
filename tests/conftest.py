"""Pytest configuration and test isolation fixtures for Mistral Nex Stocks."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Create a temporary directory for test-run files (to avoid corrupting workspace/user directories)
# Set environment variables BEFORE any other imports, so config_store and other modules
# resolve their paths inside this isolated temporary directory.
test_temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

from tests.test_support import (
    MemoryKeyring,
    SynchronousExecutor,
    apply_all_test_patches,
    apply_baseline_test_env,
    get_baseline_test_env,
    reset_app_state_internals,
)

__all__ = [
    "MemoryKeyring",
    "SynchronousExecutor",
]

apply_baseline_test_env(test_temp_dir.name)
sys.modules["secretstorage"] = None  # type: ignore[assignment]

_BASELINE_TEST_ENV: dict[str, str] = get_baseline_test_env(test_temp_dir.name)

import keyring
import keyring.core

_mem_keyring = MemoryKeyring()
keyring.core._keyring = _mem_keyring  # type: ignore[attr-defined]
keyring.set_keyring(_mem_keyring)

import pytest


def pytest_configure(config):
    """Print diagnostic environment & collection startup info."""
    import platform

    print(
        f"\n[MNS TEST DIAGNOSTIC] Python {sys.version} on {platform.system()} ({platform.platform()})",
        flush=True,
    )
    print(
        f"[MNS TEST DIAGNOSTIC] MNS_SKIP_BOOTSTRAP={os.environ.get('MNS_SKIP_BOOTSTRAP')}",
        flush=True,
    )
    print(
        f"[MNS TEST DIAGNOSTIC] KEYRING_BACKEND={os.environ.get('PYTHON_KEYRING_BACKEND')}",
        flush=True,
    )


_collected_files: set[str] = set()


def pytest_collectstart(collector):
    """Log test file collection progress in real-time if verbose output is requested."""
    if (
        hasattr(collector, "config")
        and getattr(collector.config.option, "verbose", 0) > 0
        and hasattr(collector, "fspath")
    ):
        path_str = str(collector.fspath)
        if path_str.endswith(".py") and path_str not in _collected_files:
            _collected_files.add(path_str)
            print(f"[MNS COLLECTING START] {path_str}", flush=True)


def pytest_collectreport(report):
    """Log completion of test file collection for CI diagnostics in verbose mode."""
    if (
        hasattr(report, "nodeid")
        and report.nodeid.endswith(".py")
        and getattr(report, "config", None)
        and getattr(report.config.option, "verbose", 0) > 0
    ):
        print(
            f"[MNS COLLECTED OK] {report.nodeid} (items={len(getattr(report, 'result', []))})",
            flush=True,
        )


@pytest.fixture(scope="session", autouse=True)
def cleanup_global_executors():
    """Legacy fixture: kept for backward compatibility with shutdown_app_state dependency ordering."""
    yield


@pytest.fixture(scope="session", autouse=True)
def ensure_manifest_exists():
    """Ensure native host test manifest exists during test session."""
    import json

    manifest_path = (
        Path(__file__).parent.parent / "native_host" / "com.mistral_nex_stocks.host.json"
    )
    created = False
    if not manifest_path.exists():
        template_path = (
            Path(__file__).parent.parent
            / "native_host"
            / "com.mistral_nex_stocks.host.json.template"
        )
        if template_path.exists():
            try:
                data = json.loads(template_path.read_text(encoding="utf-8"))
                data["allowed_origins"] = ["chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef"]
                manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                created = True
            except Exception:
                pass
    yield
    if created and manifest_path.exists():
        try:
            manifest_path.unlink()
        except Exception:
            pass


@pytest.fixture(scope="session", autouse=True)
def shutdown_app_state(cleanup_global_executors):
    """Cleanly shut down app_state executors on session finish."""
    yield
    try:
        from app_state import app_state

        app_state.shutdown_executors()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_temp_directory():
    """Ensure test_temp_dir is cleaned up on session completion."""
    yield
    try:
        test_temp_dir.cleanup()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def isolate_and_restore_environment():
    """Snapshot and restore os.environ for every test to prevent cross-test pollution."""
    for base_key, base_val in _BASELINE_TEST_ENV.items():
        if os.environ.get(base_key) != base_val:
            os.environ[base_key] = base_val

    old_environ = dict(os.environ)
    try:
        yield
    finally:
        current_keys = list(os.environ.keys())
        for key in current_keys:
            if key not in old_environ:
                del os.environ[key]
        for key, value in old_environ.items():
            if os.environ.get(key) != value:
                os.environ[key] = value
        for base_key, base_val in _BASELINE_TEST_ENV.items():
            if os.environ.get(base_key) != base_val:
                os.environ[base_key] = base_val


@pytest.fixture(autouse=True)
def reset_app_state():
    """Per-test fixture resetting global application state and persistence boundaries."""
    import config_store
    from session_manager import yf_session_manager

    def _clear_persisted_state():
        reset_app_state_internals()
        _mem_keyring.passwords.clear()
        yf_session_manager._reset_for_testing()
        config_store._reset_legacy_merge_flag()
        for _f in (
            Path(test_temp_dir.name) / "ai_portfolios.json",
            Path(test_temp_dir.name) / "config.json",
            Path(test_temp_dir.name) / "user_stocks.json",
        ):
            try:
                _f.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            from utils.chat_history import _reset_db_state

            _reset_db_state()
        except Exception:
            pass

    _clear_persisted_state()
    yield
    _clear_persisted_state()
    try:
        import app_bg as _bg

        _bg.schedule_sync_all_stocks_now = lambda *a, **kw: False  # type: ignore[assignment]
        _bg.announce_current_market_state = lambda: None  # type: ignore[assignment]
        _bg.fetch_stocks_batch = lambda items, snapshot_ts_ms=None, **kwargs: []  # type: ignore[assignment]
        _bg.fetch_index_data = lambda *a, **kw: None  # type: ignore[assignment]
    except Exception:
        pass


@pytest.fixture
def client():
    """Reusable Flask test client fixture with CSRF disabled for convenience."""
    from app import app

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client
    if orig_csrf is not None:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


@pytest.fixture
def temp_config_file():
    """Reusable temporary configuration file fixture with auto-cleanup."""
    from tests import create_temp_config

    path = create_temp_config()
    yield path
    try:
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


# Apply module-level test monkeypatches (caches, executors, stubs)
apply_all_test_patches(Path(test_temp_dir.name))
