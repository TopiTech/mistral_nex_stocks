"""
market_state.py - Market data state management.

Extracted from app_state.py to reduce module complexity.
Manages stock data, market status, yfinance rate limiting, and circuit breakers.
"""

import logging
import math
import threading
import time
from collections import OrderedDict
from typing import Any

from cachetools import TTLCache

from constants import (
    DEFAULT_USDJPY_RATE,
    HISTORY_SEMAPHORE_CAPACITY,
    SCRAPER_BACKOFF_INITIAL,
    SCRAPER_BACKOFF_MAX,
    SCRAPER_BACKOFF_MULTIPLIER,
    YFINANCE_ADAPTIVE_INTERVAL_FACTOR,
    YFINANCE_BACKOFF_INITIAL,
    YFINANCE_BACKOFF_MAX,
    YFINANCE_BACKOFF_MULTIPLIER,
    YFINANCE_JITTER_FACTOR,
    YFINANCE_MIN_INTERVAL,
    YFINANCE_SHORT_CACHE_TTL,
)
from session_manager import yf_session_manager
from utils.normalization import finite_float, is_bool_like

logger = logging.getLogger("backend")


def _make_circuit_state() -> "CircuitState":
    """Factory helper; returns a default (CLOSED) CircuitState."""
    return CircuitState()


class CircuitState:
    """State of a circuit breaker for an external service.

    CLOSED: Service is operating normally.
    OPEN: Service is failing; requests are fast-failing.
    HALF_OPEN: Service is being probed for recovery. Only 1 thread probes at a time.

    Supports both attribute and dict-style access for backward compatibility
    with code that was written when CircuitState was a TypedDict.
    """

    def __init__(
        self,
        status: str = "CLOSED",
        timeout_streak: int = 0,
        open_until: float = 0.0,
        probing: bool = False,
        probing_since: float = 0.0,
    ):
        self.status = status
        self.timeout_streak = timeout_streak
        self.open_until = open_until
        self.probing = probing
        self.probing_since = probing_since

    # Dict-style access for backward compatibility
    def __getitem__(self, key: str):
        return getattr(self, key)

    def __setitem__(self, key: str, value):
        setattr(self, key, value)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


class MarketDataState:
    """Manages stock data, market conditions, and yfinance rate limiting."""

    def __init__(self):
        self.user_us: dict[str, Any] = {}
        self.user_jp: dict[str, Any] = {}
        self.user_idx: dict[str, Any] = {}
        self.user_stocks_lock = threading.RLock()

        self.last_usdjpy_rate = DEFAULT_USDJPY_RATE
        # R5: record the timestamp of the last USDJPY update so consumers can
        # warn (or refuse) when the cached rate is stale. A backend that
        # boots, runs once, then idles for a day will leave this older than
        # 24h and surface a "FX rate may be stale" warning instead of
        # silently computing share counts with a day-old rate.
        self.last_usdjpy_rate_ts: float = 0.0
        self.last_usdjpy_persisted_ts: float = 0.0
        self.usdjpy_rate_lock = threading.RLock()

        self.last_modified_ns = 0
        # Process-internal monotonic version counter for user_stocks.json.
        # Incremented inside user_stocks_lock on every successful save; readers
        # compare it to last_loaded_rev so stale reads are detected reliably
        # without depending solely on filesystem mtime.
        # Initialise last_loaded_rev to -1 so the first non-force load does not
        # skip loading (both initial 0 would match and cause an early return).
        self.user_stocks_rev = 0
        self.last_loaded_rev = -1
        # Depth counter of in-flight save_user_stocks() calls (H3 of the
        # 2026-11-07 review). save_user_stocks() deliberately releases
        # user_stocks_lock during its disk I/O; load_user_stocks() must not
        # publish the on-disk (pre-swap) state over un-flushed in-memory
        # mutations in that window — a queued second save would snapshot the
        # reverted state and persist it, silently losing the earlier mutation.
        # Increments happen under user_stocks_lock at save snapshot time and
        # decrement in the save's finally, so load can skip publishing (and
        # leave last_loaded_rev untouched) whenever the count is positive.
        self.user_stocks_save_pending = 0
        # Set when load_user_stocks() fails to decrypt the stored blob (e.g.
        # master key rotated / keyring reset). When set, the in-memory lists are
        # deliberately NOT reset to {} so a subsequent save cannot overwrite the
        # on-disk backup with an empty set. Callers may surface this to the user.
        self.user_stocks_load_error = False
        self.current_stocks_cache: dict[str, list[Any]] = {"us": [], "jp": [], "idx": []}
        self.target_stocks_cache: dict[str, list[Any]] = {"us": [], "jp": [], "idx": []}
        self.current_indices_cache: dict[str, Any] = {}
        self.target_indices_cache: dict[str, Any] = {}

        self.is_syncing = False
        self.is_syncing_lock = threading.RLock()
        self.sync_scheduled = False
        self.sync_schedule_lock = threading.RLock()
        self.sync_pending = False
        self.sync_forced = False

        self.market_status_cache: dict[str, str | None] = {"us": None, "jp": None, "idx": None}
        self.market_status_lock = threading.RLock()

        # yfinance rate limiting
        self.yfinance_lock = threading.RLock()
        self.is_yfinance_rate_limited = False
        self.yfinance_rate_limit_until = 0.0
        self.yfinance_last_request_ts = 0.0
        self.yfinance_min_interval_sec = YFINANCE_MIN_INTERVAL
        self.yfinance_adaptive_interval_sec = YFINANCE_MIN_INTERVAL
        self.yfinance_jitter_factor = YFINANCE_JITTER_FACTOR
        self.yfinance_429_streak = 0
        self.yfinance_429_backoff_multiplier = YFINANCE_BACKOFF_MULTIPLIER
        self.yfinance_backoff_initial = YFINANCE_BACKOFF_INITIAL
        self.yfinance_max_backoff_sec = YFINANCE_BACKOFF_MAX
        # Capacity matches data_executor workers (default 4) to prevent artificial
        # worker bottlenecks on concurrent chart history fetches.
        self.yfinance_history_semaphore = threading.Semaphore(HISTORY_SEMAPHORE_CAPACITY)
        self.yfinance_short_cache_lock = threading.RLock()
        self.yfinance_short_cache: TTLCache[str, Any] = TTLCache(
            maxsize=512,
            ttl=YFINANCE_SHORT_CACHE_TTL,
        )

        # Negative symbol cache (invalid / delisted tickers) to avoid repeating failing requests (429 prevention)
        self.negative_symbol_cache_lock = threading.RLock()
        self.negative_symbol_cache: TTLCache[str, str] = TTLCache(
            maxsize=1024,
            ttl=600.0,
        )

        # Web scraper global block detection (Yahoo JP / Kabutan / SBI / Minkabu).
        # Mirrors the yfinance rate-limit state: a site-wide 401/403/429/439 pauses
        # ALL scrapers until the graduated cooldown elapses so a blocked IP stops
        # hammering the upstream providers (which would deepen the block).
        self.scraper_block_lock = threading.RLock()
        self.scraper_block_streak = 0
        self.scraper_block_until = 0.0
        self.scraper_backoff_initial = SCRAPER_BACKOFF_INITIAL
        self.scraper_backoff_multiplier = SCRAPER_BACKOFF_MULTIPLIER
        self.scraper_max_backoff_sec = SCRAPER_BACKOFF_MAX

        # Previous-close price cache for realtime producers. TradingView WS
        # qsd messages arrive several times per second per symbol, and each
        # delta needs the previous close to derive change. Looking it up in
        # target_stocks_cache under sse_data_lock on every message caused
        # lock contention with SSE stream serialization (up to ~0.9s stalls
        # during initial snapshots). This dict is maintained by the payload
        # build/sync path and bounded to prevent memory growth.
        self.previous_close_cache: TTLCache[str, float] = TTLCache(maxsize=2048, ttl=86400.0)
        self.previous_close_cache_lock = threading.RLock()
        self.yfinance_volume_cache: TTLCache[str, int] = TTLCache(maxsize=2048, ttl=86400.0)
        self.yfinance_volume_cache_lock = threading.RLock()

        # Circuit breakers
        self.circuit_lock = threading.RLock()
        self.history_circuit_lock = self.circuit_lock
        self.history_circuit_state: TTLCache[str, CircuitState] = TTLCache(maxsize=2048, ttl=3600.0)
        self.circuit_states: dict[str, CircuitState] = {
            "mistral": _make_circuit_state(),
            "langsearch": _make_circuit_state(),
            "openai_compatible": _make_circuit_state(),
        }
        self.history_circuit_states: TTLCache[str, CircuitState] = self.history_circuit_state

        # Track consecutive yfinance fetch failures per user-added symbol.
        # Symbols that exceed INVALID_SYMBOL_REMOVAL_THRESHOLD consecutive
        # failures are automatically removed from the user stock list.
        self.invalid_symbol_streak: TTLCache[str, int] = TTLCache(maxsize=1024, ttl=86400.0)
        self.invalid_symbol_lock = threading.RLock()
        self.first_sync_attempted: bool = False
        self.first_sync_completed_at: float = 0.0
        self.removed_watchlist_symbols: set[tuple[str, str]] = set()
        self._removed_watchlist_order: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._removed_watchlist_maxsize = 4096
        self.removed_watchlist_lock = threading.Lock()

    @staticmethod
    def _watchlist_aliases(symbol: str, market: str) -> list[tuple[str, str]]:
        norm_market = str(market or "").strip().lower()
        upper = str(symbol or "").strip().upper()
        if not upper or not norm_market:
            return []
        aliases = [(upper, norm_market)]
        if norm_market == "jp":
            if upper.endswith(".T"):
                aliases.append((upper[:-2], norm_market))
            else:
                aliases.append((f"{upper}.T", norm_market))
        return aliases

    def mark_watchlist_removed(self, symbol: str, market: str) -> None:
        """Record a user/auto-removed watchlist symbol so an overlapping sync cannot resurrect it."""
        aliases = self._watchlist_aliases(symbol, market)
        if not aliases:
            return
        with self.removed_watchlist_lock:
            if not self.removed_watchlist_symbols and self._removed_watchlist_order:
                self._removed_watchlist_order.clear()
            for key in aliases:
                self.removed_watchlist_symbols.add(key)
                self._removed_watchlist_order[key] = None
                self._removed_watchlist_order.move_to_end(key)
            while (
                len(self.removed_watchlist_symbols) > self._removed_watchlist_maxsize
                and self._removed_watchlist_order
            ):
                evicted, _ = self._removed_watchlist_order.popitem(last=False)
                self.removed_watchlist_symbols.discard(evicted)

    def clear_watchlist_removed(self, symbol: str, market: str) -> None:
        """Clear a removed-watchlist marker when the symbol is explicitly added/restored."""
        aliases = self._watchlist_aliases(symbol, market)
        if not aliases:
            return
        with self.removed_watchlist_lock:
            for key in aliases:
                self.removed_watchlist_symbols.discard(key)
                self._removed_watchlist_order.pop(key, None)

    def is_watchlist_removed(self, symbol: str, market: str) -> bool:
        """Return True if *(symbol, market)* was removed and not yet re-added."""
        aliases = self._watchlist_aliases(symbol, market)
        if not aliases:
            return False
        with self.removed_watchlist_lock:
            return any(key in self.removed_watchlist_symbols for key in aliases)

    INVALID_SYMBOL_REMOVAL_THRESHOLD: int = 3

    def record_symbol_fetch_result(self, symbol: str, failed: bool) -> None:
        """Record whether a symbol fetch succeeded or failed.

        Resets the streak on success; increments on failure. Only *genuine*
        invalid-symbol failures (delisted / not found) should be passed as
        ``failed=True`` — transient outages (rate-limit, timeout, network) must
        be passed as ``failed=False`` so they never accumulate into silent
        deletion of user stocks. See ``_auto_remove_invalid_symbols`` in
        ``app_bg.py``.
        """
        with self.invalid_symbol_lock:
            if failed:
                self.invalid_symbol_streak[symbol] = self.invalid_symbol_streak.get(symbol, 0) + 1
            else:
                self.invalid_symbol_streak.pop(symbol, None)

    def get_symbols_to_remove(self, threshold: int | None = None) -> list[str]:
        """Return symbols whose consecutive failure streak exceeds threshold."""
        if threshold is None:
            threshold = self.INVALID_SYMBOL_REMOVAL_THRESHOLD
        with self.invalid_symbol_lock:
            return [
                sym for sym, streak in self.invalid_symbol_streak.items() if streak >= threshold
            ]

    # --- Negative Symbol Cache ---

    def is_negative_cached_symbol(self, symbol: str) -> bool:
        """Return True if symbol is cached as an invalid / non-existent ticker."""
        if not symbol:
            return False
        with self.negative_symbol_cache_lock:
            return symbol.strip().upper() in self.negative_symbol_cache

    def mark_negative_cached_symbol(self, symbol: str, reason: str = "") -> None:
        """Mark a symbol as invalid / non-existent to avoid repeated failing requests."""
        if not symbol:
            return
        with self.negative_symbol_cache_lock:
            self.negative_symbol_cache[symbol.strip().upper()] = reason or "invalid_symbol"

    def clear_negative_symbol_cache(self, symbol: str | None = None) -> None:
        """Clear negative cache for a specific symbol or all symbols."""
        with self.negative_symbol_cache_lock:
            if symbol:
                self.negative_symbol_cache.pop(symbol.strip().upper(), None)
            else:
                self.negative_symbol_cache.clear()

    # --- Circuit Breaker ---

    def get_circuit_state(self, service: str, symbol: str | None = None) -> CircuitState:
        with self.circuit_lock:
            if symbol:
                if symbol not in self.history_circuit_states:
                    self.history_circuit_states[symbol] = _make_circuit_state()
                return self.history_circuit_states[symbol]
            if service not in self.circuit_states:
                self.circuit_states[service] = _make_circuit_state()
            return self.circuit_states[service]

    def report_circuit_result(
        self,
        service: str,
        success: bool,
        symbol: str | None = None,
        threshold=3,
        open_sec=30,
    ):
        now = time.time()
        with self.circuit_lock:
            if symbol and symbol not in self.history_circuit_states:
                self.history_circuit_states[symbol] = _make_circuit_state()
            elif not symbol and service not in self.circuit_states:
                self.circuit_states[service] = _make_circuit_state()
            target: CircuitState | None = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if not target:
                return
            if success:
                target["status"] = "CLOSED"
                target["timeout_streak"] = 0
                target["open_until"] = 0.0
                target["probing"] = False
                target["probing_since"] = 0.0
            else:
                if target.get("status") == "HALF_OPEN":
                    target["status"] = "OPEN"
                    target["open_until"] = now + open_sec
                    target["timeout_streak"] = 0
                    target["probing"] = False
                    target["probing_since"] = 0.0
                else:
                    target["timeout_streak"] = (target.get("timeout_streak") or 0) + 1
                    if target["timeout_streak"] >= threshold:
                        target["status"] = "OPEN"
                        target["open_until"] = now + open_sec
                        target["timeout_streak"] = 0
                        target["probing"] = False
                        target["probing_since"] = 0.0

    def is_circuit_open(self, service: str, symbol: str | None = None) -> bool:
        now = time.time()
        with self.circuit_lock:
            target: CircuitState | None = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if not target:
                return False
            status = target.get("status")
            if status == "OPEN":
                open_until = finite_float(target.get("open_until"), 0.0) or 0.0
                if now >= open_until:
                    target["status"] = "HALF_OPEN"
                    target["probing"] = False
                    target["probing_since"] = 0.0
                    return False  # Allow probe; route layer claims probing
                return True
            elif status == "HALF_OPEN":
                return False
            return False

    def peek_circuit_open(self, service: str, symbol: str | None = None) -> bool:
        """Read-only check of whether a circuit breaker is open without state transition."""
        now = time.time()
        with self.circuit_lock:
            target: CircuitState | None = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if not target:
                return False
            if target.get("status") == "OPEN":
                open_until = finite_float(target.get("open_until"), 0.0) or 0.0
                return now < open_until
            return False

    _CIRCUIT_PROBE_TTL_SEC: float = 300.0

    def try_claim_circuit_probe(
        self,
        service: str,
        symbol: str | None = None,
        probe_ttl_sec: float = _CIRCUIT_PROBE_TTL_SEC,
    ) -> bool:
        """Atomically claim the HALF_OPEN probe slot.

        Returns True if the caller owns the probe (and should execute the
        backing call), False if another thread already claimed a live probe.
        Reclaims a probe whose TTL expired without completion so crashed or
        stalled probes cannot permanently wedge HALF_OPEN.
        """
        now_mono = time.monotonic()
        with self.circuit_lock:
            target: CircuitState | None = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if not target or target.get("status") != "HALF_OPEN":
                return False
            if target.get("probing"):
                # A missing or non-finite timestamp means the claim cannot be
                # proven stale, so treat it as live instead of silently starting
                # a second concurrent probe.
                since = finite_float(target.get("probing_since"), None)
                if since is None or (now_mono - since) < probe_ttl_sec:
                    return False
            target["probing"] = True
            target["probing_since"] = now_mono
            return True

    def enter_circuit_call(
        self, service: str, symbol: str | None = None
    ) -> tuple[bool, bool, str]:
        """Evaluate circuit state and claim a HALF_OPEN probe if required.

        Returns ``(allowed, probe_claimed, deny_reason)`` where ``deny_reason``
        is ``""`` when allowed, ``"open"`` when OPEN, or ``"busy"`` when a
        HALF_OPEN probe is actively in flight. Re-checks post-claim state so a
        concurrent probe resolving ``HALF_OPEN -> CLOSED`` does not trigger a
        false busy rejection.
        """
        with self.circuit_lock:
            if symbol is None:
                is_open = self.is_circuit_open(service)
            else:
                is_open = self.is_circuit_open(service, symbol=symbol)
            if is_open:
                return False, False, "open"
            circuit_state = (
                self.get_circuit_state(service)
                if symbol is None
                else self.get_circuit_state(service, symbol=symbol)
            )
            status = circuit_state.get("status")
            if status == "OPEN":
                return False, False, "open"
            if status == "HALF_OPEN":
                claimed = (
                    self.try_claim_circuit_probe(service)
                    if symbol is None
                    else self.try_claim_circuit_probe(service, symbol=symbol)
                )
                if claimed:
                    return True, True, ""
                post_state = (
                    self.get_circuit_state(service)
                    if symbol is None
                    else self.get_circuit_state(service, symbol=symbol)
                )
                post_status = post_state.get("status")
                if post_status == "CLOSED":
                    return True, False, ""
                if post_status == "OPEN":
                    return False, False, "open"
                return False, False, "busy"
            return True, False, ""

    def release_circuit_probe(
        self,
        service: str,
        symbol: str | None = None,
        probe_since: float | None = None,
    ) -> None:
        """Release a HALF_OPEN probe claim without changing circuit health.

        Some failures, such as an upstream 429, are intentionally tracked by a
        separate rate limiter rather than by the availability circuit. Those
        paths still must clear the single-probe claim so the circuit cannot be
        left permanently marked as probing.

        When *probe_since* is provided, only clear the claim if it still matches
        the active ``probing_since`` token so a timed-out stale probe cannot
        clear a newer probe's ownership.
        """
        with self.circuit_lock:
            target: CircuitState | None = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if target and target.get("probing"):
                if probe_since is not None and target.get("probing_since") != probe_since:
                    return
                target["probing"] = False
                target["probing_since"] = 0.0

    # --- Syncing ---

    def set_syncing(self, value: bool):
        with self.is_syncing_lock:
            self.is_syncing = value

    # --- Market Status ---

    def update_market_status(self, market: str, status: str | None):
        with self.market_status_lock:
            self.market_status_cache[market] = status

    def get_market_status(self, market: str) -> str | None:
        with self.market_status_lock:
            value = self.market_status_cache.get(market)
            return None if value is None else value

    # --- yfinance Rate Limiting ---

    def is_yf_rate_limited(self) -> bool:
        """Return True while either recorded yfinance cooldown is active.

        ``mark_yf_429`` raises both ``is_yfinance_rate_limited`` and the
        session-manager window.  The local window is the fallback when the
        session-manager update fails (the failure is logged and swallowed), and
        gating it on the flag keeps a stale future timestamp from a previous
        run from blocking requests on its own (M29).
        """
        with self.yfinance_lock:
            now = time.time()
            local_limited = (
                self.is_yfinance_rate_limited and now < self.yfinance_rate_limit_until
            )
            limited = local_limited or yf_session_manager.is_rate_limited("yfinance")
            if not limited:
                self.is_yfinance_rate_limited = False
            return limited

    def get_yf_rate_limit_until(self) -> float:
        """Return the effective UNIX expiry of the yfinance cooldown.

        The session manager and the local 429 window can each outlive the
        other (a failed session-manager update, or a scraper block propagated
        only into the session manager), so status endpoints must report their
        union instead of one half.
        """
        with self.yfinance_lock:
            local_until = (
                self.yfinance_rate_limit_until if self.is_yfinance_rate_limited else 0.0
            )
            return max(
                local_until,
                yf_session_manager.get_rate_limit_until("yfinance"),
            )

    # --- Previous-close cache (realtime producer hot path) ---

    def update_previous_close_cache(self, symbol: str, prev_close: float | str | None) -> None:
        """Record the yfinance-derived previous close for *symbol*.

        Called from the payload-build/sync path (and realtime producer updates)
        so realtime delta producers can resolve the previous close without
        taking ``sse_data_lock`` on every TradingView WS message.
        """
        if not symbol:
            return
        variants = self._symbol_cache_variants(symbol)
        with self.previous_close_cache_lock:
            if prev_close is not None and not is_bool_like(prev_close):
                try:
                    num = float(prev_close)
                    if math.isfinite(num) and num > 0:
                        for variant in variants:
                            self.previous_close_cache[variant] = num
                        return
                except (ValueError, TypeError):
                    pass
            for variant in variants:
                self.previous_close_cache.pop(variant, None)

    def get_previous_close_cached(self, symbol: str) -> float | None:
        """Return the cached previous close for *symbol* without sse_data_lock."""
        if not symbol:
            return None
        variants = self._symbol_cache_variants(symbol)
        with self.previous_close_cache_lock:
            for variant in variants:
                value = self.previous_close_cache.get(variant)
                if value is not None:
                    return value
        return None

    @staticmethod
    def _coerce_finite_float(raw: Any, default: float = 0.0) -> float:
        result = finite_float(raw, default)
        return default if result is None else result

    def get_usdjpy_rate_snapshot(self) -> tuple[float, float]:
        """Return ``(last_usdjpy_rate, last_usdjpy_rate_ts)`` atomically under lock."""
        with self.usdjpy_rate_lock:
            return (
                self._coerce_finite_float(self.last_usdjpy_rate, 0.0),
                self._coerce_finite_float(self.last_usdjpy_rate_ts, 0.0),
            )

    def update_usdjpy_rate(
        self, rate: float, ts: float | None = None
    ) -> tuple[float, float]:
        """Atomically update ``last_usdjpy_rate`` and optional ``last_usdjpy_rate_ts``."""
        safe_rate = self._coerce_finite_float(rate, 0.0)
        safe_ts: float | None = None
        if ts is not None:
            parsed_ts = self._coerce_finite_float(ts, 0.0)
            safe_ts = max(parsed_ts, 0.0)
        with self.usdjpy_rate_lock:
            if safe_rate <= 0.0:
                existing_rate = self._coerce_finite_float(self.last_usdjpy_rate, 0.0)
                if existing_rate <= 0.0:
                    self.last_usdjpy_rate = DEFAULT_USDJPY_RATE
                    self.last_usdjpy_rate_ts = 0.0
                return self.last_usdjpy_rate, self.last_usdjpy_rate_ts
            self.last_usdjpy_rate = safe_rate
            if safe_ts is not None:
                self.last_usdjpy_rate_ts = safe_ts
            return self.last_usdjpy_rate, self.last_usdjpy_rate_ts

    @staticmethod
    def _symbol_cache_variants(symbol: str) -> tuple[str, ...]:
        if symbol.endswith(".T") and len(symbol) > 2:
            return (symbol, symbol[:-2])
        return (symbol, f"{symbol}.T")

    def clear_previous_close_cache(self, symbol: str | None = None) -> None:
        """Drop one symbol or the whole previous-close cache."""
        with self.previous_close_cache_lock:
            if symbol is None:
                self.previous_close_cache.clear()
            else:
                for variant in self._symbol_cache_variants(symbol):
                    self.previous_close_cache.pop(variant, None)

    def update_volume_cache(self, symbol: str, volume: float | str | None) -> None:
        """Update the in-memory volume cache for *symbol*."""
        if not symbol:
            return
        variants = self._symbol_cache_variants(symbol)
        with self.yfinance_volume_cache_lock:
            if volume is not None and not is_bool_like(volume):
                try:
                    parsed = float(str(volume).replace(",", "").strip())
                    if math.isfinite(parsed) and parsed > 0:
                        num = int(parsed)
                        if num > 0:
                            for variant in variants:
                                self.yfinance_volume_cache[variant] = num
                            return
                except (ValueError, TypeError, OverflowError):
                    pass
            for variant in variants:
                self.yfinance_volume_cache.pop(variant, None)

    def get_volume_cached(self, symbol: str) -> int | None:
        """Return the cached volume for *symbol* without sse_data_lock."""
        if not symbol:
            return None
        with self.yfinance_volume_cache_lock:
            value = self.yfinance_volume_cache.get(symbol)
            if value is not None:
                return value
            if symbol.endswith(".T"):
                return self.yfinance_volume_cache.get(symbol[:-2])
            return self.yfinance_volume_cache.get(f"{symbol}.T")

    def clear_volume_cache(self, symbol: str | None = None) -> None:
        """Drop one symbol or the whole volume cache."""
        with self.yfinance_volume_cache_lock:
            if symbol is None:
                self.yfinance_volume_cache.clear()
            else:
                for variant in self._symbol_cache_variants(symbol):
                    self.yfinance_volume_cache.pop(variant, None)

    def mark_yf_429(self, retry_after: float | None = None) -> float:
        """
        Record a yfinance 429/401/402/439 with graduated exponential backoff.

        Backoff progression (default 30s initial, 2x multiplier):
          streak 1 = 30s, streak 2 = 60s, ..., streak 5 = 480s (capped at 600s)

        If the server supplied a ``Retry-After`` hint (via ``retry_after``), the
        effective backoff is the larger of the graduated value and that hint, so we
        never back off for *less* than Yahoo asks even on the first strike.
        """
        with self.yfinance_lock:
            self.yfinance_429_streak = min(self.yfinance_429_streak + 1, 5)
            self.is_yfinance_rate_limited = True
            graduated = min(
                self.yfinance_backoff_initial
                * (self.yfinance_429_backoff_multiplier ** (self.yfinance_429_streak - 1)),
                self.yfinance_max_backoff_sec,
            )
            retry_after_val = self._coerce_finite_float(retry_after, 0.0)
            if retry_after_val > 0:
                backoff = min(max(graduated, retry_after_val), self.yfinance_max_backoff_sec)
            else:
                backoff = graduated
            # Monotonic window: a later report of the same block event with a
            # shorter backoff must never shorten an already-recorded exclusion.
            new_until = time.time() + backoff
            self.yfinance_rate_limit_until = max(self.yfinance_rate_limit_until, new_until)
            self.yfinance_adaptive_interval_sec = self.yfinance_min_interval_sec * min(
                YFINANCE_ADAPTIVE_INTERVAL_FACTOR,
                1.0 + self.yfinance_429_streak * 0.5,
            )
            try:
                yf_session_manager.mark_rate_limited("yfinance", int(backoff))
            except Exception as e:
                logger.debug("Failed to call yf_session_manager.mark_rate_limited: %s", e)
            return backoff

    # --- Web scraper global block detection ---

    def is_scraper_blocked(self) -> bool:
        """True while the web-scraper global block cooldown is active."""
        with self.scraper_block_lock:
            return self.scraper_block_until > time.time()

    def scraper_block_clears_in(self) -> float:
        """Seconds until the web-scraper block cooldown clears (0 when clear)."""
        with self.scraper_block_lock:
            return max(0.0, self.scraper_block_until - time.time())

    def mark_scraper_blocked(
        self, retry_after: float | None = None, propagate_to_yfinance: bool = False
    ) -> float:
        """
        Record a web-scraper 401/402/403/429/439 with graduated exponential backoff.

        Backoff progression (default 60s initial, 2x multiplier):
          streak 1 = 60s, streak 2 = 120s, ..., streak 6 = 1920s (capped at 600s)

        The streak auto-decays once the previous cooldown has fully elapsed, so a
        single transient block does not permanently inflate future backoffs.
        A server-supplied ``Retry-After`` hint is honored as a floor.

        ``propagate_to_yfinance`` (default False) additionally pauses the
        yfinance session pool. This is only enabled for Yahoo-hosted scrapers:
        Kabutan / SBI / Minkabu blocks are site-local bot protection and must
        not rotate/destroy the yfinance session pool (UA rotation + epoch bump
        + crumb reset on every third-party 403 would destabilize yfinance).
        """
        with self.scraper_block_lock:
            now_ts = time.time()
            was_blocked = self.scraper_block_until > now_ts
            if not was_blocked:
                self.scraper_block_streak = 0
            self.scraper_block_streak = min(self.scraper_block_streak + 1, 6)
            graduated = min(
                self.scraper_backoff_initial
                * (self.scraper_backoff_multiplier ** (self.scraper_block_streak - 1)),
                self.scraper_max_backoff_sec,
            )
            retry_after_val = self._coerce_finite_float(retry_after, 0.0)
            if retry_after_val > 0:
                backoff = min(max(graduated, retry_after_val), self.scraper_max_backoff_sec)
            else:
                backoff = graduated
            # Monotonic window: a later report of the same block event with a
            # shorter backoff (e.g. a 60s default racing an earlier 300s
            # Retry-After) must never shorten the cooldown and resume hammering
            # the blocked upstream. Mirrors ``mark_yf_429``.
            self.scraper_block_until = max(self.scraper_block_until, now_ts + backoff)
            if not was_blocked:
                logger.warning(
                    "Web scraper global block detected; pausing all scrapers for %.0fs "
                    "(streak=%d, propagate_to_yfinance=%s)",
                    backoff,
                    self.scraper_block_streak,
                    propagate_to_yfinance,
                )

            # Cross-link into yfinance pacing ONLY for Yahoo-hosted scrapers:
            # they share Yahoo's rate-limit enforcement (and the IP) with
            # yfinance, so a Yahoo block is strong evidence yfinance should
            # back off too. The scraper worker loops additionally check
            # ``is_yf_rate_limited`` so the reverse direction (yfinance 429 ->
            # scraper pause) is covered.
            if propagate_to_yfinance:
                try:
                    yf_session_manager.mark_rate_limited("yfinance", int(backoff))
                except Exception as exc:
                    logger.debug("Failed to propagate scraper block to yfinance pacing: %s", exc)
            return backoff
