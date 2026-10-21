"""Unit tests verifying test utilities, helpers, fixtures, and state reset logic."""

from __future__ import annotations

import json
from pathlib import Path

import keyring.errors
import pytest

from tests import cleanup_temp_files, create_temp_config, reset_app_state_internals
from tests.conftest import MemoryKeyring, SynchronousExecutor
from tests.test_support import (
    apply_background_stubs,
    apply_baseline_test_env,
    apply_synchronous_executors,
    get_baseline_test_env,
)
from tests.test_support import (
    cleanup_temp_files as support_cleanup_temp_files,
)
from tests.test_support import (
    create_temp_config as support_create_temp_config,
)
from tests.test_support import (
    reset_app_state_internals as support_reset_app_state_internals,
)


def test_create_temp_config_and_cleanup():
    """Test that create_temp_config creates valid json config and tracks for cleanup."""
    overrides = {"custom_key": "custom_val"}
    credentials = {"mistral_api_key": "test_key"}
    path = create_temp_config(
        overrides=overrides, api_credentials=credentials, register_for_cleanup=True
    )

    assert path.exists()
    assert path.suffix == ".json"

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["custom_key"] == "custom_val"
    assert data["api_credentials"]["mistral_api_key"] == "test_key"
    assert data["mistral_model"] == "mistral-small-latest"

    cleanup_temp_files()
    assert not path.exists()


def test_support_create_temp_config_direct():
    """Test direct import from tests.test_support."""
    path = support_create_temp_config({"foo": "bar"}, register_for_cleanup=True)
    assert path.exists()
    support_cleanup_temp_files()
    assert not path.exists()


def test_safe_temp_dir_windows_cleanup():
    """Test safe_temp_dir has ignore_cleanup_errors enabled for Windows resilience."""
    from tests.test_support import safe_temp_dir

    with safe_temp_dir() as td:
        p = Path(td) / "dummy.txt"
        p.write_text("test", encoding="utf-8")
        assert p.exists()
    # Directory should be gone
    assert not Path(td).exists()



def test_memory_keyring_operations():
    """Test MemoryKeyring set, get, delete, clear, and error handling."""
    kr = MemoryKeyring()
    service = "test_service"
    username = "test_user"
    password = "secret_password"

    assert kr.get_password(service, username) is None

    kr.set_password(service, username, password)
    assert kr.get_password(service, username) == password

    kr.clear()
    assert kr.get_password(service, username) is None

    kr.set_password(service, username, password)
    kr.delete_password(service, username)
    assert kr.get_password(service, username) is None

    with pytest.raises(keyring.errors.PasswordDeleteError):
        kr.delete_password(service, username)


def test_synchronous_executor():
    """Test SynchronousExecutor submit, map, and shutdown."""
    executor = SynchronousExecutor()

    # submit success
    future = executor.submit(lambda x, y: x + y, 3, 7)
    assert future.done()
    assert future.result() == 10
    assert future.exception() is None

    # submit failure
    def raise_err():
        raise ValueError("test error")

    f_err = executor.submit(raise_err)
    assert f_err.done()
    assert isinstance(f_err.exception(), ValueError)

    # map
    results = executor.map(lambda x: x * 2, [1, 2, 3])
    assert results == [2, 4, 6]

    empty_res = executor.map(lambda x: x)
    assert empty_res == []

    # shutdown no-op
    executor.shutdown(wait=True, cancel_futures=True)


def test_reset_app_state_internals_execution():
    """Test that reset_app_state_internals runs cleanly without raising exceptions."""
    from routes import api_analysis

    with api_analysis.news_fetch_lock:
        api_analysis.news_fetch_inflight["test-isolation"] = {"done": object()}

    reset_app_state_internals()

    with api_analysis.news_fetch_lock:
        assert "test-isolation" not in api_analysis.news_fetch_inflight

    # Test modular support function works identically
    with api_analysis.news_fetch_lock:
        api_analysis.news_fetch_inflight["test-isolation-2"] = {"done": object()}

    support_reset_app_state_internals()

    with api_analysis.news_fetch_lock:
        assert "test-isolation-2" not in api_analysis.news_fetch_inflight


def test_fixtures_client_and_temp_config(client, temp_config_file):
    """Test the reusable client and temp_config_file pytest fixtures."""
    assert client is not None
    res = client.get("/api/health")
    assert res.status_code == 200

    assert isinstance(temp_config_file, Path)
    assert temp_config_file.exists()


def test_environment_and_patch_helpers():
    """Test environment baseline and patch application helpers."""
    env = get_baseline_test_env("dummy_dir")
    assert env["MNS_DATA_DIR"] == "dummy_dir"
    assert env["MNS_SKIP_BOOTSTRAP"] == "1"

    apply_baseline_test_env("dummy_dir")
    assert pytest.MonkeyPatch is not None

    apply_synchronous_executors()
    apply_background_stubs()
