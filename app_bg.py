# app_bg.py
"""Background synchronization, yfinance fetching, and SSE interpolation loop."""

from __future__ import annotations

import atexit
import copy
import json
import logging
import math
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

import config_store

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

import concurrent.futures
import queue

import pandas as pd
from requests.exceptions import RequestException

from app_state import app_state
from constants import (
    SIMULATE_FLUCTUATION,
    SSE_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP,
    SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_NO_LISTENER_SLEEP,
)
from messaging import sse_event_log
from route_helpers import (
    ensure_stock_placeholder_in_caches,
    invalidate_stock_caches,
    remove_stock_from_caches,
)

# yfinance raises this for a ticker that does not exist (delisted / typo). It
# is a plain Exception subclass (not a ValueError/RuntimeError), so it must be
# listed explicitly in the except tuples below; otherwise a single invalid
# symbol would escape fetch_stock/fetch_index_data and abort the whole sync.
from services.stock_provider import YFTickerMissingError
from utils.http_utils import parse_retry_after
from utils.market_utils import acquire_yfinance_slot, is_market_open
from utils.normalization import _fmt, _fmt_vol, normalize_history_frame, normalize_optional_number
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    _strip_portfolio_fields,
    build_stock_payload,
)
from utils.storage import load_user_stocks, save_user_stocks

logger = logging.getLogger(__name__)


try:
    if "_LEADER_LOCK_FILE" in globals() and globals()["_LEADER_LOCK_FILE"] is not None:
        try:
            globals()["_LEADER_LOCK_FILE"].close()
        except OSError:
            pass
except Exception:
    pass
_LEADER_LOCK_FILE = None
_is_sync_leader = True  # Default to True so it functions normally in single-process mode
_sync_start_time: float = 0.0
_sync_generation: int = 0
_last_loaded_mtimes: dict[str, float] = {}

# Maximum time (seconds) a single sync_all_stocks_now() may run before the
# is_syncing lock is treated as stale. yfinance timeouts are shorter (batch=20s,
# single=6s), so this is a defense-in-depth guard against unexpected hangs.
SYNC_STALE_TIMEOUT_SEC: float = 120.0
# When a sync is detected as stale, callers wait up to this long for the wedged
# run to release the execution lock before giving up. Bounded so a stuck sync
# can never turn an ordinary caller (route handler, background loop) into a
# permanently blocked request; the generation guard in ``_process_fetched_stocks``
# discards any late result from the stale run, so a takeover cannot publish
# outdated data.
# Note: while a sync stays permanently wedged, the background loop blocks up to
# this long on each cycle's takeover attempt before retrying the next cycle.
SYNC_STALE_LOCK_WAIT_SEC: float = 15.0

# Stale threshold for the atomic leader-lock fallback: a lock file with no
# readable PID is reclaimed when older than this (crashed leader cleanup).
_ATOMIC_LOCK_STALE_SEC: float = 60.0


def _recover_stale_sync_state_if_needed() -> bool:
    """Reset a wedged sync's state when it exceeds the stale threshold.

    This is the only place ``is_syncing`` is cleared outside the sync thread's
    own ``finally`` block (``sync_all_stocks_now``). It lets scheduling, the UI
    status, and lock takeovers recover even when the wedged thread never
    returns.

    Safe because the generation guard in ``_process_fetched_stocks`` discards
    any late result from the stale run, so recovery never publishes outdated
    data.

    Returns True when stale state was detected and cleared.
    """
    global _sync_start_time
    with app_state.market.is_syncing_lock:
        if not app_state.market.is_syncing:
            return False
        elapsed = time.time() - _sync_start_time if _sync_start_time > 0 else 0.0
        if elapsed <= SYNC_STALE_TIMEOUT_SEC:
            return False
        logger.critical(
            "Sync is stale (running %.0fs, threshold %.0fs). Resetting sync state "
            "so scheduling, the UI, and lock takeovers can recover.",
            elapsed,
            SYNC_STALE_TIMEOUT_SEC,
        )
        app_state.market.is_syncing = False
        _sync_start_time = 0.0
        return True


def _pid_is_alive(pid: int) -> bool:
    """Return True when a process with *pid* exists.

    Prefers ``psutil.pid_exists`` (psutil is already a dependency), which is
    reliable on every platform. A signal-0 probe is only a fallback and treats
    non-``ProcessLookupError`` ``OSError`` results as "alive" conservatively:
    on Windows a nonexistent PID can surface as a misleading OSError (e.g.
    WinError 11) and ``os.kill(pid, 0)`` can disturb signal state at shutdown,
    so the fallback deliberately errs toward "alive" (which merely delays stale
    lock reclamation by one threshold).
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _release_leader_lock() -> None:
    """Close the leader lock file handle on process exit (M-3: prevent FD leak)."""
    global _LEADER_LOCK_FILE
    _lock_file = _LEADER_LOCK_FILE
    _LEADER_LOCK_FILE = None
    if _lock_file is not None:
        try:
            _lock_file.close()
        except OSError:
            pass
    # R1: Remove the atomic lock file if we own it, so a clean shutdown releases
    # the lock immediately instead of waiting for the stale threshold. The
    # lock file now lives in the runtime data directory (APP_DATA_DIR) so it
    # is not left in the source tree; if a legacy project-root file exists
    # from a prior run, remove that one too.
    try:
        lock_path = APP_DATA_DIR / ".mns_sync_leader.lock"
        if lock_path.exists():
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            raw = lock_path.read_text(encoding="utf-8").strip()
            if raw.isdigit() and int(raw) == os.getpid():
                lock_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        legacy_lock = Path(__file__).resolve().parent / ".mns_sync_leader.lock"
        if legacy_lock.exists():
            legacy_lock.unlink(missing_ok=True)
    except OSError:
        pass


# R1: re-export APP_DATA_DIR so tests (and any future caller) can override
# the lock-file location through a clean module-level patch target instead
# of having to reach into config_store.
APP_DATA_DIR = config_store.APP_DATA_DIR


atexit.register(_release_leader_lock)


def _try_acquire_leader_lock() -> bool:
    """Try to acquire a non-blocking lock on the leader lock file.

    Uses the most reliable locking mechanism available per platform:
    - fcntl.flock on Unix (blocking flock with LOCK_NB)
    - msvcrt.locking on Windows
    - Atomic file creation (O_CREAT | O_EXCL) as universal fallback

    The atomic-file-creation fallback ensures leader election still works
    even when neither fcntl nor msvcrt is importable (Cygwin, Wine, Docker
    with minimal environment, etc.). The lock file is written with the
    current PID so stale locks can be detected and cleaned up.
    """
    global _LEADER_LOCK_FILE
    # R1: keep the leader lock in the runtime data directory so it does not
    # leak into the source tree. APP_DATA_DIR is the same directory the rest
    # of the runtime state already uses.
    base_dir = APP_DATA_DIR
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lock_path = base_dir / ".mns_sync_leader.lock"
    pid = os.getpid()

    try:
        if os.name == "nt":  # Windows
            if msvcrt is not None:
                if _LEADER_LOCK_FILE is None:
                    lock_path.touch(exist_ok=True)
                    _LEADER_LOCK_FILE = open(lock_path, "r+", encoding="utf-8")  # noqa: SIM115
                fd = _LEADER_LOCK_FILE.fileno()
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    _LEADER_LOCK_FILE.seek(0)
                    _LEADER_LOCK_FILE.truncate(0)
                    _LEADER_LOCK_FILE.write(str(pid))
                    _LEADER_LOCK_FILE.flush()
                    return True
                except OSError:
                    return False
            # Fallback: atomic file creation
            return _try_acquire_atomic_lock(lock_path, pid)
        else:  # Unix
            if fcntl is not None:
                if _LEADER_LOCK_FILE is None:
                    lock_path.touch(exist_ok=True)
                    _LEADER_LOCK_FILE = open(lock_path, "r+", encoding="utf-8")  # noqa: SIM115
                try:
                    fcntl.flock(_LEADER_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                    _LEADER_LOCK_FILE.seek(0)
                    _LEADER_LOCK_FILE.truncate(0)
                    _LEADER_LOCK_FILE.write(str(pid))
                    _LEADER_LOCK_FILE.flush()
                    return True
                except OSError:
                    return False
            # Fallback: atomic file creation
            return _try_acquire_atomic_lock(lock_path, pid)
    except (OSError, ValueError) as exc:
        logger.debug("Failed to acquire sync leader lock: %s", exc)
        return False


def _try_acquire_atomic_lock(lock_path: Path, pid: int) -> bool:
    """Attempt to acquire the leader lock via atomic file creation.

    Universal fallback used when neither fcntl nor msvcrt is importable
    (Cygwin, Wine, minimal Docker, etc.). ``O_CREAT | O_EXCL`` is atomic: only
    one process can create the file, so a second process sees ``EEXIST`` and
    fails. The lock file stores the owner PID so a crashed leader can be
    detected (the owning process no longer exists) and reclaimed instead of
    wedging leader election forever.
    """
    global _LEADER_LOCK_FILE
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
    except FileExistsError:
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
            owner_pid = int(raw) if raw.isdigit() else None
        except (OSError, ValueError):
            owner_pid = None
        if owner_pid == pid:
            # Re-entrant acquire from this process: the lock is already ours.
            logger.debug("Atomic leader lock already held by pid=%d", pid)
            return True
        stale = False
        try:
            if owner_pid is not None:
                stale = not _pid_is_alive(owner_pid)
            elif time.time() - lock_path.stat().st_mtime > _ATOMIC_LOCK_STALE_SEC:
                stale = True
        except OSError:
            stale = False
        if not stale:
            return False
        logger.warning("Reclaiming stale atomic leader lock at %s", lock_path)
        # Drop our own handle to the old lock file first: on Windows a file
        # with an open handle cannot be deleted.
        if _LEADER_LOCK_FILE is not None:
            try:
                _LEADER_LOCK_FILE.close()
            except OSError:
                pass
            _LEADER_LOCK_FILE = None
        try:
            lock_path.unlink()
        except OSError:
            return False
        return _try_acquire_atomic_lock(lock_path, pid)
    except OSError as exc:
        logger.debug("Failed to acquire atomic leader lock: %s", exc)
        return False

    try:
        f = os.fdopen(fd, "w", encoding="utf-8")
        f.write(str(pid))
        f.flush()
        f.seek(0)
    except OSError as exc:
        logger.debug("Failed to write atomic leader lock: %s", exc)
        try:
            f.close()
        except OSError:
            pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    _LEADER_LOCK_FILE = f
    logger.debug("Acquired atomic leader lock at %s (pid=%d)", lock_path, pid)
    return True


def bg_leader_election_loop():
    """Periodically check and run leader election."""
    global _is_sync_leader
    acquired = _try_acquire_leader_lock()
    _is_sync_leader = acquired
    if acquired:
        logger.info("This process has acquired the sync leader lock. Running as MASTER.")
    else:
        logger.debug("This process failed to acquire the sync leader lock. Running as FOLLOWER.")

    while not app_state.execution.shutdown_event.is_set():
        if not _is_sync_leader:
            acquired = _try_acquire_leader_lock()
            if acquired:
                _is_sync_leader = True
                logger.info("Sync leader changed: this process is now the MASTER.")
        app_state.execution.shutdown_event.wait(10.0)


def _handle_yfinance_error(exc, symbol=""):
    """Handle exceptions from yfinance queries and bump rate limits if blocked."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    exc_str_lower = str(exc).lower()

    if (
        status_code in (401, 429, 402, 439)
        or "too many requests" in exc_str_lower
        or "payment required" in exc_str_lower
        or "invalid crumb" in exc_str_lower
        or "unauthorized" in exc_str_lower
    ):
        backoff_time = app_state.market.mark_yf_429(retry_after=parse_retry_after(exc))
        # mark_yf_429() already handles yf_session_manager UA rotation and cookie clearing
        logger.warning(
            "yfinance rate limit / block hit (%s) for symbol=%s; backing off for %d seconds.",
            status_code if status_code else "unknown",
            symbol,
            int(backoff_time),
        )
    elif "timeout" in exc_str_lower:
        logger.debug("yfinance timeout detected. symbol=%s", symbol)
    else:
        with app_state.market.yfinance_lock:
            app_state.market.yfinance_429_streak = 0


def fetch_stock(
    symbol: str,
    name_or_dict: Any,
    market: str,
    snapshot_ts_ms: int | None = None,
) -> dict[str, Any] | tuple[str, str] | None:
    """単一銘柄のデータを取得する

    Returns the payload dict on success, ``None`` on transient failure (callers
    treat this as NOT-removable), or ``("__INVALID_SYMBOL__", symbol)`` when the
    exception proves the ticker is genuinely invalid (delisted / not found).
    """
    if not acquire_yfinance_slot():
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for symbol=%s",
                symbol,
            )
        return None

    try:
        # Pick ONE period by market state — no fallback.
        from utils.market_utils import is_market_open

        period = "3mo" if is_market_open(market) else "1mo"

        hist = pd.DataFrame()
        try:
            hist = app_state.stock_provider.get_history(symbol, period=period)
        except (
            RequestException,
            ValueError,
            KeyError,
            IndexError,
            OSError,
            YFTickerMissingError,
        ) as e:
            logger.debug("Fetch failed for %s with period %s: %s", symbol, period, e)
            invalid = _invalid_tuple_if_applicable(symbol, e)
            if invalid is not None:
                return invalid

        if hist.empty or "Close" not in hist.columns or len(hist) < 1:
            logger.warning(
                "No valid history data found for %s after period %s",
                symbol,
                period,
            )
            return None

        payload = build_stock_payload(
            symbol, name_or_dict, market, hist, snapshot_ts_ms=snapshot_ts_ms
        )
        if isinstance(payload, dict):
            try:
                # Holdings are restored from the encrypted user-stocks store at
                # response time; the reusable market-data cache must stay public.
                app_state.payload_disk_cache.set(
                    f"payload_{symbol}_{market}", _strip_portfolio_fields(payload)
                )
            except (OSError, TypeError):
                logger.debug("Failed to cache payload for %s", symbol)
            return payload
        return None
    except (
        RequestException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OSError,
        YFTickerMissingError,
    ) as exc:
        _handle_yfinance_error(exc, symbol)
        logger.exception("Stock fetch failed (%s)", symbol)
        invalid = _invalid_tuple_if_applicable(symbol, exc)
        if invalid is not None:
            return invalid
        return None


def extract_batch_history(downloaded, symbol, single_symbol=False):
    """バッチ取得されたDataFrameから単一銘柄の履歴を抽出"""
    if downloaded is None or getattr(downloaded, "empty", True):
        return pd.DataFrame()
    try:
        if not isinstance(downloaded, pd.DataFrame):
            return pd.DataFrame()

        if isinstance(downloaded.columns, pd.MultiIndex):
            try:
                return normalize_history_frame(downloaded.xs(symbol, axis=1, level=1))
            except (KeyError, IndexError, ValueError):
                pass

            try:
                return normalize_history_frame(downloaded[symbol])
            except (KeyError, IndexError, ValueError):
                pass

            try:
                matching_cols = [
                    col for col in downloaded.columns if isinstance(col, tuple) and symbol in col
                ]
                if matching_cols:
                    extracted = downloaded[matching_cols].copy()
                    extracted.columns = [
                        next(part for part in col if part != symbol) for col in matching_cols
                    ]
                    return normalize_history_frame(extracted)
            except (KeyError, IndexError, TypeError, StopIteration, ValueError):
                pass

            return pd.DataFrame()
        elif single_symbol:
            return normalize_history_frame(downloaded)
        else:
            return pd.DataFrame()
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        logger.debug("extract_batch_history error for %s: %s", symbol, exc)
        return pd.DataFrame()


def fetch_stocks_batch(
    items: list[tuple[str, str, str]],
    snapshot_ts_ms: int | None = None,
    lightweight: bool = False,
    period: str = "3mo",
) -> list[Any]:
    """複数銘柄をバッチで取得。

    Returns a list aligned with ``items`` where each element is either:
      * a payload ``dict`` (success), or
      * ``None`` (transient failure / no data — treat as NOT-removable), or
      * ``("__INVALID_SYMBOL__", symbol)`` tuple (the ticker is genuinely
        invalid on Yahoo — the only case that should count toward auto-removal).

    Callers must use ``_is_batch_result_invalid`` to distinguish the third case;
    a plain ``result is None`` is intentionally NOT treated as invalid, so a
    temporary Yahoo/network outage cannot silently delete user stocks.
    """
    if not items:
        return []

    from utils.disk_cache import is_disk_cache_degraded

    if is_disk_cache_degraded():
        logger.warning("Disk cache is in degraded state; skipping batch stock fetch")
        return [None] * len(items)

    symbols = [item[0] for item in items]
    logger.info("Batch stock fetch starting: count=%d", len(symbols))

    # When rate-limited recently, use smaller batches to reduce load
    max_batch_size = len(symbols)
    if app_state.market.is_yf_rate_limited():
        max_batch_size = max(5, min(len(symbols), 10))
        if len(symbols) > max_batch_size:
            logger.info(
                "Rate limit active: reducing batch from %d to %d symbols",
                len(symbols),
                max_batch_size,
            )
            symbols = symbols[:max_batch_size]
            items = items[:max_batch_size]

    downloaded = None
    if acquire_yfinance_slot():
        try:
            downloaded = app_state.stock_provider.download_batch(
                symbols, period=period, lightweight=lightweight
            )
        except (RequestException, ValueError, TypeError, KeyError, OSError) as exc:
            _handle_yfinance_error(exc, "batch_fetch")
            logger.warning(
                "Batch fetch failed with exception: %s.",
                exc,
                exc_info=True,
            )
    else:
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for batch fetch."
            )

    if downloaded is None or downloaded.empty:
        logger.warning(
            "Batch fetch completely failed or empty. Preserving previous state."
        )
        return [None] * len(items)

    results_map = {}
    fallback_items = []
    # Cap parallel per-symbol fallbacks. Each fallback is a fresh yfinance
    # history fetch, so we keep this small (2) and skip it entirely when
    # already rate-limited to avoid fanning out N individual requests that
    # would deepen the block.
    max_fallbacks = 2

    for symbol, name, market in items:
        payload = None
        if downloaded is not None and not downloaded.empty:
            try:
                hist = extract_batch_history(downloaded, symbol, single_symbol=(len(symbols) == 1))
                if not hist.empty and len(hist) >= 1:
                    payload = build_stock_payload(
                        symbol,
                        name,
                        market,
                        hist,
                        snapshot_ts_ms=snapshot_ts_ms,
                        lightweight=lightweight,
                    )
                else:
                    # No usable history for this symbol in the batch. This is
                    # ambiguous (could be a brand-new listing or a delisted
                    # ticker), so do NOT mark it invalid here — let the per-
                    # symbol fallback path decide via its own exception.
                    pass
            except (KeyError, IndexError, ValueError, TypeError) as extract_exc:
                logger.debug("Failed to extract %s from batch: %s", symbol, extract_exc)

        if payload is not None:
            results_map[symbol] = payload
        else:
            fallback_items.append((symbol, name, market))

    if lightweight:
        logger.debug("Lightweight mode: skipping all %d fallbacks", len(fallback_items))
        for symbol, name, market in fallback_items:
            results_map[symbol] = None
        results = [results_map.get(item[0]) for item in items]
        return results

    if app_state.market.is_yf_rate_limited():
        # Don't hammer Yahoo with N individual fallbacks while blocked; the
        # existing target-cache entry (if any) is preserved by the caller.
        logger.warning(
            "yfinance rate-limited: skipping %d batch fallbacks.",
            len(fallback_items),
        )
        results = [results_map.get(item[0]) for item in items]
        return results

    to_fetch = fallback_items[:max_fallbacks]
    skipped_items = fallback_items[max_fallbacks:]

    for symbol, _, _ in skipped_items:
        logger.debug("Skipping fallback for %s: limit reached", symbol)
        results_map[symbol] = None

    if to_fetch:
        futures_map: dict[concurrent.futures.Future[Any], str] = {}

        logger.info(
            "Fallback parallel single queries triggered for %d stocks (limit %d)",
            len(to_fetch),
            max_fallbacks,
        )

        for symbol, name, market in to_fetch:
            try:
                fut = app_state.execution.data_executor.submit(
                    fetch_stock, symbol, name, market, snapshot_ts_ms
                )
            except (queue.Full, RuntimeError) as submit_exc:
                # Bounded executor exhausted (or shutting down): skip the
                # fallback for this symbol instead of letting queue.Full abort
                # the entire sync cycle (it is not in sync_all_stocks_now's
                # handled exception tuple).
                logger.warning("Fallback fetch submission skipped for %s: %s", symbol, submit_exc)
                results_map[symbol] = None
                continue
            futures_map[fut] = symbol
        from utils.env_helpers import _env_float

        fallback_timeout = _env_float("MNS_FALLBACK_FETCH_TIMEOUT", 10.0, 1.0, 60.0)
        done, not_done = concurrent.futures.wait(
            futures_map.keys(),
            timeout=fallback_timeout,
        )

        for fut in done:
            symbol = futures_map[fut]
            try:
                payload = fut.result()
                results_map[symbol] = payload
            except (RequestException, ValueError, TypeError, RuntimeError) as exc:
                logger.warning("Parallel fallback fetch failed for %s: %s", symbol, exc)
                results_map[symbol] = _invalid_tuple_if_applicable(symbol, exc)
        for fut in not_done:
            symbol = futures_map[fut]
            logger.warning("Parallel fallback fetch timed out for %s", symbol)
            results_map[symbol] = None
            # A future that finishes after the wait() timeout still holds a
            # possible exception. Consume it via a done-callback so the error
            # is logged (and the future is not silently discarded).

            def _log_late_failure(f, _sym=symbol):
                try:
                    exc = f.exception()
                except Exception as log_exc:  # pragma: no cover - defensive
                    exc = log_exc
                if exc is not None:
                    logger.warning("Parallel fallback fetch failed late for %s: %s", _sym, exc)

            fut.add_done_callback(_log_late_failure)

    results = [results_map.get(item[0]) for item in items]
    return results


_BATCH_INVALID_MARKER = "__INVALID_SYMBOL__"


def _invalid_tuple_if_applicable(symbol: str, exc: Exception) -> Any:
    """Return an invalid-symbol marker tuple if the exception proves the symbol
    is genuinely invalid (delisted / not found), else ``None`` (transient)."""
    from services.stock_provider import _is_yfinance_invalid_symbol_error

    if _is_yfinance_invalid_symbol_error(exc):
        return (_BATCH_INVALID_MARKER, symbol)
    return None


def _is_batch_result_invalid(result: Any) -> bool:
    """True only when the batch result explicitly marks the symbol invalid."""
    return isinstance(result, tuple) and len(result) == 2 and result[0] == _BATCH_INVALID_MARKER


def fetch_index_data(key: str, symbol: str) -> tuple[str, dict[str, Any]] | None:
    """指数データ取得（シングルピリオド、フォールバック無し）"""
    if not acquire_yfinance_slot():
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for index=%s", key
            )
        return None

    try:
        # Single period — no multi-period fallback loop.
        # "1mo" provides enough context for change computation without
        # the redundant 5d->1mo fallback that doubled request volume.
        hist = app_state.stock_provider.get_history(symbol, period="1mo")

        if len(hist) < 2:
            return None

        last_row = hist.iloc[-1]
        prev_close = hist["Close"].iloc[-2]

        price = float(last_row["Close"])
        change = price - float(prev_close)
        pct = (change / float(prev_close) * 100) if prev_close else 0.0

        market_type = "jp" if key == "N225" else "us"
        # Use the 5-minute cached market state instead of forcing a live
        # yfinance metadata query per index update (bypass_cache=True caused an
        # extra get_history_metadata network request on every call, which
        # multiplies across the safety-net path and the SSE fetch loop).
        is_open = is_market_open(market_type)
        market_state = "REGULAR" if is_open else "CLOSED"

        return key, {
            "price": _fmt(price),
            "change": _fmt(change),
            "percent": _fmt(pct),
            "high": _fmt(last_row.get("High")),
            "low": _fmt(last_row.get("Low")),
            "open": _fmt(last_row.get("Open")),
            "volume": _fmt_vol(last_row.get("Volume")),
            "market_state": market_state,
        }
    except (RequestException, ValueError, TypeError, KeyError, IndexError, OSError):
        logger.exception("Index fetch failed for %s", key)
        return None


def _build_sse_light_stocks_payload(stocks_by_market):
    """SSE配信用の軽量株価ペイロードを構築

    Portfolio fields (shares/avg_price/avg_fx_rate/portfolio_*/portfolio_pl) are
    intentionally excluded from the unauthenticated SSE stream (H-3). Holdings
    stay in the encrypted holdings store and in-memory; clients that need them
    must call a trusted path.
    The whitelist below ensures only public market data is emitted. Additionally,
    ``_strip_portfolio_fields`` is applied as defense-in-depth so that if the
    whitelist is later modified to include a portfolio key, the data is still
    stripped before reaching SSE listeners.
    """
    fields = (
        "symbol",
        "name",
        "market",
        "price",
        "change",
        "change_percent",
        "high",
        "low",
        "volume",
        "currency",
        "market_state",
        "sector",
        "industry",
    )
    payload: dict[str, list[Any]] = {"us": [], "jp": [], "idx": []}
    for market in ("us", "jp", "idx"):
        rows = stocks_by_market.get(market, []) if isinstance(stocks_by_market, dict) else []
        out = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            # Defense-in-depth: strip portfolio fields even though the whitelist
            # above excludes them, to guard against future code changes.
            safe_item = _strip_portfolio_fields(item)
            row = {k: safe_item.get(k) for k in fields if k in safe_item}
            row["snapshot_ts_ms"] = safe_item.get("snapshot_ts_ms")

            chart_rows = (
                safe_item.get("chart_data") if isinstance(safe_item.get("chart_data"), list) else []
            )
            if chart_rows:
                compact_chart = []
                for p in chart_rows[-24:]:
                    if not isinstance(p, dict):
                        continue
                    price = p.get("price")
                    if price is None:
                        continue
                    compact_chart.append(
                        {
                            "x": p.get("x"),
                            "price": price,
                            "ma5": p.get("ma5"),
                        }
                    )
                if compact_chart:
                    row["chart_data"] = compact_chart

            out.append(row)
        payload[market] = out
    return payload


def _interpolate_and_fluctuate_market(
    target_list: list[dict],
    current_list: list[dict],
    is_open: bool,
    market: str,
) -> list[dict]:
    """ターゲットキャッシュから現在キャッシュの価格を補間し、市場オープン時は微小変動を加える。

    前日比・前日比率も整合的に更新する。snapshot_ts_ms は価格または市場
    開閉状態が実際に変化した場合のみ現在時刻で更新する（変化が無い銘柄は
    以前のタイムスタンプを保持する）。これにより SSE diff エンジンは変化の
    あった銘柄だけを配信できる。
    """
    if not target_list:
        return []

    current_map = {}
    for item in current_list:
        if isinstance(item, dict) and item.get("symbol"):
            current_map[item["symbol"]] = item

    new_current = []
    now_ms = int(time.time() * 1000)

    for t_item in target_list:
        if not isinstance(t_item, dict) or not t_item.get("symbol"):
            continue

        sym = t_item["symbol"]
        if sym in current_map:
            c_item = current_map[sym].copy()
            for k in (
                "name",
                "market",
                "currency",
                "shares",
                "avg_price",
                "portfolio_value",
                "portfolio_pl",
                "sector",
                "industry",
                "high",
                "low",
                "volume",
                "chart_data",
                "ohlc_data",
            ):
                if k in t_item:
                    c_item[k] = t_item[k]
        else:
            c_item = copy.deepcopy(t_item)

        prev_market_state = c_item.get("market_state")
        prev_price = c_item.get("price")
        c_item["market_state"] = "REGULAR" if is_open else "CLOSED"

        target_price_val = t_item.get("price")
        if target_price_val is not None and target_price_val not in ("--", ""):
            try:
                target_price = float(target_price_val)
                target_change = float(t_item.get("change") or 0.0)
                previous_close = target_price - target_change

                # Reject non-finite values (NaN/Inf) from the data source so they
                # never propagate into current_stocks_cache or the SSE JSON stream.
                if not math.isfinite(target_price) or not math.isfinite(previous_close):
                    raise ValueError("non-finite price from data source")

                current_price = float(c_item.get("price") or target_price)
                diff = target_price - current_price
                step = diff * 0.25

                if is_open and SIMULATE_FLUCTUATION and random.random() < 0.25:  # nosec B311
                    volatility = 0.0002
                    step += target_price * random.uniform(-volatility, volatility)  # nosec B311

                new_price = current_price + step
                new_price = max(target_price * 0.99, min(target_price * 1.01, new_price))

                is_jpy = (c_item.get("currency") == "JPY") or (sym.endswith(".T"))
                decimals = 2 if is_jpy else 4

                c_item["price"] = round(new_price, decimals)
                if previous_close != 0:
                    new_change = new_price - previous_close
                    new_change_percent = (new_change / previous_close) * 100
                    c_item["change"] = round(new_change, decimals)
                    c_item["change_percent"] = round(new_change_percent, 2)
            except (ValueError, TypeError):
                pass

        # snapshot_ts_ms は価格または市場開閉状態が実際に変化した場合のみ更新する。
        # 以前は毎ティック全銘柄に現在時刻をスタンプしていたため、_build_sse_diff が
        # 毎回「全銘柄が変化した」と判定し、diff ペイロードが常にフルサイズになって
        # いた（15KB→1KB の diff 最適化が機能しない根本原因）。
        if c_item.get("price") != prev_price or c_item.get("market_state") != prev_market_state:
            c_item["snapshot_ts_ms"] = now_ms

        new_current.append(c_item)

    return new_current


def _fluctuate_indices(indices_dict: dict, us_open: bool, jp_open: bool) -> None:
    """開場ステータスに応じて主要指数の価格を微小変動させる"""
    for key, info in indices_dict.items():
        if not isinstance(info, dict) or "price" not in info:
            continue
        price_val = info.get("price")
        try:
            price = normalize_optional_number(price_val)
            change = normalize_optional_number(info.get("change"), allow_negative=True) or 0.0
        except (ValueError, TypeError):
            continue
        if price is None:
            continue

        # Reject non-finite values (NaN/Inf) so they never reach the SSE JSON stream.
        if not math.isfinite(price) or not math.isfinite(change):
            continue

        should_fluctuate = (
            (key == "N225" and jp_open)
            or (key in ("DJI", "SP500", "NASDAQ", "VIX") and us_open)
            or (key in ("USDJPY", "EURJPY") and (us_open or jp_open))
        )

        if should_fluctuate and SIMULATE_FLUCTUATION and random.random() < 0.3:  # nosec B311
            vol = 0.0001
            change_factor = 1.0 + random.uniform(-vol, vol)  # nosec B311
            new_price = price * change_factor
            prev_close = price - change

            if prev_close != 0:
                new_change = new_price - prev_close
                new_percent = (new_change / prev_close) * 100

                if key in ("USDJPY", "EURJPY"):
                    info["price"] = round(new_price, 3)
                    info["change"] = round(new_change, 3)
                else:
                    info["price"] = round(new_price, 2)
                    info["change"] = round(new_change, 2)
                info["percent"] = round(new_percent, 2)


def bg_interpolate_loop() -> None:
    """全銘柄の現在値を目標値へ補間し、リアルタイム風の価格変動を模擬しながらSSE配信する"""
    from constants import SSE_MARKET_CLOSED_SLEEP, SSE_MARKET_OPEN_SLEEP

    app_state.execution.shutdown_event.wait(2.0)

    while not app_state.execution.shutdown_event.is_set():
        try:
            listener_count = app_state.sse_announcer_mode1.listener_count()
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(5.0)
                continue

            us_open = is_market_open("us")
            jp_open = is_market_open("jp")
            idx_open = is_market_open("idx")
            any_open = us_open or jp_open or idx_open

            with app_state.cache.sse_data_lock:
                target_us = list(app_state.market.target_stocks_cache.get("us", []))
                target_jp = list(app_state.market.target_stocks_cache.get("jp", []))
                target_idx = list(app_state.market.target_stocks_cache.get("idx", []))

                current_us = list(app_state.market.current_stocks_cache.get("us", []))
                current_jp = list(app_state.market.current_stocks_cache.get("jp", []))
                current_idx = list(app_state.market.current_stocks_cache.get("idx", []))

                indices_copy = copy.deepcopy(app_state.market.current_indices_cache)

            if any((target_us, target_jp, target_idx)):
                new_current_stocks = {
                    "us": _interpolate_and_fluctuate_market(target_us, current_us, us_open, "us"),
                    "jp": _interpolate_and_fluctuate_market(target_jp, current_jp, jp_open, "jp"),
                    "idx": _interpolate_and_fluctuate_market(
                        target_idx, current_idx, idx_open, "idx"
                    ),
                }

                if any_open:
                    _fluctuate_indices(indices_copy, us_open, jp_open)

                with app_state.cache.sse_data_lock:
                    app_state.market.current_stocks_cache = new_current_stocks
                    app_state.market.current_indices_cache = indices_copy

                _invalidate_sse_payload_cache()
                announce_current_market_state()

            if not any_open:
                app_state.execution.shutdown_event.wait(SSE_MARKET_CLOSED_SLEEP)
            else:
                app_state.execution.shutdown_event.wait(SSE_MARKET_OPEN_SLEEP)

        except Exception:
            logger.exception("bg_interpolate_loop error")
            app_state.execution.shutdown_event.wait(1.0)


# ---------------------------------------------------------------------------
# SSE payload diff engine
# ---------------------------------------------------------------------------
# announce_current_market_state is called every ~0.5s while the market is open.
# Instead of serialising the entire stock list on every tick, we compute a
# *diff* between the previous and current cached states and only emit the
# changed symbols.  For a typical sync cycle where 1-2 prices move, the
# payload shrinks from ~15 KB to ~1 KB.
#
# The diff is computed by comparing symbol-level snapshot_ts_ms values.
# A full snapshot is sent every N ticks (FULL_SNAPSHOT_INTERVAL) so that
# clients that miss messages can recover without reconnecting.
# ---------------------------------------------------------------------------

# Comment keepalive frame emitted when nothing changed (mode 1). SSE comment
# lines (starting with ``:``) are ignored by the browser's EventSource, so this
# keeps the stream flowing every tick without forcing the client to re-merge /
# re-render the (unchanged) stock state on every background tick.
_SSE_KEEPALIVE_FRAME = ": keepalive\n\n"

_sse_payload_cache: str = (
    'data: {"stocks":[],"indices":[],'
    '"is_yfinance_rate_limited":false,'
    '"is_us_market_open":false,"is_jp_market_open":false}\n\n'
)
_sse_payload_generation: int = 0
_sse_payload_cached_generation: int = -1
_sse_payload_yf_limited: bool = False
_sse_payload_us_open: bool = False
_sse_payload_jp_open: bool = False

# Thread lock for SSE payload generation counter and related module-level globals.
# Although CPython's GIL serialises most bytecode, ``_sse_payload_generation += 1``
# is a 4-bytecode read-modify-write that is *not* atomic.  This lock formalises
# correctness without depending on CPython implementation details and mirrors the
# pattern used elsewhere in the module (e.g. ``_CONFIG_LOCK``, ``sse_data_lock``).
_sse_payload_lock = threading.Lock()

# Previous snapshot for diff computation
_sse_prev_stocks: dict[str, dict[str, Any]] = {"us": {}, "jp": {}, "idx": {}}
_sse_full_snapshot_counter: int = 0
# Send a full snapshot every N sync cycles to allow client recovery
FULL_SNAPSHOT_INTERVAL: int = 6


def _invalidate_sse_payload_cache() -> None:
    """Invalidate the SSE payload cache, forcing re-serialization on next announce.

    Called by sync_all_stocks_now() after updating the target_stocks_cache.
    """
    global _sse_payload_generation
    with _sse_payload_lock:
        _sse_payload_generation += 1


def _build_sse_diff(
    new_stocks: dict[str, list[dict[str, Any]]],
    prev_map: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Compute the diff between the previous and current stock snapshots.

    Returns a payload in the same shape as _build_sse_light_stocks_payload but
    containing only symbols whose snapshot_ts_ms (or price) has changed.
    Portfolio fields are stripped from diff items as defense-in-depth (H-3).

    ``us``/``jp``/``idx`` are all diffed so every watchlist section (including
    user-added index/ETF symbols) receives live updates over SSE.
    """
    diff: dict[str, list[dict[str, Any]]] = {"us": [], "jp": [], "idx": []}
    for market in ("us", "jp", "idx"):
        current_list = new_stocks.get(market, [])
        current_map: dict[str, dict[str, Any]] = {}
        for item in current_list:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol")
            if not sym:
                continue
            safe_item = _strip_portfolio_fields(item)
            current_map[sym] = safe_item
            prev_item = prev_map.get(market, {}).get(sym)
            if prev_item is None:
                # New symbol not seen before
                diff[market].append(safe_item)
            else:
                # Compare by snapshot_ts_ms (if available) or price+change
                prev_ts = prev_item.get("snapshot_ts_ms") or 0
                curr_ts = safe_item.get("snapshot_ts_ms") or 0
                if curr_ts != prev_ts or safe_item.get("price") != prev_item.get("price"):
                    diff[market].append(safe_item)
        # Detect removed symbols (present in prev but not in current)
        for sym in prev_map.get(market, {}):
            if sym not in current_map:
                diff[market].append({"symbol": sym, "_removed": True})
    return diff


def _announce_frame(announcer: Any, frame: str, mode: int) -> None:
    """Broadcast a complete SSE frame with a single replay-log sequence id.

    The sequence id is allocated and recorded ONCE here, at broadcast time,
    instead of once per connected listener: ``announce`` delivers the same
    ``frame`` to N listeners, and per-listener allocation would (a) fill the
    replay buffer N times faster and (b) break Last-Event-ID resume (each
    client would end up holding an id that does not correspond to the recorded
    entry, causing duplicate replays on reconnect). Consumers emit the
    pre-assigned id without re-recording.
    """
    seq = sse_event_log.next_id()
    sse_event_log.record(seq, mode, "frame", frame)
    announcer.announce((seq, frame))


def announce_current_market_state() -> None:
    """現在のインメモリキャッシュ状態をシリアライズしてSSE配信する"""
    global _sse_payload_cache, _sse_payload_cached_generation
    global \
        _sse_payload_yf_limited, \
        _sse_payload_us_open, \
        _sse_payload_jp_open, \
        _sse_full_snapshot_counter
    with app_state.cache.sse_data_lock:
        stocks = app_state.market.current_stocks_cache
        indices = app_state.market.current_indices_cache
    yf_limited = app_state.market.is_yf_rate_limited()

    us_open = is_market_open("us")
    jp_open = is_market_open("jp")

    with _sse_payload_lock:
        current_gen = _sse_payload_generation
        cached_gen = _sse_payload_cached_generation
        cached_yf = _sse_payload_yf_limited
        cached_us = _sse_payload_us_open
        cached_jp = _sse_payload_jp_open

    if (
        current_gen == cached_gen
        and yf_limited == cached_yf
        and us_open == cached_us
        and jp_open == cached_jp
    ):
        # Nothing changed: emit a comment keepalive instead of re-announcing the
        # previous (diff/full) payload. The comment is ignored by EventSource on
        # the client, so this keeps the stream alive without redundant re-merges
        # of unchanged data every tick.
        app_state.sse_announcer_mode1.announce(_SSE_KEEPALIVE_FRAME)
        return

    # H-1 fix: increment counter inside the lock so concurrent callers
    # don't corrupt the counter or snapshot map (non-atomic read-modify-write).
    with _sse_payload_lock:
        _sse_full_snapshot_counter += 1
        send_full_snapshot = _sse_full_snapshot_counter % FULL_SNAPSHOT_INTERVAL == 0

    with _sse_payload_lock:
        # Build payload AND update _sse_prev_stocks under ONE lock acquisition
        # so concurrent callers never see a partially-updated snapshot map.
        if send_full_snapshot:
            light_stocks = _build_sse_light_stocks_payload(stocks)
            payload = json.dumps(
                {
                    "stream_event": "full_snapshot",
                    "stocks": light_stocks,
                    "indices": indices,
                    "is_yfinance_rate_limited": yf_limited,
                    "is_us_market_open": us_open,
                    "is_jp_market_open": jp_open,
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        else:
            diff = _build_sse_diff(stocks, _sse_prev_stocks)
            # Only send a diff if there are actual changes
            diff_size = sum(len(v) for v in diff.values())
            if diff_size > 0:
                payload = json.dumps(
                    {
                        "stream_event": "diff",
                        "stocks": diff,
                        "indices": indices,
                        "is_yfinance_rate_limited": yf_limited,
                        "is_us_market_open": us_open,
                        "is_jp_market_open": jp_open,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                )
            else:
                # No changes: announce a comment keepalive (the client ignores
                # comments; a repeated diff would only force redundant merges).
                app_state.sse_announcer_mode1.announce(_SSE_KEEPALIVE_FRAME)
                _sse_payload_cached_generation = _sse_payload_generation
                _sse_payload_yf_limited = yf_limited
                _sse_payload_us_open = us_open
                _sse_payload_jp_open = jp_open
                return

        # Update the previous snapshot map AND cache state inside the same
        # lock acquisition to prevent concurrent callers from corrupting the
        # diff computation state (non-atomic read-modify-write on module-level dicts).
        for market in ("us", "jp", "idx"):
            new_map: dict[str, dict[str, Any]] = {}
            for item in stocks.get(market, []):
                if isinstance(item, dict) and item.get("symbol"):
                    new_map[item["symbol"]] = item
            _sse_prev_stocks[market] = new_map

        _sse_payload_cache = f"data: {payload}\n\n"
        _sse_payload_cached_generation = _sse_payload_generation
        _sse_payload_yf_limited = yf_limited
        _sse_payload_us_open = us_open
        _sse_payload_jp_open = jp_open
    _announce_frame(app_state.sse_announcer_mode1, _sse_payload_cache, 1)


def announce_real_market_state() -> None:
    """Announce real market data (target_stocks_cache) via Mode 2 SSE."""
    if app_state.sse_announcer_mode2.listener_count() == 0:
        return
    with app_state.cache.sse_data_lock:
        target_stocks = copy.deepcopy(app_state.market.target_stocks_cache)
        indices = copy.deepcopy(app_state.market.current_indices_cache)
    yf_limited = app_state.market.is_yf_rate_limited()
    us_open = is_market_open("us")
    jp_open = is_market_open("jp")
    light_stocks = _build_sse_light_stocks_payload(target_stocks)
    payload = json.dumps(
        {
            "stream_event": "full_snapshot",
            "stocks": light_stocks,
            "indices": indices,
            "is_yfinance_rate_limited": yf_limited,
            "is_us_market_open": us_open,
            "is_jp_market_open": jp_open,
        },
        ensure_ascii=False,
        allow_nan=False,
    )
    _announce_frame(app_state.sse_announcer_mode2, f"data: {payload}\n\n", 2)


_original_announce_current_market_state = announce_current_market_state


def _run_scheduled_sync_job():
    """スケジュールされた同期ジョブを実行"""
    forced = False
    with app_state.market.sync_schedule_lock:
        if getattr(app_state.market, "sync_forced", False):
            forced = True
            app_state.market.sync_forced = False
    try:
        sync_all_stocks_now(force_fetch=forced)
    finally:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
            pending = app_state.market.sync_pending
            if pending:
                app_state.market.sync_pending = False
        if pending:
            logger.info("Triggering pending stock sync.")
            schedule_sync_all_stocks_now()


def schedule_sync_all_stocks_now(force: bool = False):
    """同期ジョブをスケジュール"""
    with app_state.market.sync_schedule_lock:
        if force:
            app_state.market.sync_forced = True
    with app_state.market.is_syncing_lock:
        if app_state.market.is_syncing and not _recover_stale_sync_state_if_needed():
            with app_state.market.sync_schedule_lock:
                app_state.market.sync_pending = True
            return False

    with app_state.market.sync_schedule_lock:
        if app_state.market.sync_scheduled:
            app_state.market.sync_pending = True
            return False
        app_state.market.sync_scheduled = True

    try:
        app_state.execution.sync_refresh_executor.submit(_run_scheduled_sync_job)
        return True
    except (RuntimeError, AttributeError, ValueError) as exc:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
        logger.warning("Failed to schedule stock sync: %s", exc)
        return False


def _warm_payload_cache_from_disk() -> None:
    """Load cached stock payloads from disk into target cache on cold start or follower sync.

    This allows the UI to display recent data immediately while the background
    thread fetches fresh data from yfinance.
    """
    try:
        # Warm indices cache from disk
        try:
            cached_indices = app_state.payload_disk_cache.get("indices_cache", ignore_ttl=True)
            if isinstance(cached_indices, dict) and cached_indices:
                with app_state.cache.sse_data_lock:
                    app_state.market.current_indices_cache.update(cached_indices)
                logger.info("Warmed indices cache from disk cache")
        except Exception as exc:
            logger.debug("Failed to warm indices cache from disk: %s", exc)

        # Ensure user stock data is loaded from file so we know which symbols to warm
        load_user_stocks(force=True)
        warmed = 0
        for market in ("us", "jp", "idx"):
            user_map = {}
            with app_state.market.user_stocks_lock:
                if market == "us":
                    user_map = dict(app_state.market.user_us)
                elif market == "jp":
                    user_map = dict(app_state.market.user_jp)
                elif market == "idx":
                    user_map = dict(app_state.market.user_idx)

            # Warm both user stocks and default stocks to populate the cache immediately on startup.
            symbols_to_warm = set(user_map.keys())
            for symbol in _default_stock_names(market):
                symbols_to_warm.add(symbol)

            for symbol in symbols_to_warm:
                key = f"payload_{symbol}_{market}"
                cache_file = app_state.payload_disk_cache._entry_path(key)

                try:
                    mtime = os.path.getmtime(cache_file) if cache_file.exists() else 0.0
                except OSError:
                    mtime = 0.0

                # Skip loading if the file has not been modified since the last check
                if mtime != 0.0 and _last_loaded_mtimes.get(key) == mtime:
                    continue

                # Set ignore_ttl=True to load cached payloads even if they are expired.
                # Background scheduler will refresh them asynchronously if market is open.
                cached = app_state.payload_disk_cache.get(key, ignore_ttl=True)
                if cached and isinstance(cached, dict) and cached.get("symbol"):
                    with app_state.cache.sse_data_lock:
                        target_list = app_state.market.target_stocks_cache.get(market, [])

                        # Replace if existing symbol, else append to preserve target_list ordering
                        found = False
                        for i, s in enumerate(target_list):
                            if isinstance(s, dict) and s.get("symbol") == symbol:
                                target_list[i] = cached
                                found = True
                                break
                        if not found:
                            target_list.append(cached)
                        app_state.market.target_stocks_cache[market] = target_list

                    _last_loaded_mtimes[key] = mtime
                    warmed += 1
        if warmed > 0:
            logger.info(
                "Warmed/Updated %d stock payloads from disk cache (including defaults)", warmed
            )
            # Only seed current_stocks_cache from the freshly warmed target when it is
            # empty. The live, interpolated prices built by bg_interpolate_loop live only
            # in current_stocks_cache; unconditionally overwriting them here would reset
            # the real-time feed to the last disk-saved values on every warm-up tick
            # (follower process / any disk payload mtime change). This matches the
            # current_empty guard used in _process_fetched_stocks.
            current_empty = not any(
                app_state.market.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
            )
            if current_empty:
                with app_state.cache.sse_data_lock:
                    app_state.market.current_stocks_cache = copy.deepcopy(
                        app_state.market.target_stocks_cache
                    )
    except (OSError, TypeError, AttributeError, RuntimeError) as exc:
        logger.debug("Disk cache warm-up failed (non-critical): %s", exc)


def _prepare_sync_items(
    force_load: bool = True, force_fetch: bool = False
) -> list[tuple[str, str, str]]:
    """Loads user stocks and default stocks, and prepares the items list for batch fetch."""
    if force_load:
        load_user_stocks(force=True)

    # Ensure all default stocks have placeholder entries in cache if not present
    for market in ("us", "jp", "idx"):
        with app_state.market.user_stocks_lock:
            if market == "us":
                user_set = set(app_state.market.user_us.keys())
            elif market == "jp":
                user_set = set(app_state.market.user_jp.keys())
            else:
                user_set = set(app_state.market.user_idx.keys())
        for symbol, name in _default_stock_names(market).items():
            if symbol not in user_set:
                ensure_stock_placeholder_in_caches(symbol, name, market)

    us_open = is_market_open("us")
    jp_open = is_market_open("jp")

    def _is_cache_incomplete(market: str) -> bool:
        target_list = (
            app_state.market.target_stocks_cache.get(market, [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )
        cached_symbols = {
            s.get("symbol")
            for s in target_list
            if isinstance(s, dict) and s.get("symbol") and s.get("price") not in (None, "--", "")
        }
        with app_state.market.user_stocks_lock:
            if market == "us":
                user_set = set(app_state.market.user_us.keys())
            elif market == "jp":
                user_set = set(app_state.market.user_jp.keys())
            else:
                user_set = set(app_state.market.user_idx.keys())
        required_symbols = set(_default_stock_names(market).keys()) | user_set
        return not required_symbols.issubset(cached_symbols)

    fetch_us = us_open or _is_cache_incomplete("us") or force_fetch
    fetch_jp = jp_open or _is_cache_incomplete("jp") or force_fetch

    def _placeholder_symbols(market):
        target_list = (
            app_state.market.target_stocks_cache.get(market, [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )
        return {
            s.get("symbol")
            for s in target_list
            if isinstance(s, dict) and s.get("price") in (None, "--", "")
        }

    us_placeholders = _placeholder_symbols("us")
    jp_placeholders = _placeholder_symbols("jp")

    items = []
    with app_state.market.user_stocks_lock:
        user_us_snapshot = dict(app_state.market.user_us)
        user_jp_snapshot = dict(app_state.market.user_jp)
        user_idx_snapshot = dict(app_state.market.user_idx)

    user_us_set = set(user_us_snapshot.keys())
    user_jp_set = set(user_jp_snapshot.keys())
    user_idx_set = set(user_idx_snapshot.keys())

    if fetch_us:
        for s, n in user_us_snapshot.items():
            items.append((s, n, "us"))
    else:
        for s, n in user_us_snapshot.items():
            if s in us_placeholders:
                items.append((s, n, "us"))
    if fetch_jp:
        for s, n in user_jp_snapshot.items():
            items.append((s, n, "jp"))
    else:
        for s, n in user_jp_snapshot.items():
            if s in jp_placeholders:
                items.append((s, n, "jp"))
    for s, n in user_idx_snapshot.items():
        items.append((s, n, "idx"))

    for market_name, user_set in (
        ("us", user_us_set),
        ("jp", user_jp_set),
        ("idx", user_idx_set),
    ):
        should_fetch = (
            fetch_us if market_name == "us" else (fetch_jp if market_name == "jp" else True)
        )
        m_placeholders = (
            us_placeholders
            if market_name == "us"
            else (jp_placeholders if market_name == "jp" else set())
        )

        for symbol, name in _default_stock_names(market_name).items():
            if symbol not in user_set and (should_fetch or symbol in m_placeholders):
                items.append((symbol, name, market_name))
    return items


def _process_fetched_stocks(
    fetched_items: list[dict | None],
    sync_generation: int | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Splits fetched items into US, JP, and IDX results and updates caches."""
    us_res, jp_res, idx_res = [], [], []
    for item in fetched_items:
        # Skip the invalid-symbol marker tuple ("__INVALID_SYMBOL__", symbol)
        # and any unexpected non-dict entry: they carry no payload to merge.
        if not isinstance(item, dict) or not item:
            continue
        m = item.get("market")
        if m == "us":
            us_res.append(item)
        elif m == "jp":
            jp_res.append(item)
        else:
            idx_res.append(item)

    with app_state.cache.sse_data_lock:
        # A stale sync may finish after the replacement invocation has already
        # fetched newer data. It must not publish an older snapshot.
        if sync_generation is not None:
            with app_state.market.is_syncing_lock:
                if sync_generation != _sync_generation:
                    logger.info("Discarding stale stock sync generation %s", sync_generation)
                    return [], [], []
        # Preserve previous cache if we skipped fetching that market
        prev_us = (
            app_state.market.target_stocks_cache.get("us", [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )
        prev_jp = (
            app_state.market.target_stocks_cache.get("jp", [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )
        prev_idx = (
            app_state.market.target_stocks_cache.get("idx", [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )

        def merge_cache(prev_list, res_list):
            if not res_list:
                return prev_list
            res_dict = {item["symbol"]: item for item in res_list if item and "symbol" in item}
            merged = []
            seen = set()
            for item in prev_list:
                if not item or "symbol" not in item:
                    continue
                sym = item["symbol"]
                if sym in res_dict:
                    merged.append(res_dict[sym])
                    seen.add(sym)
                else:
                    merged.append(item)
            for item in res_list:
                if not item or "symbol" not in item:
                    continue
                sym = item["symbol"]
                if sym not in seen:
                    merged.append(item)
            return merged

        new_us = merge_cache(prev_us, us_res)
        new_jp = merge_cache(prev_jp, jp_res)
        new_idx = merge_cache(prev_idx, idx_res)

        app_state.market.target_stocks_cache = {"us": new_us, "jp": new_jp, "idx": new_idx}

        # Refresh the previous-close cache from the fresh payloads so realtime
        # producers resolve prev_close without scanning caches under
        # sse_data_lock on every TradingView WS message.
        for market_rows in (new_us, new_jp, new_idx):
            for row in market_rows:
                if not isinstance(row, dict):
                    continue
                sym = row.get("symbol")
                if not sym:
                    continue
                prev_close = row.get("previous_close")
                if prev_close is None:
                    price = row.get("price")
                    change = row.get("change")
                    if price is not None and change is not None:
                        try:
                            prev_close = float(price) - float(change)
                        except (TypeError, ValueError):
                            prev_close = None
                app_state.market.update_previous_close_cache(sym, prev_close)

        announce_real_market_state()
        current_empty = not any(
            app_state.market.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if current_empty:
            app_state.market.current_stocks_cache = copy.deepcopy(
                app_state.market.target_stocks_cache
            )
    return new_us, new_jp, new_idx


def _update_indices_data(idx_res: list[dict], us_res: list[dict], jp_res: list[dict]) -> None:
    """Updates the current indices cache and market status cache with fresh values."""
    header_mapping = {
        "^N225": "N225",
        "^DJI": "DJI",
        "USDJPY=X": "USDJPY",
        "JPY=X": "USDJPY",
        "EURJPY=X": "EURJPY",
        "^IXIC": "NASDAQ",
        "^GSPC": "SP500",
        "^VIX": "VIX",
    }
    new_header_data = {}
    for item in idx_res + us_res + jp_res:
        if not isinstance(item, dict) or not item:
            continue
        sym = item.get("symbol")
        if sym in header_mapping:
            h_key = header_mapping[sym]
            new_header_data[h_key] = {
                "price": item.get("price"),
                "change": item.get("change"),
                "percent": item.get("change_percent") or item.get("percent"),
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "volume": item.get("volume"),
                "market_state": item.get("market_state", "UNKNOWN"),
                "market": item.get("market"),
            }

    critical_indices = {
        "N225": "^N225",
        "DJI": "^DJI",
        "USDJPY": "USDJPY=X",
        "EURJPY": "EURJPY=X",
        "VIX": "^VIX",
        "NASDAQ": "^IXIC",
        "SP500": "^GSPC",
    }
    # The safety net runs sequential retried single fetches while the sync
    # holds _sync_execution_lock (each get_history can take ~25s worst case
    # with retries). Bounding it to a few indices per cycle keeps a legitimate
    # slow sync from tripping the SYNC_STALE_TIMEOUT_SEC watchdog (which would
    # clear is_syncing and spawn a wasted takeover run) and avoids hammering
    # Yahoo with 7 individual fetches during the very outage that caused the
    # batch to fail. Remaining indices are picked up by the next cycle.
    _safety_net_budget = 2
    for key, sym in critical_indices.items():
        if key not in new_header_data or new_header_data[key].get("price") == "--":
            if app_state.market.is_yf_rate_limited():
                continue
            if _safety_net_budget <= 0:
                logger.debug("Safety net budget exhausted for this cycle; deferring %s", key)
                continue
            _safety_net_budget -= 1
            try:
                logger.debug(
                    "Safety net trigger: fetching %s (%s) individually",
                    key,
                    sym,
                )
                res = fetch_index_data(key, sym)
                if res and res[1]:
                    new_header_data[key] = res[1]
            except (
                RequestException,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                YFTickerMissingError,
            ) as safety_exc:
                logger.warning("Safety net failed for %s: %s", key, safety_exc)
    if new_header_data:
        with app_state.cache.sse_data_lock:
            app_state.market.current_indices_cache.update(new_header_data)

        try:
            app_state.payload_disk_cache.set(
                "indices_cache", app_state.market.current_indices_cache
            )
        except Exception as e:
            logger.debug("Failed to cache current_indices_cache to disk: %s", e)

        with app_state.market.market_status_lock:
            if "N225" in new_header_data:
                app_state.market.market_status_cache["jp"] = new_header_data["N225"].get(
                    "market_state"
                )
            if "SP500" in new_header_data:
                st = new_header_data["SP500"].get("market_state")
                app_state.market.market_status_cache["us"] = st
                app_state.market.market_status_cache["idx"] = st

        if "USDJPY" in new_header_data:
            rate_dict = new_header_data["USDJPY"]
            price_val: object = rate_dict.get("price")
            if price_val not in (None, "--", ""):
                try:
                    rate_float = float(price_val)  # type: ignore[arg-type]
                    if math.isfinite(rate_float) and rate_float > 0:
                        # Persist the rate and its freshness together. Merely
                        # updating memory would make every restart report a
                        # false stale warning until another holdings mutation.
                        with app_state.market.user_stocks_lock:
                            app_state.market.last_usdjpy_rate = rate_float
                            app_state.market.last_usdjpy_rate_ts = time.time()
                            try:
                                save_user_stocks()
                            except Exception as persist_exc:  # pylint: disable=broad-exception-caught
                                logger.warning(
                                    "Failed to persist fresh USDJPY state: %s",
                                    persist_exc,
                                )
                except (ValueError, TypeError) as save_exc:
                    logger.debug("Failed to parse USDJPY rate: %s", save_exc)


def _auto_remove_invalid_symbols(
    items: list[tuple[str, str, str]],
    fetched_items: list[dict | None],
) -> None:
    """Track consecutive fetch failures for user-added symbols and auto-remove
    those that exceed the removal threshold.

    Only applied to user-added symbols (not default stocks or indices).
    Skips entirely when yfinance rate limiting is active or when the entire
    batch fetch failed (global issue, not per-symbol).
    """
    if not items or not fetched_items or len(items) != len(fetched_items):
        return
    if app_state.market.is_yf_rate_limited():
        logger.debug("yfinance rate limited; skipping invalid symbol cleanup.")
        return
    if all(f is None for f in fetched_items):
        logger.debug("Entire batch fetch failed; skipping invalid symbol cleanup.")
        return

    threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD

    # Build a set of default symbols so we never auto-remove them
    default_symbols: set[str] = set()
    for m in ("us", "jp", "idx"):
        default_symbols.update(_default_stock_names(m).keys())

    removed_any = False

    # Phase 1: record fetch success/failure per symbol.
    #
    # H3 fix (data-loss protection): a `None` result means the fetch could NOT
    # be completed (transient outage, rate-limit, timeout, skipped fallback) and
    # is NOT evidence that the symbol is invalid — so it must NOT advance the
    # removal streak. Only an explicit invalid-symbol marker (returned when
    # yfinance raises a "ticker missing / delisted" error) counts as a real
    # failure toward auto-removal. This prevents a temporary Yahoo/network
    # outage from silently deleting user stocks.
    with app_state.market.invalid_symbol_lock:
        for (symbol, _name_or_dict, market), result in zip(items, fetched_items):
            if symbol in default_symbols or market == "idx":
                continue
            if _is_batch_result_invalid(result):
                # Genuinely invalid symbol (delisted / not found) -> advance streak.
                app_state.market.record_symbol_fetch_result(symbol, failed=True)
            else:
                # Success OR transient failure -> reset streak (do not penalize).
                app_state.market.record_symbol_fetch_result(symbol, failed=False)

    # Phase 2: check which symbols exceed the threshold and remove them
    symbols_to_remove = app_state.market.get_symbols_to_remove(threshold)
    if not symbols_to_remove:
        return

    # Keep the streak and stock-list mutation in one critical section.  The
    # streak lock is acquired first so a concurrent successful fetch cannot
    # clear its streak between selection, deletion, and a failed-save rollback.
    # save_user_stocks uses the reentrant stock lock, so a storage failure can
    # restore the exact in-memory snapshot before another mutation observes it.
    removed: list[tuple[str, str, Any, int]] = []
    persist_error: Exception | None = None
    with app_state.market.invalid_symbol_lock, app_state.market.user_stocks_lock:
        for symbol in symbols_to_remove:
            if app_state.market.invalid_symbol_streak.get(symbol, 0) < threshold:
                continue
            for market in ("us", "jp"):
                container = _get_stock_container(market)
                if container and symbol in container:
                    original_stock = copy.deepcopy(container[symbol])
                    del container[symbol]
                    streak = app_state.market.invalid_symbol_streak.pop(symbol, 0)
                    logger.warning(
                        "Auto-removed invalid symbol %s from %s (consecutive failures: %d)",
                        symbol,
                        market,
                        streak,
                    )
                    removed.append((symbol, market, original_stock, streak))
                    removed_any = True
                    break

        if removed_any:
            try:
                save_user_stocks()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Do not expose or retain a deletion that could not be persisted.
                # This also prevents a later unrelated save from committing it.
                for symbol, market, original_stock, streak in removed:
                    container = _get_stock_container(market)
                    if container is not None:
                        container[symbol] = original_stock
                    app_state.market.invalid_symbol_streak[symbol] = streak
                persist_error = exc

    if persist_error is not None:
        logger.error(
            "Failed to persist auto-removed invalid symbols; restored %s: %s",
            [(symbol, market) for symbol, market, _stock, _streak in removed],
            persist_error,
        )
        # Schedule a future sync that forces a clean reload from disk to ensure self-healing
        schedule_sync_all_stocks_now()
        return

    if removed_any:
        # Purge the symbol from in-memory caches so it disappears from the
        # UI immediately (rather than lingering via _process_fetched_stocks
        # which preserves old entries for None results).
        from services.realtime_engine import realtime_market_engine

        for symbol, market, _stock, _streak in removed:
            invalidate_stock_caches(symbol)
            remove_stock_from_caches(symbol, market)
            realtime_market_engine.unregister_symbol(symbol, market)
        # The mutation is a watchlist membership change, so Mode 2 needs the
        # same authoritative target snapshot as the regular SSE mode.
        announce_real_market_state()
        schedule_sync_all_stocks_now()


_sync_execution_lock = threading.Lock()


def sync_all_stocks_now(force_fetch: bool = False):
    """Yahoo Financeから全銘柄を一括同期し、ターゲットキャッシュを更新する"""
    global _sync_start_time, _sync_generation
    if not _sync_execution_lock.acquire(blocking=False):
        if not _recover_stale_sync_state_if_needed():
            logger.info("Sync already in progress, skipping.")
            return
        # Stale sync: the previous run exceeded the threshold. Wait a bounded
        # amount of time for it to release the lock (it may still be unwinding
        # after the fetch timeouts); give up so callers never block indefinitely.
        if not _sync_execution_lock.acquire(timeout=SYNC_STALE_LOCK_WAIT_SEC):
            logger.critical(
                "Sync lock takeover timed out after %.0fs; the previous sync is still wedged.",
                SYNC_STALE_LOCK_WAIT_SEC,
            )
            return

    try:
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
            _sync_start_time = time.time()
            _sync_generation += 1
            sync_generation = _sync_generation

        if not _is_sync_leader:
            logger.debug("Follower process: reloading cache from disk payloads")
            _warm_payload_cache_from_disk()
            _invalidate_sse_payload_cache()
            announce_current_market_state()
            return
        with app_state.cache.sse_data_lock:
            if getattr(app_state.market, "current_indices_cache", None) is None:
                app_state.market.current_indices_cache = {}

        # Cold-start: warm in-memory cache from disk before fetching
        target_empty = not any(
            app_state.market.target_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if target_empty:
            _warm_payload_cache_from_disk()

        if any(app_state.market.target_stocks_cache.get(m) for m in ("us", "jp", "idx")):
            app_state.market.first_sync_attempted = True
            if not getattr(app_state.market, "first_sync_completed_at", 0.0):
                app_state.market.first_sync_completed_at = time.time()

        items = _prepare_sync_items(force_load=not target_empty, force_fetch=force_fetch)

        snapshot_ts_ms = int(time.time() * 1000)
        fetched_items = fetch_stocks_batch(items, snapshot_ts_ms=snapshot_ts_ms)

        # Auto-remove persistently failing user-added symbols (TEST1, etc.)
        _auto_remove_invalid_symbols(items, fetched_items)

        us_res, jp_res, idx_res = _process_fetched_stocks(fetched_items, sync_generation)

        if items and not (us_res or jp_res or idx_res):
            logger.warning("Stock sync produced no valid items; preserving previous target cache.")
            return

        with app_state.market.is_syncing_lock:
            is_current_generation = sync_generation == _sync_generation
        if not is_current_generation:
            logger.info(
                "Discarding stale stock sync generation %s before cache publish", sync_generation
            )
            return

        _update_indices_data(idx_res, us_res, jp_res)
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = copy.deepcopy(
                app_state.market.target_stocks_cache
            )
        # Pre-warm heatmap payloads asynchronously in background
        try:
            from constants import POPULAR_JP, POPULAR_US
            from routes.api_stocks import _fetch_heatmap_cached

            app_state.execution.data_executor.submit(
                _fetch_heatmap_cached, "heatmap_us", "us", POPULAR_US
            )
            app_state.execution.data_executor.submit(
                _fetch_heatmap_cached, "heatmap_jp", "jp", POPULAR_JP
            )
        except Exception as prewarm_exc:
            logger.debug("Heatmap pre-warm submission skipped: %s", prewarm_exc)

        # H-7: Invalidate SSE payload cache so announce_current_market_state()
        # rebuilds the serialized payload with the updated data.
        _invalidate_sse_payload_cache()
        announce_current_market_state()
        logger.info("Sync completed.")
    except (
        RequestException,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        RuntimeError,
        YFTickerMissingError,
    ):
        logger.exception("sync_all_stocks_now error")
        raise
    finally:
        try:
            app_state.market.first_sync_attempted = True
            app_state.market.first_sync_completed_at = time.time()
            with app_state.market.is_syncing_lock:
                if sync_generation == _sync_generation:
                    app_state.market.is_syncing = False
        finally:
            _sync_execution_lock.release()


def bg_yahoo_fetch_loop():
    """Yahoo Financeデータの定期取得ループ"""
    app_state.execution.shutdown_event.wait(SSE_MARKET_OPEN_SLEEP)

    while not app_state.execution.shutdown_event.is_set():
        try:
            sync_all_stocks_now()
        except Exception:
            logger.exception("sync_all_stocks_now failed")
            # wrapped_loop in _start_background_threads handles crash recovery

        try:
            listener_count = (
                app_state.sse_announcer_mode1.listener_count()
                + app_state.sse_announcer_mode2.listener_count()
            )
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_NO_LISTENER_SLEEP)
            elif not is_market_open("us") and not is_market_open("jp"):
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP)
            else:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP)
        except Exception:
            logger.exception("Error in market check")
            app_state.execution.shutdown_event.wait(60.0)


def _watchdog_restart_dead_realtime_engine(engine=None) -> list[str]:
    """Restart the realtime engine when any internal producer thread died.

    The TradingView WS / Yahoo JP / PTS workers live inside the engine rather
    than in ``app_state.execution.background_threads``, so the watchdog checks
    them explicitly. Returns the names of restarted threads (empty list when
    all producers are healthy).
    """
    if engine is None:
        from services.realtime_engine import realtime_market_engine

        engine = realtime_market_engine
    try:
        dead = [t for t in engine.worker_threads() if not t.is_alive()]
        if dead:
            logger.warning(
                "Watchdog detected %d dead realtime producer thread(s): %s — restarting engine.",
                len(dead),
                ", ".join(t.name for t in dead),
            )
            engine.restart()
        return [t.name for t in dead]
    except Exception as exc:
        logger.debug("Realtime engine watchdog check failed: %s", exc)
        return []


def _start_background_threads():
    """バックグラウンドスレッドを安全に開始（クラッシュ時に指数バックオフで再起動）"""

    def wrapped_loop(func, name):
        consecutive_errors = 0
        # H-6: 20→10に削減。20回の指数バックオフは最大2^20≈100万秒の待機を
        # 発生させる可能性がある（キャップ600秒でも合計で非常に長い）。
        # 10回でも10分程度のクールダウンで十分な保護が得られる。
        max_consecutive_errors = 10
        while not app_state.execution.shutdown_event.is_set():
            try:
                func()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors > max_consecutive_errors:
                    logger.critical(
                        "%s thread stopped after %d consecutive errors. Restarting...",
                        name,
                        max_consecutive_errors,
                    )
                    # Reset error counter and restart the loop instead of breaking permanently
                    consecutive_errors = 0
                    app_state.execution.shutdown_event.wait(60.0)
                    continue
                sleep_time = min(2**consecutive_errors, 600)
                logger.error(
                    "%s thread crashed (consecutive=%d/%d). Retrying in %ds. Error: %s",
                    name,
                    consecutive_errors,
                    max_consecutive_errors,
                    sleep_time,
                    e,
                )
                app_state.execution.shutdown_event.wait(sleep_time)

    t1 = threading.Thread(target=wrapped_loop, args=(bg_yahoo_fetch_loop, "Yahoo"), daemon=True)
    app_state.execution.background_threads.append(t1)
    t1.start()

    t_leader = threading.Thread(
        target=wrapped_loop, args=(bg_leader_election_loop, "LeaderElection"), daemon=True
    )
    app_state.execution.background_threads.append(t_leader)
    t_leader.start()

    # Reclaim idle yfinance sessions periodically to prevent FD/memory leaks
    # from unbounded session growth during long-running operation.
    from session_manager import bg_session_reap_loop

    t_reap = threading.Thread(
        target=wrapped_loop, args=(bg_session_reap_loop, "SessionReap"), daemon=True
    )
    app_state.execution.background_threads.append(t_reap)
    t_reap.start()

    # Start Realtime Market Data Engine (TradingView WS, Yahoo JP)
    try:
        from constants import POPULAR_JP, POPULAR_US
        from services.realtime_engine import realtime_market_engine
        from utils.storage import load_user_stocks

        # load_user_stocks() populates app_state.market.user_* (its return value
        # is always None), so read the saved watchlist from the app state after
        # loading. Fixes saved symbols never being registered with the realtime
        # engine after a restart.
        load_user_stocks(force=True)
        with app_state.market.user_stocks_lock:
            user_us = list(app_state.market.user_us.keys())
            user_jp = list(app_state.market.user_jp.keys())

        us_defaults = list(dict.fromkeys(POPULAR_US + user_us + ["INDEX:SPX", "INDEX:IUXX"]))
        jp_all_symbols = list(dict.fromkeys(POPULAR_JP + user_jp))

        realtime_market_engine.register_symbols(us_defaults, jp_all_symbols)
        realtime_market_engine.start()
        logger.info(
            "RealtimeMarketEngine started successfully with %d US and %d JP symbols.",
            len(us_defaults),
            len(jp_all_symbols),
        )
    except Exception as e:
        logger.info("Failed to start RealtimeMarketEngine: %s", e)

    t_interp = threading.Thread(
        target=wrapped_loop, args=(bg_interpolate_loop, "Interpolate"), daemon=True
    )
    app_state.execution.background_threads.append(t_interp)
    t_interp.start()

    # Watchdog thread to monitor health of daemon threads
    def bg_threads_watchdog_loop():
        while not app_state.execution.shutdown_event.is_set():
            app_state.execution.shutdown_event.wait(60.0)
            if app_state.execution.shutdown_event.is_set():
                break
            dead_threads = [
                t for t in list(app_state.execution.background_threads) if not t.is_alive()
            ]
            if dead_threads:
                logger.warning(
                    "Watchdog detected %d dead thread(s): %s",
                    len(dead_threads),
                    ", ".join(t.name for t in dead_threads),
                )

            # Realtime engine producers are owned by the engine, not the
            # background_threads registry — check them explicitly and restart
            # any that died so realtime quotes recover without a full reboot.
            _watchdog_restart_dead_realtime_engine()

    t_watchdog = threading.Thread(target=bg_threads_watchdog_loop, name="Watchdog", daemon=True)
    app_state.execution.background_threads.append(t_watchdog)
    t_watchdog.start()
