import os
import tempfile

# Create a temporary directory for test-run files (to avoid corrupting workspace/user directories)
# We set this environment variable BEFORE any other imports, so that `config_store.py`
# and other modules resolve their paths inside this isolated temporary directory.
test_temp_dir = tempfile.TemporaryDirectory()
os.environ["MNS_DATA_DIR"] = test_temp_dir.name
os.environ["MNS_APP_DATA_DIR"] = test_temp_dir.name

# Prevent `app` import from running its runtime bootstrap (background thread
# startup, news/trends warmup, initial yfinance sync). These perform real
# network I/O and, because conftest replaces the thread-pool executors with
# SynchronousExecutor, the scheduled jobs would execute synchronously on the
# import and block/hang pytest collection. `app.py` documents MNS_SKIP_BOOTSTRAP
# as the test opt-out path; it only skips side effects that tests do not depend
# on (bootstrap_ready is never awaited, and token/user-stock loading is done
# explicitly inside the tests that need it).
os.environ.setdefault("MNS_SKIP_BOOTSTRAP", "1")


import keyring
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 10

    def __init__(self):
        self.passwords = {}

    def set_password(self, servicename, username, password):
        self.passwords[(servicename, username)] = password

    def get_password(self, servicename, username):
        return self.passwords.get((servicename, username), None)

    def delete_password(self, servicename, username):
        self.passwords.pop((servicename, username), None)


keyring.set_keyring(MemoryKeyring())

from tests import reset_app_state_internals  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def cleanup_global_executors():
    """Legacy fixture: _EXECUTOR is now replaced at module level with SynchronousExecutor,
    so no shutdown is needed. Kept for backward compatibility with shutdown_app_state
    dependency ordering.
    """
    yield


@pytest.fixture(scope="session", autouse=True)
def ensure_manifest_exists():
    from pathlib import Path
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
    yield
    try:
        from app_state import app_state

        app_state.shutdown_executors()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_app_state():
    reset_app_state_internals()
    from session_manager import yf_session_manager

    yf_session_manager._reset_for_testing()
    # Reset the config_store legacy merge flag so each test starts with
    # a clean process-lifetime merge state (the flag persists across tests
    # because the config_store module is loaded once per process).
    import config_store

    config_store._reset_legacy_merge_flag()
    yield
    reset_app_state_internals()
    yf_session_manager._reset_for_testing()
    config_store._reset_legacy_merge_flag()


# テスト中は yfinance 履歴取得などの非同期処理を同期的に実行してタイミング問題を回避する
from app_state import app_state
from typing import Any
from concurrent.futures import Future
import tempfile
from pathlib import Path
import utils.storage

# Patch shutdown token manager files
app_state.shutdown_manager.token_file = Path(test_temp_dir.name) / ".mns_shutdown_token"
app_state.shutdown_manager.used_marker = Path(test_temp_dir.name) / ".mns_shutdown_token.used"

# Patch user stocks file path
utils.storage.USER_STOCKS_FILE = str(Path(test_temp_dir.name) / "user_stocks.json")


class SynchronousExecutor:
    def submit(self, fn, *args, **kwargs):
        f: Future[Any] = Future()
        try:
            res = fn(*args, **kwargs)
            f.set_result(res)
        except Exception as e:
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        pass


# Replace all thread pool executors with synchronous mocks to prevent pytest-cov
# from hanging after test completion. The coverage.py atexit handler can deadlock
# when real daemon thread pools are still active during finalization.
app_state.execution.executor = SynchronousExecutor()  # type: ignore[assignment]
app_state.execution.data_executor = SynchronousExecutor()  # type: ignore[assignment]
app_state.execution.news_executor = SynchronousExecutor()  # type: ignore[assignment]
app_state.execution.sync_refresh_executor = SynchronousExecutor()  # type: ignore[assignment]

# Also replace trend_sources._EXECUTOR, which creates a global thread pool with 6 daemon
# workers at module import time (trend_sources.py line 61). If left as a real thread pool,
# tasks submitted to it will spawn real threads that do network I/O and can block coverage
# finalization even after all tests complete.
try:
    import trend_sources as _ts

    _ts._EXECUTOR = SynchronousExecutor()  # type: ignore[assignment]
except (ImportError, AttributeError):
    pass

# Patch background sync operations to be no-ops so real yfinance calls are never
# triggered during tests via SynchronousExecutor.submit(). Route handlers call
# schedule_sync_all_stocks_now() and announce_current_market_state(), which
# would otherwise run synchronously and make real network calls.
import app_bg as _app_bg

_app_bg.schedule_sync_all_stocks_now = lambda: False  # type: ignore[assignment]
_app_bg.announce_current_market_state = lambda: None  # type: ignore[assignment]

# Stub the heavy yfinance batch fetch. Endpoints such as /api/heatmap (and the
# background sync loop) offload this to app_state.execution.executor, which conftest
# forces to be a SynchronousExecutor. Without this stub the fetch would run inline
# on the request/collection thread, perform real yfinance network I/O, and
# block/hang the test run. routes.api_stocks does `from app_bg import
# fetch_stocks_batch`, so patching the name here (before any test imports `app`)
# makes the route bind the stub at import time. Tests that need real behavior
# patch this symbol locally and are unaffected.
_app_bg.fetch_stocks_batch = lambda items, snapshot_ts_ms=None, **kwargs: []  # type: ignore[assignment]
