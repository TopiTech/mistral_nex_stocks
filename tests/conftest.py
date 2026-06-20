from tests import reset_app_state_internals

import pytest


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

