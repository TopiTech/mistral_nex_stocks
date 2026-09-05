"""
text_utils.py - Text sanitization, token formatting, and JSON parsing utilities.

Extracted from app_helpers.py to reduce module complexity.
All functions are pure (no app_state dependency).
"""

import hashlib
import logging
import math
import re

from flask import request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

from constants import MAX_JSON_SIZE

logger = logging.getLogger(__name__)


def sanitize_cdata(text: str | None) -> str:
    """Escape the only delimiter that can terminate an XML CDATA section."""
    if not text:
        return "データなし"
    return text.replace("]]>", "]]]]><![CDATA[>")


def wrap_cdata(text: str | None) -> str:
    """Wrap untrusted text in a CDATA section without permitting breakout."""
    return f"<![CDATA[{sanitize_cdata(text)}]]>"


def _short_text(value, limit=160):
    """Truncate text to a limit with ellipsis.

    Strips C0 control characters (0x00-0x1F, 0x7F) to prevent log injection /
    forging via crafted header values containing CR/LF/TAB etc.
    """
    text = str(value or "")
    text = "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 127)
    text = text.strip()
    return text if len(text) <= limit else (text[:limit] + "...")


def _token_fingerprint(token):
    """Generate a safe SHA-256 fingerprint of a token.

    Never reveals the full token, only the first 16 hex characters of its hash.
    """
    t = (token or "").strip()
    if not t:
        return "none"
    digest = hashlib.sha256(t.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"sha256={digest}"


def _token_mask(token):
    """Mask a token showing only the first and last 2 characters."""
    t = (token or "").strip()
    if not t:
        return "none"
    if len(t) <= 4:
        return "*" * len(t)
    return f"{t[:2]}...{t[-2:]}"


def _is_valid_api_key(value, min_length=8):
    """Validate API key format for minimum length and no whitespace."""
    if not value or not isinstance(value, str):
        return False
    token = value.strip()
    if len(token) < min_length:
        return False
    return not re.search(r"\s", token)


def _read_bounded_request_body(*, max_size: int) -> bytes | None:
    """Read at most one byte beyond a request-body limit.

    ``Content-Length`` is optional for streaming (for example, chunked)
    requests, so it cannot be the only size check.  Read one sentinel byte past
    the accepted limit and reject it when present.  This lets the parser
    distinguish a body exactly at the limit from a body that Flask would
    otherwise truncate at that boundary before JSON decoding.
    """
    content_length = request.content_length
    if content_length is not None and content_length > max_size:
        return None

    try:
        # Flask/Werkzeug enforce this limit for WSGI streams marked as
        # terminated (including chunked bodies).  The extra byte is deliberate:
        # a LimitedStream does not raise after returning exactly its limit.
        request.max_content_length = max_size + 1
        body = request.get_data(cache=True)
    except (BadRequest, RequestEntityTooLarge, ValueError, TypeError, OSError):
        return None
    except Exception:
        return None

    return body if len(body) <= max_size else None


def _parse_json_request(*, max_size: int = MAX_JSON_SIZE) -> dict | None:
    """Parse a bounded JSON-object request body safely.

    Rejects payloads larger than ``max_size`` (1 MiB by default), including
    chunked requests without a ``Content-Length`` header.  The raw body is
    cached before JSON decoding so a bounded read, rather than a client-supplied
    header, is authoritative.

    Args:
        max_size: Maximum JSON body size in bytes. Only endpoints with an
            explicitly larger, bounded request limit should override it.
    """
    if _read_bounded_request_body(max_size=max_size) is None:
        return None

    try:
        payload = request.get_json(force=False, silent=False)
    except (ValueError, TypeError, AttributeError):
        return None
    except (BadRequest, UnsupportedMediaType, RequestEntityTooLarge):
        return None
    except Exception:
        return None

    if payload is None or not isinstance(payload, dict):
        return None
    return payload


def _parse_optional_json_request(*, max_size: int = MAX_JSON_SIZE) -> dict | None:
    """Parse an optional JSON-object body without accepting malformed input.

    Some endpoints intentionally support an empty POST body and use server-side
    defaults. ``_parse_json_request() or {}`` is unsafe for those endpoints:
    it also turns invalid JSON (or a JSON array) into an empty object, which
    can trigger an expensive operation with saved credentials.

    Return an empty object only when the request body is genuinely empty.
    Return ``None`` for malformed, non-object, or over-limit bodies so callers
    can consistently respond with a client error.
    """
    payload = _parse_json_request(max_size=max_size)
    if payload is not None:
        return payload

    # Avoid reading a known non-empty body a second time. For a chunked body
    # (no Content-Length), get_data(cache=True) preserves Flask's request cache
    # while distinguishing an absent body from invalid JSON.
    if request.content_length not in (None, 0):
        return None
    try:
        return {} if not request.get_data(cache=True) else None
    except (RequestEntityTooLarge, ValueError, TypeError, OSError):
        return None


def _sanitize_error_message(error_msg):
    """Remove sensitive information (API keys, tokens, passwords) from error messages."""
    if not error_msg:
        return ""
    sensitive_patterns = [
        r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"authorization['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"bearer\s+[a-z0-9\._\-]{10,}",
        r"https?://[a-z0-9]+:[a-z0-9]+@",
        r"secret['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
    ]
    sanitized = str(error_msg)
    for pattern in sensitive_patterns:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


def parse_non_negative_float(value, field_name, max_value=None):
    """Safely parse a number and ensure it is non-negative and finite."""
    if isinstance(value, bool) or type(value).__name__ in ("bool_", "bool"):
        raise ValueError(f"{field_name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field_name} must be <= {max_value}")
    return parsed
