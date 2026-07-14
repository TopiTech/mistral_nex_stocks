"""
Environment variable access helpers.
Provides safe retrieval of integers and floats from environment variables
with defaults and bounds.
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
        logger.warning("Invalid float env %s=%r; using default %s", name, raw, default)
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _is_testing() -> bool:
    """Check if the application is running in a test environment.

    Single source of truth for test environment detection, used across
    multiple modules (stock_provider, session_manager, etc.) to avoid
    hard-to-discover duplicate patterns like:

        import sys
        is_testing = "pytest" in sys.modules or "unittest" in sys.modules

    All modules should call ``_is_testing()`` instead of checking
    ``sys.modules`` directly. This function is intentionally kept in
    ``utils.env_helpers`` (not in a test helper) so that production code
    paths can use it without circular imports.

    Returns:
        True if pytest or unittest is currently loaded (i.e., we are inside
        a test runner).
    """
    import sys
    return "pytest" in sys.modules or "unittest" in sys.modules


def _is_production_env() -> bool:
    """Check if the application is running in a production environment.

    Single source of truth for production environment detection used across
    app.py, security_config.py, and other modules.

    H-4: A remote/reverse-proxy deployment (MNS_ALLOW_REMOTE_API=1 with
    MNS_PROXY_FIX=1) is treated as production-equivalent for transport
    security: it exposes the API beyond loopback, so it must not run with
    auto-generated plaintext-stored secrets, plaintext cookies, or absent HSTS.
    """
    if os.environ.get("MNS_PROD", "").lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("MNS_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
        return True
    return (
        os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in ("1", "true", "yes")
        and os.environ.get("MNS_PROXY_FIX", "").strip().lower() in ("1", "true", "yes")
    )
