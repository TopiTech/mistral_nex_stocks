"""管理トークンゲート。`routes.api_system` からの純粋な移動。"""

from __future__ import annotations

import os

from error_codes import ErrorCode
from utils.env_helpers import _env_bool
from utils.networking import check_admin_token
from utils.stock_payload import error_response


def _require_admin_token_if_remote(request_obj):
    """Require the admin token when the app is exposed beyond loopback or when configured."""
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    if allow_remote and len(admin_token) < 32:
        return False, error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "admin token required for remote mode"},
            status_code=403,
        )

    if not admin_token:
        return True, None

    provided_token = request_obj.headers.get("X-MNS-Admin-Token", "").strip()
    if not check_admin_token(provided_token):
        return False, error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "invalid admin token"},
            status_code=403,
        )
    return True, None
