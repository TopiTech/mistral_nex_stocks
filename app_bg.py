# app_bg.py
"""Background synchronization, yfinance fetching, and SSE interpolation loop."""

from __future__ import annotations

import atexit
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

import pandas as pd
from requests.exceptions import RequestException

from utils.http_utils import parse_retry_after
from utils.market_utils import acquire_yfinance_slot, is_market_open
from utils.normalization import _fmt, _fmt_vol, normalize_history_frame
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    _strip_portfolio_fields,
    build_stock_payload,
)
from app_state import app_state
from constants import (
    SSE_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP,
    SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_NO_LISTENER_SLEEP,
)
from route_helpers import (
    invalidate_stock_caches,
    remove_stock_from_caches,
)
from utils.storage import load_user_stocks, save_user_stocks

import concurrent.futures

logger = logging.getLogger(__name__)


_LEADER_LOCK_FILE = None
_is_sync_leader = True  # Default to True so it functions normally in single-process mode
_sync_start_time: float = 0.0
_last_loaded_mtimes: dict[str, float] = {}

# Maximum time (seconds) a single sync_all_stocks_now() may run before the
# is_syncing lock is treated as stale. yfinance timeouts are shorter (batch=20s,
# single=6s), so this is a defense-in-depth guard against unexpected hangs.
SYNC_STALE_TIMEOUT_SEC: float = 120.0


def _release_leader_lock() -> None:
    """Close the leader lock file handle on process exit (M-3: prevent FD leak)."""
    global _LEADER_LOCK_FILE
    if _LEADER_LOCK_FILE is not None:
        try:
            _LEADER_LOCK_FILE.close()
        except OSError:
            pass
        _LEADER_LOCK_FILE = None


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
    base_dir = Path(__file__).resolve().parent
    lock_path = base_dir / ".mns_sync_leader.lock"
    pid = os.getpid()

    try:
        if os.name == "nt":  # Windows
            if msvcrt is not None:
                if _LEADER_LOCK_FILE is None:
                    _LEADER_LOCK_FILE = open(lock_path, "w", encoding="utf-8")
                fd = _LEADER_LOCK_FILE.fileno()
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
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
                    _LEADER_LOCK_FILE = open(lock_path, "w", encoding="utf-8")
                try:
                    fcntl.flock(_LEADER_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                    _LEADER_LOCK_FILE.write(str(pid))
                    _LEADER_LOCK_FILE.flush()
                    return True
                except OSError:
                    return False
            # Fallback: atomic file creation
            return _try_acquire_atomic_lock(lock_path, pid)
    except (OSError, IOError, ValueError) as exc:
        logger.debug("Failed to acquire sync leader lock: %s", exc)
        return False


def _try_acquire_atomic_lock(lock_path: Path, pid: int) -> bool:
    """Try to acquire a lock via atomic file creation (O_CREAT | O_EXCL).

    This is a universal fallback that works on any platform without
    platform-specific locking libraries (fcntl, msvcrt).

    If the lock file already exists, checks whether the owning process is
    still alive. If the PID is stale (process no longer running), the stale
    lock is removed and re-acquired.
    """
    global _LEADER_LOCK_FILE
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(pid).encode())
        os.close(fd)
        _LEADER_LOCK_FILE = open(lock_path, "r+", encoding="utf-8")
        logger.debug("Acquired atomic leader lock at %s (pid=%d)", lock_path, pid)
        return True
    except FileExistsError:
        # Lock file exists — check if PID is stale
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                try:
                    existing_pid = int(content)
                    if existing_pid != pid:
                        # Check if the process still exists
                        try:
                            os.kill(existing_pid, 0)  # Signal 0 = existence check only
                            # Process still alive — lock is valid
                            return False
                        except OSError:
                            # Process no longer exists — stale lock, remove and retry
                            logger.info(
                                "Removing stale leader lock from pid=%s (process no longer running)",
                                existing_pid,
                            )
                            try:
                                lock_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                            # Retry acquisition once
                            return _try_acquire_atomic_lock(lock_path, pid)
                except (ValueError, TypeError):
                    pass
        except (IOError, OSError):
            pass
        return False
    except (OSError, IOError) as exc:
        logger.debug("Failed to acquire atomic leader lock: %s", exc)
        return False


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
    """Handle exceptions from yfinance queries and increment/set rate limits if 429/401/402/439 is received."""
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
    snapshot_ts_ms: Optional[int] = None,
) -> dict[str, Any] | None:
    """単一銘柄のデータを取得する"""
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
        except (RequestException, ValueError, KeyError, IndexError, OSError) as e:
            logger.debug("Fetch failed for %s with period %s: %s", symbol, period, e)

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
                app_state.payload_disk_cache.set(f"payload_{symbol}_{market}", payload)
            except (IOError, OSError, TypeError):
                logger.debug("Failed to cache payload for %s", symbol)
            return payload
        return None
    except (RequestException, ValueError, TypeError, KeyError, IndexError, OSError) as exc:
        _handle_yfinance_error(exc, symbol)
        logger.error("Stock fetch failed (%s): %s", symbol, exc, exc_info=True)
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
    items: List[Tuple[str, str, str]], snapshot_ts_ms: Optional[int] = None, lightweight: bool = False
) -> List[Any]:
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
            downloaded = app_state.stock_provider.download_batch(symbols, period="3mo", lightweight=lightweight)
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
            "Batch fetch completely failed or empty. Preserving previous state to avoid N+1 rate limiting."
        )
        return [None] * len(items)

    results_map = {}
    fallback_items = []
    # Cap parallel per-symbol fallbacks. Each fallback is a fresh yfinance
    # history fetch, so we keep this small (2) and skip it entirely when
    # already rate-limited to avoid fanning out N individual requests that
    # would deepen the block.
    MAX_FALLBACKS = 2

    for symbol, name, market in items:
        payload = None
        if downloaded is not None and not downloaded.empty:
            try:
                hist = extract_batch_history(downloaded, symbol, single_symbol=(len(symbols) == 1))
                if not hist.empty and len(hist) >= 1:
                    payload = build_stock_payload(
                        symbol, name, market, hist, snapshot_ts_ms=snapshot_ts_ms, lightweight=lightweight
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

    to_fetch = fallback_items[:MAX_FALLBACKS]
    skipped_items = fallback_items[MAX_FALLBACKS:]

    for symbol, _, _ in skipped_items:
        logger.debug("Skipping fallback for %s: limit reached", symbol)
        results_map[symbol] = None

    if to_fetch:
        futures_map = {}

        logger.info(
            "Fallback parallel single queries triggered for %d stocks (limit %d)",
            len(to_fetch),
            MAX_FALLBACKS,
        )

        for symbol, name, market in to_fetch:
            fut = app_state.execution.data_executor.submit(
                fetch_stock, symbol, name, market, snapshot_ts_ms
            )
            futures_map[fut] = symbol

        done, not_done = concurrent.futures.wait(
            futures_map.keys(),
            timeout=5.0,
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


def fetch_index_data(key: str, symbol: str) -> Optional[Tuple[str, Dict[str, Any]]]:
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
        is_open = is_market_open(market_type, bypass_cache=True)
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
    except (RequestException, ValueError, TypeError, KeyError, IndexError, OSError) as exc:
        logger.error(
            "Index fetch failed for %s: %s",
            key,
            exc,
            exc_info=True,
        )
        return None


def _build_sse_light_stocks_payload(stocks_by_market):
    """SSE配信用の軽量株価ペイロードを構築

    Portfolio fields (shares/avg_price/avg_fx_rate/portfolio_*/portfolio_pl) are
    intentionally excluded from the unauthenticated SSE stream (H-3). Holdings
    stay on disk and in-memory; clients that need them must call a trusted path.
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

            chart_rows = safe_item.get("chart_data") if isinstance(safe_item.get("chart_data"), list) else []
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

_sse_payload_cache: str = 'data: {"stocks":[],"indices":[],"is_yfinance_rate_limited":false}\n\n'
_sse_payload_generation: int = 0
_sse_payload_cached_generation: int = -1
_sse_payload_yf_limited: bool = False

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
                if curr_ts != prev_ts:
                    diff[market].append(safe_item)
                elif safe_item.get("price") != prev_item.get("price"):
                    diff[market].append(safe_item)
        # Detect removed symbols (present in prev but not in current)
        for sym in prev_map.get(market, {}):
            if sym not in current_map:
                diff[market].append({"symbol": sym, "_removed": True})
    return diff


def announce_current_market_state() -> None:
    """現在のインメモリキャッシュ状態をシリアライズしてSSE配信する"""
    global _sse_payload_cache, _sse_payload_cached_generation
    global _sse_payload_yf_limited, _sse_full_snapshot_counter
    with app_state.cache.sse_data_lock:
        stocks = app_state.market.current_stocks_cache
        indices = app_state.market.current_indices_cache
        yf_limited = app_state.market.is_yf_rate_limited()

    # H-7: Use a generation counter (incremented by sync_all_stocks_now) instead
    # of object identity comparison. _process_fetched_stocks creates new list
    # objects on every sync, so identity checks always fail and the cache is
    # rebuilt on every tick — defeating the purpose of the cache entirely.
    # The generation counter is cheap to increment and avoids this O(N) rebuild.
    with _sse_payload_lock:
        current_gen = _sse_payload_generation
        cached_gen = _sse_payload_cached_generation
        cached_yf = _sse_payload_yf_limited

    if current_gen == cached_gen and yf_limited == cached_yf:
        app_state.sse_announcer.announce(_sse_payload_cache)
        return

    # H-1 fix: increment counter inside the lock so concurrent callers
    # don't corrupt the counter or snapshot map (non-atomic read-modify-write).
    with _sse_payload_lock:
        _sse_full_snapshot_counter += 1
        send_full_snapshot = _sse_full_snapshot_counter % FULL_SNAPSHOT_INTERVAL == 0

    if send_full_snapshot:
        light_stocks = _build_sse_light_stocks_payload(stocks)
        payload = json.dumps(
            {
                "stream_event": "full_snapshot",
                "stocks": light_stocks,
                "indices": indices,
                "is_yfinance_rate_limited": yf_limited,
            },
            ensure_ascii=False,
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
                },
                ensure_ascii=False,
            )
        else:
            # No changes: announce the cached payload directly
            app_state.sse_announcer.announce(_sse_payload_cache)
            with _sse_payload_lock:
                _sse_payload_cached_generation = _sse_payload_generation
                _sse_payload_yf_limited = yf_limited
            return

    # H-1 fix: update previous snapshot map AND cache state inside the same
    # lock acquisition to prevent concurrent callers from corrupting the diff
    # computation state (non-atomic read-modify-write on module-level dicts).
    with _sse_payload_lock:
        # Update the previous snapshot map for next diff computation
        for market in ("us", "jp", "idx"):
            new_map: dict[str, dict[str, Any]] = {}
            for item in stocks.get(market, []):
                if isinstance(item, dict) and item.get("symbol"):
                    new_map[item["symbol"]] = item
            _sse_prev_stocks[market] = new_map

        _sse_payload_cache = f"data: {payload}\n\n"
        _sse_payload_cached_generation = _sse_payload_generation
        _sse_payload_yf_limited = yf_limited
    app_state.sse_announcer.announce(_sse_payload_cache)


def _run_scheduled_sync_job():
    """スケジュールされた同期ジョブを実行"""
    forced = False
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
    if force:
        app_state.market.sync_forced = True
    with app_state.market.is_syncing_lock:
        if app_state.market.is_syncing:
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
            for symbol in _default_stock_names(market).keys():
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
            logger.info("Warmed/Updated %d stock payloads from disk cache (including defaults)", warmed)
            with app_state.cache.sse_data_lock:
                current_empty = not any(
                    app_state.market.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
                )
                if current_empty:
                    app_state.market.current_stocks_cache = copy.deepcopy(
                        app_state.market.target_stocks_cache
                    )
    except (IOError, OSError, TypeError, AttributeError, RuntimeError) as exc:
        logger.debug("Disk cache warm-up failed (non-critical): %s", exc)


def _prepare_sync_items(
    force_load: bool = True, force_fetch: bool = False
) -> List[Tuple[str, str, str]]:
    """Loads user stocks and default stocks, and prepares the items list for batch fetch."""
    if force_load:
        load_user_stocks(force=True)

    us_open = is_market_open("us")
    jp_open = is_market_open("jp")

    us_cache_empty = not (
        isinstance(app_state.market.current_stocks_cache, dict)
        and app_state.market.current_stocks_cache.get("us")
    )
    jp_cache_empty = not (
        isinstance(app_state.market.current_stocks_cache, dict)
        and app_state.market.current_stocks_cache.get("jp")
    )

    fetch_us = us_open or us_cache_empty or force_fetch
    fetch_jp = jp_open or jp_cache_empty or force_fetch

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

    us_placeholders = _placeholder_symbols("us") if not fetch_us else set()
    jp_placeholders = _placeholder_symbols("jp") if not fetch_jp else set()

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
        if market_name == "us" and not fetch_us:
            continue
        if market_name == "jp" and not fetch_jp:
            continue

        for symbol, name in _default_stock_names(market_name).items():
            if symbol not in user_set:
                items.append((symbol, name, market_name))
    return items


def _process_fetched_stocks(
    fetched_items: List[Optional[dict]],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Splits fetched items into US, JP, and IDX results and updates caches."""
    us_res, jp_res, idx_res = [], [], []
    for item in fetched_items:
        if not item:
            continue
        m = item.get("market")
        if m == "us":
            us_res.append(item)
        elif m == "jp":
            jp_res.append(item)
        else:
            idx_res.append(item)

    with app_state.cache.sse_data_lock:
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
        current_empty = not any(
            app_state.market.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if current_empty:
            app_state.market.current_stocks_cache = copy.deepcopy(
                app_state.market.target_stocks_cache
            )
    return new_us, new_jp, new_idx


def _update_indices_data(idx_res: List[dict], us_res: List[dict], jp_res: List[dict]) -> None:
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
        if not item:
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
    for key, sym in critical_indices.items():
        if key not in new_header_data or new_header_data[key].get("price") == "--":
            if app_state.market.is_yf_rate_limited():
                continue
            try:
                logger.debug(
                    "Safety net trigger: fetching %s (%s) individually",
                    key,
                    sym,
                )
                res = fetch_index_data(key, sym)
                if res and res[1]:
                    new_header_data[key] = res[1]
            except (RequestException, ValueError, KeyError, IndexError, TypeError) as safety_exc:
                logger.warning("Safety net failed for %s: %s", key, safety_exc)
    if new_header_data:
        with app_state.cache.sse_data_lock:
            app_state.market.current_indices_cache.update(new_header_data)

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
                    if rate_float > 0:
                        app_state.market.last_usdjpy_rate = rate_float
                except (ValueError, TypeError) as save_exc:
                    logger.debug("Failed to parse USDJPY rate: %s", save_exc)


def _auto_remove_invalid_symbols(
    items: List[Tuple[str, str, str]],
    fetched_items: List[Optional[dict]],
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

    # Track which market each removed symbol belonged to
    removed: list[tuple[str, str]] = []

    with app_state.market.user_stocks_lock:
        for symbol in symbols_to_remove:
            for market in ("us", "jp"):
                container = _get_stock_container(market)
                if container and symbol in container:
                    del container[symbol]
                    streak = app_state.market.invalid_symbol_streak.pop(symbol, 0)
                    logger.warning(
                        "Auto-removed invalid symbol %s from %s (consecutive failures: %d)",
                        symbol,
                        market,
                        streak,
                    )
                    removed.append((symbol, market))
                    removed_any = True
                    break

    if removed_any:
        # Purge the symbol from in-memory caches so it disappears from the
        # UI immediately (rather than lingering via _process_fetched_stocks
        # which preserves old entries for None results).
        for symbol, market in removed:
            invalidate_stock_caches(symbol)
            remove_stock_from_caches(symbol, market)
        try:
            save_user_stocks()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Background path: log loudly but keep the in-memory removal so the
            # next successful save or manual reset can recover consistency.
            logger.error(
                "Failed to persist auto-removed invalid symbols %s: %s",
                removed,
                exc,
            )
        schedule_sync_all_stocks_now()


def sync_all_stocks_now(force_fetch: bool = False):
    """Yahoo Financeから全銘柄を一括同期し、ターゲットキャッシュを更新する"""
    global _sync_start_time
    with app_state.market.is_syncing_lock:
        if app_state.market.is_syncing:
            # M-6: Stale sync detection — if the sync lock has been held for
            # longer than SYNC_STALE_TIMEOUT_SEC, a previous invocation may
            # have timed out without releasing the lock. Force-reset it to
            # unblock future sync cycles.
            elapsed = time.time() - _sync_start_time if _sync_start_time > 0 else 0.0
            if elapsed > SYNC_STALE_TIMEOUT_SEC:
                logger.warning(
                    "sync_all_stocks_now lock is stale (elapsed=%.0fs), force-resetting",
                    elapsed,
                )
                app_state.market.is_syncing = False
            else:
                logger.info("Sync already in progress, skipping.")
                return
        app_state.market.is_syncing = True
        _sync_start_time = time.time()

    try:
        if not _is_sync_leader:
            logger.debug("Follower process: reloading cache from disk payloads")
            _warm_payload_cache_from_disk()
            _invalidate_sse_payload_cache()
            announce_current_market_state()
            return
        with app_state.cache.sse_data_lock:
            if getattr(app_state, "current_indices_cache", None) is None:
                app_state.market.current_indices_cache = {}

        # Cold-start: warm in-memory cache from disk before fetching
        target_empty = not any(
            app_state.market.target_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if target_empty:
            _warm_payload_cache_from_disk()

        items = _prepare_sync_items(force_load=not target_empty, force_fetch=force_fetch)

        snapshot_ts_ms = int(time.time() * 1000)
        fetched_items = fetch_stocks_batch(items, snapshot_ts_ms=snapshot_ts_ms)

        # Auto-remove persistently failing user-added symbols (TEST1, etc.)
        _auto_remove_invalid_symbols(items, fetched_items)

        us_res, jp_res, idx_res = _process_fetched_stocks(fetched_items)

        if items and not (us_res or jp_res or idx_res):
            logger.warning("Stock sync produced no valid items; preserving previous target cache.")
            return

        _update_indices_data(idx_res, us_res, jp_res)
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = copy.deepcopy(
                app_state.market.target_stocks_cache
            )
        # H-7: Invalidate SSE payload cache so announce_current_market_state()
        # rebuilds the serialized payload with the updated data.
        _invalidate_sse_payload_cache()
        announce_current_market_state()
        logger.info("Sync completed.")
    except (RequestException, ValueError, TypeError, KeyError, OSError, RuntimeError) as e:
        logger.error("sync_all_stocks_now: %s", e, exc_info=True)
        raise
    finally:
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False


def bg_yahoo_fetch_loop():
    """Yahoo Financeデータの定期取得ループ"""
    app_state.execution.shutdown_event.wait(SSE_MARKET_OPEN_SLEEP)

    while not app_state.execution.shutdown_event.is_set():
        try:
            sync_all_stocks_now()
        except (RequestException, ValueError, TypeError, RuntimeError, AttributeError) as e:
            logger.error("sync_all_stocks_now failed: %s", e)
            # wrapped_loop in _start_background_threads handles crash recovery

        try:
            listener_count = app_state.sse_announcer.listener_count()
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_NO_LISTENER_SLEEP)
            elif not is_market_open("us") and not is_market_open("jp"):
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP)
            else:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP)
        except (OSError, RuntimeError, ValueError, TypeError) as e:
            logger.error("Error in market check: %s", e)
            app_state.execution.shutdown_event.wait(60.0)


def _start_background_threads():
    """バックグラウンドスレッドを安全に開始（クラッシュ時に指数バックオフで再起動）"""

    def wrapped_loop(func, name):
        consecutive_errors = 0
        # H-6: 20→10に削減。20回の指数バックオフは最大2^20≈100万秒の待機を
        # 発生させる可能性がある（キャップ600秒でも合計で非常に長い）。
        # 10回でも10分程度のクールダウンで十分な保護が得られる。
        MAX_CONSECUTIVE_ERRORS = 10
        while not app_state.execution.shutdown_event.is_set():
            try:
                func()
                consecutive_errors = 0
            except (RequestException, ValueError, TypeError, OSError, RuntimeError) as e:
                consecutive_errors += 1
                if consecutive_errors > MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        "%s thread stopped after %d consecutive errors. Restarting...",
                        name,
                        MAX_CONSECUTIVE_ERRORS,
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
                    MAX_CONSECUTIVE_ERRORS,
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
