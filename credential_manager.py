"""
認証情報管理モジュール
config_utils.py から抽出した API 鍵・シークレットキー管理関数群
"""
# pylint: disable=missing-function-docstring

import logging
import os
import secrets

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
        _get_api_credentials_blob().get("mistral_api_key"), "mistral_api_key",
        load_config_fn=config_store.load_config
    )


def get_langsearch_api_key():
    """LangSearch API鍵を取得"""
    env_key = os.environ.get("LANGSEARCH_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return crypto_utils._decode_secret(
        _get_api_credentials_blob().get("langsearch_api_key"), "langsearch_api_key",
        load_config_fn=config_store.load_config
    )


def get_tavily_api_key():
    """Tavily API鍵を取得"""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return crypto_utils._decode_secret(
        _get_api_credentials_blob().get("tavily_api_key"), "tavily_api_key",
        load_config_fn=config_store.load_config
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


def save_api_credentials(mistral_api_key=None, langsearch_api_key=None, tavily_api_key=None):
    """API認証情報を安全に保存"""
    cfg = config_store.load_config()
    credentials = {
        key: value
        for key, value in _get_api_credentials_blob(cfg).items()
        if isinstance(value, dict)
    }

    if mistral_api_key is not None:
        if str(mistral_api_key).strip():
            encoded = crypto_utils._encode_secret(
                mistral_api_key, "mistral_api_key",
                load_config_fn=config_store.load_config
            )
            if not encoded:
                raise RuntimeError("Failed to securely encode mistral_api_key")
            credentials["mistral_api_key"] = encoded

    if langsearch_api_key is not None:
        if str(langsearch_api_key).strip():
            encoded = crypto_utils._encode_secret(
                langsearch_api_key, "langsearch_api_key",
                load_config_fn=config_store.load_config
            )
            if not encoded:
                raise RuntimeError("Failed to securely encode langsearch_api_key")
            credentials["langsearch_api_key"] = encoded

    if tavily_api_key is not None:
        if str(tavily_api_key).strip():
            encoded = crypto_utils._encode_secret(
                tavily_api_key, "tavily_api_key",
                load_config_fn=config_store.load_config
            )
            if not encoded:
                raise RuntimeError("Failed to securely encode tavily_api_key")
            credentials["tavily_api_key"] = encoded

    cfg["api_credentials"] = credentials
    config_store.save_config(cfg)


def clear_api_credentials():
    """全API認証情報を削除"""
    cfg = config_store.load_config()
    if _keyring_available():
        kr = _keyring()
        for key_name in ("mistral_api_key", "langsearch_api_key", "tavily_api_key"):
            try:
                kr.delete_password(KEYRING_SERVICE_NAME, key_name)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Keyring credential deletion failed for %s: %s",
                    key_name,
                    exc,
                )
    cfg["api_credentials"] = {}
    config_store.save_config(cfg, create_backup=False)


def get_api_credential_state():
    """API認証情報の設定状況を取得"""
    return {
        "has_mistral_api_key": has_mistral_api_key(),
        "has_langsearch_api_key": has_langsearch_api_key(),
        "has_tavily_api_key": has_tavily_api_key(),
    }


def get_model_name():
    """現在のMistralモデル名を取得"""
    return config_store.load_config().get("mistral_model", config_store.DEFAULT_CONFIG["mistral_model"])


def get_model_badge():
    """現在のモデルバッジ（UI表示用）を取得"""
    return config_store.load_config().get("model_badge", config_store.DEFAULT_CONFIG["model_badge"])


def get_custom_ai_prompt():
    """カスタムAI分析プロンプトを取得"""
    return config_store.load_config().get("custom_ai_prompt", "")


def set_custom_ai_prompt(prompt: str):
    """カスタムAI分析プロンプトを保存"""
    cfg = config_store.load_config()
    cfg["custom_ai_prompt"] = (prompt or "").strip()
    config_store.save_config(cfg)


def get_or_create_flask_secret_key() -> str:
    """
    Flaskのシークレットキーを取得、または生成して安全に保存する。
    再起動後もセッションを維持するために使用する。
    """
    cfg = config_store.load_config()
    secret_entry = cfg.get("flask_secret_key")
    if secret_entry:
        secret = crypto_utils.unprotect_data(secret_entry, "flask_secret_key", config_store)
        if secret and len(secret) >= 32:
            return secret

    # Generate a new 32-byte hex string (64 characters) if not available or invalid
    new_secret = secrets.token_hex(32)

    # Store it securely
    protected_entry = crypto_utils.protect_data(new_secret, "flask_secret_key", config_store)
    cfg["flask_secret_key"] = protected_entry
    config_store.save_config(cfg)
    return new_secret


def get_or_create_extension_api_token() -> str:
    """
    ブラウザ拡張機能からのAPIアクセス用トークンを取得または生成する。
    """
    cfg = config_store.load_config()
    secret_entry = cfg.get("extension_api_token")
    if secret_entry:
        secret = crypto_utils.unprotect_data(secret_entry, "extension_api_token", config_store)
        if secret and len(secret) >= 32:
            return secret

    new_secret = secrets.token_urlsafe(32)

    protected_entry = crypto_utils.protect_data(new_secret, "extension_api_token", config_store)
    cfg["extension_api_token"] = protected_entry
    config_store.save_config(cfg)
    return new_secret
