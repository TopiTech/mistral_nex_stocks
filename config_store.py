"""
設定ストレージモジュール
config_utils.py から抽出した設定ファイル読み書き関連の関数群

このモジュールは後方互換性を維持するためのファサードです。
実装は `config/` パッケージに関心事ごとに分割されています:
  - config.paths: ランタイムディレクトリとパス解決
  - config.locks: プロセス間ロック
  - config.atomic_write: 原子書き込みヘルパー
  - config.corruption: 破損検出とサニタイズバックアップ
  - config.merge: レガシー設定のマージ・除去
  - config.io: load_config / save_config
  - config.master_key: マスターキー管理

共有状態 (パス定数・ロック・キャッシュ・破損フラグ) はこのモジュールが
所有します。`config/` 側はこれらを `config_store` 属性として参照する
ため、`patch(\"config_store.X\")` 系のテスト隔離は従来どおり機能します。
"""

import logging
import os  # noqa: F401 -- テストが patch.object(config_store.os, ...) で参照する
import threading
from pathlib import Path

from config.atomic_write import (  # noqa: F401
    _durable_write_json,
    _migrate_legacy_runtime_file,
    _safe_write_json,
    _write_and_replace_with_fcntl_lock,
    _write_and_replace_with_lock,
    _write_and_replace_with_msvcrt_lock,
)
from config.corruption import (  # noqa: F401
    _rotate_corrupt_backups,
    _sanitize_and_backup_corrupt_config,
    clear_config_corruption_flag,
    is_config_corrupted,
)
from config.io import _config_cache_key, load_config, save_config  # noqa: F401
from config.locks import _master_key_update_lock, config_update_lock  # noqa: F401
from config.master_key import get_or_create_master_key  # noqa: F401
from config.merge import (  # noqa: F401
    _merge_configs,
    _remove_stale_legacy_config,
    _reset_legacy_merge_flag,
)
from config.paths import _ensure_runtime_dir, _get_runtime_data_dir  # noqa: F401
from crypto_utils import (  # noqa: F401 -- 再エクスポート互換 + patch 対象
    _decode_secret,
    _encode_secret,
    _is_windows,
)

logger = logging.getLogger(__name__)

# --- 定数定義 ---
BASE_DIR = Path(__file__).resolve().parent
LEGACY_CONFIG_FILE = BASE_DIR / "config.json"


APP_DATA_DIR = _get_runtime_data_dir()
CONFIG_FILE = APP_DATA_DIR / "config.json"
USER_STOCKS_FILE = APP_DATA_DIR / "user_stocks.json"
_CONFIG_LOCK = threading.RLock()
_MASTER_KEY_LOCK = threading.Lock()

# プロセス内キャッシュ: load_config() はAIリクエスト等のホットパスから頻繁に呼ばれるため、
# ファイルI/Oとロック取得を抑える。キャッシュはファイルのmtime+sizeでキーされ、
# ファイルが変更/削除されると自動的に無効化される（save_config時は即時クリア）。
_CONFIG_CACHE: dict = {"data": None, "key": None}

# レガシーコンフィグのマージはプロセス起動後に1回のみ実行する。
# save_configによるキャッシュ無効化後も再実行しないことで、不要なファイルI/O（stat）と
# 意図しない設定上書きを防止する。テストでリセットが必要な場合は _reset_legacy_merge_flag() を使用。
_LEGACY_MERGE_DONE: bool = False
_CONFIG_CORRUPTED: bool = False


DEFAULT_CONFIG = {
    "mistral_model": "mistral-medium-3.5",
    "api_credentials": {},
    "custom_ai_prompt": "",
    # "chat" = standard Chat Completions API (model chosen in the app),
    # "agents" = Mistral Agents API (model fixed by the agent in Mistral Console).
    "mistral_api_mode": "chat",
    "mistral_agent_id": "",
    # Keyless demo mode: AI features return built-in sample output.
    "preview_mode": False,
    # LLM provider: "mistral" (standard / optimized) or "openai_compatible" (beta)
    "ai_provider": "mistral",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "gpt-4o-mini",
    "openai_embeddings_model": "text-embedding-3-small",
}


# Keys the legacy workspace config is NEVER allowed to write into the runtime
# config. Secrets/generated tokens are runtime-authoritative: a stale or
# checked-in repo-root config.json must not silently clobber the user's real
# runtime secrets. (REV-02)
_MERGE_PROTECTED_KEYS = frozenset(
    [
        "mns_master_key",
        "extension_api_token",
        "extension_api_token_created",
        "flask_secret_key",
        "api_credentials",
    ]
)

# Non-secret preference keys synced from the legacy config into the runtime
# config. Unlike protected keys (secrets/tokens), these may be overwritten
# when the legacy config is newer. See ``_merge_configs``.
# R4: the only key propagated from the legacy workspace config today is
# ``mistral_model`` — other keys (e.g. custom_ai_prompt) are intentionally
# runtime-only. Keep the allowlist explicit so workspace edits are never
# silently ignored without a log line (see _merge_configs else branch).
_MERGE_SEED_KEYS = ("mistral_model",)
