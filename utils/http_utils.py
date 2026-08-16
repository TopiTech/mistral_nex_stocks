"""
utils/http_utils.py - HTTP request/response helper utilities.
"""

import math
import time
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
            return _clamp_retry_after(dt.timestamp() - time.time())
        except Exception:
            return None
