"""破損検出・サニタイズバックアップ。`config_store` からの純粋な移動。

共有状態 (`CONFIG_FILE` / `_CONFIG_CORRUPTED`) およびテストでモックされる
`_is_windows` は `config_store` 属性として参照する。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import config_store

logger = logging.getLogger("config_store")


def is_config_corrupted() -> bool:
    """Return True if the configuration file was found to be corrupted."""
    if config_store._CONFIG_CORRUPTED:
        if not config_store.CONFIG_FILE.exists():
            config_store._CONFIG_CORRUPTED = False
            return False
        try:
            with open(config_store.CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                config_store._CONFIG_CORRUPTED = False
                return False
            return True
        except Exception:
            return True
    return False


def clear_config_corruption_flag() -> None:
    """Clear the corruption flag (for recovery / test reset)."""
    config_store._CONFIG_CORRUPTED = False


def _sanitize_and_backup_corrupt_config(source: Path, dest: Path) -> None:
    """Create a sanitized backup of a corrupted config file.

    Strips all secret-bearing keys before persisting, matching the
    sanitization applied to normal ``.bak`` backups. If the raw JSON
    cannot be parsed (truncated header), attempts a regex strip of
    known secret keys. Falls back to an empty sanitized object so no
    secret material is ever copied verbatim.
    """
    import re as _re

    sanitized: dict | None = None
    try:
        with open(source, "r", encoding="utf-8") as f:
            raw_text = f.read()
        raw_data: Any = json.loads(raw_text)
        if isinstance(raw_data, dict):
            sanitized = dict(raw_data)
            sanitized["api_credentials"] = {}
            for secret_key in ("flask_secret_key", "mns_master_key", "extension_api_token"):
                sanitized.pop(secret_key, None)
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    if sanitized is None:
        try:
            with open(source, "r", encoding="utf-8") as f:
                raw_text = f.read()
            for secret_key in ("flask_secret_key", "mns_master_key", "extension_api_token"):
                raw_text = _re.sub(
                    r'"' + _re.escape(secret_key) + r'"\s*:\s*(\{[^}]*\}|\"[^\"]*\"|[^,\}\n]*)',
                    f'"{secret_key}": "[REDACTED]"',
                    raw_text,
                )
            raw_text = _re.sub(
                r'"api_credentials"\s*:\s*\{[^}]*\}', '"api_credentials": {}', raw_text
            )
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    sanitized = parsed
                    sanitized["api_credentials"] = {}
                    for sk in ("flask_secret_key", "mns_master_key", "extension_api_token"):
                        if sk in sanitized and sanitized[sk] == "[REDACTED]":
                            sanitized.pop(sk, None)
            except (json.JSONDecodeError, ValueError):
                sanitized = {
                    "api_credentials": {},
                    "_corrupt_backup_note": "sanitized; original was not parseable JSON",
                }
        except (OSError, ValueError):
            sanitized = {
                "api_credentials": {},
                "_corrupt_backup_note": "sanitized; original was not readable",
            }

    if sanitized is None:
        sanitized = {
            "api_credentials": {},
            "_corrupt_backup_note": "sanitized fallback",
        }
    tmp_dest = dest.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        if config_store._is_windows():
            with open(tmp_dest, "w", encoding="utf-8") as f:
                json.dump(sanitized, f, ensure_ascii=False, indent=2)
        else:
            old_umask = os.umask(0o077)
            try:
                fd = os.open(str(tmp_dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(sanitized, f, ensure_ascii=False, indent=2)
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
            finally:
                os.umask(old_umask)
        os.replace(tmp_dest, dest)
        if not config_store._is_windows() and dest.exists():
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
    finally:
        if tmp_dest.exists():
            try:
                tmp_dest.unlink()
            except OSError:
                pass


def _rotate_corrupt_backups(directory: Path, limit: int = 5):
    """Keep only the latest N corrupted backup files and remove the older ones."""
    try:

        def _get_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        # Pattern: config.json.corrupt.*.bak
        backups = sorted(directory.glob("config.json.corrupt.*.bak"), key=_get_mtime)
        if len(backups) > limit:
            to_remove = backups[:-limit]
            for p in to_remove:
                try:
                    p.unlink(missing_ok=True)
                    logger.info("Removed old corrupt config backup: %s", p.name)
                except OSError as exc:
                    logger.debug("Failed to remove old corrupt backup %s: %s", p.name, exc)
    except OSError as exc:
        logger.warning("Error during corrupt backups rotation: %s", exc, exc_info=True)
