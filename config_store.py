"""
設定ストレージモジュール
config_utils.py から抽出した設定ファイル読み書き関連の関数群
"""
# pylint: disable=missing-function-docstring

import copy
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from crypto_utils import (  # noqa: F401
    _decode_secret,
    _encode_secret,
    _is_windows,  # used by save_config
)

logger = logging.getLogger(__name__)

# --- 定数定義 ---
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
_CONFIG_LOCK = threading.RLock()

# プロセス内キャッシュ: load_config() はAIリクエスト等のホットパスから頻繁に呼ばれるため、
# ファイルI/Oとロック取得を抑える。キャッシュはファイルのmtime+sizeでキーされ、
# ファイルが変更/削除されると自動的に無効化される（save_config時は即時クリア）。
_CONFIG_CACHE: dict = {"data": None, "key": None}

DEFAULT_CONFIG = {
    "mistral_model": "mistral-medium-3.5",
    "model_badge": "mistral-medium-v3.5",
    "api_credentials": {},
    "custom_ai_prompt": "",
}


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


def _write_and_replace_with_fcntl_lock(
    data: dict, tmp_file: Path, target_file: Path, lock_file: Path
) -> None:
    """Write and replace with POSIX fcntl.flock locking."""
    try:
        import fcntl  # type: ignore[import-untyped]

        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
            # Restrictive umask so the temp file is never world/readable,
            # even momentarily, before the final chmod (M-5).
            old_umask = os.umask(0o077)
            try:
                fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
            finally:
                os.umask(old_umask)
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
            try:
                os.unlink(lock_file)
            except OSError:
                pass
    except (ImportError, OSError) as exc:
        logger.debug("fcntl lock unavailable, writing without lock: %s", exc)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
        max_lock_retries = 10
        try:
            # Ensure the lock file has at least 1 byte of data so msvcrt.locking succeeds.
            # Otherwise, locking a 0-byte file might fail or be ignored on Windows.
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"L")
                os.lseek(fd, 0, os.SEEK_SET)

            for attempt in range(max_lock_retries):
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    locked = True
                    break
                except OSError:
                    if attempt < max_lock_retries - 1:
                        base_delay = 0.05 * (attempt + 1)
                        jitter = random.uniform(0.01, 0.05)
                        time.sleep(base_delay + jitter)
                        continue
                    raise RuntimeError(f"msvcrt lock busy, failed to acquire lock on: {lock_file}")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
            try:
                os.unlink(lock_file)
            except OSError:
                pass
    except (ImportError, OSError) as exc:
        logger.debug("msvcrt lock unavailable for config save, writing without lock: %s", exc)
        # Only reached when msvcrt is genuinely unavailable (not contention),
        # so a lock-free write is the last-resort fallback.
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
    except (IOError, OSError) as exc:
        logger.warning("Error during corrupt backups rotation: %s", exc, exc_info=True)


def _config_cache_key():
    """ファイルのmtime+sizeからキャッシュキーを生成（存在しない場合は 'missing'）。"""
    try:
        st = CONFIG_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return "missing"


def load_config():
    """設定ファイルを読み込む。存在しない場合は初期化。

    Always returns a deep copy of the cached config. Callers that mutate the
    returned dict and then pass it to ``save_config`` must not accidentally
    corrupt the in-process cache (H-1). Mutations that are not saved will not
    leak into subsequent ``load_config`` results either.
    """
    with _CONFIG_LOCK:
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
                        
                        # LK_RLCK is a read-only (shared) lock on Windows
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
            return copy.deepcopy(cfg)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            corrupt_backup = CONFIG_FILE.with_suffix(
                CONFIG_FILE.suffix + f".corrupt.{datetime.now():%Y%m%d%H%M%S}.bak"
            )
            try:
                shutil.copy2(CONFIG_FILE, corrupt_backup)
                logger.warning(
                    "Corrupted config backed up to %s",
                    corrupt_backup,
                )
                _rotate_corrupt_backups(BASE_DIR)
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
    with _CONFIG_LOCK:
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
            try:
                backup_data = copy.deepcopy(data)
                if isinstance(backup_data.get("api_credentials"), dict):
                    backup_data["api_credentials"] = {}
                # Strip all secret entries from backups to avoid leaking secrets
                for secret_key in ("flask_secret_key", "mns_master_key", "extension_api_token"):
                    if secret_key in backup_data:
                        del backup_data[secret_key]
                # H-4: Write backup to a temp file with restricted permissions
                # BEFORE the rename, so the backup file is never exposed with
                # open permissions, even momentarily.
                import uuid

                backup_tmp = CONFIG_FILE.with_suffix(
                    CONFIG_FILE.suffix + f".bak.{uuid.uuid4().hex}.tmp"
                )
                if _is_windows():
                    with open(backup_tmp, "w", encoding="utf-8") as f:
                        json.dump(backup_data, f, ensure_ascii=False, indent=2)
                else:
                    # Write with 0o600 umask so the file is never world-readable
                    old_umask = os.umask(0o177)
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
            except (OSError, TypeError) as e:
                logger.warning("Failed to create config backup: %s", e)

        import uuid

        tmp_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + f".{uuid.uuid4().hex}.tmp")
        lock_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".lock")

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
                if tmp_file.exists():
                    try:
                        tmp_file.unlink()
                    except OSError as unlink_exc:
                        logger.debug("Failed to remove temp config file: %s", unlink_exc)
                logger.error(
                    "Failed to save config to %s after %d attempts: %s",
                    CONFIG_FILE,
                    max_retries,
                    exc,
                    exc_info=True,
                )
                raise

        # Set restrictive file permissions for security on non-Windows systems
        if not _is_windows() and CONFIG_FILE.exists():
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except Exception as exc:
                logger.warning("Failed to set config file permissions: %s", exc)


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

    # Ephemeral fallback check to prevent silent data loss upon restart in headless/container environments
    from crypto_utils import KEYRING_AVAILABLE, _is_windows
    if not KEYRING_AVAILABLE and not _is_windows() and os.environ.get("MNS_EPHEMERAL_FALLBACK") == "1":
        raise RuntimeError(
            "FATAL: Secure storage (keyring/DPAPI) is unavailable, and MNS_EPHEMERAL_FALLBACK=1 is active, "
            "but MNS_MASTER_KEY is not set in the environment. "
            "Generating or using a temporary master key would cause encrypted configurations and portfolio data "
            "to become unreadable and lost upon next restart. Please set a persistent MNS_MASTER_KEY in your environment."
        )

    cfg = load_config()
    if not isinstance(cfg, dict):
        cfg = {}

    key_entry = cfg.get("mns_master_key")
    if key_entry and isinstance(key_entry, dict):
        key = _decode_secret(key_entry, "mns_master_key")
        if key:
            return key

    # Generate a new Fernet key
    from cryptography.fernet import Fernet

    new_key = Fernet.generate_key().decode("ascii")
    protected_entry = _encode_secret(new_key, "mns_master_key")

    # Reuse cfg already loaded above instead of re-reading the file
    cfg["mns_master_key"] = protected_entry

    try:
        save_config(cfg)
    except Exception as exc:
        logger.error("Failed to save generated master key to config file: %s", exc)

    return new_key
