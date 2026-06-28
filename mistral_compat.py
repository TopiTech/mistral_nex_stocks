"""
mistral_compat.py - Centralized Mistral SDK import compatibility layer.

All modules that import from mistralai should use this module instead of
repeating the same try/except fallback chain.

Exports:
    Mistral            - Mistral SDK client (or lightweight fallback)
    SDKError           - SDK error type (or fallback)
    SystemMessage      - Helper that returns {"role": "system", "content": content}
    UserMessage        - Helper that returns {"role": "user", "content": content}
    AssistantMessage   - Helper that returns {"role": "assistant", "content": content}
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Mistral client
# --------------------------------------------------------------------------
try:
    from mistralai.client import Mistral
except ImportError:
    try:
        from mistralai import Mistral  # type: ignore[attr-defined,no-redef,unused-ignore]  # older layouts
    except ImportError:

        class Mistral:  # type: ignore[no-redef]
            """Lightweight runtime fallback used when mistralai is not installed."""

            def __init__(self, api_key: str, **kwargs: Any) -> None:
                self.api_key = api_key
                self.kwargs = kwargs

        logger.warning(
            "mistralai SDK is not installed. Using a no-op Mistral client fallback. "
            "AI features (chat, analysis, news) will not work."
        )

# --------------------------------------------------------------------------
# SDKError
# --------------------------------------------------------------------------
try:
    from mistralai.client.errors import SDKError
except ImportError:
    try:
        from mistralai.errors import SDKError  # type: ignore[no-redef,unused-ignore]
    except ImportError:

        class SDKError(Exception):  # type: ignore[no-redef]
            """Fallback SDK error used when mistralai SDK errors are unavailable."""


# --------------------------------------------------------------------------
# Message helpers (dict-based; compatible with both real SDK and fallback)
# --------------------------------------------------------------------------
# The real mistralai SDK v2 accepts plain dicts for messages, so these
# helpers work identically whether or not the package is installed.


def SystemMessage(content: str) -> dict[str, str]:
    return {"role": "system", "content": content}


def UserMessage(content: str) -> dict[str, str]:
    return {"role": "user", "content": content}


def AssistantMessage(content: str) -> dict[str, str]:
    return {"role": "assistant", "content": content}
