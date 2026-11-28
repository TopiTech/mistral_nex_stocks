"""AI 利用統計。`routes.api_system` からの純粋な移動。

`_is_local_request` / `_require_admin_token_if_remote` はテストで
`patch(\"routes.api_system.X\")` されるため、呼び出し時に
`routes.api_system` 属性として解決する。
"""

from __future__ import annotations

from flask import request

import routes.api_system
from app_state import app_state
from credential_manager import get_ai_provider
from route_helpers import rate_limit
from routes.system.blueprint import api_system_bp
from routes.system.guards import _check_system_read_origin_and_locality
from utils.stock_payload import ok_response


@api_system_bp.route("/api/system/ai-usage", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def get_ai_usage():
    """Return aggregated AI token usage statistics and cost estimates."""
    if request.method == "OPTIONS":
        return ok_response()
    ok, denied = routes.api_system._require_admin_token_if_remote(request)
    if not ok:
        return denied
    denied_access = _check_system_read_origin_and_locality(request)
    if denied_access is not None:
        return denied_access
    cfg = (
        app_state.config
        if hasattr(app_state, "config") and isinstance(app_state.config, dict)
        else {}
    )
    raw_provider = cfg.get("ai_provider") or get_ai_provider()
    active_provider = "openai_compatible" if raw_provider == "openai_compatible" else "mistral"
    if active_provider == "openai_compatible":
        stats = app_state.ai.openai_usage_stats()
    else:
        stats = app_state.ai.mistral_usage_stats()
    providers_stats = app_state.ai.ai_usage_stats()
    return ok_response({
        "usage": stats,
        "active_provider": active_provider,
        "providers": providers_stats,
    })
