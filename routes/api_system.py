"""システム系エンドポイント (Blueprint + ハンドラ)。

このモジュールは後方互換性を維持するためのファサードです。
実装は `routes/system/` パッケージに関心事ごとに分割されています:
  - routes.system.blueprint: `api_system_bp`
  - routes.system.guards: 管理トークンゲート
  - routes.system.credentials: 資格情報管理
  - routes.system.observe: health/cache-stats/metrics
  - routes.system.security: CSRF トークン発行
  - routes.system.csp_report: CSP レポート受信
  - routes.system.shutdown: シャットダウン
  - routes.system.ai_usage: AI 利用統計

テストで `patch(\"routes.api_system.X\")` される名前と `os` / `logging` /
`app_state` 属性はこのモジュールが所有し、`routes/system/` 側は呼び出し時に
属性解決するため、モック・隔離は従来どおり機能します。URL 契約
(endpoint `api_system.*`) は不変です。
"""

import logging  # noqa: F401 -- patch 対象 (`routes.api_system.logging`)
import os  # noqa: F401 -- patch 対象 (`routes.api_system.os`)

from app_state import app_state  # noqa: F401 -- 属性チェーン参照互換
from credential_manager import (  # noqa: F401 -- 再エクスポート互換 + patch 対象
    clear_api_credentials,
    get_api_credential_state,
    get_custom_ai_prompt,
    save_api_credentials,
)
from routes.system.ai_usage import get_ai_usage  # noqa: F401
from routes.system.blueprint import api_system_bp  # noqa: F401
from routes.system.credentials import (  # noqa: F401
    _build_safe_credentials_response,
    api_credentials,
    api_credentials_verify,
)
from routes.system.csp_report import (  # noqa: F401
    _CSP_LOG_NUMERIC_FIELDS,
    _CSP_LOG_TOKEN_FIELDS,
    _CSP_LOG_URI_FIELDS,
    _CSP_OPAQUE_URI_VALUES,
    _clean_csp_log_text,
    _csp_origin_for_log,
    _sanitize_csp_report_for_log,
    api_csp_report,
)
from routes.system.guards import _require_admin_token_if_remote  # noqa: F401
from routes.system.observe import api_cache_stats, api_health, api_metrics  # noqa: F401
from routes.system.security import api_csrf_token  # noqa: F401
from routes.system.shutdown import _terminate_current_process, api_shutdown  # noqa: F401
from utils.networking import _is_local_request  # noqa: F401 -- patch 対象
