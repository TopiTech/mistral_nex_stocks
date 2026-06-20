"""
mistral_compat.py - Centralized Mistral SDK import compatibility layer.

All modules that import from mistralai should use this module instead of
repeating the same try/except fallback chain.
"""

try:
    from mistralai.client import Mistral  # type: ignore[attr-defined,no-redef]
except ImportError:
    try:
        from mistralai import Mistral  # type: ignore[attr-defined,no-redef]
    except ImportError:
        try:
            from mistralai.client.sdk import (  # type: ignore[attr-defined,no-redef]
                Mistral,
            )
        except ImportError:
            # Fallback/mock if mistralai is not installed in some test contexts
            class Mistral:  # type: ignore[no-redef]
                def __init__(self, api_key: str, **kwargs):
                    self.api_key = api_key
                    self.kwargs = kwargs


try:
    from mistralai.errors import SDKError  # type: ignore[import-untyped]
except ImportError:
    try:
        from mistralai.client.errors import SDKError  # type: ignore[import-untyped]
    except ImportError:
        class SDKError(Exception):  # type: ignore[no-redef]
            """Fallback if mistralai SDK errors are not available."""
            pass


try:
    from mistralai.models import AssistantMessage, SystemMessage, UserMessage  # type: ignore[attr-defined,no-redef]
except ImportError:
    try:
        from mistralai.client.models import AssistantMessage, SystemMessage, UserMessage  # type: ignore[attr-defined,no-redef]
    except ImportError:
        # Callable fallbacks for environments where mistralai models aren't available
        def SystemMessage(content):  # type: ignore[no-redef]
            return {"role": "system", "content": content}

        def UserMessage(content):  # type: ignore[no-redef]
            return {"role": "user", "content": content}

        def AssistantMessage(content):  # type: ignore[no-redef]
            return {"role": "assistant", "content": content}
