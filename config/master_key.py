"""マスターキー管理。`config_store` からの純粋な移動。

`load_config` / `save_config` はテストで `patch(\"config_store.X\")`
されるため、呼び出し時に `config_store` 属性として解決する。
"""

from __future__ import annotations

import logging
import os

import config_store

logger = logging.getLogger("config_store")


def get_or_create_master_key() -> str:
    """Get or create the master key for Fernet symmetric encryption.

    Checks in order:
    1. MNS_MASTER_KEY environment variable
    2. mns_master_key in config.json (decoded)
    3. Generates a new key, stores it encrypted in config.json

    Returns:
        str: The master key (base64-encoded, compatible with cryptography.fernet)
    """
    env_key = os.environ.get("MNS_MASTER_KEY", "").strip()
    if env_key:
        return env_key

    # Production check: MNS_MASTER_KEY must be set in production mode to prevent data loss.
    from utils.env_helpers import _is_production_env

    if _is_production_env():
        raise RuntimeError(
            "FATAL: MNS_MASTER_KEY is not set in the environment, but the application is running in production mode. "
            "Using an ephemeral or auto-generated key would cause encrypted configurations and portfolio data "
            "to become unreadable and lost upon next restart. Please set a persistent MNS_MASTER_KEY in your environment."
        )

    from crypto_utils import KEYRING_AVAILABLE, _is_windows
    from utils.env_helpers import _env_bool as _config_store_env_bool

    _allow_ephemeral_master = _config_store_env_bool("MNS_ALLOW_EPHEMERAL_MASTER_KEY")
    if (
        not KEYRING_AVAILABLE
        and not _is_windows()
        and os.environ.get("MNS_EPHEMERAL_FALLBACK") == "1"
        and not _allow_ephemeral_master
    ):
        raise RuntimeError(
            "FATAL: Secure storage (keyring/DPAPI) is unavailable, and MNS_EPHEMERAL_FALLBACK=1 is active, "
            "but MNS_MASTER_KEY is not set in the environment. "
            "Generating or using a temporary master key would cause encrypted configurations and portfolio data "
            "to become unreadable and lost upon next restart. Please set a persistent MNS_MASTER_KEY in your environment."
        )

    # The read/generate/write sequence must be serialized inside a process.
    # save_config() already has an OS-level atomic replacement lock, while this
    # dedicated lock prevents two startup threads from generating divergent
    # keys before either one reaches that replacement.
    with config_store._MASTER_KEY_LOCK, config_store._master_key_update_lock():
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        cfg = config_store.load_config()
        if not isinstance(cfg, dict):
            cfg = {}

        key_entry = cfg.get("mns_master_key")
        if key_entry and isinstance(key_entry, dict):
            key = config_store._decode_secret(key_entry, "mns_master_key")
            if key:
                return key
            # The entry exists but could not be decoded. This is NOT the same as
            # "no key generated yet": silently generating a new key here would
            # overwrite mns_master_key and permanently orphan every Fernet-
            # encrypted value protected with the original key (user_stocks.json,
            # flask_secret_key, extension_api_token). Fail closed instead of
            # silently destroying data.
            raise RuntimeError(
                "FATAL: mns_master_key exists in config but could not be decoded "
                "(keyring/DPAPI read failure, corrupted entry, or ciphertext "
                "encrypted by another identity). Refusing to overwrite it: doing "
                "so would make all encrypted data permanently unreadable. "
                "Restore the secret store backing this key or set MNS_MASTER_KEY "
                "to the correct key, then retry."
            )

        from cryptography.fernet import Fernet

        new_key = Fernet.generate_key().decode("ascii")
        cfg["mns_master_key"] = config_store._encode_secret(new_key, "mns_master_key")

        try:
            config_store.save_config(cfg)
        except Exception as exc:
            logger.error("Failed to persist generated master key: %s", exc)
            raise RuntimeError("Failed to persist generated master key") from exc

        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        persisted_config = config_store.load_config()
        persisted = (
            persisted_config.get("mns_master_key") if isinstance(persisted_config, dict) else None
        )
        persisted_key = (
            config_store._decode_secret(persisted, "mns_master_key")
            if isinstance(persisted, dict)
            else ""
        )
        if not persisted_key:
            raise RuntimeError("Failed to verify persisted master key")
        return persisted_key
