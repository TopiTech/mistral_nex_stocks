"""Unit tests verifying _build_mistral_cache_key resilience with non-JSON-serializable objects.

Covers:
- Standard serializable inputs
- Non-serializable objects (custom instances, Path, datetime, functions) in messages or tool params
- Deterministic output
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from services.ai_service import _build_mistral_cache_key


class NonSerializablePayload:
    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return f"Payload({self.name})"


def test_build_mistral_cache_key_with_standard_inputs():
    key1 = _build_mistral_cache_key("mistral-small-latest", [{"role": "user", "content": "hi"}], 100, None)
    key2 = _build_mistral_cache_key("mistral-small-latest", [{"role": "user", "content": "hi"}], 100, None)
    assert key1 == key2
    assert key1.startswith("mistral_chat_")


def test_build_mistral_cache_key_with_non_serializable_objects():
    custom_obj = NonSerializablePayload("custom_tool_call")
    path_obj = Path("/tmp/test")
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    # Should serialize cleanly via default=str without raising TypeError
    key = _build_mistral_cache_key(
        "mistral-small-latest",
        [{"role": "user", "content": "test", "extra": custom_obj, "path": path_obj, "time": dt}],
        100,
        None,
        tools=[custom_obj],
    )
    assert isinstance(key, str)
    assert key.startswith("mistral_chat_")

    # Identical non-serializable payloads produce identical keys
    key_repeat = _build_mistral_cache_key(
        "mistral-small-latest",
        [{"role": "user", "content": "test", "extra": custom_obj, "path": path_obj, "time": dt}],
        100,
        None,
        tools=[custom_obj],
    )
    assert key == key_repeat
