"""リクエストフック (Sec-Fetch-Site/CORS/ログ)。`app` からの純粋な移動。"""

from __future__ import annotations

import logging
import os
import time
import uuid

from flask import Response, current_app, g, request

from error_codes import ErrorCode
from logging_config import DETAILED_API_LOG_PATHS, LOG_LEVEL
from utils.env_helpers import _env_bool
from utils.networking import (
    _is_allowed_shutdown_origin,
    _normalize_origin,
    check_admin_token,
    get_allowed_cors_origins,
)
from utils.text_utils import _short_text

logger = logging.getLogger("app")

# #region Request Hooks

_CROSS_SITE_COSTLY_GET_PATHS = frozenset(
    {
        "/api/indices",
        "/api/stocks",
        "/api/stock-details",
        "/api/stock-history",
        "/api/search",
        "/api/heatmap",
        "/api/trending",
        "/api/screener",
        "/api/stocks/stream",
        "/api/news",
        "/api/ai-portfolio",
    }
)


def _forbidden_cross_site_response() -> tuple[Response, int]:
    """Unified error envelope for blocked cross-site requests (H-3).

    Built through ``error_response`` — the single owner of the error envelope —
    instead of a hand-written ``jsonify`` payload. The import is deferred to
    call time to avoid an import cycle while the app is being wired.
    """
    from utils.stock_payload import error_response

    return error_response(
        ErrorCode.FORBIDDEN, 403, message="forbidden cross-site request"
    )


def _enforce_sec_fetch_site_check():
    """Enforce Sec-Fetch-Site metadata checks to block cross-site request forgery.

    Mutating methods are always protected. A small allowlisted set of GET API
    routes can trigger external work or allocate streaming resources, so those
    routes also reject browser requests explicitly marked ``cross-site``.
    Additionally, POST/DELETE/PATCH/PUT with missing or ``none`` Sec-Fetch-Site
    must carry a trusted Origin (R5 hardening) — otherwise a non-browser
    loopback caller (curl/local malware) could burn LLM quota without the
    browser Origin header.
    """
    is_mutating = request.method in ("POST", "DELETE", "PUT", "PATCH")
    is_costly_get = request.method == "GET" and request.path in _CROSS_SITE_COSTLY_GET_PATHS
    if not is_mutating and not is_costly_get:
        return None

    if request.path == "/api/csp-report":
        return None

    sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    # R5 hardening (opt-in): when MNS_STRICT_SEC_FETCH_SITE=1, a loopback
    # caller presenting Sec-Fetch-Site:none/missing without a trusted Origin
    # and without a CSRF token header must prove same-origin. Default (off)
    # preserves the original permissive local-first contract (REV-03) so
    # personal use and existing integration tests keep passing without
    # Origin. Enable this flag only on deployments that can guarantee the
    # browser always sends Origin on mutating POSTs.
    _csrf_exempt_post_paths = frozenset({"/api/shutdown", "/api/stocks/add_ext", "/api/csp-report"})
    _has_csrf_header = bool(
        request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
    )
    _strict_origin = _env_bool("MNS_STRICT_SEC_FETCH_SITE")
    if (
        _strict_origin
        and is_mutating
        and sec_fetch_site in ("", "none")
        and request.path not in _csrf_exempt_post_paths
        and not _has_csrf_header
        and not _is_allowed_shutdown_origin(request)
    ):
        logger.warning(
            "Block mutating %s without trusted Origin: Sec-Fetch-Site=%s path=%s",
            request.method,
            sec_fetch_site or "(missing)",
            request.path,
        )
        return _forbidden_cross_site_response()
    # Hardening for costly GETs: when strict mode is enabled, a loopback caller
    # presenting Sec-Fetch-Site:none/missing without a trusted Origin must be
    # rejected before it can burn LLM quota or trigger market refreshes via
    # /api/heatmap / /api/trending / /api/stocks etc. Pass-through for
    # same-site / same-origin browser requests.
    if (
        _strict_origin
        and is_costly_get
        and sec_fetch_site in ("", "none")
        and not _is_allowed_shutdown_origin(request)
    ):
        logger.warning(
            "Block costly GET without trusted Origin: Sec-Fetch-Site=%s path=%s",
            sec_fetch_site or "(missing)",
            request.path,
        )
        return _forbidden_cross_site_response()
    # M-7: "cross-site" is the only metadata value that reliably indicates a
    # cross-site request forgery attempt and is blocked.
    # "none" means the request was not initiated by a same-site page context
    # (e.g. direct navigation, bookmark, or a non-browser client such as the
    # native host, curl, or an extension background page issuing fetch). Many
    # legitimate local-only POSTs send Sec-Fetch-Site: none, so blocking it
    # would reject valid same-machine requests. We therefore allow "none" and
    # rely on the trusted-origin / loopback gate for those requests. (REV-03)
    #
    # Cross-site from a TRUSTED origin (chrome-extension://) is also permitted
    # by design: the browser's CORS enforcement already gates these requests,
    # and Flask-WTF's CSRF protection (which runs before this hook) blocks
    # cross-site POSTs to non-exempt endpoints. The three CSRF-exempt POST
    # endpoints (csp-report, shutdown, add_ext) are each protected by their own
    # token mechanism (none / shutdown token / extension Bearer token), so
    # allowing cross-site from a known extension origin does not weaken the
    # security model. (REV-04)
    if sec_fetch_site == "cross-site":
        allowed = _is_allowed_shutdown_origin(request)
        if not allowed:
            # Remote/admin-token mode: allow cross-site from remote origins when
            # the mandatory admin token authenticates the request.
            allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
            admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
            if allow_remote and admin_token:
                provided = request.headers.get("X-MNS-Admin-Token", "").strip()
                if check_admin_token(provided):
                    return None
            logger.warning(
                "Block cross-site request to %s: Origin/Referer not allowed. Sec-Fetch-Site=%s",
                request.path,
                sec_fetch_site,
            )
            return _forbidden_cross_site_response()


def _log_request_start():
    """Log the start of an incoming request with a unique request ID."""
    g.request_start_ts = time.time()
    g.request_id = uuid.uuid4().hex[:10]

    if LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        from utils.networking import mask_sensitive_url

        current_app.logger.info(
            "REQ start id=%s method=%s path=%s remote=%s origin=%s ua=%s",
            g.request_id,
            request.method,
            mask_sensitive_url(request.full_path),
            request.remote_addr,
            _short_text(request.headers.get("Origin"), 80),
            _short_text(request.headers.get("User-Agent"), 120),
        )


def add_extension_cors_headers(response):
    """Inject CORS and security headers into outgoing responses."""
    # Match the origin normalization used by the shutdown / Sec-Fetch-Site gate
    # (_normalize_origin): ``http://127.0.0.1:PORT`` and ``http://localhost:PORT``
    # are treated as the same allowed origin. The response header still echoes
    # the RAW request origin so the browser's exact-match check succeeds.
    allowed_origins = get_allowed_cors_origins()
    raw_origin = (request.headers.get("Origin") or "").strip()
    normalized_origin = _normalize_origin(raw_origin)
    if normalized_origin and normalized_origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = raw_origin

    vary_values = [v.strip() for v in str(response.headers.get("Vary", "")).split(",") if v.strip()]
    if "origin" not in {v.lower() for v in vary_values}:
        vary_values.append("Origin")
    response.headers["Vary"] = ", ".join(vary_values) if vary_values else "Origin"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-CSRFToken, X-CSRF-Token, X-MNS-Shutdown-Token, "
        "X-MNS-Admin-Token, X-MNS-Extension-Request, Authorization"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Access-Control-Max-Age"] = "600"

    # M-2: Reporting-Endpoints header for CSP Level 3 report-to directive.
    # Injected here (alongside the CSP report-uri fallback) so that both
    # old and new browser CSP reporting modes are covered by a single
    # after_request handler rather than split across registrations.
    response.headers["Reporting-Endpoints"] = 'csp-endpoint="/api/csp-report"'

    req_id = getattr(g, "request_id", "-")
    response.headers["X-MNS-Request-Id"] = req_id
    response.headers["Access-Control-Expose-Headers"] = "X-MNS-Request-Id"

    started = getattr(g, "request_start_ts", None)
    elapsed_ms = int((time.time() - started) * 1000) if isinstance(started, (int, float)) else -1
    status_code = int(response.status_code or 0)
    from utils.networking import mask_sensitive_url

    _masked_path = mask_sensitive_url(request.full_path)
    if status_code >= 400:
        if status_code == 404:
            # 頻繁に発生し、かつ対応不要な404エラーをINFOレベルに下げる（本番ログのノイズ防止）
            ignored_404_paths = (
                "favicon.ico",
                "apple-touch-icon.png",
                "apple-touch-icon-precomposed.png",
                "robots.txt",
                ".map",
                "com.chrome.devtools.json",
            )
            if any(path in _masked_path for path in ignored_404_paths):
                logger.info(
                    "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
                    req_id,
                    request.method,
                    _masked_path,
                    status_code,
                    elapsed_ms,
                )
            else:
                logger.warning(
                    "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
                    req_id,
                    request.method,
                    _masked_path,
                    status_code,
                    elapsed_ms,
                )
        else:
            logger.warning(
                "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
                req_id,
                request.method,
                _masked_path,
                status_code,
                elapsed_ms,
            )
    elif LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        logger.info(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            _masked_path,
            status_code,
            elapsed_ms,
        )

    return response


# #endregion
