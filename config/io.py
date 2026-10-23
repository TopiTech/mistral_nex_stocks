"""設定の読み書き (`load_config` / `save_config`)。`config_store` からの純粋な移動。

共有状態およびテストでモックされる振る舞いは `config_store` 属性として
参照する。兄弟モジュール (`merge` / `atomic_write` / `corruption` /
`paths`) の関数は直接 import する (それらは `config_store` 上で
patch されない)。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import config_store
from config.atomic_write import _migrate_legacy_runtime_file, _write_and_replace_with_lock
from config.corruption import (
    _rotate_corrupt_backups,
    _sanitize_and_backup_corrupt_config,
    is_config_corrupted,
)
from config.merge import _merge_configs, _remove_stale_legacy_config
from config.paths import _ensure_runtime_dir

logger = logging.getLogger("config_store")


def _config_cache_key() -> tuple[int, int] | Literal["missing"]:
    """ファイルのmtime+sizeでキャッシュキーを生成(存在しない場合は 'missing')。"""
    try:
        st = config_store.CONFIG_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return "missing"


def load_config() -> dict[str, Any]:
    """設定ファイルを読み込む。存在しない場合は初期化。

    Always returns a deep copy of the cached config. Callers that mutate the
    returned dict and then pass it to ``save_config`` must not accidentally
    corrupt the in-process cache (H-1). Mutations that are not saved will not
    leak into subsequent ``load_config`` results either.
    """
    with config_store._CONFIG_LOCK:
        _ensure_runtime_dir()

        # One-time, process-lifetime legacy config merge. Performed at most once
        # per process to avoid unnecessary stat() calls and prevent a stale
        # workspace config.json from repeatedly overwriting runtime preferences.
        # Guard with CONFIG_FILE.parent == APP_DATA_DIR to prevent merging local
        # legacy config into mocked configs during testing.
        if not config_store._LEGACY_MERGE_DONE:
            legacy_merge_succeeded = True
            if (
                config_store.CONFIG_FILE.parent == config_store.APP_DATA_DIR
                and config_store.LEGACY_CONFIG_FILE.exists()
            ):
                if not config_store.CONFIG_FILE.exists():
                    try:
                        merged = _merge_configs(
                            config_store.LEGACY_CONFIG_FILE, config_store.CONFIG_FILE
                        )
                        # Remove the legacy config only after a successful merge so the
                        # project root does not accumulate runtime state that could
                        # be accidentally bundled or expose secrets.
                        if not merged:
                            legacy_merge_succeeded = False
                            raise RuntimeError("legacy config merge did not complete")
                        try:
                            config_store.LEGACY_CONFIG_FILE.unlink()
                            logger.info(
                                "Removed legacy config after migration: %s",
                                config_store.LEGACY_CONFIG_FILE,
                            )
                        except OSError as rm_exc:
                            logger.debug(
                                "Could not remove legacy config after migration: %s",
                                rm_exc,
                            )
                    except Exception as exc:
                        legacy_merge_succeeded = False
                        logger.warning("Failed to migrate legacy config: %s", exc)
                else:
                    try:
                        if (
                            config_store.LEGACY_CONFIG_FILE.stat().st_mtime_ns
                            > config_store.CONFIG_FILE.stat().st_mtime_ns
                        ):
                            merged = _merge_configs(
                                config_store.LEGACY_CONFIG_FILE, config_store.CONFIG_FILE
                            )
                            if not merged:
                                legacy_merge_succeeded = False
                                raise RuntimeError("legacy config sync did not complete")
                            try:
                                config_store.LEGACY_CONFIG_FILE.unlink()
                                logger.info(
                                    "Removed legacy config after sync: %s",
                                    config_store.LEGACY_CONFIG_FILE,
                                )
                            except OSError as rm_exc:
                                logger.debug(
                                    "Could not remove legacy config after sync: %s",
                                    rm_exc,
                                )
                        else:
                            _remove_stale_legacy_config(
                                config_store.LEGACY_CONFIG_FILE, config_store.CONFIG_FILE
                            )
                    except Exception as exc:
                        legacy_merge_succeeded = False
                        logger.warning("Failed to sync newer legacy config: %s", exc)
            if (
                config_store.CONFIG_FILE.parent == config_store.APP_DATA_DIR
                and not config_store.CONFIG_FILE.exists()
                and legacy_merge_succeeded
            ):
                _migrate_legacy_runtime_file(
                    config_store.LEGACY_CONFIG_FILE, config_store.CONFIG_FILE
                )
            config_store._LEGACY_MERGE_DONE = True  # Mark done even if merge was skipped/failed

        # ファイルのmtime+sizeでキャッシュキーを作り、変更があれば再読込する
        cached = config_store._CONFIG_CACHE["data"]
        cache_key = _config_cache_key()
        if cached is not None and config_store._CONFIG_CACHE["key"] == cache_key:
            return copy.deepcopy(cached)

        if config_store.CONFIG_FILE.exists():
            # crypto_utilsの循環参照を避けるため直接 chmod を試みる
            try:
                if not config_store._is_windows():
                    config_store.CONFIG_FILE.chmod(0o600)
            except Exception as exc:
                logger.debug("Failed to chmod config file: %s", exc)
        else:
            config_store.save_config(config_store.DEFAULT_CONFIG)
            config_store._CONFIG_CACHE["data"] = copy.deepcopy(config_store.DEFAULT_CONFIG)
            config_store._CONFIG_CACHE["key"] = _config_cache_key()
            return copy.deepcopy(config_store._CONFIG_CACHE["data"])

        # Acquire a shared process-level lock before reading the JSON file.
        lock_file = config_store.CONFIG_FILE.with_suffix(
            config_store.CONFIG_FILE.suffix + ".lock"
        )
        data = None
        read_key: tuple[int, int] | Literal["missing"] | None = None

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
                        with open(config_store.CONFIG_FILE, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            # Key the cache from the handle we just read so the
                            # stored key always matches the stored content, even
                            # if another process replaces the file between the
                            # read and the stat (otherwise the stale content
                            # would be cached under the NEW file's key and
                            # served indefinitely).
                            try:
                                _st = os.fstat(f.fileno())
                                read_key = (_st.st_mtime_ns, _st.st_size)
                            except OSError:
                                read_key = None
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
                        with open(config_store.CONFIG_FILE, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            # See the Windows branch: fstat the handle we read.
                            try:
                                _st = os.fstat(f.fileno())
                                read_key = (_st.st_mtime_ns, _st.st_size)
                            except OSError:
                                read_key = None
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

            # Fallback to unlocked read if locking failed or was not supported (with bounded retry for Windows sharing violations)
            if data is None:
                last_err = None
                for attempt in range(3):
                    try:
                        with open(config_store.CONFIG_FILE, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            try:
                                _st = os.fstat(f.fileno())
                                read_key = (_st.st_mtime_ns, _st.st_size)
                            except OSError:
                                read_key = None
                        break
                    except (json.JSONDecodeError, OSError) as read_exc:
                        last_err = read_exc
                        if attempt < 2:
                            time.sleep(0.05)
                if data is None and last_err is not None:
                    raise last_err

            # A syntactically valid JSON value is not necessarily a valid
            # configuration.  Treat non-object roots as corruption just like
            # malformed JSON; otherwise the next settings update would replace
            # the original file with defaults and silently discard the only
            # recoverable copy of the user's configuration.
            if not isinstance(data, dict):
                raise TypeError(
                    f"configuration root must be a JSON object, got {type(data).__name__}"
                )
            cfg = data
            # Ensure default keys
            for k, v in config_store.DEFAULT_CONFIG.items():
                cfg.setdefault(k, copy.deepcopy(v))
            if not isinstance(cfg.get("api_credentials"), dict):
                cfg["api_credentials"] = {}
            config_store._CONFIG_CACHE["data"] = cfg
            # Fall back to a path stat only when the handle-based key could not
            # be captured (os.fstat failed on every read path above).
            if read_key is None:
                read_key = _config_cache_key()
            config_store._CONFIG_CACHE["key"] = read_key
            config_store._CONFIG_CORRUPTED = False
            return copy.deepcopy(cfg)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            config_store._CONFIG_CORRUPTED = True
            corrupt_backup = config_store.CONFIG_FILE.with_suffix(
                config_store.CONFIG_FILE.suffix + f".corrupt.{datetime.now(UTC):%Y%m%d%H%M%S}.bak"
            )
            try:
                _sanitize_and_backup_corrupt_config(config_store.CONFIG_FILE, corrupt_backup)
                logger.warning(
                    "Corrupted config backed up to %s",
                    corrupt_backup,
                )
                _rotate_corrupt_backups(config_store.CONFIG_FILE.parent)
            except Exception as backup_exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Failed to backup corrupted config %s: %s",
                    config_store.CONFIG_FILE,
                    backup_exc,
                )
            logger.warning(
                "Failed to load config from %s: %s. Using defaults.",
                config_store.CONFIG_FILE,
                e,
                exc_info=True,
            )
            return copy.deepcopy(config_store.DEFAULT_CONFIG)


def save_config(cfg, create_backup=True):
    """設定ファイルに保存。デフォルト値との統合を保証"""
    with config_store._CONFIG_LOCK:
        if is_config_corrupted():
            raise RuntimeError(
                f"Refusing to save config over corrupted configuration file {config_store.CONFIG_FILE}. "
                "Manual recovery required or remove corrupted file first."
            )
        _ensure_runtime_dir()

        # 保存直前にプロセス内キャッシュを無効化し、次回 load_config で最新を読む
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        data = cfg.copy() if isinstance(cfg, dict) else {}
        for k, v in config_store.DEFAULT_CONFIG.items():
            data.setdefault(k, copy.deepcopy(v))
        if not isinstance(data.get("api_credentials"), dict):
            data["api_credentials"] = {}

        # 既存の設定があれば、秘密情報を除いたバックアップを作成 (.bak)
        if create_backup and config_store.CONFIG_FILE.exists():
            backup_tmp = config_store.CONFIG_FILE.with_suffix(
                config_store.CONFIG_FILE.suffix + f".bak.{uuid.uuid4().hex}.tmp"
            )
            try:
                # Back up the configuration that is about to be replaced, not
                # the caller's new value (which is written below). This makes
                # .bak a usable rollback point after an accidental change.
                with config_store.CONFIG_FILE.open("r", encoding="utf-8") as existing_file:
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

                if config_store._is_windows():
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
                backup_file = config_store.CONFIG_FILE.with_suffix(
                    config_store.CONFIG_FILE.suffix + ".bak"
                )
                os.replace(backup_tmp, backup_file)
                if not config_store._is_windows() and backup_file.exists():
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

        tmp_file = config_store.CONFIG_FILE.with_suffix(
            config_store.CONFIG_FILE.suffix + f".{uuid.uuid4().hex}.tmp"
        )
        lock_file = config_store.CONFIG_FILE.with_suffix(
            config_store.CONFIG_FILE.suffix + ".lock"
        )

        try:
            # Windowsでのファイルアクセス競合対策（リトライロジック + プラットフォーム別ファイルロック）
            # Unix: fcntl.flock, Windows: msvcrt.locking
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    # --- プラットフォーム別ファイルロック & アトミック置換 ---
                    # 置換処理（os.replace）までをファイルロック保持のスコープ内でアトミックに実施
                    try:
                        _write_and_replace_with_lock(
                            data, tmp_file, config_store.CONFIG_FILE, lock_file
                        )
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
                        # Fail-fast contract (see _write_and_replace_with_msvcrt_lock):
                        # RuntimeError means the update lock could not be
                        # acquired after its own bounded retry budget —
                        # re-spinning here would hold config_update_lock for
                        # another ~30s and block every config transaction.
                        raise
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
                        config_store.CONFIG_FILE,
                        max_retries,
                    )
                    raise

            # Set restrictive file permissions for security on non-Windows systems
            if not config_store._is_windows() and config_store.CONFIG_FILE.exists():
                try:
                    os.chmod(config_store.CONFIG_FILE, 0o600)
                except Exception as exc:
                    logger.warning("Failed to set config file permissions: %s", exc)
            config_store._CONFIG_CORRUPTED = False
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as unlink_exc:
                    logger.debug("Failed to remove temp config file: %s", unlink_exc)
