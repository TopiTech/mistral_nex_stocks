"""Temporary configuration management utilities for tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

_CREATED_TEMP_FILES: set[Path] = set()


def create_temp_config(
    overrides: dict[str, Any] | None = None,
    api_credentials: dict[str, Any] | None = None,
    register_for_cleanup: bool = True,
) -> Path:
    """Create a temporary config file for testing.

    Args:
        overrides: Dict of config values to override defaults.
        api_credentials: Dict of API credentials to inject.
        register_for_cleanup: If True, tracks the file for automatic deletion.

    Returns:
        Path to the created config file.
    """
    config: dict[str, Any] = {
        "mistral_model": "mistral-small-latest",
        "api_credentials": api_credentials or {},
    }
    if overrides:
        config.update(overrides)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(config, tmp, ensure_ascii=False, indent=2)
        tmp_name = tmp.name

    path = Path(tmp_name)
    if register_for_cleanup:
        _CREATED_TEMP_FILES.add(path)
    return path


def cleanup_temp_files() -> None:
    """Clean up any temporary config files tracked by create_temp_config."""
    while _CREATED_TEMP_FILES:
        path = _CREATED_TEMP_FILES.pop()
        try:
            if path.exists():
                path.unlink(missing_ok=True)
        except Exception:
            pass
