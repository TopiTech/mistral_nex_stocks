# utils/storage.py
"""Data persistence logic, managing encrypted saving and loading of user stock configurations."""

import copy
import datetime
import json
import logging
import math
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import config_store
from app_state import app_state
from constants import DEFAULT_USDJPY_RATE
from crypto_utils import _is_windows, protect_data, unprotect_data
from utils.normalization import is_bool_like, normalize_symbol_for_market
from utils.threading import restrictive_umask_lock

logger = logging.getLogger(__name__)

_DEFAULT_USER_STOCKS_FILE = str(config_store.USER_STOCKS_FILE)
_DEFAULT_LEGACY_USER_STOCKS_FILE = str(config_store.BASE_DIR / "user_stocks.json")
USER_STOCKS_FILE = _DEFAULT_USER_STOCKS_FILE
LEGACY_USER_STOCKS_FILE = _DEFAULT_LEGACY_USER_STOCKS_FILE
_USER_STOCKS_READ_FAILED = object()
_LEGACY_MIGRATION_LOCK = threading.Lock()


def _get_user_stocks_path() -> Path:
    """Resolve user_stocks path dynamically unless monkeypatched on module."""
    if USER_STOCKS_FILE != _DEFAULT_USER_STOCKS_FILE:
        return Path(USER_STOCKS_FILE)
    return Path(config_store.USER_STOCKS_FILE)


def _get_legacy_user_stocks_path() -> Path:
    """Resolve legacy user_stocks path dynamically unless monkeypatched."""
    if LEGACY_USER_STOCKS_FILE != _DEFAULT_LEGACY_USER_STOCKS_FILE:
        return Path(LEGACY_USER_STOCKS_FILE)
    return Path(config_store.BASE_DIR / "user_stocks.json")


def _update_market_usdjpy_rate(rate: float, ts: float | None = None) -> None:
    market = app_state.market
    updater = getattr(market, "update_usdjpy_rate", None)
    if callable(updater):
        updater(rate, ts)
    else:
        market.last_usdjpy_rate = rate
        if ts is not None:
            market.last_usdjpy_rate_ts = ts


def _snapshot_market_usdjpy_rate() -> tuple[float, float]:
    market = app_state.market
    snapshot_fn = getattr(market, "get_usdjpy_rate_snapshot", None)
    if callable(snapshot_fn):
        return snapshot_fn()
    raw_rate = getattr(market, "last_usdjpy_rate", DEFAULT_USDJPY_RATE)
    raw_ts = getattr(market, "last_usdjpy_rate_ts", 0.0)
    return raw_rate, raw_ts

# A-2 (cross-cutting twin of DATA-4): bounded POSIX write-lock retry budget for
# the user_stocks persistence path — ~2.5 s worst case, mirroring the bounded
# LOCK_EX|LOCK_NB pattern of config/atomic_write.py and config/locks.py instead
# of an unbounded blocking flock(LOCK_EX). Module-level constants so tests can
# shrink the budget.
_STORAGE_FCNTL_LOCK_MAX_RETRIES = 100
_STORAGE_FCNTL_LOCK_RETRY_DELAY_SEC = 0.025


def _coerce_positive_fx_rate(value: Any) -> float:
    """Return a finite positive USD/JPY rate or the documented default.

    ``bool`` is rejected explicitly: ``float(True)`` is ``1.0``, which would
    silently value every USD holding at 1 JPY/USD when a corrupted store
    contains ``true``. NaN/Inf and non-positive values are equally unusable.
    """
    if value is None or is_bool_like(value):
        return DEFAULT_USDJPY_RATE
    try:
        rate = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_USDJPY_RATE
    return rate if math.isfinite(rate) and rate > 0.0 else DEFAULT_USDJPY_RATE


def _coerce_fx_rate_timestamp(value: Any) -> float:
    """Return a finite positive timestamp, or 0.0 for missing/invalid input."""
    if value is None or is_bool_like(value):
        return 0.0
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp > 0.0 else 0.0


def _normalize_jp_holding_keys(holdings: dict) -> dict:
    """Canonicalize unambiguous persisted JP numeric tickers to ``.T`` form and sanitize FX fields.

    Earlier public ingress paths accepted ``7203`` for market ``jp`` while the
    rest of the provider/cache/realtime contract uses ``7203.T``.  Normalize
    the legacy key before it reaches those consumers.  If both representations
    are already present, retain both rather than guessing how to merge separate
    user positions.  The delete API deliberately removes both aliases as one
    explicit user action; no implicit load/save operation discards a position.

    Additionally, Japanese equities are JPY-denominated in the portfolio model
    and do not track USD/JPY FX rates, so strip any legacy or corrupted
    ``avg_fx_rate`` fields.
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

    for symbol, holding in list(normalized.items()):
        if isinstance(holding, dict) and "avg_fx_rate" in holding:
            clean_holding = dict(holding)
            clean_holding.pop("avg_fx_rate", None)
            normalized[symbol] = clean_holding

    return normalized


def _normalize_idx_holding_fields(holdings: dict) -> dict:
    """Remove legacy FX fields from persisted index holdings.

    Index holdings are JPY-denominated in the portfolio model.  Older versions
    accepted ``avg_fx_rate`` for ``idx`` and could persist it, so sanitize both
    loaded data and the in-memory value before the next save.
    """
    normalized = {}
    for symbol, holding in holdings.items():
        if isinstance(holding, dict) and "avg_fx_rate" in holding:
            clean_holding = dict(holding)
            clean_holding.pop("avg_fx_rate", None)
            normalized[symbol] = clean_holding
        else:
            normalized[symbol] = holding
    return normalized


def _fsync_directory_strict(directory: Path) -> None:
    """Fsync a directory entry, raising ``OSError`` when the platform supports it.

    ``config.atomic_write._fsync_directory`` is deliberately best-effort for
    callers that cannot act on a failure.  The legacy migration deletes its
    source immediately afterwards, so it must be able to distinguish "the
    target is durable" from "the directory fsync failed" and keep the source
    in the latter case.
    """
    o_dir = getattr(os, "O_DIRECTORY", None)
    if o_dir is None:
        return
    dir_fd = os.open(str(directory), o_dir)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _acquire_exclusive_lock_fd(lock_file: Path) -> int:
    """Acquire an advisory cross-process exclusive lock on *lock_file*."""
    if os.name == "nt":
        import msvcrt

        fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        locked = False
        max_retries = 20
        try:
            for attempt in range(max_retries):
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    locked = True
                    break
                except OSError as err:
                    if attempt < max_retries - 1:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    raise UserStocksPersistError(
                        f"user_stocks.json lock busy on Windows after {max_retries} retries: {lock_file}"
                    ) from err
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"L")
                os.lseek(fd, 0, os.SEEK_SET)
            return fd
        except Exception:
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
            raise
    else:
        import errno as _errno
        import fcntl

        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            for attempt in range(_STORAGE_FCNTL_LOCK_MAX_RETRIES):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                    locked = True
                    break
                except OSError as fl_err:
                    if getattr(fl_err, "errno", None) not in (
                        _errno.EAGAIN,
                        _errno.EWOULDBLOCK,
                    ):
                        raise
                    if attempt == _STORAGE_FCNTL_LOCK_MAX_RETRIES - 1:
                        raise BlockingIOError(
                            _errno.EWOULDBLOCK, "user_stocks lock busy"
                        ) from fl_err
                    time.sleep(_STORAGE_FCNTL_LOCK_RETRY_DELAY_SEC)
            return fd
        except Exception:
            if locked:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
            raise


def _release_lock_fd(fd: int) -> None:
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


def _open_exclusive_with_retry(target: Path) -> int:
    """Create *target* via ``O_EXCL``, retrying transient Windows AV locks."""
    for attempt in range(4):
        try:
            return os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise
        except PermissionError:
            if attempt >= 3:
                raise
            time.sleep(0.015 * (attempt + 1))
    raise PermissionError(f"Failed to exclusively open {target}")


def _migrate_legacy_user_stocks() -> bool:
    """Migrate the legacy plaintext store and report whether it is safe to load.

    ``False`` is deliberately reserved for the case where a legacy file exists
    but could not be migrated.  Callers must distinguish that state from a
    genuinely absent store: treating a failed migration as an empty portfolio
    would allow the next save to overwrite the only recoverable copy.
    """
    legacy = _get_legacy_user_stocks_path()
    target = _get_user_stocks_path()
    with _LEGACY_MIGRATION_LOCK:
        if target.exists() or not legacy.exists():
            return True
        lock_fd: int | None = None
        created_target = False
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
            config_store._ensure_runtime_dir()
            target.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(protected, ensure_ascii=False, indent=2)

            lock_file = target.with_suffix(".lock")
            lock_fd = _acquire_exclusive_lock_fd(lock_file)
            if target.exists():
                return True

            if _is_windows():
                fd = _open_exclusive_with_retry(target)
                created_target = True
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
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
            else:
                with restrictive_umask_lock():
                    old_umask = os.umask(0o077)
                    try:
                        fd = os.open(
                            str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        created_target = True
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                f.write(content)
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
                os.chmod(target, 0o600)
                _fsync_directory_strict(target.parent)
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
        except FileExistsError:
            # Another thread/process atomically won O_EXCL creation; leave it intact.
            return True
        except (OSError, TypeError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            if created_target:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            logger.warning("Failed to migrate legacy user stocks file %s: %s", legacy, exc)
            return False
        finally:
            if lock_fd is not None:
                _release_lock_fd(lock_fd)


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
                # Bounded non-blocking retry so a wedged peer cannot stall
                # readers forever; exhaustion returns the read-failed sentinel
                # and the caller falls back to an unlocked read.
                for _attempt in range(20):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                        locked = True
                        break
                    except OSError:
                        if _attempt == 19:
                            break
                        time.sleep(0.05 * (_attempt + 1))
                if not locked:
                    raise OSError("user_stocks read lock busy")

                # [R1] Ensure file size checks and writes are done after acquiring the lock to prevent race conditions.
                if os.fstat(fd).st_size < 1:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)

                with open(_get_user_stocks_path(), "r", encoding="utf-8") as f:
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
                # Bounded non-blocking shared lock (see Windows branch).
                import errno as _errno

                for _attempt in range(20):
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                        locked = True
                        break
                    except OSError as _err:
                        if getattr(_err, "errno", None) not in (
                            _errno.EAGAIN,
                            _errno.EWOULDBLOCK,
                        ):
                            raise
                        if _attempt == 19:
                            break
                        time.sleep(0.05 * (_attempt + 1))
                if not locked:
                    raise OSError("user_stocks read lock busy")
                with open(_get_user_stocks_path(), "r", encoding="utf-8") as f:
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


def _remove_stale_user_stocks_tmp_files() -> None:
    """Sweep orphaned ``user_stocks.*.tmp`` files from crash-killed writes.

    ``save_user_stocks`` writes through a uuid tmp file and deletes it on
    every in-process error path, but a hard process kill between the fsync'd
    tmp write and the ``os.replace`` swap leaks the file forever — no other
    cleanup path matches the ``*.tmp`` suffix (the backup rotator only looks
    at ``user_stocks.bak.*``). Sweep tmp files older than the grace window so
    repeated crashes cannot accumulate garbage; younger files may belong to a
    concurrent in-flight save and are left alone.
    """
    threshold = time.time() - 600.0
    try:
        for tmp_entry in _get_user_stocks_path().parent.glob("user_stocks.*.tmp"):
            try:
                if tmp_entry.stat().st_mtime < threshold:
                    tmp_entry.unlink()
                    logger.info("Removed stale user_stocks tmp file: %s", tmp_entry.name)
            except OSError as exc:
                logger.debug("Could not sweep stale tmp %s: %s", tmp_entry.name, exc)
    except OSError as exc:
        logger.debug("user_stocks tmp sweep failed: %s", exc)


def load_user_stocks(force=False):
    """ユーザーの銘柄設定をファイルから読み込む。"""
    if getattr(app_state.market, "user_stocks_save_pending", 0) > 0:
        # H3 (2026-11-07 review): a save_user_stocks() call is mid-flight.
        # Its snapshot already captured the newest in-memory state and the
        # disk swap may still be in progress, so publishing the on-disk
        # state now would revert unsaved mutations (a queued second save
        # would snapshot the reverted memory and persist it, silently
        # losing a mutation that already reported success). Skip the
        # publish; the next load after the save completes re-reads the
        # swapped file (the save bumps user_stocks_rev, so last_loaded_rev
        # stays stale and even non-force loads refresh).
        return
    config_store._ensure_runtime_dir()
    _remove_stale_user_stocks_tmp_files()
    migration_ok = _migrate_legacy_user_stocks()
    user_stocks_path = _get_user_stocks_path()
    if not os.path.exists(user_stocks_path):
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
            # Re-check under the same lock that save_user_stocks() uses to
            # increment the counter.  The early check above is only an
            # optimization: a save can start during the file-system work
            # between that check and this acquisition, and publishing the
            # pre-save disk snapshot here would revert the mutation the save
            # is about to persist.
            if getattr(app_state.market, "user_stocks_save_pending", 0) > 0:
                return
            if not force and app_state.market.user_stocks_rev == app_state.market.last_loaded_rev:
                return

            lock_file = user_stocks_path.with_suffix(".lock")
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
                        with open(user_stocks_path, "r", encoding="utf-8") as f:
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
            idx = _normalize_idx_holding_fields(data.get("idx", {}))
            app_state.market.user_us = us
            app_state.market.user_jp = jp
            app_state.market.user_idx = idx
            snapshot_us_keys = list(us.keys())
            snapshot_jp_keys = list(jp.keys())
            _update_market_usdjpy_rate(
                _coerce_positive_fx_rate(data.get("last_usdjpy_rate")),
                _coerce_fx_rate_timestamp(data.get("last_usdjpy_rate_ts")),
            )
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
    except Exception as exc:
        # U-01: this handler is the only thing that arms the destructive-save
        # guard (``user_stocks_load_error``). Narrowing it to
        # ``(OSError, json.JSONDecodeError)`` let ``RuntimeError`` from
        # ``get_or_create_master_key()`` (undecodable key, missing keyring,
        # production without MNS_MASTER_KEY) and crypto ``InvalidToken`` escape
        # unmarked, so the next ``save_user_stocks()`` overwrote a recoverable
        # encrypted portfolio with the empty in-memory state. Any failure to
        # load must fail closed, so the breadth here is deliberate.
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
    target_path = _get_user_stocks_path()
    backup_path = target_path.with_suffix(
        ".bak." + datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
    )
    try:
        shutil.copy2(target_path, backup_path)
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
            import errno as _errno
            import fcntl  # type: ignore[import]  # Unix-only; unavailable on Windows

            lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                # A-2 (DATA-4 twin): bounded LOCK_EX|LOCK_NB retries instead of
                # an unbounded blocking LOCK_EX. A hung holder (slow disk,
                # debugger stop, stuck NFS) must not freeze every user_stocks
                # save indefinitely. Budget exhaustion raises BlockingIOError
                # (an OSError), which the fail-closed handler below converts to
                # UserStocksPersistError — the same rollback contract as any
                # other lock failure; non-EAGAIN errors still raise immediately.
                for attempt in range(_STORAGE_FCNTL_LOCK_MAX_RETRIES):
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                        break
                    except OSError as fl_err:
                        if getattr(fl_err, "errno", None) not in (
                            _errno.EAGAIN,
                            _errno.EWOULDBLOCK,
                        ):
                            raise
                        if attempt == _STORAGE_FCNTL_LOCK_MAX_RETRIES - 1:
                            raise BlockingIOError(
                                _errno.EWOULDBLOCK, "user_stocks write lock busy"
                            ) from fl_err
                        time.sleep(_STORAGE_FCNTL_LOCK_RETRY_DELAY_SEC)
                with restrictive_umask_lock():
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


_user_stocks_save_lock = threading.Lock()


def save_user_stocks():
    """ユーザーの銘柄設定をファイルに保存する。

    Uses threading.Lock (_user_stocks_save_lock) to serialize filesystem persistence
    across threads. In-memory data is validated and snapshotted under user_stocks_lock,
    which is then released before JSON serialization, encryption, and disk I/O / fsync.
    This prevents blocking concurrent SSE broadcasts and watchlist reads during slow disk writes.
    A process-internal monotonic version counter (user_stocks_rev) and mtime are updated
    under user_stocks_lock after the atomic file swap.

    Raises:
        UserStocksPersistError: if the write cannot complete safely. Callers
            must treat this as failure (do not report success to the client).
    """
    # H3 (2026-11-07 review): mark "save in flight" at entry, before the
    # serialized section, so a save that is queued behind another one also
    # blocks load_user_stocks() from publishing the on-disk state over the
    # mutations this call was invoked to persist. The counter is only ever
    # mutated while holding user_stocks_lock and is decremented in the
    # finally below, also under the lock.
    with app_state.market.user_stocks_lock:
        app_state.market.user_stocks_save_pending += 1
    try:
        with _user_stocks_save_lock:
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
                normalized_idx = _normalize_idx_holding_fields(app_state.market.user_idx)
                app_state.market.user_idx.clear()
                app_state.market.user_idx.update(normalized_idx)

                raw_rate_val, raw_rate_ts = _snapshot_market_usdjpy_rate()
                rate_ts = _coerce_fx_rate_timestamp(raw_rate_ts)
                rate_val = _coerce_positive_fx_rate(raw_rate_val)
                data = {
                    "us": copy.deepcopy(app_state.market.user_us),
                    "jp": copy.deepcopy(app_state.market.user_jp),
                    "idx": copy.deepcopy(app_state.market.user_idx),
                    "last_usdjpy_rate": rate_val,
                    "last_usdjpy_rate_ts": rate_ts,
                }
                saved_us_keys = list(app_state.market.user_us.keys())
                saved_jp_keys = list(app_state.market.user_jp.keys())

            # Data snapshot completed; release user_stocks_lock during slow serialization,
            # encryption, and filesystem disk I/O (fsync)
            encoded = json.dumps(data, ensure_ascii=False, indent=2)
            _master_key = config_store.get_or_create_master_key()
            protected = protect_data(encoded, key_name="user_stocks", master_key=_master_key)

            # Write through a unique tmp file with cross-platform file locking.
            # The tmp file is unique per call (uuid) so concurrent writers never
            # clobber each other's buffer, and os.replace(tmp -> final) is done
            # INSIDE the lock inside _write_user_stocks_with_lock (no TOCTOU window).
            encoded_data = json.dumps(protected, ensure_ascii=False, indent=2)
            import uuid

            user_stocks_path = _get_user_stocks_path()
            tmp_file = user_stocks_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            lock_file = user_stocks_path.with_suffix(".lock")

            _write_user_stocks_with_lock(encoded_data, tmp_file, user_stocks_path, lock_file)

            if not _is_windows():
                try:
                    os.chmod(user_stocks_path, 0o600)
                except OSError as exc:
                    logger.debug(
                        "Failed to set restrictive permissions on %s: %s", user_stocks_path, exc
                    )

            with app_state.market.user_stocks_lock:
                app_state.market.user_stocks_rev += 1
                # The write already succeeded (tmp + fsync + os.replace). The
                # mtime read is best-effort observability: a transient OSError
                # here (Windows sharing violation, AV scanner holding the
                # handle) must not turn a durable save into a reported failure,
                # because callers would then roll back in-memory state that the
                # disk already reflects.
                try:
                    app_state.market.last_modified_ns = os.stat(user_stocks_path).st_mtime_ns
                except OSError as stat_exc:
                    logger.debug(
                        "Saved user stocks but could not read mtime for %s: %s",
                        user_stocks_path,
                        stat_exc,
                    )

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
    finally:
        # H3: always release the in-flight marker — including the refusal and
        # error paths — so load_user_stocks() can never stay blocked forever.
        with app_state.market.user_stocks_lock:
            app_state.market.user_stocks_save_pending -= 1
