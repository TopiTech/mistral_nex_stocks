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
import config_store as _config_store
import crypto_utils as _crypto_utils

# config_store からの再エクスポート
from config_store import (  # noqa: F401
    _CONFIG_LOCK,
    BASE_DIR,
    CONFIG_FILE,
    DEFAULT_CONFIG,
    load_config,
    save_config,
)

# credential_manager からの再エクスポート
from credential_manager import (  # noqa: F401
    _get_api_credentials_blob,
    clear_api_credentials,
    get_alphavantage_api_key,
    get_api_credential_state,
    get_custom_ai_prompt,
    get_langsearch_api_key,
    get_mistral_api_key,
    get_model_badge,
    get_model_name,
    get_or_create_extension_api_token,
    get_or_create_flask_secret_key,
    get_tavily_api_key,
    has_alphavantage_api_key,
    has_langsearch_api_key,
    has_mistral_api_key,
    has_tavily_api_key,
    is_medium_or_large_model,
    save_api_credentials,
    set_custom_ai_prompt,
)

# crypto_utils からの再エクスポート
from crypto_utils import (  # noqa: F401
    KEYRING_AVAILABLE,
    KEYRING_SERVICE_NAME,
    DataBlob,
    _blob_from_bytes,
    _decode_secret,
    _dpapi_protect,
    _dpapi_unprotect,
    _encode_secret,
    _is_windows,
    decode_secret,
    encode_secret,
    enforce_secure_permissions,
    protect_data,
    unprotect_data,
)
from utils.env_helpers import _env_float, _env_int  # noqa: F401 -- re-exported for other modules

# --- 定数定義（モデル関連は config_utils に残す） ---
MISTRAL_MODELS = {
    "1": {
        "id": "1",
        "name": "mistral-small-2603",
        "badge": "mistral-small-v4",
        "label": "Mistral Small 4 (高速・軽量)",
        "description": "無料Tier完全対応。軽量かつ高速な汎用モデル。低レイテンシで軽快に動作します。",
        "recommended": False,
        "tier": "free",
        "tier_label": "Free Tier 推奨",
        "supports_reasoning": True,
        "supports_tools": True,
        "supports_vision": False,
    },
    "2": {
        "id": "2",
        "name": "mistral-medium-2604",
        "badge": "mistral-medium-v3.5",
        "label": "Mistral Medium 3.5 (推奨・バランス)",
        "description": "高度な推論・分析・エージェント機能を備えたフラッグシップモデル。分析品質と応答速度のバランスが最良です。（有料/従量課金プラン推奨）",
        "recommended": True,
        "tier": "paid",
        "tier_label": "有料・従量課金プラン",
        "supports_reasoning": True,
        "supports_tools": True,
        "supports_vision": False,
    },
    "3": {
        "id": "3",
        "name": "mistral-large-2512",
        "badge": "mistral-large-v3",
        "label": "Mistral Large 3 (最高精度・推論重視)",
        "description": "最も深い推論能力を持つフロンティアモデル。※有料プラン専用。無料Tierでは利用できません。",
        "recommended": False,
        "tier": "paid",
        "tier_label": "有料プラン専用",
        "supports_reasoning": True,
        "supports_tools": True,
        "supports_vision": False,
    },
    "4": {
        "id": "4",
        "name": "ministral-8b-latest",
        "badge": "ministral-8b",
        "label": "Ministral 8B (高効率エッジ・Free Tier対応)",
        "description": "エッジおよび軽量タスクに最適化された8Bパラメータ高効率モデル。無料Tier対応。",
        "recommended": False,
        "tier": "free",
        "tier_label": "Free Tier 推奨",
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_vision": False,
    },
    "5": {
        "id": "5",
        "name": "ministral-3b-latest",
        "badge": "ministral-3b",
        "label": "Ministral 3B (超軽量・最速・Free Tier対応)",
        "description": "最小フットプリントで超高速に応答する3Bパラメータモデル。無料Tier対応。",
        "recommended": False,
        "tier": "free",
        "tier_label": "Free Tier 推奨",
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_vision": False,
    },
    "6": {
        "id": "6",
        "name": "codestral-latest",
        "badge": "codestral",
        "label": "Codestral (コード・数式特化)",
        "description": "コード生成・プログラミング推論・定量的数式計算に特化した高性能モデル。",
        "recommended": False,
        "tier": "free",
        "tier_label": "コード特化",
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_vision": False,
    },
    "7": {
        "id": "7",
        "name": "pixtral-large-latest",
        "badge": "pixtral-large",
        "label": "Pixtral Large (マルチモーダル画像認識)",
        "description": "チャート画像やIR資料・図表を直接読み取り解析可能な画像認識フロンティアモデル。",
        "recommended": False,
        "tier": "paid",
        "tier_label": "画像認識・有料プラン",
        "supports_reasoning": True,
        "supports_tools": True,
        "supports_vision": True,
    },
}

MISTRAL_SUPPORTED_MODELS = {
    # Versioned API model IDs (primary identifiers)
    "mistral-small-2603",
    "mistral-medium-2604",
    "mistral-large-2512",
    "ministral-8b-latest",
    "ministral-3b-latest",
    "ministral-14b-latest",
    "codestral-latest",
    "codestral-2508",
    "devstral-2512",
    "pixtral-large-latest",
    "pixtral-12b-2409",
    "mistral-embed",
    # Legacy identifiers kept for backward compatibility
    "ministral-3-14b-2512",
    "ministral-3-8b-2512",
    "ministral-3-3b-2512",
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
    "mistral-nemo-12b": "ministral-8b-latest",
    "open-mistral-nemo": "ministral-8b-latest",
    "ministral-3-14b": "ministral-14b-latest",
    "ministral-3-8b": "ministral-8b-latest",
    "ministral-3-3b": "ministral-3b-latest",
    "ministral-3-14b-2512": "ministral-14b-latest",
    "ministral-3-8b-2512": "ministral-8b-latest",
    "ministral-3-3b-2512": "ministral-3b-latest",
    "ministral-14b": "ministral-14b-latest",
    "ministral-8b": "ministral-8b-latest",
    "ministral-3b": "ministral-3b-latest",
    "codestral": "codestral-latest",
    "devstral-2": "devstral-2512",
    "pixtral-large": "pixtral-large-latest",
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
        if not isinstance(name, str) or not name or not name.endswith("-latest"):
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
    if not arg:
        return None
    raw = arg.strip()
    if raw in MISTRAL_MODELS:
        return dict(MISTRAL_MODELS[raw])
    # Check if raw matches any entry's name
    for entry in MISTRAL_MODELS.values():
        if entry.get("name") == raw:
            return dict(entry)
    # Check legacy aliases (e.g. "mistral-small-4" -> "mistral-small-2603")
    resolved = MISTRAL_LEGACY_ALIASES.get(raw)
    if resolved:
        match = next((v for v in MISTRAL_MODELS.values() if v.get("name") == resolved), None)
        if match:
            return dict(match)
        return {"name": resolved, "badge": resolved}
    if raw in MISTRAL_SUPPORTED_MODELS:
        return {"name": raw, "badge": raw, "label": raw}
    return None


def get_model_catalog() -> list[dict]:
    """フロントエンド表示用のモデルカタログ一覧を取得"""
    catalog = []
    for key in sorted(MISTRAL_MODELS.keys(), key=lambda x: int(x) if str(x).isdigit() else 99):
        entry = dict(MISTRAL_MODELS[key])
        entry["id"] = key
        catalog.append(entry)
    return catalog


def get_all_models():
    """利用可能なすべてのモデルを取得"""
    return MISTRAL_MODELS


# keyring の再エクスポート（テスト互換性のため）
keyring = getattr(_crypto_utils, "keyring", None)
