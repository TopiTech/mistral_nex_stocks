"""
text_utils.py - Text sanitization, token formatting, and JSON parsing utilities.

Extracted from app_helpers.py to reduce module complexity.
All functions are pure (no app_state dependency).
"""

import hashlib
import json
import logging
import math
import numbers
import re
from typing import Any

from flask import request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

from constants import MAX_JSON_SIZE
from utils.normalization import is_bool_like

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
    # Reject control characters as well as whitespace: a NUL cannot be placed
    # in os.environ and would otherwise fail after the config was committed.
    return not re.search(r"[\s\x00-\x1f\x7f]", token)


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
        # Let Flask's 413 handler produce the canonical PAYLOAD_TOO_LARGE
        # envelope instead of every route collapsing this into MALFORMED_INPUT.
        raise RequestEntityTooLarge()

    try:
        # Flask/Werkzeug enforce this limit for WSGI streams marked as
        # terminated (including chunked bodies).  The extra byte is deliberate:
        # a LimitedStream does not raise after returning exactly its limit.
        request.max_content_length = max_size + 1
        body = request.get_data(cache=True)
    except RequestEntityTooLarge:
        # R17-03: RequestEntityTooLarge is a BadRequest subclass; catching it
        # as BadRequest here turns a 413 into a 400 MALFORMED_INPUT and
        # bypasses the canonical 413 handler. Re-raise it.
        raise
    except (BadRequest, ValueError, TypeError, OSError):
        return None
    except Exception:
        return None

    if len(body) > max_size:
        raise RequestEntityTooLarge()
    return body


# Lone UTF-16 surrogates (U+D800..U+DFFF) are not Unicode scalar values, so they
# cannot be encoded as UTF-8. ``json.loads`` still accepts them from a
# ``"\ud800"`` escape, which means a JSON request body can deliver a ``str``
# that every later ``text.encode("utf-8")`` rejects (cache-key sanitising,
# token digests, Fernet encryption, SQLite text binding). That turned a
# malformed client body into an unhandled UnicodeEncodeError / HTTP 500.
# No browser input can produce one, so the single JSON boundary rejects them.
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _contains_lone_surrogate(payload: object) -> bool:
    """Return ``True`` when *payload* holds a lone surrogate anywhere.

    Walks dict keys, values and nested lists iteratively so that a deeply
    nested body cannot exhaust the interpreter stack while it is being
    validated.
    """
    stack: list[object] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            # ``str.isascii()`` is O(1) for the compact strings ``json.loads``
            # produces, so the common case (ASCII JSON, multi-MB base64 image
            # bodies) never reaches the regex scan.
            if not item.isascii() and _LONE_SURROGATE_RE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def _contains_nested_non_finite_number(payload: object) -> bool:
    """Return ``True`` for NaN/Infinity inside nested arbitrary structures.

    ``json.loads`` accepts the non-standard ``NaN``/``Infinity`` literals even
    though JSON and the browser's ``JSON.stringify`` do not.  A client can
    therefore inject values into untyped containers (``chart_data``,
    ``history_data``, ``portfolio``) that later fail
    ``json.dumps(..., allow_nan=False)`` in the portfolio/SSE paths.

    Direct scalar fields of the root object are deliberately left to the
    endpoint's Pydantic schema: those fields own the historical error code and
    message (for example ``price must be a finite non-negative number``), and
    rejecting them earlier would turn ``INVALID_INPUT`` into
    ``MALFORMED_INPUT``.  Every value inside a nested list/dict has no typed
    schema owner, so the single JSON boundary rejects it here.

    Booleans are not numbers here (``bool`` is an ``int`` subclass), and the
    walk is iterative so a deeply nested body cannot exhaust the stack.
    """
    stack: list[tuple[object, int]] = [(payload, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, float):
            # depth 1 = a direct field of the root object (schema-owned).
            if depth > 1 and not math.isfinite(item):
                return True
        elif isinstance(item, dict):
            stack.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((value, depth + 1) for value in item)
    return False


def _parse_json_request(*, max_size: int = MAX_JSON_SIZE) -> dict | None:
    """Parse a bounded JSON-object request body safely.

    Rejects payloads larger than ``max_size`` (1 MiB by default), including
    chunked requests without a ``Content-Length`` header.  The raw body is
    cached before JSON decoding so a bounded read, rather than a client-supplied
    header, is authoritative.  Lone UTF-16 surrogates and non-standard
    ``NaN``/``Infinity`` values inside untyped nested containers are rejected
    because they cannot be represented by standards-compliant JSON/UTF-8
    consumers; direct scalar fields remain schema-owned so endpoint-specific
    validation error codes are preserved.

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
    except (BadRequest, UnsupportedMediaType):
        return None
    except Exception:
        return None

    if payload is None or not isinstance(payload, dict):
        return None
    if _contains_lone_surrogate(payload) or _contains_nested_non_finite_number(payload):
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
    except RequestEntityTooLarge:
        raise
    except (ValueError, TypeError, OSError):
        return None


def _sanitize_error_message(error_msg):
    """Remove sensitive information (API keys, tokens, passwords) from error messages."""
    if not error_msg:
        return ""
    sensitive_patterns = [
        r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"authorization['\"]?\s*[:=]\s*['\"]?(?:(?:bearer|basic|token|digest)\s+)?[^\s'\"]+",
        r"bearer\s+[a-z0-9\._\-]{10,}",
        r"https?://[^\s/'\"@]+:[^\s/'\"@]+@",
        r"secret['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"\bsk-[a-zA-Z0-9_\-]{20,}\b",
        r"\btvly-[a-zA-Z0-9_\-]{20,}\b",
    ]
    sanitized = str(error_msg)
    for pattern in sensitive_patterns:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


_SENSITIVE_LOG_KEY_RE = re.compile(
    r"^(?:.*_)?(?:api_key|apikey|secret|client_secret|password|passwd|authorization|"
    r"auth_token|bearer_token|access_token|refresh_token|admin_token|shutdown_token|"
    r"extension_token|csrf_token|master_key|private_key|token)$"
)
_NON_SECRET_KEY_PREFIXES = ("has_", "is_", "use_", "enable_", "allow_", "num_", "count_", "max_")


def _is_sensitive_log_key(key: Any) -> bool:
    """Return True when a structured log key holds a credential/secret rather than metadata."""
    normalized = str(key).strip().lower().replace("-", "_")
    if not normalized or normalized == "request_token":
        return False
    if normalized.startswith(_NON_SECRET_KEY_PREFIXES) or normalized.endswith(("_count", "_length", "_len")):
        return False
    return bool(_SENSITIVE_LOG_KEY_RE.match(normalized))


def sanitize_log_record_data(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Recursively sanitize structured log values BEFORE JSON serialization.

    Redacts secret-bearing dict keys in-place-safe copies and applies
    ``_sanitize_error_message`` to string leaves without corrupting JSON delimiters.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_error_message(value)
    if _seen is None:
        _seen = set()
    obj_id = id(value)
    if isinstance(value, dict):
        if obj_id in _seen:
            return "[circular]"
        _seen.add(obj_id)
        try:
            sanitized_dict: dict[Any, Any] = {}
            for k, v in value.items():
                if _is_sensitive_log_key(k):
                    if isinstance(v, (bool, int, float)) or v is None:
                        sanitized_dict[k] = v
                    elif isinstance(v, str):
                        sanitized_dict[k] = "[REDACTED]" if v else ""
                    else:
                        sanitized_dict[k] = "[REDACTED]"
                else:
                    sanitized_dict[k] = sanitize_log_record_data(v, _seen=_seen)
            return sanitized_dict
        finally:
            _seen.discard(obj_id)
    if isinstance(value, (list, tuple)):
        if obj_id in _seen:
            return "[circular]"
        _seen.add(obj_id)
        try:
            items = [sanitize_log_record_data(item, _seen=_seen) for item in value]
            return tuple(items) if isinstance(value, tuple) else items
        finally:
            _seen.discard(obj_id)
    return _sanitize_error_message(str(value))


def parse_non_negative_float(value, field_name, max_value=None):
    """Safely parse a number and ensure it is non-negative and finite."""
    if is_bool_like(value):
        raise ValueError(f"{field_name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field_name} must be <= {max_value}")
    return parsed


def sanitize_prompt_text(value: Any, max_len: int = 120) -> str:
    """Strip control/XML metacharacters from client-influenced values before LLM prompt interpolation."""
    text = str(value if value is not None else "")
    text = "".join(c for c in text if ord(c) >= 0x20 or c in ("\t", "\n", "\r"))
    text = text.replace("<", " ").replace(">", " ")
    return text.strip()[:max_len]


def json_safe_tool_value(value: Any) -> Any:
    """Convert provider/tool values into JSON-safe types before entering an LLM prompt."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool) or type(value).__name__ in ("bool_", "bool"):
        return bool(value)
    if hasattr(value, "tolist") and callable(value.tolist):
        try:
            return json_safe_tool_value(value.tolist())
        except Exception:
            pass
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return json_safe_tool_value(value.to_dict())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): json_safe_tool_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_tool_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe_tool_value(item) for item in sorted(value, key=str)]
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        val_f = float(value)
        return val_f if math.isfinite(val_f) else None
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return str(value)[:2_000]
