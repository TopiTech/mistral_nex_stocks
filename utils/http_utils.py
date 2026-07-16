"""
utils/http_utils.py - HTTP request/response helper utilities.
"""

import time
from typing import Any, Optional
from email.utils import parsedate_to_datetime


def parse_retry_after(resp_or_exc: Any) -> Optional[float]:
    """Parse a Retry-After header (seconds or HTTP-date) from a response or an exception.

    Args:
        resp_or_exc: A response object (having headers) or an exception object
                     (potentially containing a response attribute).

    Returns:
        The retry delay in seconds as a float, or None if not found/invalid.
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
        return float(raw)
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(raw))
            return max(0.0, dt.timestamp() - time.time())
        except Exception:
            return None
