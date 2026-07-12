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

from crypto_utils import _is_windows  # noqa: F401 -- used by save_config

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


def _write_with_lock(data: dict, tmp_file: Path, lock_file: Path) -> None:
    """Write JSON data to tmp_file with platform-appropriate file locking.

    Uses fcntl.flock on Unix/POSIX and msvcrt.locking on Windows.
    Falls back to lock-free write if neither is available.
    """
    if os.name == "nt":  # Windows
        _write_with_msvcrt_lock(data, tmp_file, lock_file)
    else:
        _write_with_fcntl_lock(data, tmp_file, lock_file)


def _write_with_fcntl_lock(data: dict, tmp_file: Path, lock_file: Path) -> None:
    """Write with POSIX fcntl.flock locking."""
    try:
        import fcntl  # type: ignore[import-untyped]
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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


def _write_with_msvcrt_lock(data: dict, tmp_file: Path, lock_file: Path) -> None:
    """Write with Windows msvcrt.locking."""
    try:
        import msvcrt  # type: ignore[import-untyped]
        fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            # Lock contention: another process is writing, write without lock
            logger.debug("msvcrt lock busy, writing without lock: %s", lock_file)
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(lock_file)
            except OSError:
                pass
    except (ImportError, OSError) as exc:
        logger.debug("msvcrt lock unavailable, writing without lock: %s", exc)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _rotate_corrupt_backups(directory: Path, limit: int = 5):
    """Keep only the latest N corrupted backup files and remove the older ones."""
    try:
        # Pattern: config.json.corrupt.*.bak
        backups = sorted(
            directory.glob("config.json.corrupt.*.bak"),
            key=lambda p: p.stat().st_mtime
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
    """設定ファイルを読み込む。存在しない場合は初期化"""
    with _CONFIG_LOCK:
        # ファイルのmtime+sizeでキャッシュキーを作り、変更があれば再読込する
        cached = _CONFIG_CACHE["data"]
        cache_key = _config_cache_key()
        if cached is not None and _CONFIG_CACHE["key"] == cache_key:
            return cached
        if CONFIG_FILE.exists():
            # crypto_utilsの循環参照を避けるため直接 chmod を試みる
            try:
                if not _is_windows():
                    CONFIG_FILE.chmod(0o600)
            except Exception:
                pass
        else:
            save_config(DEFAULT_CONFIG)
            _CONFIG_CACHE["data"] = copy.deepcopy(DEFAULT_CONFIG)
            _CONFIG_CACHE["key"] = _config_cache_key()
            return _CONFIG_CACHE["data"]
        try:
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
            return cfg
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
                backup_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
                with open(backup_file, "w", encoding="utf-8") as f:
                    json.dump(backup_data, f, ensure_ascii=False, indent=2)
                if not _is_windows():
                    try:
                        os.chmod(backup_file, 0o600)
                    except Exception as exc:
                        logger.warning(
                            "Failed to set config backup permissions: %s", exc
                        )
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
                # --- プラットフォーム別ファイルロック ---
                _write_with_lock(data, tmp_file, lock_file)

                # os.replace はアトミックだが、Windowsではファイルが開かれていると失敗する
                try:
                    os.replace(tmp_file, CONFIG_FILE)
                except PermissionError as perm_exc:
                    logger.warning(
                        "PermissionError during config replace (attempt %d/%d): %s. Retrying...",
                        attempt + 1,
                        max_retries,
                        perm_exc,
                    )
                    if attempt < max_retries - 1:
                        time.sleep(0.1 * (attempt + 1))
                        continue
                    raise
                break  # 成功
            except (OSError, TypeError) as exc:
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
