"""
utils/http_utils.py - HTTP request/response helper utilities.
"""

import math
import re
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

# Maximum allowed Retry-After value (24 hours). Values beyond this are clamped
# to prevent overflow errors (int(inf) / int(huge)) and accidental permanent
# exclusion windows from malformed or malicious Retry-After headers.
_MAX_RETRY_AFTER_SEC: float = 86400.0


def _clamp_retry_after(value: float) -> float | None:
    """Validate and clamp a parsed Retry-After value.

    Returns ``None`` for non-finite values (inf/NaN), and clamps negative
    values to 0.0 and excessively large values to ``_MAX_RETRY_AFTER_SEC``.
    """
    if not math.isfinite(value):
        return None
    if value < 0.0:
        return 0.0
    if value > _MAX_RETRY_AFTER_SEC:
        return _MAX_RETRY_AFTER_SEC
    return value


def parse_retry_after(resp_or_exc: Any) -> float | None:
    """Parse a Retry-After header (seconds or HTTP-date) from a response or an exception.

    Args:
        resp_or_exc: A response object (having headers) or an exception object
                     (potentially containing a response attribute).

    Returns:
        The retry delay in seconds as a float, or None if not found/invalid.
        Non-finite values (inf/NaN), negative values, and values exceeding
        ``_MAX_RETRY_AFTER_SEC`` (24h) are clamped or rejected.
    """
    if resp_or_exc is None:
        return None

    # Resolve actual response object
    resp = resp_or_exc
    if hasattr(resp_or_exc, "response"):
        try:
            resp = getattr(resp_or_exc, "response", None)
        except Exception:
            resp = None

    if resp is None:
        return None

    # Resolve headers
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None

    raw = None
    if isinstance(headers, dict):
        raw = headers.get("Retry-After") or headers.get("retry-after")
    else:
        # e.g., requests.structures.CaseInsensitiveDict or email Message
        get = getattr(headers, "get", None)
        if get is not None:
            raw = get("Retry-After") or get("retry-after")

    if not raw:
        return None

    try:
        return _clamp_retry_after(float(raw))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return _clamp_retry_after(dt.timestamp() - time.time())
        except Exception:
            return None


def extract_external_error_metadata(exc: BaseException | None) -> tuple[str, str]:
    """Return safe diagnostics for an external-provider exception.

    Error text and response bodies are controlled by a remote service and can
    reflect request data or credentials, so only expose the exception class and
    HTTP status.
    """
    if exc is None:
        return "UnknownError", "unknown"
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return type(exc).__name__, str(status) if status is not None else "unknown"


def close_response_quietly(resp: Any) -> None:
    """Close an HTTP response object without raising or masking caller errors."""
    close_fn = getattr(resp, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*['"]?\s*([A-Za-z0-9._-]+)""",
    re.IGNORECASE,
)


def decode_http_response_text(resp: Any, *, default_encoding: str = "utf-8") -> str:
    """Decode an HTTP response body without triggering requests' ISO-8859-1 fallback.

    When HTTP servers omit ``charset=`` in ``Content-Type: text/html`` or
    ``text/xml``, ``requests`` defaults ``response.encoding`` to ``ISO-8859-1``,
    corrupting UTF-8/Shift_JIS/EUC-JP pages before parsers see them. Prefer
    raw ``content`` bytes when present (honoring explicit HTTP charset or
    HTML ``<meta charset>``), falling back to ``response.text`` only for mock
    objects that expose text without raw bytes.
    """
    if resp is None:
        return ""

    raw = getattr(resp, "content", None)
    if isinstance(raw, (bytes, bytearray)) and raw:
        raw_bytes = bytes(raw)
        candidates: list[str] = []

        headers = getattr(resp, "headers", None)
        content_type = None
        if isinstance(headers, dict) or (
            headers is not None and callable(getattr(headers, "get", None))
        ):
            content_type = headers.get("Content-Type") or headers.get("content-type")
        if isinstance(content_type, str):
            m_hdr = _CHARSET_RE.search(content_type)
            if m_hdr:
                candidates.append(m_hdr.group(1))

        resp_enc = getattr(resp, "encoding", None)
        if (
            isinstance(resp_enc, str)
            and resp_enc.strip()
            and resp_enc.strip().lower() not in ("iso-8859-1", "latin-1", "latin1")
        ):
            candidates.append(resp_enc.strip())

        m_meta = _META_CHARSET_RE.search(raw_bytes[:4096])
        if m_meta:
            try:
                candidates.append(m_meta.group(1).decode("ascii"))
            except UnicodeDecodeError:
                pass

        for enc in candidates:
            try:
                return raw_bytes.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue

        # Try strict UTF-8 before apparent_encoding so clean UTF-8 pages succeed fast.
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            pass

        apparent = getattr(resp, "apparent_encoding", None)
        if isinstance(apparent, str) and apparent.strip():
            try:
                return raw_bytes.decode(apparent.strip())
            except (LookupError, UnicodeDecodeError):
                pass

        for fallback_enc in (default_encoding, "cp932", "euc_jp", "utf-8"):
            if not fallback_enc:
                continue
            try:
                return raw_bytes.decode(fallback_enc)
            except (LookupError, UnicodeDecodeError):
                continue

        return raw_bytes.decode(default_encoding or "utf-8", errors="replace")

    text = getattr(resp, "text", None)
    return text if isinstance(text, str) else ""
