"""
market_state.py - Market data state management.

Extracted from app_state.py to reduce module complexity.
Manages stock data, market status, yfinance rate limiting, and circuit breakers.
"""

import os
import threading
import time
from typing import Any, Dict, List, Optional, TypedDict

from cachetools import TTLCache

from constants import (
    YFINANCE_MIN_INTERVAL,
    YFINANCE_BACKOFF_MULTIPLIER,
    YFINANCE_BACKOFF_MAX,
    YFINANCE_JITTER_FACTOR,
    YFINANCE_BACKOFF_INITIAL,
    YFINANCE_SHORT_CACHE_TTL,
    YFINANCE_ADAPTIVE_INTERVAL_FACTOR,
)
from session_manager import yf_session_manager

import logging

logger = logging.getLogger("backend")


class CircuitStateDict(TypedDict):
    """Alias for backward compatibility with existing tests."""
    status: str
    timeout_streak: int
    open_until: float


class CircuitState(TypedDict):
    """State of a circuit breaker for an external service."""
    status: str  # CLOSED | OPEN | HALF_OPEN
    timeout_streak: int
    open_until: float


def _make_circuit_state(
    status: str = "CLOSED",
    timeout_streak: int = 0,
    open_until: float = 0.0,
) -> "CircuitState":
    """Factory for creating a default CircuitState dict.

    M-9: TypedDict is a type-checking construct only; at runtime it is a plain
    dict. Using ``CircuitState(status=..., ...)`` looks like class instantiation
    but actually just calls ``dict()``. This factory makes the intent explicit
    and avoids confusion about runtime isinstance checks (which always fail for
    TypedDict).
    """
    return {"status": status, "timeout_streak": timeout_streak, "open_until": open_until}


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

        self.last_modified_ns = 0
        self.current_stocks_cache: Dict[str, List[Any]] = {"us": [], "jp": [], "idx": []}
        self.target_stocks_cache: Dict[str, List[Any]] = {"us": [], "jp": [], "idx": []}
        self.current_indices_cache: dict[str, Any] = {}
        self.target_indices_cache: dict[str, Any] = {}

        self.is_syncing = False
        self.is_syncing_lock = threading.RLock()
        self.sync_scheduled = False
        self.sync_schedule_lock = threading.RLock()
        self.sync_pending = False

        self.market_status_cache: Dict[str, Optional[str]] = {"us": None, "jp": None, "idx": None}
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
        self.yfinance_history_semaphore = threading.Semaphore(4)
        self.yfinance_short_cache_lock = threading.RLock()
        self.yfinance_short_cache: TTLCache[str, Any] = TTLCache(
            maxsize=512,
            ttl=YFINANCE_SHORT_CACHE_TTL,
        )

        # Circuit breakers
        self.circuit_lock = threading.RLock()
        self.history_circuit_lock = self.circuit_lock
        self.history_circuit_state: Dict[str, CircuitState] = {}
        self.circuit_states: Dict[str, CircuitState] = {
            "mistral": _make_circuit_state(),
            "langsearch": _make_circuit_state(),
        }
        self.history_circuit_states: Dict[str, CircuitState] = self.history_circuit_state

    # --- Circuit Breaker ---

    def get_circuit_state(self, service: str, symbol: Optional[str] = None) -> CircuitState:
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
        symbol: Optional[str] = None,
        threshold=3,
        open_sec=30,
    ):
        now = time.time()
        with self.circuit_lock:
            if symbol and symbol not in self.history_circuit_states:
                self.history_circuit_states[symbol] = _make_circuit_state()
            target: Optional[CircuitState] = (
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
            else:
                if target.get("status") == "HALF_OPEN":
                    target["status"] = "OPEN"
                    target["open_until"] = now + open_sec
                    target["timeout_streak"] = 0
                else:
                    target["timeout_streak"] = (target.get("timeout_streak") or 0) + 1
                    if target["timeout_streak"] >= threshold:
                        target["status"] = "OPEN"
                        target["open_until"] = now + open_sec
                        target["timeout_streak"] = 0

    def is_circuit_open(self, service: str, symbol: Optional[str] = None) -> bool:
        now = time.time()
        with self.circuit_lock:
            target: Optional[CircuitState] = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if not target:
                return False
            if target.get("status") == "OPEN":
                if now >= (target.get("open_until") or 0.0):
                    target["status"] = "HALF_OPEN"
                    return False
                return True
            return False

    # --- Syncing ---

    def set_syncing(self, value: bool):
        with self.is_syncing_lock:
            self.is_syncing = value

    # --- Market Status ---

    def update_market_status(self, market: str, status: Optional[str]):
        with self.market_status_lock:
            self.market_status_cache[market] = status

    def get_market_status(self, market: str) -> Optional[str]:
        with self.market_status_lock:
            value = self.market_status_cache.get(market)
            return None if value is None else value

    # --- yfinance Rate Limiting ---

    def is_yf_rate_limited(self) -> bool:
        with self.yfinance_lock:
            return yf_session_manager.is_rate_limited("yfinance")

    def mark_yf_429(self) -> float:
        """
        Record a yfinance 429 with graduated exponential backoff.

        Backoff progression (default 30s initial, 2x multiplier):
          streak 1 = 30s, streak 2 = 60s, ..., streak 5 = 480s (capped at 600s)
        """
        with self.yfinance_lock:
            self.yfinance_429_streak = min(self.yfinance_429_streak + 1, 5)
            self.is_yfinance_rate_limited = True
            backoff = min(
                self.yfinance_backoff_initial * (self.yfinance_429_backoff_multiplier ** (self.yfinance_429_streak - 1)),
                self.yfinance_max_backoff_sec,
            )
            self.yfinance_rate_limit_until = time.time() + backoff
            self.yfinance_adaptive_interval_sec = self.yfinance_min_interval_sec * min(
                YFINANCE_ADAPTIVE_INTERVAL_FACTOR,
                1.0 + self.yfinance_429_streak * 0.5,
            )
            try:
                yf_session_manager.mark_rate_limited("yfinance", int(backoff))
            except Exception as e:
                logger.debug("Failed to call yf_session_manager.mark_rate_limited: %s", e)
            return backoff
