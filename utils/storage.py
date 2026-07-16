# utils/storage.py
"""Data persistence logic, managing encrypted saving and loading of user stock configurations."""

import copy
import datetime
import json
import logging
import os
import shutil
from pathlib import Path

import config_store
from app_state import app_state
from crypto_utils import _is_windows, protect_data, unprotect_data

logger = logging.getLogger(__name__)

USER_STOCKS_FILE = str(config_store.USER_STOCKS_FILE)
LEGACY_USER_STOCKS_FILE = str(config_store.BASE_DIR / "user_stocks.json")


def _migrate_legacy_user_stocks() -> None:
    legacy = Path(LEGACY_USER_STOCKS_FILE)
    target = Path(USER_STOCKS_FILE)
    if target.exists() or not legacy.exists():
        return
    try:
        config_store.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
        logger.info("Migrated legacy user stocks file %s -> %s", legacy, target)
    except OSError as exc:
        logger.warning("Failed to migrate legacy user stocks file %s: %s", legacy, exc)


def load_user_stocks(force=False):
    """ユーザーの銘柄設定をファイルから読み込む。"""
    config_store.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_user_stocks()
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

            lock_file = Path(USER_STOCKS_FILE).with_suffix(".lock")
            raw_data = None

            if os.name == "nt":  # Windows
                try:
                    import msvcrt

                    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                    locked = False
                    try:
                        if os.fstat(fd).st_size < 1:
                            os.write(fd, b"L")
                            os.lseek(fd, 0, os.SEEK_SET)

                        msvcrt.locking(fd, msvcrt.LK_RLCK, 1)  # type: ignore[attr-defined]
                        locked = True
                        with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                            raw_data = json.load(f)
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
                except (ImportError, OSError, json.JSONDecodeError) as exc:
                    logger.debug(
                        "msvcrt shared lock read failed for user_stocks, falling back: %s", exc
                    )
            else:  # Unix
                try:
                    import fcntl

                    lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                    locked = False
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_SH)  # type: ignore[attr-defined]
                        locked = True
                        with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                            raw_data = json.load(f)
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
                except (ImportError, OSError, json.JSONDecodeError) as exc:
                    logger.debug(
                        "fcntl shared lock read failed for user_stocks, falling back: %s", exc
                    )

            if raw_data is None:
                with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)

            if isinstance(raw_data, dict) and "scheme" in raw_data and "value" in raw_data:
                _master_key = config_store.get_or_create_master_key()
                unprotected = unprotect_data(
                    raw_data, key_name="user_stocks", master_key=_master_key
                )
                if unprotected:
                    data = json.loads(unprotected)
                else:
                    # Decryption failed: DO NOT reset the in-memory lists to {}.
                    # Wiping them would let a later save_user_stocks() persist an
                    # empty set over the (backed-up) on-disk data, causing
                    # irreversible loss of the user's portfolio. Instead we keep
                    # the current in-memory state, flag the error, and abort the
                    # load so the on-disk data remains recoverable.
                    app_state.market.user_stocks_load_error = True
                    try:
                        _backup_unreadable_user_stocks()
                    except (IOError, OSError) as backup_exc:
                        logger.debug(
                            "Failed to back up unreadable user_stocks.json: %s", backup_exc
                        )
                    logger.error(
                        "Failed to decrypt user_stocks.json (master key / keyring mismatch?). "
                        "Keeping current in-memory data; NOT overwriting. Check MNS_MASTER_KEY "
                        "or the OS credential store."
                    )
                    return
            else:
                data = raw_data

            # Reset any prior load error now that we read successfully.
            app_state.market.user_stocks_load_error = False

            if not isinstance(data, dict):
                data = {}
            # Validate each sub-container is a dict; a malformed/hand-edited file
            # could carry a list/string here, which would crash later on
            # `container[symbol] = name`. Fall back to {} per section.
            us = data.get("us", {}) if isinstance(data.get("us"), dict) else {}
            jp = data.get("jp", {}) if isinstance(data.get("jp"), dict) else {}
            idx = data.get("idx", {}) if isinstance(data.get("idx"), dict) else {}
            app_state.market.user_us = us
            app_state.market.user_jp = jp
            app_state.market.user_idx = idx
            try:
                app_state.market.last_usdjpy_rate = float(data.get("last_usdjpy_rate", 150.00))
            except (ValueError, TypeError):
                app_state.market.last_usdjpy_rate = 150.00
            app_state.market.last_loaded_rev = app_state.market.user_stocks_rev
    except (IOError, OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load user stocks: %s", exc)


def _backup_unreadable_user_stocks() -> None:
    """Create a recoverable copy of an unreadable/encrypted user_stocks.json.

    The decryption-failure path keeps the in-memory data and aborts the load,
    so the on-disk file is the only recoverable artifact. Copy it to a .bak so
    the user can recover once the master key / keyring is fixed.
    """
    backup_path = Path(USER_STOCKS_FILE).with_suffix(
        ".bak." + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    )
    try:
        shutil.copy2(USER_STOCKS_FILE, backup_path)
        logger.info("Backed up unreadable user_stocks.json to %s", backup_path)
    except (IOError, OSError) as exc:
        logger.warning("Could not back up unreadable user_stocks.json: %s", exc)


class UserStocksPersistError(RuntimeError):
    """Raised when user_stocks.json could not be written safely."""


def _write_user_stocks_with_lock(
    data_encoded: str, tmp_file: Path, target_file: Path, lock_file: Path
) -> None:
    """Write encrypted user stock data with cross-platform file locking.

    Uses fcntl.flock on Unix and msvcrt.locking on Windows, matching the
    pattern in config_store._write_with_lock.

    Raises:
        UserStocksPersistError: when the Windows lock cannot be acquired after
            retries (callers must surface this instead of treating skip as success).
    """
    if os.name == "nt":  # Windows
        try:
            import msvcrt
            import time

            fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
            locked = False
            max_lock_retries = 5
            try:
                # Ensure the lock file has at least 1 byte of data so msvcrt.locking succeeds.
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
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        raise UserStocksPersistError(
                            f"user_stocks.json lock busy on Windows after {max_lock_retries} retries: {lock_file}"
                        )
                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(data_encoded)
                # Promote inside the lock so there is no window (lock-release ->
                # os.replace) during which a concurrent writer could overwrite
                # the temp file or publish another writer's content.
                if tmp_file.exists():
                    os.replace(tmp_file, target_file)
                else:
                    raise UserStocksPersistError(
                        f"user_stocks tmp file missing after locked write: {tmp_file}"
                    )
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
        except UserStocksPersistError:
            raise
        except (ImportError, OSError) as exc:
            logger.debug("msvcrt lock unavailable for user_stocks: %s", exc)
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(data_encoded)
            if tmp_file.exists():
                os.replace(tmp_file, target_file)
    else:  # Unix/POSIX
        try:
            import fcntl  # type: ignore[import]  # Unix-only; unavailable on Windows

            lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
                old_umask = os.umask(0o077)
                try:
                    fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.write(data_encoded)
                    except Exception:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        raise
                finally:
                    os.umask(old_umask)
                # Promote inside the lock (no TOCTOU window).
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

    Raises:
        UserStocksPersistError: if the write cannot complete safely. Callers
            must treat this as failure (do not report success to the client).
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

            # Write through a unique tmp file with cross-platform file locking.
            # The tmp file is unique per call (uuid) so concurrent writers never
            # clobber each other's buffer, and os.replace(tmp -> final) is done
            # INSIDE the lock inside _write_user_stocks_with_lock (no TOCTOU window).
            encoded_data = json.dumps(protected, ensure_ascii=False, indent=2)
            import uuid

            tmp_file = Path(USER_STOCKS_FILE).with_suffix(f".{uuid.uuid4().hex}.tmp")
            lock_file = Path(USER_STOCKS_FILE).with_suffix(".lock")

            _write_user_stocks_with_lock(encoded_data, tmp_file, Path(USER_STOCKS_FILE), lock_file)

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
    except UserStocksPersistError:
        # Propagate explicitly so API handlers can return 503/409 instead of lying.
        raise
    except (IOError, OSError, TypeError) as exc:
        logger.error("Failed to save user stocks: %s", exc)
        raise UserStocksPersistError(f"Failed to save user stocks: {exc}") from exc
