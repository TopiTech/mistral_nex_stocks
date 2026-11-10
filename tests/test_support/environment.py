"""Baseline test environment constants and helpers."""

from __future__ import annotations

import os


def get_baseline_test_env(temp_dir_name: str) -> dict[str, str]:
    """Return dictionary of baseline test environment variables."""
    return {
        "MNS_DATA_DIR": temp_dir_name,
        "MNS_APP_DATA_DIR": temp_dir_name,
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "PYTHONKEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "DBUS_SESSION_BUS_ADDRESS": "",
        "MNS_ALLOW_CLIENT_API_KEY": "1",
        "MNS_MISTRAL_MIN_INTERVAL": "0",
        "MNS_SKIP_BOOTSTRAP": "1",
    }


def apply_baseline_test_env(temp_dir_name: str) -> None:
    """Apply baseline environment variables to os.environ."""
    env = get_baseline_test_env(temp_dir_name)
    for key, value in env.items():
        os.environ[key] = value

    # Clear external API keys and tokens to prevent leaking local environment into tests
    for key in (
        "OPENAI_API_KEY",
        "MISTRAL_API_KEY",
        "TAVILY_API_KEY",
        "ALPHAVANTAGE_API_KEY",
        "LANGSEARCH_API_KEY",
        "MNS_ADMIN_TOKEN",
        "MNS_API_TOKEN",
        "MNS_MASTER_KEY",
        # Ambient agent-harness variables are not application configuration.
        # The Mistral Agents SDK reads the generic ``AGENT`` env var through
        # pydantic-settings and fails to import when a harness sets it to a
        # non-object value (e.g. ``AGENT=1``), which would make the SDK tests
        # environment-dependent.
        "AGENT",
    ):
        os.environ.pop(key, None)
