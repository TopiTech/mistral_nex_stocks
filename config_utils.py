# -*- coding: utf-8 -*-
"""
統一設定管理モジュール（ファサード）
app.py, switch_model.py の設定読み込み・保存の重複を排除

このモジュールは後方互換性を維持するためのファサードです。
新しいコードでは、以下のモジュールを直接インポートしてください：
  - crypto_utils: 暗号化/復号化関連
  - credential_manager: API鍵・シークレットキー管理
  - config_store: 設定ファイル読み書き
"""
# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-return-statements,too-many-arguments,too-many-positional-arguments

# --- 再エクスポート: 既存のインポートをすべて維持 ---
import crypto_utils as _crypto_utils  # noqa: F401 -- also needed as module ref
import config_store as _config_store  # noqa: F401 -- also needed as module ref

from utils.env_helpers import _env_float, _env_int  # noqa: F401 -- re-exported for other modules

# config_store からの再エクスポート
from config_store import (  # noqa: F401
    BASE_DIR,
    CONFIG_FILE,
    DEFAULT_CONFIG,
    _CONFIG_LOCK,
    load_config,
    save_config,
)

# crypto_utils からの再エクスポート
from crypto_utils import (  # noqa: F401
    KEYRING_AVAILABLE,
    KEYRING_SERVICE_NAME,
    DataBlob,
    _decode_secret,
    _dpapi_protect,
    _dpapi_unprotect,
    _encode_secret,
    _is_windows,
    _blob_from_bytes,
    encode_secret,
    decode_secret,
    enforce_secure_permissions,
    protect_data,
    unprotect_data,
)

# credential_manager からの再エクスポート
from credential_manager import (  # noqa: F401
    get_mistral_api_key,
    get_langsearch_api_key,
    get_tavily_api_key,
    has_mistral_api_key,
    has_langsearch_api_key,
    has_tavily_api_key,
    save_api_credentials,
    clear_api_credentials,
    get_api_credential_state,
    get_model_name,
    get_model_badge,
    get_custom_ai_prompt,
    set_custom_ai_prompt,
    get_or_create_flask_secret_key,
    get_or_create_extension_api_token,
    _get_api_credentials_blob,
)

# --- 定数定義（モデル関連は config_utils に残す） ---
MISTRAL_MODELS = {
    "1": {"name": "mistral-small-2603", "badge": "mistral-small-v4"},
    "2": {"name": "mistral-medium-2604", "badge": "mistral-medium-v3.5"},
    "3": {"name": "mistral-large-2512", "badge": "mistral-large-v3"},
    "4": {"name": "ministral-3-8b-2512", "badge": "ministral-8b"},
    "5": {"name": "ministral-3-14b-2512", "badge": "ministral-14b"},
    "6": {"name": "ministral-3-3b-2512", "badge": "ministral-3b"},
    "7": {"name": "mistral-medium-2604", "badge": "mistral-medium-v3.5"},
}

MISTRAL_SUPPORTED_MODELS = {
    # Versioned API model IDs (primary identifiers)
    "mistral-small-2603",
    "mistral-medium-2604",
    "mistral-large-2512",
    "ministral-3-14b-2512",
    "ministral-3-8b-2512",
    "ministral-3-3b-2512",
    "codestral-2508",
    "devstral-2512",
}

MISTRAL_LEGACY_ALIASES = {
    # Friendly name -> versioned API model ID
    "mistral-small-4": "mistral-small-2603",
    "mistral-small-latest": "mistral-small-2603",
    "mistral-medium-3.5": "mistral-medium-2604",
    "mistral-medium-3-5": "mistral-medium-2604",
    "mistral-medium-latest": "mistral-medium-2604",
    "mistral-medium-3.1": "mistral-medium-2604",
    "mistral-large-3": "mistral-large-2512",
    "mistral-large-latest": "mistral-large-2512",
    "mistral-nemo-12b": "ministral-3-8b-2512",
    "open-mistral-nemo": "ministral-3-8b-2512",
    "ministral-3-14b": "ministral-3-14b-2512",
    "ministral-3-8b": "ministral-3-8b-2512",
    "ministral-3-3b": "ministral-3-3b-2512",
    "ministral-8b-latest": "ministral-3-8b-2512",
    "ministral-3b-latest": "ministral-3-3b-2512",
    "codestral": "codestral-2508",
    "devstral-2": "devstral-2512",
    "pixtral-large-latest": "mistral-medium-2604",
    "magistral-medium-1.2": "mistral-medium-2604",
}


def _build_mistral_legacy_aliases():
    """Derive additional legacy aliases from MISTRAL_MODELS entries ending in '-latest'.

    For every model in MISTRAL_MODELS whose canonical name ends with '-latest'
    and is not in MISTRAL_SUPPORTED_MODELS, resolve it through
    MISTRAL_LEGACY_ALIASES. This keeps a single source of truth: when a new
    model is added to MISTRAL_MODELS, the derived alias is registered at
    import time without requiring a separate edit.
    """
    derived = {}
    for entry in MISTRAL_MODELS.values():
        name = entry.get("name", "")
        if not name or not name.endswith("-latest"):
            continue
        if name in MISTRAL_SUPPORTED_MODELS:
            continue
        canonical = MISTRAL_LEGACY_ALIASES.get(name)
        if canonical:
            derived[name] = canonical
    return derived


# Augment the legacy alias table at import time so MISTRAL_MODELS additions
# that need aliasing are picked up automatically.
for _alias, _target in _build_mistral_legacy_aliases().items():
    MISTRAL_LEGACY_ALIASES.setdefault(_alias, _target)


def get_or_create_master_key() -> str:
    """Get or create the master key for Fernet symmetric encryption.

    .. deprecated:: 3.0.0
       This function is a legacy wrapper and will be removed in a future release.
       Use ``config_store.get_or_create_master_key()`` directly.
    """
    import warnings

    warnings.warn(
        "config_utils.get_or_create_master_key is deprecated and will be removed in a future version. "
        "Use config_store.get_or_create_master_key directly instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _config_store.get_or_create_master_key()


def resolve_model_target(arg: str):
    """
    ユーザー入力からモデル情報を解決

    Args:
        arg: "1", "2", "3" または "mistral-small-latest" など

    Returns:
        {"name": "...", "badge": "..."} または None
    """
    if arg in MISTRAL_MODELS:
        return MISTRAL_MODELS[arg]
    # Check legacy aliases (e.g. "mistral-small-4" -> "mistral-small-2603")
    resolved = MISTRAL_LEGACY_ALIASES.get(arg)
    if resolved:
        return next(
            (v for v in MISTRAL_MODELS.values() if v["name"] == resolved),
            {"name": resolved, "badge": resolved},
        )
    return next((v for v in MISTRAL_MODELS.values() if v["name"] == arg), None)


def get_all_models():
    """利用可能なすべてのモデルを取得"""
    return MISTRAL_MODELS


# keyring の再エクスポート（テスト互換性のため）
keyring = _crypto_utils.keyring
