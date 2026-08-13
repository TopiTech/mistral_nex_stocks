"""
market_state.py - Market data state management.

Extracted from app_state.py to reduce module complexity.
Manages stock data, market status, yfinance rate limiting, and circuit breakers.
"""

import logging
import math
import os
import threading
import time
from typing import Any

from cachetools import TTLCache

from constants import (
    SCRAPER_BACKOFF_INITIAL,
    SCRAPER_BACKOFF_MAX,
    SCRAPER_BACKOFF_MULTIPLIER,
    YFINANCE_ADAPTIVE_INTERVAL_FACTOR,
    YFINANCE_BACKOFF_INITIAL,
    YFINANCE_BACKOFF_MAX,
    YFINANCE_BACKOFF_MULTIPLIER,
    YFINANCE_JITTER_FACTOR,
    YFINANCE_MAX_CONCURRENT_REQUESTS,
    YFINANCE_MIN_INTERVAL,
    YFINANCE_SHORT_CACHE_TTL,
)
from session_manager import yf_session_manager

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
    ):
        self.status = status
        self.timeout_streak = timeout_streak
        self.open_until = open_until
        self.probing = probing

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

        default_usdjpy = 150.00
        try:
            default_usdjpy = float(os.environ.get("MNS_DEFAULT_USDJPY", "150.00"))
        except (ValueError, TypeError):
            pass
        self.last_usdjpy_rate = default_usdjpy
        # R5: record the timestamp of the last USDJPY update so consumers can
        # warn (or refuse) when the cached rate is stale. A backend that
        # boots, runs once, then idles for a day will leave this older than
        # 24h and surface a "FX rate may be stale" warning instead of
        # silently computing share counts with a day-old rate.
        self.last_usdjpy_rate_ts: float = 0.0

        self.last_modified_ns = 0
        # Process-internal monotonic version counter for user_stocks.json.
        # Incremented inside user_stocks_lock on every successful save; readers
        # compare it to last_loaded_rev so stale reads are detected reliably
        # without depending solely on filesystem mtime.
        # Initialise last_loaded_rev to -1 so the first non-force load does not
        # skip loading (both initial 0 would match and cause an early return).
        self.user_stocks_rev = 0
        self.last_loaded_rev = -1
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
        # Increased from 2 to 4 to allow more concurrent history fetches.
        # This benefits the /api/stock-history endpoint which serves user-triggered
        # chart fetches that can arrive simultaneously for different symbols.
        # The semaphore timeout (6s) still protects against thundering herd.
        # Use the same concurrency limit as the session manager to stay under
        # Yahoo's anonymous concurrency ceiling.
        self.yfinance_history_semaphore = threading.Semaphore(YFINANCE_MAX_CONCURRENT_REQUESTS)
        self.yfinance_short_cache_lock = threading.RLock()
        self.yfinance_short_cache: TTLCache[str, Any] = TTLCache(
            maxsize=512,
            ttl=YFINANCE_SHORT_CACHE_TTL,
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
        # build/sync path and read without sse_data_lock.
        self.previous_close_cache: dict[str, float] = {}
        self.previous_close_cache_lock = threading.RLock()

        # Circuit breakers
        self.circuit_lock = threading.RLock()
        self.history_circuit_lock = self.circuit_lock
        self.history_circuit_state: dict[str, CircuitState] = {}
        self.circuit_states: dict[str, CircuitState] = {
            "mistral": _make_circuit_state(),
            "langsearch": _make_circuit_state(),
        }
        self.history_circuit_states: dict[str, CircuitState] = self.history_circuit_state

        # Track consecutive yfinance fetch failures per user-added symbol.
        # Symbols that exceed INVALID_SYMBOL_REMOVAL_THRESHOLD consecutive
        # failures are automatically removed from the user stock list.
        self.invalid_symbol_streak: dict[str, int] = {}
        self.invalid_symbol_lock = threading.RLock()
        self.first_sync_attempted: bool = False
        self.first_sync_completed_at: float = 0.0

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

    # --- Circuit Breaker ---

    def get_circuit_state(self, service: str, symbol: str | None = None) -> CircuitState:
        with self.circuit_lock:
            if symbol:
                if symbol not in self.history_circuit_states:
                    self.history_circuit_states[symbol] = _make_circuit_state()
                return self.history_circuit_states[symbol]
            return self.circuit_states.get(service, _make_circuit_state())

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
            else:
                if target.get("status") == "HALF_OPEN":
                    target["status"] = "OPEN"
                    target["open_until"] = now + open_sec
                    target["timeout_streak"] = 0
                    target["probing"] = False
                else:
                    target["timeout_streak"] = (target.get("timeout_streak") or 0) + 1
                    if target["timeout_streak"] >= threshold:
                        target["status"] = "OPEN"
                        target["open_until"] = now + open_sec
                        target["timeout_streak"] = 0
                        target["probing"] = False

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
                if now >= (target.get("open_until") or 0.0):
                    target["status"] = "HALF_OPEN"
                    return False  # Allow probe; route layer claims probing
                return True
            elif status == "HALF_OPEN":
                return False
            return False

    def try_claim_circuit_probe(self, service: str, symbol: str | None = None) -> bool:
        """Atomically claim the HALF_OPEN probe slot.

        Returns True if the caller owns the probe (and should execute the
        backing call), False if another thread already claimed it.
        """
        with self.circuit_lock:
            target: CircuitState | None = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if not target or target.get("status") != "HALF_OPEN":
                return False
            if target.get("probing"):
                return False
            target["probing"] = True
            return True

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
        with self.yfinance_lock:
            return yf_session_manager.is_rate_limited("yfinance")

    # --- Previous-close cache (realtime producer hot path) ---

    def update_previous_close_cache(self, symbol: str, prev_close: float | None) -> None:
        """Record the yfinance-derived previous close for *symbol*.

        Called from the payload-build/sync path (and realtime producer updates)
        so realtime delta producers can resolve the previous close without
        taking ``sse_data_lock`` on every TradingView WS message.
        """
        if not symbol:
            return
        with self.previous_close_cache_lock:
            if prev_close is not None and math.isfinite(prev_close) and prev_close > 0:
                self.previous_close_cache[symbol] = float(prev_close)
            else:
                self.previous_close_cache.pop(symbol, None)

    def get_previous_close_cached(self, symbol: str) -> float | None:
        """Return the cached previous close for *symbol* without sse_data_lock."""
        if not symbol:
            return None
        with self.previous_close_cache_lock:
            value = self.previous_close_cache.get(symbol)
        return value if value is not None else None

    def clear_previous_close_cache(self, symbol: str | None = None) -> None:
        """Drop one symbol or the whole previous-close cache."""
        with self.previous_close_cache_lock:
            if symbol is None:
                self.previous_close_cache.clear()
            else:
                self.previous_close_cache.pop(symbol, None)
                self.previous_close_cache.pop(f"{symbol}.T", None)

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
            if retry_after and retry_after > 0:
                backoff = min(max(graduated, retry_after), self.yfinance_max_backoff_sec)
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
            if retry_after and retry_after > 0:
                backoff = min(max(graduated, retry_after), self.scraper_max_backoff_sec)
            else:
                backoff = graduated
            self.scraper_block_until = now_ts + backoff
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
