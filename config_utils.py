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
    get_ai_provider,
    get_alphavantage_api_key,
    get_api_credential_state,
    get_custom_ai_prompt,
    get_langsearch_api_key,
    get_mistral_api_key,
    get_model_badge,
    get_model_name,
    get_openai_api_key,
    get_openai_base_url,
    get_openai_embeddings_model,
    get_openai_model,
    get_or_create_extension_api_token,
    get_or_create_flask_secret_key,
    get_tavily_api_key,
    has_alphavantage_api_key,
    has_langsearch_api_key,
    has_mistral_api_key,
    has_openai_api_key,
    has_tavily_api_key,
    is_medium_or_large_model,
    is_openai_provider,
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
        "badge": "ministral-3-8b",
        "label": "Ministral 3 8B (高効率エッジ・画像＆テキスト対応)",
        "description": "エッジおよび軽量タスクに最適化された8Bパラメータ高効率マルチモーダルモデル。無料Tier対応。",
        "recommended": False,
        "tier": "free",
        "tier_label": "Free Tier 推奨",
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_vision": True,
    },
    "5": {
        "id": "5",
        "name": "ministral-3b-latest",
        "badge": "ministral-3-3b",
        "label": "Ministral 3 3B (超軽量・最速・画像＆テキスト対応)",
        "description": "最小フットプリントで超高速に応答する3Bパラメータマルチモーダルモデル。無料Tier対応。",
        "recommended": False,
        "tier": "free",
        "tier_label": "Free Tier 推奨",
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_vision": True,
    },
    "6": {
        "id": "6",
        "name": "codestral-latest",
        "badge": "codestral-v25.08",
        "label": "Codestral (コード・数式特化)",
        "description": "コード生成・プログラミング推論・定量的数式計算に特化した高性能モデル (256kコンテキスト)。",
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
    "8": {
        "id": "8",
        "name": "pixtral-12b-2409",
        "badge": "pixtral-12b",
        "label": "Pixtral 12B (軽量マルチモーダル・画像認識)",
        "description": "チャート画像やIR資料・図表を直接読み取り解析可能な12Bパラメータの画像認識モデル。無料Tier対応。",
        "recommended": False,
        "tier": "free",
        "tier_label": "Free Tier 推奨・画像認識",
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_vision": True,
    },
    "9": {
        "id": "9",
        "name": "mistral-large-4-0",
        "badge": "mistral-large-v4",
        "label": "Mistral Large 4 (最新フラッグシップ・マルチモーダルMoE)",
        "description": "52B active/1.05T paramsの最新フロンティアモデル。1.6B Vision Encoder標準搭載で深層推論と画像解析を両立。（有料プラン専用）",
        "recommended": False,
        "tier": "paid",
        "tier_label": "有料プラン専用・マルチモーダル",
        "supports_reasoning": True,
        "supports_tools": True,
        "supports_vision": True,
    },
    "10": {
        "id": "10",
        "name": "mistral-ocr-latest",
        "badge": "ocr-v4.1",
        "label": "Mistral OCR 4.1 (ドキュメントAI・高精度構造化抽出)",
        "description": "段落単位バウンディングボックス抽出・構造ブロック分類を備えた最新のOCRモデル。",
        "recommended": False,
        "tier": "paid",
        "tier_label": "ドキュメントAI・有料プラン",
        "supports_reasoning": False,
        "supports_tools": False,
        "supports_vision": True,
    },
    "11": {
        "id": "11",
        "name": "ministral-14b-latest",
        "badge": "ministral-3-14b",
        "label": "Ministral 3 14B (エッジ最高峰・画像＆テキスト対応)",
        "description": "Ministral 3シリーズ最大モデル。Mistral Small匹敵の性能を持ちテキスト・画像両対応。",
        "recommended": False,
        "tier": "free",
        "tier_label": "Free Tier 推奨",
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_vision": True,
    },
}

MISTRAL_SUPPORTED_MODELS = {
    # Frontier & General-purpose (Mistral Large)
    "mistral-large-4-0",
    "mistral-large-2610",
    "mistral-large-2512",
    "mistral-large-2411",
    "mistral-large-2407",
    "mistral-large-2402",
    "mistral-large-latest",
    # Balanced & Reasoning (Mistral Medium)
    "mistral-medium-2604",
    "mistral-medium-3.5",
    "mistral-medium-3-5",
    "mistral-medium-2508",
    "mistral-medium-2505",
    "mistral-medium-2312",
    "mistral-medium-latest",
    # Fast & Lightweight (Mistral Small)
    "mistral-small-2603",
    "mistral-small-2506",
    "mistral-small-2501",
    "mistral-small-2409",
    "mistral-small-2402",
    "mistral-small-2312",
    "mistral-small-latest",
    # Edge & Ministral Family (Ministral 3 & v24.10)
    "ministral-14b-latest",
    "ministral-3-14b-2512",
    "ministral-8b-latest",
    "ministral-3-8b-2512",
    "ministral-8b-2410",
    "ministral-3b-latest",
    "ministral-3-3b-2512",
    "ministral-3b-2410",
    # Code models
    "codestral-latest",
    "codestral-2508",
    "codestral-2501",
    "codestral-2405",
    "devstral-2512",
    "codestral-mamba-7b-0-1",
    # Multimodal & Vision
    "pixtral-large-latest",
    "pixtral-large-2411",
    "pixtral-12b-latest",
    "pixtral-12b-2409",
    # OCR / Document AI models
    "mistral-ocr-latest",
    "mistral-ocr-4-1",
    "ocr-3-25-12",
    "mistral-ocr-2503",
    # Audio / Speech models (Voxtral)
    "voxtral-mini-transcribe-3-0",
    "voxtral-small-transcribe-3-0",
    "voxtral-mini-transcribe-realtime-3-0",
    "voxtral-mini-tts-3-0",
    "voxtral-small-25-07",
    "voxtral-mini-transcribe-26-02",
    "voxtral-mini-transcribe-25-07",
    # Embedding models
    "mistral-embed",
    "mistral-embed-2312",
    "codestral-embed",
    "codestral-embed-2505",
    # Moderation & Safety models
    "mistral-moderation-latest",
    "mistral-moderation-2603",
    "mistral-moderation-2411",
    "shieldstral-1-0",
    # Time Series models (Timestral)
    "timestral-1-0-209m",
    "timestral-1-0-123m",
    "timestral-1-0-28m",
    "timestral-1-0-5m",
    # Open-weight baselines
    "open-mistral-7b",
    "open-mixtral-8x7b",
    "open-mixtral-8x22b",
    "open-mistral-nemo",
    "open-mistral-nemo-2407",
    # Third-party models hosted on Mistral
    "zai-glm-5-3",
    "zai-glm-5-3-flash",
    "deepseek-v4-flash-1017",
    # Labs & experimental models
    "labs-mistral-small-creative",
    "labs-devstral-small-2512",
    "labs-leanstral-1-5",
    "labs-leanstral-2603",
}

MISTRAL_LEGACY_ALIASES = {
    # Friendly name -> versioned API model ID
    "mistral-large-4": "mistral-large-4-0",
    "mistral-large-2610": "mistral-large-4-0",
    "mistral-small-4": "mistral-small-2603",
    "mistral-small-4-0-26-03": "mistral-small-2603",
    "mistral-small-latest": "mistral-small-2603",
    "mistral-small-2501": "mistral-small-2603",
    "mistral-medium-3.5": "mistral-medium-2604",
    "mistral-medium-3-5": "mistral-medium-2604",
    "mistral-medium-latest": "mistral-medium-2604",
    "mistral-medium-3.1": "mistral-medium-2604",
    "mistral-large-3": "mistral-large-2512",
    "mistral-large-latest": "mistral-large-2512",
    "mistral-large-2411": "mistral-large-2512",
    "pixtral-large-2411": "pixtral-large-latest",
    "pixtral-12b": "pixtral-12b-2409",
    "codestral-2501": "codestral-latest",
    "codestral-2405": "codestral-latest",
    "ministral-8b-2410": "ministral-8b-latest",
    "ministral-3b-2410": "ministral-3b-latest",
    "mistral-moderation-2411": "mistral-moderation-latest",
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
    "codestral-embed": "codestral-embed-2505",
    "devstral-2": "devstral-2512",
    "pixtral-large": "pixtral-large-latest",
    "magistral-medium-1.2": "mistral-medium-2604",
    "ocr-4-1": "mistral-ocr-4-1",
    "ocr-4": "mistral-ocr-latest",
    "ocr-3": "ocr-3-25-12",
    "mistral-ocr": "mistral-ocr-latest",
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
    if not isinstance(arg, str) or not arg.strip():
        return None
    raw = arg.strip()
    if raw in MISTRAL_MODELS:
        return dict(MISTRAL_MODELS[raw])
    # Check if raw matches any entry's name
    for entry in MISTRAL_MODELS.values():
        if entry.get("name") == raw:
            return dict(entry)
    # M27 (2026-11-07 review): the supported-ids check must run BEFORE the
    # legacy-alias fallback. Several explicitly supported versioned ids
    # (mistral-large-2411, codestral-2501, ...) also appear in the alias
    # table, and the alias consult prefaced the membership check — silently
    # substituting a different model instead of serving the requested id
    # verbatim.
    #
    # Pointer tags are the one intentional exception: "-latest" names that
    # the alias table maps (mistral-{small,medium,large}-latest) are POINTERS
    # by design — the alias entry is the single source of truth for which
    # versioned snapshot the tag currently resolves to, and the catalog/UI
    # contract pins those expansions (e.g. mistral-medium-latest ->
    # mistral-medium-2604). They must therefore expand BEFORE the
    # supported-verbatim branch, which would otherwise return the raw tag
    # as the display name.
    pointing_target = MISTRAL_LEGACY_ALIASES.get(raw) if raw.endswith("-latest") else None
    if pointing_target:
        match = next((v for v in MISTRAL_MODELS.values() if v.get("name") == pointing_target), None)
        if match:
            return dict(match)
        return {"name": pointing_target, "badge": pointing_target}
    if raw in MISTRAL_SUPPORTED_MODELS:
        return {"name": raw, "badge": raw, "label": raw}
    # Check legacy aliases (e.g. "mistral-small-4" -> "mistral-small-2603")
    resolved = MISTRAL_LEGACY_ALIASES.get(raw)
    if resolved:
        match = next((v for v in MISTRAL_MODELS.values() if v.get("name") == resolved), None)
        if match:
            return dict(match)
        return {"name": resolved, "badge": resolved}
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
