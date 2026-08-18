"""Focused regression tests for review findings R1 and R5 only."""

from contextlib import ExitStack, nullcontext
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import credential_manager
import crypto_utils


KEY_NAMES = (
    "mistral_api_key",
    "langsearch_api_key",
    "tavily_api_key",
    "alphavantage_api_key",
)


def _patched_clear(config, keyring, *, keyring_available=True, save_config=None):
    """Patch credential storage dependencies without touching process state."""
    stack = ExitStack()
    stack.enter_context(
        patch.object(credential_manager.config_store, "config_update_lock", return_value=nullcontext())
    )
    stack.enter_context(
        patch.object(credential_manager.config_store, "load_config", return_value=deepcopy(config))
    )
    stack.enter_context(patch.object(credential_manager, "_keyring_available", return_value=keyring_available))
    stack.enter_context(patch.object(credential_manager, "_keyring", return_value=keyring))
    stack.enter_context(
        patch.object(credential_manager.config_store, "save_config", side_effect=save_config)
    )
    return stack


def test_r1_clear_succeeds_for_missing_keyring_entries_and_returns_empty():
    config = {"mistral_model": "test", "api_credentials": {"mistral_api_key": {"scheme": "keyring"}}}
    keyring = MagicMock()
    keyring.get_password.return_value = None
    from keyring.errors import PasswordDeleteError

    keyring.delete_password.side_effect = PasswordDeleteError("not found")
    with patch.object(crypto_utils, "_EPHEMERAL_CREDENTIALS", {}), patch.object(
        crypto_utils, "_EPHEMERAL_KEY", None
    ):
        with _patched_clear(config, keyring):
            assert credential_manager.clear_api_credentials() == []

    keyring.delete_password.assert_any_call(credential_manager.KEYRING_SERVICE_NAME, KEY_NAMES[0])


def test_r1_clear_without_keyring_removes_ephemeral_and_config_credentials():
    config = {"mistral_model": "test", "api_credentials": {"mistral_api_key": {"scheme": "ephemeral"}}}
    ephemeral = {"mistral_api_key": "encrypted-secret", "mns_master_key": "master-ciphertext"}
    saved = []

    def save_config(value, create_backup=True):
        saved.append((deepcopy(value), create_backup))

    with patch.object(crypto_utils, "_EPHEMERAL_CREDENTIALS", ephemeral), patch.object(
        crypto_utils, "_EPHEMERAL_KEY", "ephemeral-key"
    ):
        with _patched_clear(
            config, MagicMock(), keyring_available=False, save_config=save_config
        ):
            assert credential_manager.clear_api_credentials() == []

        assert ephemeral == {"mns_master_key": "master-ciphertext"}
        assert saved == [({"mistral_model": "test", "api_credentials": {}}, False)]


def test_r1_clear_keyring_failure_rolls_back_successful_destructive_steps(caplog):
    config = {
        "mistral_model": "test",
        "api_credentials": {
            "mistral_api_key": {"scheme": "keyring"},
            "langsearch_api_key": {"scheme": "keyring"},
        },
    }
    stored = {
        "mistral_api_key": "secret-a",
        "langsearch_api_key": "secret-b",
    }
    keyring = MagicMock()

    def get_password(_service, key_name):
        return stored.get(key_name)

    def delete_password(_service, key_name):
        if key_name == "langsearch_api_key":
            raise RuntimeError("backend included secret-b in failure")
        stored.pop(key_name, None)

    def set_password(_service, key_name, value):
        stored[key_name] = value

    keyring.get_password.side_effect = get_password
    keyring.delete_password.side_effect = delete_password
    keyring.set_password.side_effect = set_password
    with _patched_clear(config, keyring):
        with caplog.at_level("WARNING"):
            assert credential_manager.clear_api_credentials() == ["langsearch_api_key"]

    assert stored == {"mistral_api_key": "secret-a", "langsearch_api_key": "secret-b"}
    assert not any(
        secret in record.getMessage()
        for record in caplog.records
        for secret in ("secret-a", "secret-b")
    )


def test_r1_config_save_failure_rolls_back_keyring_ephemeral_and_config():
    config = {
        "mistral_model": "test",
        "api_credentials": {"mistral_api_key": {"scheme": "keyring"}},
    }
    stored = {"mistral_api_key": "secret-a"}
    keyring = MagicMock()

    def get_password(_service, key_name):
        return stored.get(key_name)

    def delete_password(_service, key_name):
        if key_name not in stored:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError("not found")
        stored.pop(key_name)

    keyring.get_password.side_effect = get_password
    keyring.delete_password.side_effect = delete_password
    keyring.set_password.side_effect = lambda _service, key_name, value: stored.__setitem__(key_name, value)
    ephemeral = {"mistral_api_key": "encrypted-secret"}
    saved = []

    def save_config(value, create_backup=True):
        if not saved:
            saved.append("failed")
            raise OSError("synthetic config save failure")
        saved.append((deepcopy(value), create_backup))

    with patch.object(crypto_utils, "_EPHEMERAL_CREDENTIALS", ephemeral), patch.object(
        crypto_utils, "_EPHEMERAL_KEY", "ephemeral-key"
    ), _patched_clear(config, keyring, save_config=save_config):
        with pytest.raises(OSError, match="synthetic config save failure"):
            credential_manager.clear_api_credentials()

    assert stored == {"mistral_api_key": "secret-a"}
    assert ephemeral == {"mistral_api_key": "encrypted-secret"}
    assert saved == ["failed", (config, False)]


def test_r1_ephemeral_delete_failure_rolls_back_keyring_and_ephemeral_state():
    config = {
        "api_credentials": {"mistral_api_key": {"scheme": "keyring"}},
    }
    stored = {"mistral_api_key": "secret-a"}
    keyring = MagicMock()
    keyring.get_password.side_effect = lambda _service, key_name: stored.get(key_name)
    keyring.delete_password.side_effect = lambda _service, key_name: stored.pop(key_name, None)
    keyring.set_password.side_effect = lambda _service, key_name, value: stored.__setitem__(key_name, value)
    ephemeral = {"mistral_api_key": "encrypted-secret"}

    def fail_clear(**_kwargs):
        ephemeral.clear()
        raise RuntimeError("synthetic ephemeral delete failure")

    with patch.object(crypto_utils, "_EPHEMERAL_CREDENTIALS", ephemeral), patch.object(
        crypto_utils, "clear_ephemeral_credentials", side_effect=fail_clear
    ), _patched_clear(config, keyring):
        with pytest.raises(RuntimeError, match="synthetic ephemeral delete failure"):
            credential_manager.clear_api_credentials()

    assert stored == {"mistral_api_key": "secret-a"}
    assert ephemeral == {"mistral_api_key": "encrypted-secret"}


def test_r1_rollback_failure_is_logged_without_secret_values(caplog):
    config = {"api_credentials": {"mistral_api_key": {"scheme": "keyring"}}}
    keyring = MagicMock()
    keyring.get_password.return_value = "secret-a"
    keyring.delete_password.return_value = None
    keyring.set_password.side_effect = RuntimeError("backend leaked secret-a")

    def fail_save(_value, create_backup=True):
        raise OSError("synthetic save failure")

    with _patched_clear(config, keyring, save_config=fail_save):
        with caplog.at_level("ERROR"):
            with pytest.raises(OSError, match="synthetic save failure"):
                credential_manager.clear_api_credentials()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Failed to roll back keyring credential storage for mistral_api_key" in messages
    assert "secret-a" not in messages


def test_r5_run_app_always_uses_locked_uv_launcher_even_with_venv_present():
    root = Path(__file__).resolve().parents[1]
    launcher = (root / "run_app.sh").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "if command -v uv" in launcher
    assert "exec uv run --locked python app.py" in launcher
    assert 'exec ".venv/bin/python" app.py' not in launcher
    assert "python3 app.py" not in launcher
    assert "always starts through the same locked environment" in readme
