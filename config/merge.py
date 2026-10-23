"""レガシー設定のマージ・除去。`config_store` からの純粋な移動。

`save_config` はテストで `patch(\"config_store.save_config\")` されるため、
呼び出し時に `config_store` 属性として解決する (直接 import しない)。
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import config_store

logger = logging.getLogger("config_store")


def _reset_legacy_merge_flag() -> None:
    """Reset the legacy merge flag for test isolation.

    TESTING ONLY: Called from conftest.py's reset fixture so that each test
    starts with a clean process-lifetime merge state.
    """
    with config_store._CONFIG_LOCK:
        config_store._LEGACY_MERGE_DONE = False


def _merge_configs(legacy_path: Path, runtime_path: Path) -> bool:
    """Sync non-secret preferences from a legacy/workspace config into the runtime config.

    The runtime config is ALWAYS authoritative for secrets and generated tokens
    (``_MERGE_PROTECTED_KEYS``); these are never read from the legacy config.
    Non-secret preference keys (``_MERGE_SEED_KEYS``) are synced: if the legacy
    config is newer, its values replace the runtime values for those keys.
    This allows workspace-level defaults (e.g. ``mistral_model``) to propagate
    to the runtime config on the next process start.

    Called once per process lifetime from ``load_config()``.
    """
    try:
        with open(legacy_path, "r", encoding="utf-8") as f:
            legacy_data = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load legacy config for merging: %s", exc)
        return False

    if not isinstance(legacy_data, dict):
        return False

    try:
        if runtime_path.exists():
            with open(runtime_path, "r", encoding="utf-8") as f:
                runtime_data = json.load(f)
        else:
            runtime_data = {}
    except Exception as exc:
        logger.warning("Failed to load runtime config for merging: %s", exc)
        # If runtime config exists but is corrupted, abort merging to avoid writing on corrupt file
        return False

    if not isinstance(runtime_data, dict):
        runtime_data = {}

    modified = False

    # Seed or update non-secret preferences from the legacy config.
    # We allow overwriting existing preference values if they differ from the legacy config,
    # but never touch protected keys (secrets, generated tokens).
    for key in config_store._MERGE_SEED_KEYS:
        if key in legacy_data and (
            key not in runtime_data or runtime_data[key] != legacy_data[key]
        ):
            runtime_data[key] = copy.deepcopy(legacy_data[key])
            modified = True

    if modified:
        logger.info("Syncing runtime config preferences from legacy config...")
        config_store.save_config(runtime_data, create_backup=False)
    else:
        # R4: surface the allowlist boundary — an operator who edits non-seed
        # keys (e.g. custom_ai_prompt) in the workspace file would otherwise
        # see no log line and assume the edit propagated after restart.
        untouched = [
            k
            for k in legacy_data
            if k not in config_store._MERGE_SEED_KEYS
            and k not in config_store._MERGE_PROTECTED_KEYS
        ]
        if untouched:
            logger.info(
                "Legacy config contains non-synced keys (runtime-only): %s — edit the runtime config or Settings page instead.",
                ", ".join(sorted(untouched)),
            )
    return True


def _remove_stale_legacy_config(legacy_path: Path, runtime_path: Path) -> bool:
    """Remove a stale legacy config after validating both config files.

    A runtime config that is already newer is authoritative.  The legacy file
    may still contain old credentials, so remove it only when both files are
    valid JSON objects and never when the two paths resolve to the same file.
    """
    try:
        if legacy_path.resolve() == runtime_path.resolve():
            return False
        with open(runtime_path, "r", encoding="utf-8") as f:
            runtime_data = json.load(f)
        with open(legacy_path, "r", encoding="utf-8") as f:
            legacy_data = json.load(f)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("Could not validate stale legacy config before removal: %s", exc)
        return False

    if not isinstance(runtime_data, dict) or not isinstance(legacy_data, dict):
        logger.debug("Keeping stale legacy config because its JSON root is not an object")
        return False

    try:
        legacy_path.unlink()
        logger.info("Removed stale legacy config after runtime validation: %s", legacy_path)
        return True
    except OSError as rm_exc:
        logger.debug("Could not remove stale legacy config: %s", rm_exc)
        return False
