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
from werkzeug.exceptions import BadRequest

from constants import MAX_JSON_SIZE

logger = logging.getLogger(__name__)


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
    if re.search(r"\s", token):
        return False
    return True



def _parse_json_request():
    """Parse a JSON request body and return an object or None for missing/malformed JSON."""
    content_length = request.content_length
    if content_length and content_length > MAX_JSON_SIZE:
        return None

    try:
        payload = request.get_json(force=False, silent=False)
    except (ValueError, TypeError, AttributeError):
        return None
    except BadRequest:
        return None

    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    return payload



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
    if isinstance(value, bool):
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
