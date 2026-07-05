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
def ensure_manifest_exists():
    from pathlib import Path
    import json
    manifest_path = Path(__file__).parent.parent / 'native_host' / 'com.mistral_nex_stocks.host.json'
    created = False
    if not manifest_path.exists():
        template_path = Path(__file__).parent.parent / 'native_host' / 'com.mistral_nex_stocks.host.json.template'
        if template_path.exists():
            try:
                data = json.loads(template_path.read_text(encoding='utf-8'))
                data['allowed_origins'] = ['chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef']
                manifest_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
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
def shutdown_app_state():
    yield
    try:
        from app_state import app_state
        app_state.shutdown_executors()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_app_state():
    reset_app_state_internals()
    yield
    reset_app_state_internals()


# テスト中は yfinance 履歴取得などの非同期処理を同期的に実行してタイミング問題を回避する
from app_state import app_state
from typing import Any
from concurrent.futures import Future
import tempfile
from pathlib import Path
import utils.storage

# Create a temporary directory for test-run files (to avoid corrupting workspace root)
test_temp_dir = tempfile.TemporaryDirectory()

# Patch shutdown token manager files
app_state.shutdown_manager.token_file = Path(test_temp_dir.name) / ".mns_shutdown_token"
app_state.shutdown_manager.used_marker = Path(test_temp_dir.name) / ".mns_shutdown_token.used"

# Patch user stocks file path
utils.storage.USER_STOCKS_FILE = str(Path(test_temp_dir.name) / "user_stocks.json")


class SynchronousExecutor:
    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future
        f: Future[Any] = Future()
        try:
            res = fn(*args, **kwargs)
            f.set_result(res)
        except Exception as e:
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        pass

app_state.execution.executor = SynchronousExecutor()  # type: ignore[assignment]


