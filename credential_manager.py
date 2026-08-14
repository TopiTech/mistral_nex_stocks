"""
認証情報管理モジュール
config_utils.py から抽出した API 鍵・シークレットキー管理関数群
"""
# pylint: disable=missing-function-docstring

import logging
import os
import secrets
import time

import config_store
import crypto_utils

logger = logging.getLogger(__name__)

KEYRING_SERVICE_NAME = crypto_utils.KEYRING_SERVICE_NAME


def _keyring_available():
    """Runtime check for keyring availability (avoids import-time evaluation)."""
    return crypto_utils.KEYRING_AVAILABLE


def _keyring():
    """Runtime access to keyring module."""
    return crypto_utils.keyring


def _get_api_credentials_blob(cfg=None):
    source = cfg if isinstance(cfg, dict) else config_store.load_config()
    raw = source.get("api_credentials") if isinstance(source, dict) else {}
    return raw if isinstance(raw, dict) else {}


def get_mistral_api_key():
    """Mistral API鍵を取得"""
    env_key = os.environ.get("MISTRAL_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return crypto_utils._decode_secret(
        _get_api_credentials_blob().get("mistral_api_key"),
        "mistral_api_key",
    )


def get_langsearch_api_key():
    """LangSearch API鍵を取得"""
    env_key = os.environ.get("LANGSEARCH_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return crypto_utils._decode_secret(
        _get_api_credentials_blob().get("langsearch_api_key"),
        "langsearch_api_key",
    )


def get_tavily_api_key():
    """Tavily API鍵を取得"""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return crypto_utils._decode_secret(
        _get_api_credentials_blob().get("tavily_api_key"),
        "tavily_api_key",
    )


def get_alphavantage_api_key():
    """Alpha Vantage API鍵を取得"""
    env_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return crypto_utils._decode_secret(
        _get_api_credentials_blob().get("alphavantage_api_key"),
        "alphavantage_api_key",
    )


def has_mistral_api_key():
    """Mistral API鍵が設定されているか確認"""
    return bool(get_mistral_api_key())


def has_langsearch_api_key():
    """LangSearch API鍵が設定されているか確認"""
    return bool(get_langsearch_api_key())


def has_tavily_api_key():
    """Tavily API鍵が設定されているか確認"""
    return bool(get_tavily_api_key())


def has_alphavantage_api_key():
    """Alpha Vantage API鍵が設定されているか確認"""
    return bool(get_alphavantage_api_key())


def save_api_credentials(
    mistral_api_key=None,
    langsearch_api_key=None,
    tavily_api_key=None,
    alphavantage_api_key=None,
    custom_ai_prompt: str | None = None,
    update_custom_ai_prompt: bool = False,
):
    """API認証情報と、必要に応じてカスタムプロンプトを単一更新で保存する。"""
    with config_store.config_update_lock():
        cfg = config_store.load_config()
        credentials = {
            key: value
            for key, value in _get_api_credentials_blob(cfg).items()
            if isinstance(value, dict)
        }

        if mistral_api_key is not None and str(mistral_api_key).strip():
            encoded = crypto_utils._encode_secret(
                mistral_api_key,
                "mistral_api_key",
            )
            if not encoded:
                raise RuntimeError("Failed to securely encode mistral_api_key")
            credentials["mistral_api_key"] = encoded

        if langsearch_api_key is not None and str(langsearch_api_key).strip():
            encoded = crypto_utils._encode_secret(
                langsearch_api_key,
                "langsearch_api_key",
            )
            if not encoded:
                raise RuntimeError("Failed to securely encode langsearch_api_key")
            credentials["langsearch_api_key"] = encoded

        if tavily_api_key is not None and str(tavily_api_key).strip():
            encoded = crypto_utils._encode_secret(
                tavily_api_key,
                "tavily_api_key",
            )
            if not encoded:
                raise RuntimeError("Failed to securely encode tavily_api_key")
            credentials["tavily_api_key"] = encoded

        if alphavantage_api_key is not None and str(alphavantage_api_key).strip():
            encoded = crypto_utils._encode_secret(
                alphavantage_api_key,
                "alphavantage_api_key",
            )
            if not encoded:
                raise RuntimeError("Failed to securely encode alphavantage_api_key")
            credentials["alphavantage_api_key"] = encoded

        cfg["api_credentials"] = credentials
        if update_custom_ai_prompt:
            cfg["custom_ai_prompt"] = (custom_ai_prompt or "").strip()
        config_store.save_config(cfg)


def clear_api_credentials() -> list[str]:
    """全API認証情報を削除。削除に失敗したキー名のリストを返す。"""
    with config_store.config_update_lock():
        cfg = config_store.load_config()
        failed_keys = []
        if _keyring_available():
            kr = _keyring()
            keyring_del_err: type[BaseException] | None = None
            try:
                from keyring.errors import PasswordDeleteError

                keyring_del_err = PasswordDeleteError
            except ImportError:
                pass

            for key_name in ("mistral_api_key", "langsearch_api_key", "tavily_api_key", "alphavantage_api_key"):
                try:
                    try:
                        kr.delete_password(KEYRING_SERVICE_NAME, key_name)
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        if keyring_del_err is not None and isinstance(exc, keyring_del_err):
                            pass
                        else:
                            logger.warning(
                                "Keyring credential deletion failed for %s: %s", key_name, exc
                            )
                            failed_keys.append(key_name)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.warning(
                        "Keyring credential check/deletion failed for %s: %s", key_name, exc
                    )
                    failed_keys.append(key_name)
        crypto_utils.clear_ephemeral_credentials(exclude={"mns_master_key"})
        cfg["api_credentials"] = {}
        config_store.save_config(cfg, create_backup=False)
        return failed_keys


def is_medium_or_large_model(model_name: str | None = None) -> bool:
    """現在のモデル（または指定モデル）が Medium または Large かを判定"""
    if not model_name:
        model_name = get_model_name()
    from config_utils import resolve_model_target

    resolved = resolve_model_target(str(model_name))
    target_name = resolved.get("name", model_name) if isinstance(resolved, dict) else str(model_name)
    target_lower = str(target_name).lower()
    model_name_lower = str(model_name).lower()
    return (
        "medium" in target_lower
        or "large" in target_lower
        or "medium" in model_name_lower
        or "large" in model_name_lower
    )


def get_api_credential_state():
    """API認証情報の設定状況を取得"""
    model_name = get_model_name()
    ephemeral_active = crypto_utils.is_ephemeral_active()
    ephemeral_keys = crypto_utils.get_ephemeral_keys()
    ephemeral_warning = (
        "API keys are stored in ephemeral memory and will be lost on restart."
        if ephemeral_active
        else None
    )
    from constants import (
        LANGSEARCH_API_KEY_MIN_LENGTH,
        MISTRAL_API_KEY_MIN_LENGTH,
        TAVILY_API_KEY_MIN_LENGTH,
    )

    return {
        "has_mistral_api_key": has_mistral_api_key(),
        "has_langsearch_api_key": has_langsearch_api_key(),
        "has_tavily_api_key": has_tavily_api_key(),
        "has_alphavantage_api_key": has_alphavantage_api_key(),
        "mistral_model": model_name,
        "is_ai_technical_lines_eligible": is_medium_or_large_model(model_name),
        "credentials_ephemeral": ephemeral_active,
        "credentials_ephemeral_keys": ephemeral_keys,
        "credentials_ephemeral_warning": ephemeral_warning,
        "mistral_api_key_min_length": MISTRAL_API_KEY_MIN_LENGTH,
        "langsearch_api_key_min_length": LANGSEARCH_API_KEY_MIN_LENGTH,
        "tavily_api_key_min_length": TAVILY_API_KEY_MIN_LENGTH,
    }


def get_model_name():
    """現在のMistralモデル名を取得"""
    return config_store.load_config().get(
        "mistral_model", config_store.DEFAULT_CONFIG["mistral_model"]
    )


def get_model_badge():
    """現在のモデルバッジ（UI表示用）を取得"""
    from config_utils import resolve_model_target

    model_name = get_model_name()
    resolved = resolve_model_target(model_name)
    if resolved and "badge" in resolved:
        return resolved["badge"]
    return model_name or "unknown"


def get_custom_ai_prompt():
    """カスタムAI分析プロンプトを取得"""
    return config_store.load_config().get("custom_ai_prompt", "")


def set_custom_ai_prompt(prompt: str):
    """カスタムAI分析プロンプトを保存"""
    with config_store.config_update_lock():
        cfg = config_store.load_config()
        cfg["custom_ai_prompt"] = (prompt or "").strip()
        config_store.save_config(cfg)


def get_or_create_flask_secret_key() -> str:
    """
    Flaskのシークレットキーを取得、または生成して安全に保存する。
    再起動後もセッションを維持するために使用する。
    """
    # Do not acquire the master-key lock while holding config_update_lock().
    # On POSIX the two locks use separately opened flock descriptors; nesting
    # them can block this process indefinitely. Acquiring the master key first
    # also gives every caller the same lock ordering.
    master_key = config_store.get_or_create_master_key()
    with config_store.config_update_lock():
        cfg = config_store.load_config()
        secret_entry = cfg.get("flask_secret_key")
        if secret_entry:
            secret = crypto_utils.unprotect_data(
                secret_entry, "flask_secret_key", master_key=master_key
            )
            if secret and len(secret) >= 32:
                return secret

        from utils.env_helpers import _is_production_env

        if _is_production_env():
            raise ValueError(
                "Security Risk: FLASK_SECRET_KEY environment variable is required in production."
            )

        # Generate a new 32-byte hex string (64 characters) if not available or invalid
        new_secret = secrets.token_hex(32)

        # Store it securely
        protected_entry = crypto_utils.protect_data(
            new_secret, "flask_secret_key", master_key=master_key
        )
        cfg["flask_secret_key"] = protected_entry
        config_store.save_config(cfg)
        return new_secret


def get_or_create_extension_api_token() -> str:
    """
    ブラウザ拡張機能からのAPIアクセス用トークンを取得または生成する。

    The token is rotated automatically once it exceeds a configurable maximum
    age (default 90 days, controlled by MNS_EXTENSION_TOKEN_MAX_AGE_DAYS) so a
    leaked static secret does not remain valid indefinitely. Existing tokens
    without a recorded creation time are grandfathered (treated as freshly
    created) to preserve backward compatibility.
    """
    # See get_or_create_flask_secret_key(): master-key initialization must
    # precede the generic config transaction to avoid a POSIX flock self-wait.
    master_key = config_store.get_or_create_master_key()
    with config_store.config_update_lock():
        cfg = config_store.load_config()
        secret_entry = cfg.get("extension_api_token")
        created_ts = cfg.get("extension_api_token_created", 0.0)
        rotated = False
        secret: str | None = None

        if secret_entry:
            secret = crypto_utils.unprotect_data(
                secret_entry, "extension_api_token", master_key=master_key
            )
            if secret and len(secret) >= 32:
                from utils.env_helpers import _env_float

                max_age_days = _env_float(
                    "MNS_EXTENSION_TOKEN_MAX_AGE_DAYS", 90.0, min_value=0.0
                )
                max_age_sec = max_age_days * 86400.0
                if (
                    max_age_sec > 0
                    and created_ts
                    and (time.time() - float(created_ts)) > max_age_sec
                ):
                    # Token expired by age -> rotate to a new value.
                    secret = None

        if not secret_entry or not secret or len(secret) < 32:
            secret = secrets.token_urlsafe(32)
            protected_entry = crypto_utils.protect_data(
                secret, "extension_api_token", master_key=master_key
            )
            cfg["extension_api_token"] = protected_entry
            cfg["extension_api_token_created"] = time.time()
            config_store.save_config(cfg)
            rotated = True

        if not rotated and not created_ts:
            # Backfill creation timestamp for a pre-existing token (no rotation).
            cfg["extension_api_token_created"] = time.time()
            config_store.save_config(cfg)

        return secret
