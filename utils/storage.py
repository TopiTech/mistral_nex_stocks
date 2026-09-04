# utils/storage.py
"""Data persistence logic, managing encrypted saving and loading of user stock configurations."""

import copy
import datetime
import json
import logging
import math
import os
import shutil
import time
import uuid
from pathlib import Path

import config_store
from app_state import app_state
from crypto_utils import _is_windows, protect_data, unprotect_data
from utils.normalization import normalize_symbol_for_market

logger = logging.getLogger(__name__)

USER_STOCKS_FILE = str(config_store.USER_STOCKS_FILE)
LEGACY_USER_STOCKS_FILE = str(config_store.BASE_DIR / "user_stocks.json")
_USER_STOCKS_READ_FAILED = object()


def _normalize_jp_holding_keys(holdings: dict) -> dict:
    """Canonicalize unambiguous persisted JP numeric tickers to ``.T`` form.

    Earlier public ingress paths accepted ``7203`` for market ``jp`` while the
    rest of the provider/cache/realtime contract uses ``7203.T``.  Normalize
    the legacy key before it reaches those consumers.  If both representations
    are already present, retain both rather than guessing how to merge separate
    user positions.  The delete API deliberately removes both aliases as one
    explicit user action; no implicit load/save operation discards a position.
    """
    normalized = dict(holdings)
    for raw_symbol in list(normalized):
        canonical = normalize_symbol_for_market(raw_symbol, "jp")
        if not canonical or canonical == raw_symbol:
            continue
        if canonical in normalized:
            logger.warning(
                "Keeping ambiguous legacy JP holdings %r and %r separate; "
                "delete either spelling to remove the logical stock",
                raw_symbol,
                canonical,
            )
            continue
        normalized[canonical] = normalized.pop(raw_symbol)
    return normalized


def _migrate_legacy_user_stocks() -> bool:
    """Migrate the legacy plaintext store and report whether it is safe to load.

    ``False`` is deliberately reserved for the case where a legacy file exists
    but could not be migrated.  Callers must distinguish that state from a
    genuinely absent store: treating a failed migration as an empty portfolio
    would allow the next save to overwrite the only recoverable copy.
    """
    legacy = Path(LEGACY_USER_STOCKS_FILE)
    target = Path(USER_STOCKS_FILE)
    if target.exists() or not legacy.exists():
        return True
    tmp_file: Path | None = None
    try:
        with legacy.open("r", encoding="utf-8") as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise TypeError("legacy user stocks payload must be a JSON object")

        # Legacy files were plaintext.  Never copy them into the active store:
        # convert the complete payload to the current Fernet envelope first.
        encoded = json.dumps(data, ensure_ascii=False, indent=2)
        master_key = config_store.get_or_create_master_key()
        protected = protect_data(encoded, key_name="user_stocks", master_key=master_key)
        config_store.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        content = json.dumps(protected, ensure_ascii=False, indent=2)
        if _is_windows():
            tmp_file.write_text(content, encoding="utf-8")
        else:
            old_umask = os.umask(0o077)
            try:
                fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
            finally:
                os.umask(old_umask)
        os.replace(tmp_file, target)
        if not _is_windows():
            os.chmod(target, 0o600)
        logger.info("Migrated and encrypted legacy user stocks file %s -> %s", legacy, target)
        # The legacy file was plaintext. Remove it now that the encrypted copy
        # is in place so the raw portfolio data does not linger unencrypted on
        # disk (migration is a one-way hardening step).
        try:
            legacy.unlink()
            logger.info("Removed legacy plaintext user stocks file %s", legacy)
        except OSError as rm_exc:
            logger.warning("Failed to remove legacy plaintext file %s: %s", legacy, rm_exc)
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        try:
            if tmp_file is not None:
                tmp_file.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("Failed to migrate legacy user stocks file %s: %s", legacy, exc)
        return False


def _locked_read_user_stocks(lock_file: Path):
    """Best-effort shared-locked read of USER_STOCKS_FILE.

    Returns parsed JSON, or the private ``_USER_STOCKS_READ_FAILED`` sentinel if
    the lock could not be acquired / the read failed. ``None`` is a valid JSON
    value and must not be conflated with an I/O failure. The lock file is kept
    persistent (see _write_user_stocks_with_lock, MNS-004) so the advisory lock
    always binds the same inode across processes.
    """
    if os.name == "nt":  # Windows
        try:
            import msvcrt

            fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            locked = False
            try:
                # [R4] msvcrt.LK_RLCK acts as an exclusive lock on Windows, similar to LK_LOCK.
                # Shared reads are inherently blocked by other readers on Windows.
                msvcrt.locking(fd, msvcrt.LK_RLCK, 1)  # type: ignore[attr-defined]
                locked = True

                # [R1] Ensure file size checks and writes are done after acquiring the lock to prevent race conditions.
                if os.fstat(fd).st_size < 1:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)

                with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
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
            logger.debug("msvcrt shared lock read failed for user_stocks: %s", exc)
            return _USER_STOCKS_READ_FAILED
    else:  # Unix
        try:
            import fcntl

            lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            locked = False
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH)  # type: ignore[attr-defined]
                locked = True
                with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
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
            logger.debug("fcntl shared lock read failed for user_stocks: %s", exc)
            return _USER_STOCKS_READ_FAILED
    return _USER_STOCKS_READ_FAILED


def _mark_user_stocks_load_failure(reason: str) -> None:
    """Preserve the current portfolio and prevent a later destructive save."""
    app_state.market.user_stocks_load_error = True
    try:
        _backup_unreadable_user_stocks()
    except OSError as backup_exc:
        logger.debug("Failed to back up unreadable user_stocks.json: %s", backup_exc)
    logger.error(
        "%s Keeping current in-memory data and refusing subsequent saves until a valid reload.",
        reason,
    )


def load_user_stocks(force=False):
    """ユーザーの銘柄設定をファイルから読み込む。"""
    config_store.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    migration_ok = _migrate_legacy_user_stocks()
    if not os.path.exists(USER_STOCKS_FILE):
        # A legacy file that could not be migrated is not equivalent to an
        # absent portfolio.  Preserve the in-memory state and fail closed so a
        # later mutation cannot replace the recoverable plaintext file with an
        # empty/stale encrypted store.
        if not migration_ok:
            _mark_user_stocks_load_failure(
                "Failed to migrate legacy user_stocks.json; refusing to treat it as empty."
            )
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
            raw_data = _locked_read_user_stocks(lock_file)

            if raw_data is _USER_STOCKS_READ_FAILED:
                # MNS-005: The locked read above failed (e.g. temporary lock contention).
                # Wait briefly and retry once before falling back to an unlocked read.
                time.sleep(0.05)
                raw_data = _locked_read_user_stocks(lock_file)
                if raw_data is _USER_STOCKS_READ_FAILED:
                    logger.warning(
                        "Locked read of user_stocks.json failed after retry; reading without lock as last resort"
                    )
                    try:
                        with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                            raw_data = json.load(f)
                    except (OSError, json.JSONDecodeError) as exc:
                        logger.error("Unlocked read of user_stocks.json also failed: %s", exc)
                        raw_data = _USER_STOCKS_READ_FAILED

            if raw_data is _USER_STOCKS_READ_FAILED:
                _mark_user_stocks_load_failure("Failed to read or parse user_stocks.json.")
                return

            if isinstance(raw_data, dict) and "scheme" in raw_data and "value" in raw_data:
                _master_key = config_store.get_or_create_master_key()
                unprotected = unprotect_data(
                    raw_data, key_name="user_stocks", master_key=_master_key
                )
                if unprotected:
                    try:
                        data = json.loads(unprotected)
                    except json.JSONDecodeError:
                        _mark_user_stocks_load_failure(
                            "Decrypted user_stocks.json contains invalid JSON."
                        )
                        return
                else:
                    # Decryption failed: DO NOT reset the in-memory lists to {}.
                    # Wiping them would let a later save_user_stocks() persist an
                    # empty set over the (backed-up) on-disk data, causing
                    # irreversible loss of the user's portfolio. Instead we keep
                    # the current in-memory state, flag the error, and abort the
                    # load so the on-disk data remains recoverable.
                    _mark_user_stocks_load_failure(
                        "Failed to decrypt user_stocks.json (master key / keyring mismatch?)."
                    )
                    return
            else:
                data = raw_data

            if not isinstance(data, dict):
                _mark_user_stocks_load_failure("user_stocks.json root value must be a JSON object.")
                return

            # Reset any prior load error now that we read successfully.
            app_state.market.user_stocks_load_error = False

            # A malformed section must not be normalized to {}: doing so would
            # let the next save overwrite recoverable holdings with an empty map.
            invalid_sections = [
                section
                for section in ("us", "jp", "idx")
                if section in data and not isinstance(data.get(section), dict)
            ]
            if invalid_sections:
                _mark_user_stocks_load_failure(
                    "user_stocks.json contains non-object portfolio sections: "
                    + ", ".join(invalid_sections)
                )
                return
            us = data.get("us", {})
            jp = _normalize_jp_holding_keys(data.get("jp", {}))
            idx = data.get("idx", {})
            app_state.market.user_us = us
            app_state.market.user_jp = jp
            app_state.market.user_idx = idx
            snapshot_us_keys = list(us.keys())
            snapshot_jp_keys = list(jp.keys())
            try:
                loaded_rate = float(data.get("last_usdjpy_rate", 150.00))
                app_state.market.last_usdjpy_rate = (
                    loaded_rate if math.isfinite(loaded_rate) and loaded_rate > 0.0 else 150.00
                )
            except (ValueError, TypeError):
                app_state.market.last_usdjpy_rate = 150.00
            try:
                rate_ts = float(data.get("last_usdjpy_rate_ts", 0.0))
                app_state.market.last_usdjpy_rate_ts = (
                    rate_ts if math.isfinite(rate_ts) and rate_ts > 0.0 else 0.0
                )
            except (ValueError, TypeError):
                app_state.market.last_usdjpy_rate_ts = 0.0
            app_state.market.last_loaded_rev = app_state.market.user_stocks_rev
        # register_symbols acquires yahoojp_scraper.lock which can deadlock
        # against _pts_worker_loop's yahoojp_scraper.lock -> user_stocks_lock
        # order. Snapshot above and publish outside the user_stocks_lock.
        try:
            from services.realtime_engine import realtime_market_engine

            realtime_market_engine.register_symbols(
                snapshot_us_keys,
                snapshot_jp_keys,
            )
        except Exception as exc:
            logger.debug("Failed registering loaded symbols with RealtimeMarketEngine: %s", exc)
    except (OSError, json.JSONDecodeError) as exc:
        _mark_user_stocks_load_failure(f"Failed to load user stocks: {exc}")


def _rotate_user_stocks_backups(directory: Path, limit: int = 5) -> None:
    """Keep only the latest N user_stocks backup files and remove the older ones."""
    try:
        backups = sorted(directory.glob("user_stocks.bak.*"), key=lambda p: p.stat().st_mtime)
        if len(backups) > limit:
            to_remove = backups[:-limit]
            for p in to_remove:
                try:
                    p.unlink(missing_ok=True)
                    logger.info("Removed old user_stocks backup: %s", p.name)
                except OSError as exc:
                    logger.debug("Failed to remove old user_stocks backup %s: %s", p.name, exc)
    except OSError as exc:
        logger.warning("Error during user_stocks backups rotation: %s", exc, exc_info=True)


def _backup_unreadable_user_stocks() -> None:
    """Create a recoverable copy of an unreadable/encrypted user_stocks.json.

    The decryption-failure path keeps the in-memory data and aborts the load,
    so the on-disk file is the only recoverable artifact. Copy it to a .bak so
    the user can recover once the master key / keyring is fixed.
    """
    target_path = Path(USER_STOCKS_FILE)
    backup_path = target_path.with_suffix(
        ".bak." + datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
    )
    try:
        shutil.copy2(USER_STOCKS_FILE, backup_path)
        logger.info("Backed up unreadable user_stocks.json to %s", backup_path)
        _rotate_user_stocks_backups(target_path.parent)
    except OSError as exc:
        logger.warning("Could not back up unreadable user_stocks.json: %s", exc)


class UserStocksPersistError(RuntimeError):
    """Raised when user_stocks.json could not be written safely."""


def _write_user_stocks_with_lock(
    data_encoded: str, tmp_file: Path, target_file: Path, lock_file: Path
) -> None:
    """Write encrypted user stock data with cross-platform file locking.

    Uses fcntl.flock on Unix and msvcrt.locking on Windows, matching the
    pattern in config_store._write_with_lock.

    Both branches are fail-closed: if the platform lock cannot be acquired or
    is unavailable, the write is abandoned rather than performed unlocked.

    Raises:
        UserStocksPersistError: when the lock cannot be acquired after retries,
            or when locking is unavailable on either platform (callers must
            surface this instead of treating a skipped write as success).
    """
    if os.name == "nt":  # Windows
        try:
            import msvcrt

            fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
            locked = False
            max_lock_retries = 5
            try:
                for attempt in range(max_lock_retries):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                        locked = True
                        break
                    except OSError as err:
                        if attempt < max_lock_retries - 1:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        raise UserStocksPersistError(
                            f"user_stocks.json lock busy on Windows after {max_lock_retries} retries: {lock_file}"
                        ) from err
                # Ensure the lock file has at least 1 byte of data after lock is acquired
                if os.fstat(fd).st_size < 1:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)

                try:
                    with open(tmp_file, "w", encoding="utf-8") as f:
                        f.write(data_encoded)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except OSError:
                            pass
                    if tmp_file.exists():
                        for attempt in range(4):
                            try:
                                os.replace(tmp_file, target_file)
                                break
                            except PermissionError:
                                if _is_windows() and attempt < 3:
                                    time.sleep(0.015 * (attempt + 1))
                                    continue
                                raise
                        o_dir = getattr(os, "O_DIRECTORY", None)
                        if os.name != "nt" and o_dir is not None:
                            try:
                                dir_fd = os.open(str(target_file.parent), o_dir)
                                try:
                                    os.fsync(dir_fd)
                                finally:
                                    os.close(dir_fd)
                            except OSError:
                                pass
                    else:
                        raise UserStocksPersistError(
                            f"user_stocks tmp file missing after locked write: {tmp_file}"
                        )
                finally:
                    if tmp_file.exists():
                        try:
                            tmp_file.unlink(missing_ok=True)
                        except OSError:
                            pass
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
        except UserStocksPersistError:
            raise
        except (ImportError, OSError) as exc:
            # Same fail-closed policy as the POSIX branch below: never fall back
            # to an unlocked write. Silently degrading here would break the
            # concurrency guarantee this function documents, and would do so
            # precisely when another process holds the file - the lost-update
            # case the lock exists to prevent. Every caller already handles
            # UserStocksPersistError by rolling back its in-memory mutation.
            logger.error("Unable to lock user_stocks for safe persistence: %s", exc)
            if tmp_file.exists():
                try:
                    tmp_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise UserStocksPersistError(
                "Cannot safely persist user_stocks without a file lock"
            ) from exc
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
                # Promote inside the lock (no TOCTOU window).
                os.replace(tmp_file, target_file)
                o_dir = getattr(os, "O_DIRECTORY", None)
                if o_dir is not None:
                    try:
                        dir_fd = os.open(str(target_file.parent), o_dir)
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                    except OSError:
                        pass
            finally:
                if tmp_file.exists():
                    try:
                        tmp_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
                except OSError:
                    pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
                # MNS-004: Keep the lock file persistent (see Windows branch).
                # Do NOT os.unlink(lock_file) here.
        except (ImportError, OSError) as exc:
            logger.error("Unable to lock user_stocks for safe persistence: %s", exc)
            raise UserStocksPersistError(
                "Cannot safely persist user_stocks without an advisory lock"
            ) from exc


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
            # MNS-001: Never persist over the on-disk data when the previous
            # load failed. In that state the only recoverable
            # artifact is the encrypted file on disk (backed up to .bak by
            # load_user_stocks). Writing would overwrite it with the in-memory
            # state (which may be stale or empty) and cause irreversible loss.
            # The load path deliberately keeps in-memory state and flags this
            # error instead of wiping the lists; the save path must honor it.
            if getattr(app_state.market, "user_stocks_load_error", False):
                logger.error(
                    "Refusing to save user stocks: previous load failed "
                    "(user_stocks_load_error is set). Repair the file or restore "
                    "the key material, then reload before saving."
                )
                raise UserStocksPersistError(
                    "Cannot save: user_stocks.json could not be loaded safely. "
                    "Repair the file or restore the key material first."
                )

            # Retain the canonical representation at the persistence boundary
            # too, so a legacy in-memory state cannot reintroduce a bare JP
            # ticker after it has been normalized on load/API ingress.
            normalized_jp = _normalize_jp_holding_keys(app_state.market.user_jp)
            app_state.market.user_jp.clear()
            app_state.market.user_jp.update(normalized_jp)

            try:
                rate_ts = float(getattr(app_state.market, "last_usdjpy_rate_ts", 0.0))
            except (TypeError, ValueError):
                rate_ts = 0.0
            if not math.isfinite(rate_ts) or rate_ts <= 0.0:
                rate_ts = 0.0
            try:
                rate_val = float(getattr(app_state.market, "last_usdjpy_rate", 150.00))
                if not math.isfinite(rate_val) or rate_val <= 0.0:
                    rate_val = 150.00
            except (TypeError, ValueError):
                rate_val = 150.00
            data = {
                "us": copy.deepcopy(app_state.market.user_us),
                "jp": copy.deepcopy(app_state.market.user_jp),
                "idx": copy.deepcopy(app_state.market.user_idx),
                "last_usdjpy_rate": rate_val,
                "last_usdjpy_rate_ts": rate_ts,
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

            app_state.market.user_stocks_rev += 1
            app_state.market.last_modified_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
            saved_us_keys = list(app_state.market.user_us.keys())
            saved_jp_keys = list(app_state.market.user_jp.keys())
        # register_symbols acquires yahoojp_scraper.lock; publish outside
        # user_stocks_lock to avoid deadlock with _pts_worker_loop (R-deadlock).
        try:
            from services.realtime_engine import realtime_market_engine

            realtime_market_engine.register_symbols(saved_us_keys, saved_jp_keys)
        except Exception as e:
            logger.debug("Failed registering new symbols with RealtimeMarketEngine: %s", e)
    except UserStocksPersistError:
        # Propagate explicitly so API handlers can return 503/409 instead of lying.
        raise
    except Exception as exc:
        # Normalize every unexpected failure (RuntimeError from master-key creation,
        # ValueError from crypto, OSError from disk, TypeError from bad payloads)
        # so mutation handlers can roll back in-memory state consistently.
        logger.error("Failed to save user stocks: %s", exc)
        raise UserStocksPersistError(f"Failed to save user stocks: {exc}") from exc
