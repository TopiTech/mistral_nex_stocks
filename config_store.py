"""
設定ストレージモジュール
config_utils.py から抽出した設定ファイル読み書き関連の関数群
"""
# pylint: disable=missing-function-docstring

import copy
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crypto_utils import (
    _decode_secret,
    _encode_secret,
    _is_windows,  # used by save_config
)

logger = logging.getLogger(__name__)

# --- 定数定義 ---
BASE_DIR = Path(__file__).resolve().parent
LEGACY_CONFIG_FILE = BASE_DIR / "config.json"


def _get_runtime_data_dir() -> Path:
    """Return the per-user runtime data directory.

    Runtime state should live outside the source tree so that config and stock
    data are not accidentally copied, committed, or bundled with the repo.
    """
    override = os.environ.get("MNS_DATA_DIR") or os.environ.get("MNS_APP_DATA_DIR")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "MistralNeXStocks"
        return BASE_DIR / ".data"

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data).expanduser() / "mistral_nex_stocks"
    return Path.home() / ".local" / "share" / "mistral_nex_stocks"


APP_DATA_DIR = _get_runtime_data_dir()
CONFIG_FILE = APP_DATA_DIR / "config.json"
USER_STOCKS_FILE = APP_DATA_DIR / "user_stocks.json"
_CONFIG_LOCK = threading.RLock()
_MASTER_KEY_LOCK = threading.Lock()

# プロセス内キャッシュ: load_config() はAIリクエスト等のホットパスから頻繁に呼ばれるため、
# ファイルI/Oとロック取得を抑える。キャッシュはファイルのmtime+sizeでキーされ、
# ファイルが変更/削除されると自動的に無効化される（save_config時は即時クリア）。
_CONFIG_CACHE: dict = {"data": None, "key": None}

# レガシーコンフィグのマージはプロセス起動後に1回のみ実行する。
# save_configによるキャッシュ無効化後も再実行しないことで、不要なファイルI/O（stat）と
# 意図しない設定上書きを防止する。テストでリセットが必要な場合は _reset_legacy_merge_flag() を使用。
_LEGACY_MERGE_DONE: bool = False
_CONFIG_CORRUPTED: bool = False


def is_config_corrupted() -> bool:
    """Return True if the configuration file was found to be corrupted."""
    global _CONFIG_CORRUPTED
    if _CONFIG_CORRUPTED:
        if not CONFIG_FILE.exists():
            _CONFIG_CORRUPTED = False
            return False
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _CONFIG_CORRUPTED = False
                return False
            return True
        except Exception:
            return True
    return False


def clear_config_corruption_flag() -> None:
    """Clear the corruption flag (for recovery / test reset)."""
    global _CONFIG_CORRUPTED
    _CONFIG_CORRUPTED = False


DEFAULT_CONFIG = {
    "mistral_model": "mistral-medium-3.5",
    "api_credentials": {},
    "custom_ai_prompt": "",
}


def _ensure_runtime_dir() -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def config_update_lock():
    """Serialize load-modify-save updates across threads and processes."""
    with _CONFIG_LOCK:
        _ensure_runtime_dir()
        update_lock_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".update.lock")
        fd = os.open(str(update_lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size < 1:
                    try:
                        os.write(fd, b"L")
                        os.lseek(fd, 0, os.SEEK_SET)
                    except OSError:
                        pass
                os.lseek(fd, 0, os.SEEK_SET)
                for attempt in range(20):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                        locked = True
                        break
                    except OSError as err:
                        if attempt == 19:
                            raise RuntimeError("config update lock is busy") from err
                        time.sleep(0.05 * (attempt + 1))
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
                locked = True
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass


@contextmanager
def _master_key_update_lock():
    """Serialize master-key initialization across local processes.

    On Windows, use a kernel mutex rather than a byte-range lock: the latter
    is already used by config writes and cannot be safely nested for the full
    load/generate/save transaction. POSIX uses the existing update lock.
    """
    if os.name != "nt":
        with config_update_lock():
            yield
        return

    import ctypes
    from ctypes import wintypes

    mutex_name = (
        "Local\\MistralNeXStocksMasterKey-"
        + hashlib.sha256(str(CONFIG_FILE.resolve()).encode("utf-8", errors="ignore")).hexdigest()
    )
    kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None
    if kernel32 is None:
        raise RuntimeError("Windows API (ctypes.windll) is not available on this platform")
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise RuntimeError("Failed to create master key initialization mutex")
    acquired = False
    try:
        wait_result = kernel32.WaitForSingleObject(handle, 15_000)
        # WAIT_OBJECT_0 (0) and WAIT_ABANDONED (0x80) both grant ownership.
        if wait_result in (0, 0x80):
            acquired = True
            if wait_result == 0x80:
                logger.warning(
                    "Master key mutex was abandoned by previous process; acquired ownership"
                )
        else:
            raise RuntimeError(
                f"Timed out waiting for master key initialization (wait_result={wait_result})"
            )
        yield
    finally:
        if acquired:
            try:
                kernel32.ReleaseMutex(handle)
            except Exception as exc:
                logger.debug("Failed to release master key mutex: %s", exc)
        try:
            kernel32.CloseHandle(handle)
        except Exception as exc:
            logger.debug("Failed to close master key mutex handle: %s", exc)


def _migrate_legacy_runtime_file(source: Path, target: Path) -> None:
    """Copy a legacy source-tree file into the runtime directory if needed.

    After a successful copy the source file is removed so the project root
    does not accumulate runtime state that could be accidentally bundled,
    copied, or committed.
    """
    if target.exists() or not source.exists():
        return
    _ensure_runtime_dir()
    try:
        shutil.copy2(source, target)
        logger.info("Migrated legacy runtime file %s -> %s", source, target)
        try:
            source.unlink()
            logger.info("Removed legacy runtime file after migration: %s", source)
        except OSError as rm_exc:
            logger.warning("Failed to remove legacy runtime file %s: %s", source, rm_exc)
    except OSError as exc:
        logger.warning("Failed to migrate legacy runtime file %s: %s", source, exc)


def _write_and_replace_with_lock(
    data: dict, tmp_file: Path, target_file: Path, lock_file: Path
) -> None:
    """Write JSON data to tmp_file and replace target_file with platform-appropriate locking.

    Uses fcntl.flock on Unix/POSIX and msvcrt.locking on Windows.
    Falls back to lock-free write and replace if neither is available.
    """
    if os.name == "nt":  # Windows
        _write_and_replace_with_msvcrt_lock(data, tmp_file, target_file, lock_file)
    else:
        _write_and_replace_with_fcntl_lock(data, tmp_file, target_file, lock_file)


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

    assert sanitized is not None
    tmp_dest = dest.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        if _is_windows():
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
        if not _is_windows() and dest.exists():
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


def _durable_write_json(path: Path, data: dict) -> None:
    """Write JSON to path with fsync durability before atomic replace."""
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        if _is_windows():
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        else:
            old_umask = os.umask(0o077)
            try:
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except OSError:
                            pass
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
            finally:
                os.umask(old_umask)
        os.replace(tmp, path)
        if not _is_windows() and hasattr(os, "O_DIRECTORY"):
            try:
                dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        if not _is_windows() and path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _safe_write_json(tmp_file: Path, data: dict) -> None:
    """Write JSON data to tmp_file using restrictive permissions (0o600) with fsync."""
    old_umask = os.umask(0o077)
    try:
        fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    finally:
        os.umask(old_umask)


def _write_and_replace_with_fcntl_lock(
    data: dict, tmp_file: Path, target_file: Path, lock_file: Path
) -> None:
    """Write and replace with POSIX fcntl.flock locking."""
    try:
        import fcntl  # type: ignore[import-untyped]

        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
            _safe_write_json(tmp_file, data)
            os.replace(tmp_file, target_file)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
    except ImportError as exc:
        logger.debug("fcntl is unavailable, writing without lock: %s", exc)
        _safe_write_json(tmp_file, data)
        os.replace(tmp_file, target_file)


def _write_and_replace_with_msvcrt_lock(
    data: dict, tmp_file: Path, target_file: Path, lock_file: Path
) -> None:
    """Write and replace with Windows msvcrt.locking."""
    try:
        import msvcrt  # type: ignore[import-untyped]
        import random

        fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        locked = False
        max_lock_retries = 30
        try:
            # Ensure the lock file has at least 1 byte of data so msvcrt.locking succeeds.
            # Otherwise, locking a 0-byte file might fail or be ignored on Windows.
            if os.fstat(fd).st_size < 1:
                try:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass

            for attempt in range(max_lock_retries):
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    locked = True
                    break
                except OSError as err:
                    if attempt < max_lock_retries - 1:
                        base_delay = 0.05 + (0.01 * attempt)
                        jitter = random.SystemRandom().uniform(0.01, 0.05)
                        time.sleep(min(base_delay + jitter, 0.5))
                        continue
                    raise RuntimeError(
                        f"msvcrt lock busy, failed to acquire lock on: {lock_file}"
                    ) from err
            if os.fstat(fd).st_size < 1:
                try:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass
            _safe_write_json(tmp_file, data)
            os.replace(tmp_file, target_file)
        finally:
            if locked:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
    except ImportError as exc:
        logger.debug("msvcrt is unavailable, writing without lock: %s", exc)
        # Only reached when msvcrt is genuinely unavailable (not contention),
        # so a lock-free write is the last-resort fallback.
        _safe_write_json(tmp_file, data)
        os.replace(tmp_file, target_file)
    except RuntimeError as exc:
        # Lock contention after retries: do NOT write lock-free (would risk a
        # partial/corrupted config). Surface the failure so the caller can
        # report it (e.g. a 503) instead of silently losing the modification.
        # Unlike OSError/TypeError, RuntimeError is NOT caught by save_config's
        # retry loop — propagate immediately to avoid endless spinning.
        logger.error("Config save skipped: Windows lock busy after retries (%s)", exc)
        raise


def _rotate_corrupt_backups(directory: Path, limit: int = 5):
    """Keep only the latest N corrupted backup files and remove the older ones."""
    try:
        # Pattern: config.json.corrupt.*.bak
        backups = sorted(
            directory.glob("config.json.corrupt.*.bak"), key=lambda p: p.stat().st_mtime
        )
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


def _config_cache_key():
    """ファイルのmtime+sizeからキャッシュキーを生成（存在しない場合は 'missing'）。"""
    try:
        st = CONFIG_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return "missing"


def _reset_legacy_merge_flag() -> None:
    """Reset the legacy merge flag for test isolation.

    TESTING ONLY: Called from conftest.py's reset fixture so that each test
    starts with a clean process-lifetime merge state.
    """
    global _LEGACY_MERGE_DONE
    with _CONFIG_LOCK:
        _LEGACY_MERGE_DONE = False


# Keys the legacy workspace config is NEVER allowed to write into the runtime
# config. Secrets/generated tokens are runtime-authoritative: a stale or
# checked-in repo-root config.json must not silently clobber the user's real
# runtime secrets. (REV-02)
_MERGE_PROTECTED_KEYS = frozenset(
    [
        "mns_master_key",
        "extension_api_token",
        "extension_api_token_created",
        "flask_secret_key",
        "api_credentials",
    ]
)

# Non-secret preference keys synced from the legacy config into the runtime
# config. Unlike protected keys (secrets/tokens), these may be overwritten
# when the legacy config is newer. See ``_merge_configs``.
# R4: the only key propagated from the legacy workspace config today is
# ``mistral_model`` — other keys (e.g. custom_ai_prompt) are intentionally
# runtime-only. Keep the allowlist explicit so workspace edits are never
# silently ignored without a log line (see _merge_configs else branch).
_MERGE_SEED_KEYS = ("mistral_model",)


def _merge_configs(legacy_path: Path, runtime_path: Path) -> None:
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
        return

    if not isinstance(legacy_data, dict):
        return

    try:
        if runtime_path.exists():
            with open(runtime_path, "r", encoding="utf-8") as f:
                runtime_data = json.load(f)
        else:
            runtime_data = {}
    except Exception as exc:
        logger.warning("Failed to load runtime config for merging: %s", exc)
        # If runtime config exists but is corrupted, abort merging to avoid writing on corrupt file
        return

    if not isinstance(runtime_data, dict):
        runtime_data = {}

    modified = False

    # Seed or update non-secret preferences from the legacy config.
    # We allow overwriting existing preference values if they differ from the legacy config,
    # but never touch protected keys (secrets, generated tokens).
    for key in _MERGE_SEED_KEYS:
        if key in legacy_data and (
            key not in runtime_data or runtime_data[key] != legacy_data[key]
        ):
            runtime_data[key] = copy.deepcopy(legacy_data[key])
            modified = True

    if modified:
        logger.info("Syncing runtime config preferences from legacy config...")
        save_config(runtime_data, create_backup=False)
    else:
        # R4: surface the allowlist boundary — an operator who edits non-seed
        # keys (e.g. custom_ai_prompt) in the workspace file would otherwise
        # see no log line and assume the edit propagated after restart.
        untouched = [
            k for k in legacy_data if k not in _MERGE_SEED_KEYS and k not in _MERGE_PROTECTED_KEYS
        ]
        if untouched:
            logger.info(
                "Legacy config contains non-synced keys (runtime-only): %s — edit the runtime config or Settings page instead.",
                ", ".join(sorted(untouched)),
            )


def load_config():
    """設定ファイルを読み込む。存在しない場合は初期化。

    Always returns a deep copy of the cached config. Callers that mutate the
    returned dict and then pass it to ``save_config`` must not accidentally
    corrupt the in-process cache (H-1). Mutations that are not saved will not
    leak into subsequent ``load_config`` results either.
    """
    with _CONFIG_LOCK:
        global _LEGACY_MERGE_DONE, _CONFIG_CORRUPTED
        _ensure_runtime_dir()

        # One-time, process-lifetime legacy config merge. Performed at most once
        # per process to avoid unnecessary stat() calls and prevent a stale
        # workspace config.json from repeatedly overwriting runtime preferences.
        # Guard with CONFIG_FILE.parent == APP_DATA_DIR to prevent merging local
        # legacy config into mocked configs during testing.
        if not _LEGACY_MERGE_DONE:
            if CONFIG_FILE.parent == APP_DATA_DIR and LEGACY_CONFIG_FILE.exists():
                if not CONFIG_FILE.exists():
                    try:
                        _merge_configs(LEGACY_CONFIG_FILE, CONFIG_FILE)
                        # Remove the legacy config after successful merge so the
                        # project root does not accumulate runtime state that could
                        # be accidentally bundled or expose secrets.
                        try:
                            LEGACY_CONFIG_FILE.unlink()
                            logger.info(
                                "Removed legacy config after migration: %s", LEGACY_CONFIG_FILE
                            )
                        except OSError as rm_exc:
                            logger.debug(
                                "Could not remove legacy config after migration: %s", rm_exc
                            )
                    except Exception as exc:
                        logger.warning("Failed to migrate legacy config: %s", exc)
                else:
                    try:
                        if LEGACY_CONFIG_FILE.stat().st_mtime_ns > CONFIG_FILE.stat().st_mtime_ns:
                            _merge_configs(LEGACY_CONFIG_FILE, CONFIG_FILE)
                    except Exception as exc:
                        logger.warning("Failed to sync newer legacy config: %s", exc)
            if CONFIG_FILE.parent == APP_DATA_DIR and not CONFIG_FILE.exists():
                _migrate_legacy_runtime_file(LEGACY_CONFIG_FILE, CONFIG_FILE)
            _LEGACY_MERGE_DONE = True  # Mark done even if merge was skipped/failed

        # ファイルのmtime+sizeでキャッシュキーを作り、変更があれば再読込する
        cached = _CONFIG_CACHE["data"]
        cache_key = _config_cache_key()
        if cached is not None and _CONFIG_CACHE["key"] == cache_key:
            return copy.deepcopy(cached)

        if CONFIG_FILE.exists():
            # crypto_utilsの循環参照を避けるため直接 chmod を試みる
            try:
                if not _is_windows():
                    CONFIG_FILE.chmod(0o600)
            except Exception as exc:
                logger.debug("Failed to chmod config file: %s", exc)
        else:
            save_config(DEFAULT_CONFIG)
            _CONFIG_CACHE["data"] = copy.deepcopy(DEFAULT_CONFIG)
            _CONFIG_CACHE["key"] = _config_cache_key()
            return copy.deepcopy(_CONFIG_CACHE["data"])

        # Acquire a shared process-level lock before reading the JSON file.
        lock_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".lock")
        data = None

        try:
            if os.name == "nt":  # Windows
                try:
                    import msvcrt

                    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                    locked = False
                    try:
                        if os.fstat(fd).st_size < 1:
                            os.write(fd, b"L")
                            os.lseek(fd, 0, os.SEEK_SET)

                        # LK_RLCK is a retry-based exclusive lock on Windows (Windows CRT does not support shared locks)
                        msvcrt.locking(fd, msvcrt.LK_RLCK, 1)  # type: ignore[attr-defined]
                        locked = True
                        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    finally:
                        if locked:
                            try:
                                os.lseek(fd, 0, os.SEEK_SET)
                                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                            except OSError:
                                pass
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                except (ImportError, OSError) as exc:
                    logger.debug("msvcrt shared lock read failed, falling back: %s", exc)
            else:  # Unix
                try:
                    import fcntl

                    lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                    locked = False
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_SH)  # type: ignore[attr-defined]
                        locked = True
                        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    finally:
                        if locked:
                            try:
                                fcntl.flock(lock_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
                            except OSError:
                                pass
                        try:
                            os.close(lock_fd)
                        except OSError:
                            pass
                except (ImportError, OSError) as exc:
                    logger.debug("fcntl shared lock read failed, falling back: %s", exc)

            # Fallback to unlocked read if locking failed or was not supported
            if data is None:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

            cfg = data if isinstance(data, dict) else {}
            # Ensure default keys
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, copy.deepcopy(v))
            if not isinstance(cfg.get("api_credentials"), dict):
                cfg["api_credentials"] = {}
            _CONFIG_CACHE["data"] = cfg
            _CONFIG_CACHE["key"] = _config_cache_key()
            _CONFIG_CORRUPTED = False
            return copy.deepcopy(cfg)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            _CONFIG_CORRUPTED = True
            corrupt_backup = CONFIG_FILE.with_suffix(
                CONFIG_FILE.suffix + f".corrupt.{datetime.now(UTC):%Y%m%d%H%M%S}.bak"
            )
            try:
                _sanitize_and_backup_corrupt_config(CONFIG_FILE, corrupt_backup)
                logger.warning(
                    "Corrupted config backed up to %s",
                    corrupt_backup,
                )
                _rotate_corrupt_backups(CONFIG_FILE.parent)
            except Exception as backup_exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Failed to backup corrupted config %s: %s",
                    CONFIG_FILE,
                    backup_exc,
                )
            logger.warning(
                "Failed to load config from %s: %s. Using defaults.",
                CONFIG_FILE,
                e,
                exc_info=True,
            )
            return copy.deepcopy(DEFAULT_CONFIG)


def save_config(cfg, create_backup=True):
    """設定ファイルに保存。デフォルト値との統合を保証"""
    if is_config_corrupted():
        raise RuntimeError(
            f"Refusing to save config over corrupted configuration file {CONFIG_FILE}. "
            "Manual recovery required or remove corrupted file first."
        )
    with _CONFIG_LOCK:
        _ensure_runtime_dir()
        # 保存直前にプロセス内キャッシュを無効化し、次回 load_config で最新を読む
        _CONFIG_CACHE["data"] = None
        _CONFIG_CACHE["key"] = None
        data = cfg.copy() if isinstance(cfg, dict) else {}
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, copy.deepcopy(v))
        if not isinstance(data.get("api_credentials"), dict):
            data["api_credentials"] = {}

        # 既存の設定があれば、秘密情報を除いたバックアップを作成 (.bak)
        if create_backup and CONFIG_FILE.exists():
            backup_tmp = CONFIG_FILE.with_suffix(
                CONFIG_FILE.suffix + f".bak.{uuid.uuid4().hex}.tmp"
            )
            try:
                # Back up the configuration that is about to be replaced, not
                # the caller's new value (which is written below). This makes
                # .bak a usable rollback point after an accidental change.
                with CONFIG_FILE.open("r", encoding="utf-8") as existing_file:
                    backup_data = json.load(existing_file)
                if not isinstance(backup_data, dict):
                    raise TypeError("existing config root must be a JSON object")
                backup_data["api_credentials"] = {}
                # Strip all secret entries from backups to avoid leaking secrets
                for secret_key in ("flask_secret_key", "mns_master_key", "extension_api_token"):
                    if secret_key in backup_data:
                        del backup_data[secret_key]
                # H-4: Write backup to a temp file with restricted permissions
                # BEFORE the rename, so the backup file is never exposed with
                # open permissions, even momentarily.

                if _is_windows():
                    with open(backup_tmp, "w", encoding="utf-8") as f:
                        json.dump(backup_data, f, ensure_ascii=False, indent=2)
                else:
                    # Write with 0o600 umask so the file is never world-readable
                    old_umask = os.umask(0o077)
                    try:
                        fd = os.open(str(backup_tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                json.dump(backup_data, f, ensure_ascii=False, indent=2)
                        except Exception:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                            raise
                    finally:
                        os.umask(old_umask)
                backup_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
                os.replace(backup_tmp, backup_file)
                if not _is_windows() and backup_file.exists():
                    try:
                        os.chmod(backup_file, 0o600)
                    except Exception as chmod_exc:
                        logger.warning(
                            "Failed to set backup config file permissions: %s", chmod_exc
                        )
            except (OSError, TypeError, json.JSONDecodeError) as e:
                logger.warning("Failed to create config backup: %s", e)
            finally:
                if backup_tmp.exists():
                    try:
                        backup_tmp.unlink()
                    except OSError:
                        pass

        tmp_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + f".{uuid.uuid4().hex}.tmp")
        lock_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".lock")

        try:
            # Windowsでのファイルアクセス競合対策（リトライロジック + プラットフォーム別ファイルロック）
            # Unix: fcntl.flock, Windows: msvcrt.locking
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    # --- プラットフォーム別ファイルロック & アトミック置換 ---
                    # 置換処理（os.replace）までをファイルロック保持のスコープ内でアトミックに実施
                    try:
                        _write_and_replace_with_lock(data, tmp_file, CONFIG_FILE, lock_file)
                    except PermissionError as perm_exc:
                        logger.warning(
                            "PermissionError during config save/replace (attempt %d/%d): %s. Retrying...",
                            attempt + 1,
                            max_retries,
                            perm_exc,
                        )
                        if attempt < max_retries - 1:
                            time.sleep(0.1 * (attempt + 1))
                            continue
                        raise
                    break  # 成功
                except (OSError, TypeError, RuntimeError) as exc:
                    if isinstance(exc, RuntimeError):
                        logger.warning(
                            "RuntimeError during config save (attempt %d/%d): %s. Retrying...",
                            attempt + 1,
                            max_retries,
                            exc,
                        )
                    else:
                        logger.warning(
                            "Error during config save (attempt %d/%d): %s. Retrying...",
                            attempt + 1,
                            max_retries,
                            exc,
                        )
                    if attempt < max_retries - 1:
                        time.sleep(0.1 * (attempt + 1))
                        continue
                    logger.exception(
                        "Failed to save config to %s after %d attempts",
                        CONFIG_FILE,
                        max_retries,
                    )
                    raise

            # Set restrictive file permissions for security on non-Windows systems
            if not _is_windows() and CONFIG_FILE.exists():
                try:
                    os.chmod(CONFIG_FILE, 0o600)
                except Exception as exc:
                    logger.warning("Failed to set config file permissions: %s", exc)
            global _CONFIG_CORRUPTED
            _CONFIG_CORRUPTED = False
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as unlink_exc:
                    logger.debug("Failed to remove temp config file: %s", unlink_exc)


def get_or_create_master_key() -> str:
    """Get or create the master key for Fernet symmetric encryption.

    Checks in order:
    1. MNS_MASTER_KEY environment variable
    2. mns_master_key in config.json (decoded)
    3. Generates a new key, stores it encrypted in config.json

    Returns:
        str: The master key (base64-encoded, compatible with cryptography.fernet)
    """
    env_key = os.environ.get("MNS_MASTER_KEY", "").strip()
    if env_key:
        return env_key

    # Production check: MNS_MASTER_KEY must be set in production mode to prevent data loss.
    from utils.env_helpers import _is_production_env

    if _is_production_env():
        raise RuntimeError(
            "FATAL: MNS_MASTER_KEY is not set in the environment, but the application is running in production mode. "
            "Using an ephemeral or auto-generated key would cause encrypted configurations and portfolio data "
            "to become unreadable and lost upon next restart. Please set a persistent MNS_MASTER_KEY in your environment."
        )

    from crypto_utils import KEYRING_AVAILABLE, _is_windows

    _allow_ephemeral_master = os.environ.get(
        "MNS_ALLOW_EPHEMERAL_MASTER_KEY", ""
    ).strip().lower() in ("1", "true", "yes")
    if (
        not KEYRING_AVAILABLE
        and not _is_windows()
        and os.environ.get("MNS_EPHEMERAL_FALLBACK") == "1"
        and not _allow_ephemeral_master
    ):
        raise RuntimeError(
            "FATAL: Secure storage (keyring/DPAPI) is unavailable, and MNS_EPHEMERAL_FALLBACK=1 is active, "
            "but MNS_MASTER_KEY is not set in the environment. "
            "Generating or using a temporary master key would cause encrypted configurations and portfolio data "
            "to become unreadable and lost upon next restart. Please set a persistent MNS_MASTER_KEY in your environment."
        )

    # The read/generate/write sequence must be serialized inside a process.
    # save_config() already has an OS-level atomic replacement lock, while this
    # dedicated lock prevents two startup threads from generating divergent
    # keys before either one reaches that replacement.
    with _MASTER_KEY_LOCK, _master_key_update_lock():
        _CONFIG_CACHE["data"] = None
        _CONFIG_CACHE["key"] = None
        cfg = load_config()
        if not isinstance(cfg, dict):
            cfg = {}

        key_entry = cfg.get("mns_master_key")
        if key_entry and isinstance(key_entry, dict):
            key = _decode_secret(key_entry, "mns_master_key")
            if key:
                return key
            # The entry exists but could not be decoded. This is NOT the same as
            # "no key generated yet": silently generating a new key here would
            # overwrite mns_master_key and permanently orphan every Fernet-
            # encrypted value protected with the original key (user_stocks.json,
            # flask_secret_key, extension_api_token). Fail closed instead of
            # silently destroying data.
            raise RuntimeError(
                "FATAL: mns_master_key exists in config but could not be decoded "
                "(keyring/DPAPI read failure, corrupted entry, or ciphertext "
                "encrypted by another identity). Refusing to overwrite it: doing "
                "so would make all encrypted data permanently unreadable. "
                "Restore the secret store backing this key or set MNS_MASTER_KEY "
                "to the correct key, then retry."
            )

        from cryptography.fernet import Fernet

        new_key = Fernet.generate_key().decode("ascii")
        cfg["mns_master_key"] = _encode_secret(new_key, "mns_master_key")

        try:
            save_config(cfg)
        except Exception as exc:
            logger.error("Failed to persist generated master key: %s", exc)
            raise RuntimeError("Failed to persist generated master key") from exc

        _CONFIG_CACHE["data"] = None
        _CONFIG_CACHE["key"] = None
        persisted_config = load_config()
        persisted = (
            persisted_config.get("mns_master_key") if isinstance(persisted_config, dict) else None
        )
        persisted_key = (
            _decode_secret(persisted, "mns_master_key") if isinstance(persisted, dict) else ""
        )
        if not persisted_key:
            raise RuntimeError("Failed to verify persisted master key")
        return persisted_key
