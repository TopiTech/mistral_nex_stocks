"""
app_state.py - Application state management facade.

Provides a unified interface to the application state, delegating to
specialized modules for each domain:
- session_manager: YFinance session management
- market_state: Stock data, circuit breakers, yfinance rate limiting
- ai_state: Mistral AI, LangSearch, chat history
- execution_state: Thread pools, background tasks
- shutdown_manager: Shutdown token lifecycle
- messaging: SSE listener management

Importing from this module will continue to work without changes
since all classes are re-exported for backward compatibility.
"""

import logging
import threading
from typing import Any, Optional

# Re-export all components from extracted modules for backward compatibility
from session_manager import yf_session_manager
from market_state import MarketDataState
from ai_state import AIState
from execution_state import ExecutionState
from shutdown_manager import ShutdownTokenManager
from messaging import MessageAnnouncer

# Re-export keyring error
try:
    import keyring.errors as _keyring_errors
    KeyringError: type[Exception] = _keyring_errors.KeyringError
except ImportError:
    class _KeyringErrorFallback(Exception):
        """Fallback if keyring is not installed."""
    KeyringError = _KeyringErrorFallback

logger = logging.getLogger("backend")

# Re-export logging filters and formatters
IMPORTANT_INFO_PATTERNS = (
    "REQ start", "REQ end",
    "api_news start", "api_analyze input",
    "News bundle refresh",
    "LangSearch used:", "DDGS fallback used:",
    "DDGS results:",
    "News trends async refresh completed",
)


class BackendLogFilter(logging.Filter):
    """Filters log messages to show only important INFO patterns."""

    def __init__(self, log_level=logging.INFO):
        super().__init__()
        self.log_level = log_level

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno < logging.INFO:
            return self.log_level <= record.levelno
        msg = record.getMessage()
        return any(pattern in msg for pattern in IMPORTANT_INFO_PATTERNS)


class PollingFilter(logging.Filter):
    """Filters out verbose polling log messages."""

    def filter(self, record):
        msg = record.getMessage()
        if " 200 -" in msg and any(
            x in msg for x in ["GET /api/indices", "GET /api/health", "GET /api/stocks"]
        ):
            return False
        return True


class AppState:
    """Unified application state facade.

    Delegates to specialized sub-objects for each domain.
    This class provides backward-compatible property access to all state.
    """

    execution: ExecutionState
    market: MarketDataState
    ai: AIState
    cache: Any  # CacheState - imported lazily
    shutdown_manager: ShutdownTokenManager
    stock_provider: Any
    stock_disk_cache: Any
    payload_disk_cache: Any
    sse_announcer: MessageAnnouncer
    history_fetch_inflight: set[str]
    history_fetch_lock: threading.Lock

    def __init__(self):
        self.execution = ExecutionState()
        self.market = MarketDataState()
        self.ai = AIState()
        from utils.caching import global_cache
        self.cache = global_cache
        self.shutdown_manager = ShutdownTokenManager()
        self.history_fetch_inflight = set()
        self.history_fetch_lock = threading.Lock()

        from services.stock_provider import YFinanceProvider
        self.stock_provider = YFinanceProvider()

        from constants import BASE_DIR, STOCK_HISTORY_CACHE_MAXSIZE, STOCK_HISTORY_DISK_CACHE_TTL
        from utils.disk_cache import StockDiskCache
        self.stock_disk_cache = StockDiskCache(
            cache_dir=BASE_DIR / ".cache" / "stock_history",
            max_entries=STOCK_HISTORY_CACHE_MAXSIZE,
            default_ttl=STOCK_HISTORY_DISK_CACHE_TTL,
        )
        self.payload_disk_cache = StockDiskCache(
            cache_dir=BASE_DIR / ".cache" / "stock_payloads",
            max_entries=256,
            default_ttl=3600,
        )

        self.sse_announcer = MessageAnnouncer()
        self._extension_origins_cache: set[str] = set()
        self._extension_origins_cache_ts = 0.0
        self._extension_origins_cache_lock = threading.Lock()
        self._extension_manifest_status = {"ok": True, "error": ""}
        self.EXTENSION_MANIFEST_ERROR_LOGGED = False
        self._EXTENSION_ORIGINS_CACHE_TTL_SEC = 30.0

    # --- Market Status ---

    def update_market_status(self, market: str, status: Optional[str]):
        return self.market.update_market_status(market, status)

    def get_market_status(self, market: str) -> Optional[str]:
        return self.market.get_market_status(market)

    # --- Circuit Breaker ---

    def is_circuit_open(self, service: str, symbol: Optional[str] = None) -> bool:
        return self.market.is_circuit_open(service, symbol)

    def report_circuit_result(self, service: str, success: bool, symbol: Optional[str] = None,
                               threshold=3, open_sec=30):
        return self.market.report_circuit_result(service, success, symbol, threshold, open_sec)

    def get_circuit_state(self, service: str, symbol: Optional[str] = None):
        return self.market.get_circuit_state(service, symbol)

    # --- Syncing ---

    def set_syncing(self, value: bool):
        return self.market.set_syncing(value)

    # --- yfinance ---

    @property
    def yfinance_short_cache(self):
        return self.market.yfinance_short_cache

    @property
    def yfinance_short_cache_lock(self):
        return self.market.yfinance_short_cache_lock

    def is_yf_rate_limited(self) -> bool:
        return self.market.is_yf_rate_limited()

    def mark_yf_429(self) -> float:
        return self.market.mark_yf_429()

    # --- AI ---

    def add_chat_history(self, key: str, message: Any):
        return self.ai.add_chat_history(key, message)

    def mark_mistral_429(self, retry_after_sec=None) -> float:
        return self.ai.mark_mistral_429(retry_after_sec)

    def reset_mistral_streak(self):
        return self.ai.reset_mistral_streak()

    def get_or_create_mistral_client(self, api_key: str):
        return self.ai.get_or_create_mistral_client(api_key)

    # --- Shutdown ---

    def shutdown_executors(self):
        """Clean up background resources with deadlock prevention."""
        self.execution.shutdown()

        try:
            yf_session_manager.close_all()
        except Exception as e:
            logger.debug("Error closing YFinance sessions: %s", e)

        try:
            lock_acquired = self.ai.mistral_clients_lock.acquire(timeout=2.0)
            if lock_acquired:
                try:
                    for client in self.ai.mistral_clients.values():
                        if hasattr(client, "close"):
                            try:
                                client.close()
                            except Exception:
                                pass
                    self.ai.mistral_clients.clear()
                finally:
                    self.ai.mistral_clients_lock.release()
            else:
                logger.warning("Timeout acquiring mistral_clients_lock during shutdown")
        except Exception as e:
            logger.debug("Error closing Mistral clients: %s", e)

    def get_or_create_shutdown_token(self) -> str:
        return self.shutdown_manager.get_or_create_shutdown_token()

    def consume_shutdown_token(self, token: str) -> bool:
        return self.shutdown_manager.consume_shutdown_token(token)

    def rotate_shutdown_token(self):
        self.shutdown_manager.rotate_shutdown_token()

    # --- Cache Stats ---

    def record_hit(self):
        self.cache.record_hit()

    def record_miss(self):
        self.cache.record_miss()

    def get_stats(self):
        return self.cache.get_stats()

    def reset_stats(self):
        self.cache.reset_stats()


# Singleton instance
app_state = AppState()
