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

import importlib.metadata
import logging
from importlib.metadata import PackageNotFoundError
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Security guard: reject the backdoored mistralai 2.4.6 release.
# --------------------------------------------------------------------------
# mistralai==2.4.6 on PyPI was a malicious release (GHSA-wx9m-wx4f-4cmg): on
# Linux it downloads and executes a payload at import time. Check the installed
# version via package metadata BEFORE importing mistralai so the dropper never
# runs. The advisory affects exactly version 2.4.6.
try:
    _installed_mistralai_version = importlib.metadata.version("mistralai")
except PackageNotFoundError:
    _installed_mistralai_version = ""

if _installed_mistralai_version == "2.4.6":
    raise ImportError(
        "Refusing to import mistralai==2.4.6: this PyPI release contains a malicious "
        "dropper (GHSA-wx9m-wx4f-4cmg) that executes arbitrary code at import time on "
        "Linux. Upgrade to a safe version, e.g. `pip install --upgrade mistralai>=2.4.7`."
    )

# --------------------------------------------------------------------------
# Mistral client
# --------------------------------------------------------------------------
try:
    from mistralai.client import Mistral
except ImportError:
    try:
        from mistralai import Mistral  # type: ignore
    except ImportError:

        class Mistral:  # type: ignore
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
        from mistralai.errors import SDKError  # type: ignore
    except ImportError:
        logger.warning(
            "mistralai SDK errors module not found; SDKError will be a generic Exception wrapper. "
            "Install the SDK with: pip install mistralai>=2.4.7"
        )

        class SDKError(Exception):  # type: ignore
            """Fallback SDK error used when mistralai SDK errors are unavailable.

            This is a non-operational stub. AI features will not work until
            the mistralai package is installed.
            """

            def __init__(self, *args, **kwargs):
                super().__init__(*args)
                self.status_code = kwargs.get("status_code", 0)
                try:
                    from requests import Response

                    self.response = kwargs.get("response") or Response()
                except ImportError:
                    self.response = None


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
