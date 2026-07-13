# utils/storage.py
"""Data persistence logic, managing encrypted saving and loading of user stock configurations."""

import copy
import json
import logging
import os
from pathlib import Path

from app_state import app_state
import config_store
from config_utils import protect_data, unprotect_data, _is_windows
from constants import BASE_DIR

logger = logging.getLogger(__name__)

USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")


def load_user_stocks(force=False):
    """ユーザーの銘柄設定をファイルから読み込む。"""
    if not os.path.exists(USER_STOCKS_FILE):
        return
    try:
        # Hold the internal lock for the whole read so a concurrent save_user_stocks()
        # cannot swap the file under us mid-read (which would raise
        # JSONDecodeError and force a corrupt-backup). The in-memory version
        # counter is the authoritative "newer than cached" signal; mtime is a
        # secondary hint only.
        with app_state.market.user_stocks_lock:
            if not force and app_state.market.user_stocks_rev == app_state.market.last_loaded_rev:
                return
            with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            if (
                isinstance(raw_data, dict)
                and "scheme" in raw_data
                and "value" in raw_data
            ):
                _master_key = config_store.get_or_create_master_key()
                unprotected = unprotect_data(raw_data, key_name="user_stocks", master_key=_master_key)
                if unprotected:
                    data = json.loads(unprotected)
                else:
                    data = {}
                    # Decryption failed or empty, backup corrupted file
                    try:
                        import time
                        import glob
                        backups = sorted(glob.glob(f"{USER_STOCKS_FILE}.bak.*"))
                        if len(backups) >= 5:
                            for old_backup in backups[:-4]:
                                try:
                                    os.remove(old_backup)
                                except OSError:
                                    pass
                        backup_file = f"{USER_STOCKS_FILE}.bak.{int(time.time())}"
                        os.replace(USER_STOCKS_FILE, backup_file)
                        logger.warning("Backed up corrupted user_stocks.json to %s due to decryption failure", backup_file)
                    except OSError:
                        pass
            else:
                data = raw_data

            if not isinstance(data, dict):
                data = {}
            app_state.market.user_us = data.get("us", {}) or {}
            app_state.market.user_jp = data.get("jp", {}) or {}
            app_state.market.user_idx = data.get("idx", {}) or {}
            app_state.market.last_usdjpy_rate = float(data.get("last_usdjpy_rate", 150.00))
            app_state.market.last_loaded_rev = app_state.market.user_stocks_rev
    except (IOError, OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load user stocks: %s", exc)


def _write_user_stocks_with_lock(data_encoded: str, tmp_file: Path, lock_file: Path) -> None:
    """Write encrypted user stock data with cross-platform file locking.

    Uses fcntl.flock on Unix and msvcrt.locking on Windows, matching the
    pattern in config_store._write_with_lock.
    """
    if os.name == "nt":  # Windows
        try:
            import msvcrt
            fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
            locked = False
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                locked = True
                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(data_encoded)
            except OSError:
                # Lock contention: write without lock and let os.replace handle atomicity
                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(data_encoded)
            finally:
                if locked:
                    try:
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
            logger.debug("msvcrt lock unavailable for user_stocks: %s", exc)
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(data_encoded)
    else:  # Unix/POSIX
        try:
            import fcntl  # type: ignore[import]  # Unix-only; unavailable on Windows
            lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(data_encoded)
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
            logger.debug("fcntl lock unavailable for user_stocks: %s", exc)
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(data_encoded)


def save_user_stocks():
    """ユーザーの銘柄設定をファイルに保存する。

    Uses threading.RLock for write-order safety. The lock is acquired
    externally (via app_state.market.user_stocks_lock), which callers must
    already hold for read operations. A process-internal monotonic version
    counter (user_stocks_rev) is bumped inside the lock after the atomic
    os.replace so that concurrent load_user_stocks() calls reliably detect the
    newer content without relying solely on filesystem mtime (which is also
    bumped here, inside the lock, to stay consistent across processes).
    File-level locking (fcntl/msvcrt) is used to prevent corruption when
    multiple processes (e.g. Gunicorn workers) write concurrently.
    """
    try:
        with app_state.market.user_stocks_lock:
            data = {
                "us": copy.deepcopy(app_state.market.user_us),
                "jp": copy.deepcopy(app_state.market.user_jp),
                "idx": copy.deepcopy(app_state.market.user_idx),
                "last_usdjpy_rate": float(getattr(app_state.market, "last_usdjpy_rate", 150.00)),
            }
            encoded = json.dumps(data, ensure_ascii=False, indent=2)
            _master_key = config_store.get_or_create_master_key()
            protected = protect_data(encoded, key_name="user_stocks", master_key=_master_key)

            # Write through a tmp file with cross-platform file locking
            encoded_data = json.dumps(protected, ensure_ascii=False, indent=2)
            tmp_file = Path(USER_STOCKS_FILE).with_suffix(".tmp")
            lock_file = Path(USER_STOCKS_FILE).with_suffix(".lock")

            _write_user_stocks_with_lock(encoded_data, tmp_file, lock_file)

            os.replace(tmp_file, USER_STOCKS_FILE)

            if not _is_windows():
                try:
                    os.chmod(USER_STOCKS_FILE, 0o600)
                except OSError as exc:
                    logger.debug(
                        "Failed to set restrictive permissions on %s: %s", USER_STOCKS_FILE, exc
                    )

            # Bump version + mtime inside the lock so the snapshot is internally
            # consistent. Order matters: readers compare user_stocks_rev (authoritative).
            app_state.market.user_stocks_rev += 1
            app_state.market.last_modified_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
    except (IOError, OSError, TypeError) as exc:
        logger.error("Failed to save user stocks: %s", exc)
