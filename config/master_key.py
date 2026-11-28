"""マスターキー管理。`config_store` からの純粋な移動。

`load_config` / `save_config` はテストで `patch(\"config_store.X\")`
されるため、呼び出し時に `config_store` 属性として解決する。
"""

from __future__ import annotations

import copy
import logging
import os

import config_store
import crypto_utils

logger = logging.getLogger("config_store")

_UNDECODABLE_MASTER_KEY_MESSAGE = (
    "FATAL: mns_master_key exists in config but could not be decoded "
    "(keyring/DPAPI read failure, corrupted entry, or ciphertext "
    "encrypted by another identity). Refusing to overwrite it: doing "
    "so would make all encrypted data permanently unreadable. "
    "Restore the secret store backing this key or set MNS_MASTER_KEY "
    "to the correct key, then retry."
)


def _validate_master_key(key: str) -> str:
    """Return *key* only when it is a valid Fernet key.

    An invalid env/stored key used to be returned as-is; ``protect_data()``
    then caught the Fernet constructor error and silently downgraded new
    secrets to keyring/DPAPI, while every existing ``scheme="fernet"`` value
    became unreadable. Fail closed instead.
    """
    from cryptography.fernet import Fernet

    try:
        Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "FATAL: the configured master key is not a valid Fernet key. "
            "Set MNS_MASTER_KEY (or restore the stored mns_master_key) to the "
            "original 32-byte URL-safe base64 key; refusing to downgrade "
            "encrypted data to a different storage scheme."
        ) from exc
    return key


def _reject_undecodable_master_key() -> None:
    """Fail closed for an existing-but-unusable ``mns_master_key`` entry.

    A truthy non-dict entry (legacy base64 string, hand-edited scalar, or a
    list written by a broken tool) is *not* "no key generated yet". Treating
    it as absent regenerates the key and overwrites the only copy, which
    permanently orphans every Fernet-encrypted value. The dict-but-undecodable
    branch already failed closed; this keeps both shapes on the same contract.
    """
    raise RuntimeError(_UNDECODABLE_MASTER_KEY_MESSAGE)


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
        return _validate_master_key(env_key)

    # R17-08: a stored key that decodes successfully IS persistent (keyring/
    # DPAPI backed). Requiring MNS_MASTER_KEY in production for that case made
    # keyring-provisioned installs fail to start on a production switch. The
    # production env requirement applies only to generation of a new key
    # below, not to reading a stored one.
    def _stored_master_key_if_present() -> str | None:
        _cfg = config_store.load_config()
        if config_store.is_config_corrupted():
            return None
        if not isinstance(_cfg, dict):
            return None
        _entry = _cfg.get("mns_master_key")
        if not _entry:
            return None
        if not isinstance(_entry, dict):
            _reject_undecodable_master_key()
        _key = config_store._decode_secret(_entry, "mns_master_key")
        if _key:
            return _validate_master_key(_key)
        _reject_undecodable_master_key()
        return None

    _stored_key = _stored_master_key_if_present()
    if _stored_key is not None:
        return _stored_key

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
        and os.environ.get("MNS_EPHEMERAL_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")
        and not _allow_ephemeral_master
    ):
        raise RuntimeError(
            "FATAL: Secure storage (keyring/DPAPI) is unavailable, and MNS_EPHEMERAL_FALLBACK=1 is active, "
            "but MNS_MASTER_KEY is not set in the environment. "
            "Generating or using a temporary master key would cause encrypted configurations and portfolio data "
            "to become unreadable and lost upon next restart. Please set a persistent MNS_MASTER_KEY in your environment."
        )

    # Fast path: reading an existing key needs no lock. ``load_config()`` is
    # already serialized internally and keyed on the file's (mtime_ns, size),
    # so a concurrent writer is observed without forcing a cache flush. Taking
    # the exclusive cross-process update lock here (as the generate path must)
    # serialized every ``user_stocks`` read/write behind one global lock and
    # re-read config.json on every call (CFG-01).
    cfg = config_store.load_config()
    if config_store.is_config_corrupted():
        raise RuntimeError(
            "Refusing to access or generate mns_master_key while configuration is corrupted."
        )
    if not isinstance(cfg, dict):
        cfg = {}
    key_entry = cfg.get("mns_master_key")
    if key_entry:
        if not isinstance(key_entry, dict):
            _reject_undecodable_master_key()
        key = config_store._decode_secret(key_entry, "mns_master_key")
        if key:
            return _validate_master_key(key)
        # The entry exists but could not be decoded. This is NOT the same as
        # "no key generated yet": silently generating a new key here would
        # overwrite mns_master_key and permanently orphan every Fernet-
        # encrypted value protected with the original key (user_stocks.json,
        # flask_secret_key, extension_api_token). Fail closed instead of
        # silently destroying data.
        _reject_undecodable_master_key()

    # The read/generate/write sequence must be serialized inside a process.
    # save_config() already has an OS-level atomic replacement lock, while this
    # dedicated lock prevents two startup threads from generating divergent
    # keys before either one reaches that replacement.
    with config_store._MASTER_KEY_LOCK, config_store._master_key_update_lock():
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        cfg = config_store.load_config()
        if config_store.is_config_unreadable(cfg):
            raise RuntimeError(
                "Refusing to access or generate mns_master_key while configuration is corrupted or unreadable."
            )
        if not isinstance(cfg, dict):
            cfg = {}

        key_entry = cfg.get("mns_master_key")
        if key_entry:
            if not isinstance(key_entry, dict):
                _reject_undecodable_master_key()
            key = config_store._decode_secret(key_entry, "mns_master_key")
            if key:
                return _validate_master_key(key)
            # The entry exists but could not be decoded. This is NOT the same as
            # "no key generated yet": silently generating a new key here would
            # overwrite mns_master_key and permanently orphan every Fernet-
            # encrypted value protected with the original key (user_stocks.json,
            # flask_secret_key, extension_api_token). Fail closed instead of
            # silently destroying data.
            _reject_undecodable_master_key()

        from cryptography.fernet import Fernet

        new_key = Fernet.generate_key().decode("ascii")
        previous_cfg = copy.deepcopy(cfg)
        previous_keyring_values, previous_ephemeral_values = crypto_utils.snapshot_secret_storage(
            ("mns_master_key",),
            log=logger,
        )
        saved_new_config = False
        try:
            encoded_master = config_store._encode_secret(new_key, "mns_master_key")
            if (
                isinstance(encoded_master, dict)
                and encoded_master.get("scheme") == "ephemeral"
                and not _allow_ephemeral_master
            ):
                raise RuntimeError(
                    "FATAL: Secure storage (keyring/DPAPI) fell back to ephemeral memory "
                    "for mns_master_key, but MNS_ALLOW_EPHEMERAL_MASTER_KEY is not enabled. "
                    "Refusing to persist an ephemeral master key; set MNS_MASTER_KEY instead."
                )
            cfg["mns_master_key"] = encoded_master
            try:
                config_store.save_config(cfg)
                saved_new_config = True
            except Exception as exc:
                logger.error("Failed to persist generated master key: %s", exc)
                raise RuntimeError("Failed to persist generated master key") from exc

            config_store._CONFIG_CACHE["data"] = None
            config_store._CONFIG_CACHE["key"] = None
            persisted_config = config_store.load_config()
            persisted = (
                persisted_config.get("mns_master_key")
                if isinstance(persisted_config, dict)
                else None
            )
            persisted_key = (
                config_store._decode_secret(persisted, "mns_master_key")
                if isinstance(persisted, dict)
                else ""
            )
            if not persisted_key or persisted_key != new_key:
                raise RuntimeError("Failed to verify persisted master key")
        except Exception:
            if saved_new_config:
                try:
                    try:
                        config_store.save_config(
                            previous_cfg,
                            create_backup=False,
                            preserve_protected=False,
                        )
                    except TypeError:
                        config_store.save_config(previous_cfg)
                except Exception as rollback_exc:
                    logger.warning(
                        "Failed to roll back config after master key verification error: %s",
                        rollback_exc,
                    )
                config_store._CONFIG_CACHE["data"] = None
                config_store._CONFIG_CACHE["key"] = None
            crypto_utils.restore_secret_storage(
                previous_keyring_values,
                previous_ephemeral_values,
                log=logger,
            )
            raise
        return persisted_key
