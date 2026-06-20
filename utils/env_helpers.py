"""
Environment variable access helpers.
Provides safe retrieval of integers and floats from environment variables with defaults and bounds.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _env_int(
    name: str,
    default: int,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    """Read an integer environment variable with bounds and safe fallback."""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "Invalid integer env %s=%r; using default %s", name, raw, default
        )
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(
    name: str,
    default: float,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> float:
    """Read a float environment variable with bounds and safe fallback."""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "Invalid float env %s=%r; using default %s", name, raw, default
        )
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value
