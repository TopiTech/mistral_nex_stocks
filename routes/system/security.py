"""CSRF トークン発行。`routes.api_system` からの純粋な移動。

`_is_local_request` / `_require_admin_token_if_remote` はテストで
`patch(\"routes.api_system.X\")` されるため、呼び出し時に
`routes.api_system` 属性として解決する。
"""

from __future__ import annotations

from flask import request

import routes.api_system
from error_codes import ErrorCode
from route_helpers import rate_limit
from routes.system.blueprint import api_system_bp
from utils.env_helpers import _env_bool
from utils.networking import is_allowed_trusted_origin
from utils.stock_payload import error_response, ok_response


@api_system_bp.route("/api/csrf-token", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_csrf_token():
    """Issue a fresh CSRF token for long-lived browser sessions.

    The page renders its CSRF token once into ``<meta name=\"csrf-token\">`` at
    load time, but the token expires after ``WTF_CSRF_TIME_LIMIT`` (1h). The
    dashboard is designed to stay open for hours (SSE stream), so a long-running
    tab would otherwise hit CSRF 400 rejections on every mutating request after
    the first hour. The frontend (utils.js) periodically calls this endpoint and
    swaps the fresh token into the meta tag so the tab keeps working without a
    manual reload.

    Security: the token alone is useless without the HttpOnly session cookie
    (SameSite=Strict), the response is not readable cross-origin (no CORS header
    is emitted for foreign origins), and state-changing routes additionally gate
    on local-origin / Sec-Fetch-Site checks. The endpoint itself is still
    local-only, matching /api/metrics and /api/cache-stats.
    """
    if request.method == "OPTIONS":
        return ok_response()
    ok, denied = routes.api_system._require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
    if not allow_remote and not routes.api_system._is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)
    if not allow_remote and request.headers.get("Origin") and not is_allowed_trusted_origin(request):
        return error_response(
            ErrorCode.FORBIDDEN, details={"reason": "untrusted origin"}, status_code=403
        )

    from flask_wtf.csrf import generate_csrf

    return ok_response({"csrf_token": generate_csrf()})
