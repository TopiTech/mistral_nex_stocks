"""Behavioral regressions for fixes implemented from the repository review."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import credential_manager
from routes import api_system


@pytest.mark.parametrize(
    ("factory", "entry_name"),
    [
        (credential_manager.get_or_create_flask_secret_key, "flask_secret_key"),
        (credential_manager.get_or_create_extension_api_token, "extension_api_token"),
    ],
)
def test_credential_initialization_never_acquires_master_key_inside_config_lock(
    factory, entry_name
):
    """Keep POSIX config/master lock acquisition ordered and non-nested."""

    lock_depth = 0
    saved: dict[str, object] = {}

    @contextmanager
    def config_lock():
        nonlocal lock_depth
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    def get_master_key() -> str:
        assert lock_depth == 0, "master key lock must be acquired before config lock"
        return "test-master-key"

    with (
        patch.object(credential_manager.config_store, "config_update_lock", config_lock),
        patch.object(credential_manager.config_store, "get_or_create_master_key", get_master_key),
        patch.object(credential_manager.config_store, "load_config", return_value={}),
        patch.object(credential_manager.config_store, "save_config", side_effect=lambda cfg: saved.update(cfg)),
        patch.object(credential_manager.crypto_utils, "protect_data", return_value={"cipher": "test"}),
        patch("utils.env_helpers._is_production_env", return_value=False),
    ):
        value = factory()

    assert len(value) >= 32
    assert entry_name in saved


def test_windows_shutdown_terminates_the_process_not_only_the_worker_thread():
    class ProcessExit(Exception):
        pass

    with (
        patch.object(api_system.os, "name", "nt"),
        patch.object(api_system.os, "_exit", side_effect=ProcessExit) as exit_mock,
        pytest.raises(ProcessExit),
    ):
        api_system._terminate_current_process(logging.getLogger(__name__))

    exit_mock.assert_called_once_with(0)


def test_security_docs_explain_ephemeral_master_key_data_loss_boundary():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    security = (root / "SECURITY.md").read_text(encoding="utf-8")

    assert "MNS_ALLOW_EPHEMERAL_MASTER_KEY" in readme
    assert "MNS_EPHEMERAL_FALLBACK" in readme
    assert "MNS_MASTER_KEY" in security
    assert "cannot be recovered after restart" in security


def test_documented_unix_launch_path_uses_the_locked_project_environment():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    launcher = (root / "run_app.sh").read_text(encoding="utf-8")

    assert "uv run --locked python app.py" in readme
    assert 'exec ".venv/bin/python" app.py' in launcher
    assert "exec uv run --locked python app.py" in launcher
    assert "python3 app.py" not in launcher
