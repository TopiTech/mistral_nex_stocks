"""システム系エンドポイントの関心事ごとの分割モジュール群。

公開 API (`api_system_bp` とハンドラ) は `routes.system` および後方互換ファサード `routes.api_system` 経由で提供
される。
"""

from __future__ import annotations

from routes.system.ai_usage import get_ai_usage
from routes.system.blueprint import api_system_bp
from routes.system.credentials import (
    api_credentials,
    api_credentials_verify,
)
from routes.system.csp_report import api_csp_report
from routes.system.observe import api_cache_stats, api_health, api_metrics
from routes.system.security import api_csrf_token
from routes.system.shutdown import api_shutdown

__all__ = [
    "api_cache_stats",
    "api_credentials",
    "api_credentials_verify",
    "api_csp_report",
    "api_csrf_token",
    "api_health",
    "api_metrics",
    "api_shutdown",
    "api_system_bp",
    "get_ai_usage",
]
