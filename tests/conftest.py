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

