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

