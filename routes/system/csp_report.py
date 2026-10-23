"""CSP レポート受信。`routes.api_system` からの純粋な移動。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from flask import current_app
from werkzeug.exceptions import BadRequest

from constants import MAX_JSON_SIZE
from route_helpers import rate_limit
from routes.system.blueprint import api_system_bp
from utils.text_utils import _read_bounded_request_body

_CSP_LOG_URI_FIELDS = frozenset({"document-uri", "blocked-uri", "source-file", "referrer"})
_CSP_LOG_TOKEN_FIELDS = frozenset({"violated-directive", "effective-directive", "disposition"})
_CSP_LOG_NUMERIC_FIELDS = frozenset({"line-number", "column-number", "status-code"})
_CSP_OPAQUE_URI_VALUES = frozenset({"inline", "eval", "wasm-eval", "self", "none"})


def _clean_csp_log_text(value: Any, max_chars: int) -> str | None:
    """Bound untrusted CSP text and remove all control characters for logs."""
    if not isinstance(value, str):
        return None
    return "".join(char for char in value[:max_chars] if ord(char) >= 0x20 and ord(char) != 0x7F)


def _csp_origin_for_log(value: Any) -> str | None:
    """Keep at most an origin/scheme from an untrusted CSP URI field.

    CSP reports may contain full document or blocked URLs.  Query strings,
    fragments, credentials, paths, and inline samples can hold user data, so
    they do not belong in application logs.
    """
    uri = _clean_csp_log_text(value, 2048)
    if not uri:
        return None
    try:
        parsed = urlsplit(uri)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if host:
            host = host.lower()
            port = parsed.port
            authority = f"{host}:{port}" if port is not None else host
            return f"{scheme}://{authority}" if scheme else authority
        if scheme:
            return f"{scheme}:"
    except ValueError:
        return "invalid-uri"
    return uri if uri.lower() in _CSP_OPAQUE_URI_VALUES else "relative-or-opaque"


def _sanitize_csp_report_for_log(report: dict[str, Any]) -> dict[str, Any]:
    """Produce compact CSP diagnostics without logging report-controlled content."""
    sanitized: dict[str, Any] = {}
    for key, value in report.items():
        if key in _CSP_LOG_URI_FIELDS:
            safe_uri = _csp_origin_for_log(value)
            if safe_uri is not None:
                sanitized[key] = safe_uri
        elif key in _CSP_LOG_TOKEN_FIELDS:
            token = _clean_csp_log_text(value, 80)
            if token and all(char.isalnum() or char in "-_" for char in token):
                sanitized[key] = token
            elif token:
                sanitized[key] = "invalid"
        elif key in _CSP_LOG_NUMERIC_FIELDS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                sanitized[key] = value
    return sanitized


@api_system_bp.route("/api/csp-report", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def api_csp_report():
    """CSP report receiver for Report-Only mode (accepts JSON POST)."""
    try:
        # Browsers use application/csp-report and application/reports+json,
        # neither of which is guaranteed to satisfy Flask's is_json check.
        # Bound the raw body first, then parse it independently of the MIME
        # type so this unauthenticated endpoint shares the 1 MiB JSON limit.
        body = _read_bounded_request_body(max_size=MAX_JSON_SIZE)
        payload = json.loads(body) if body else {}
        if (
            isinstance(payload, dict)
            and "csp-report" in payload
            and isinstance(payload["csp-report"], dict)
        ):
            payload = payload["csp-report"]
        elif isinstance(payload, list):
            reports = [
                item.get("body", item) if isinstance(item, dict) else {}
                for item in payload
                if isinstance(item, dict)
            ]
            payload = reports
        if not isinstance(payload, dict):
            payload = payload if isinstance(payload, list) else [{}]
        modern_key_map = {
            "documentURL": "document-uri",
            "blockedURL": "blocked-uri",
            "effectiveDirective": "effective-directive",
            "originalPolicy": "original-policy",
            "lineNumber": "line-number",
            "columnNumber": "column-number",
            "sourceFile": "source-file",
            "statusCode": "status-code",
            "sample": "script-sample",
        }
        payloads = payload if isinstance(payload, list) else [payload]
        payloads = [
            {modern_key_map.get(key, key): value for key, value in item.items()}
            for item in payloads
            if isinstance(item, dict)
        ]
        sanitized_reports = [_sanitize_csp_report_for_log(report) for report in payloads[:20]]
        current_app.logger.info(
            "CSP report received: %s",
            json.dumps(sanitized_reports, ensure_ascii=False)[:2000],
        )
    except (BadRequest, RecursionError, TypeError, ValueError) as exc:
        current_app.logger.debug("Failed to parse CSP report error_type=%s", type(exc).__name__)
    # Return 204 No Content as recommended for CSP reports
    return ("", 204)
