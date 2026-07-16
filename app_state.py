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
from typing import Any

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
    info_fetch_inflight: set[str]
    heatmap_fetch_inflight: set[str]
    heatmap_fetch_lock: threading.Lock

    def __init__(self):
        self.execution = ExecutionState()
        self.market = MarketDataState()
        self.ai = AIState()
        from utils.caching import global_cache
        self.cache = global_cache
        self.shutdown_manager = ShutdownTokenManager()
        self.bootstrap_ready = threading.Event()
        self.history_fetch_inflight = set()
        self.history_fetch_lock = threading.Lock()
        self.info_fetch_inflight = set()
        self.heatmap_fetch_inflight = set()
        self.heatmap_fetch_lock = threading.Lock()

        self.sse_announcer = MessageAnnouncer()
        self._extension_origins_cache: set[str] = set()
        self._extension_origins_cache_ts = 0.0
        self._extension_origins_cache_lock = threading.Lock()
        self._extension_manifest_status = {"ok": True, "error": ""}
        self.EXTENSION_MANIFEST_ERROR_LOGGED = False
        self._EXTENSION_ORIGINS_CACHE_TTL_SEC = 30.0

        # stock_provider, disk caches: initialized eagerly in __init__ without
        # file-system side effects (those are deferred to initialize_yfinance_cache).
        from services.stock_provider import YFinanceProvider
        self.stock_provider = YFinanceProvider(self.market)

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

    def initialize_yfinance_cache(self) -> None:
        """Configure yfinance timezone cache isolation.

        Extracted from __init__ to avoid file-system side effects at import
        time (which interfere with test isolation). Call once explicitly from
        app startup (create_app) rather than at construction time.

        Mitigates sqlite3 locking issues on tkr-tz.db in parallel environments:
        - Clears the global tz cache file if it exists (prevents corruption-based failures)
        - Sets a process-specific temp directory to avoid cross-process conflicts
        """
        try:
            import yfinance as yf
            import tempfile
            import platformdirs
            import os

            # Clear legacy global cache file if it exists to prevent corruption-based failures
            global_cache_dir = os.path.join(platformdirs.user_cache_dir(), "py-yfinance")
            global_tz_db = os.path.join(global_cache_dir, "tkr-tz.db")
            if os.path.exists(global_tz_db):
                try:
                    os.remove(global_tz_db)
                    logger.info("Cleared legacy global yfinance cache at %s", global_tz_db)
                except OSError as exc:
                    logger.debug("Failed to remove legacy yfinance cache: %s", exc)

            custom_cache_dir = tempfile.mkdtemp(prefix="py-yfinance-mns-")
            yf.set_tz_cache_location(custom_cache_dir)
            logger.info("Set yfinance timezone cache location to %s", custom_cache_dir)
        except Exception as e:
            logger.warning("Failed to configure process-isolated yfinance cache: %s", e)

    # --- yfinance (active: used by routes, services, tests) ---

    @property
    def yfinance_short_cache(self):
        return self.market.yfinance_short_cache

    @property
    def yfinance_short_cache_lock(self):
        return self.market.yfinance_short_cache_lock

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
                            except Exception:  # nosec B110
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

    def validate_shutdown_token(self, token: str) -> bool:
        return self.shutdown_manager.validate_shutdown_token(token)

    def commit_shutdown_token(self) -> None:
        self.shutdown_manager.commit_shutdown_token()

    def rotate_shutdown_token(self):
        self.shutdown_manager.rotate_shutdown_token()


# Singleton instance
app_state = AppState()
