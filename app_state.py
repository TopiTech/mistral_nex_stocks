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
import shutil
import threading
from datetime import timedelta
from typing import Any

from ai_state import AIState
from execution_state import ExecutionState
from market_state import MarketDataState
from messaging import MessageAnnouncer, SseListenerLimiter

# Re-export all components from extracted modules for backward compatibility
from session_manager import yf_session_manager
from shutdown_manager import ShutdownTokenManager

# Re-export keyring error
try:
    import keyring.errors as _keyring_errors

    KeyringError: type[Exception] = _keyring_errors.KeyringError
except ImportError:

    class _KeyringErrorFallback(Exception):
        """Fallback if keyring is not installed."""

    KeyringError = _KeyringErrorFallback

logger = logging.getLogger("backend")


class _InMemoryYfCache:
    """Thread-safe in-memory drop-in for yfinance's SQLite-backed caches.

    yfinance persists exchange timezones, login cookies, and ISIN lookups to
    SQLite files, which raised ``OperationalError: database is locked`` under
    this app's parallel fetch pattern. The earlier fix swapped in the
    ``_*Dummy`` caches, which never stored anything — so *every* ``yf.Ticker()``
    construction re-fetched the exchange timezone over the network (one
    v8/finance/chart request per ticker). This variant keeps the
    process-isolated, no-SQLite property while still memoizing lookups in
    memory, eliminating the per-ticker network calls. ``store(key, None)``
    evicts the key, mirroring yfinance's cache contract (used to invalidate
    stale entries). The store is bounded (oldest entries evicted first) so a
    long-running process with a growing symbol universe cannot leak memory.
    """

    # Bounds the store for long-running processes; eviction only costs a
    # re-fetch of that single key (same behaviour as a cold cache miss).
    _MAX_ENTRIES = 4096

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._lock = threading.Lock()

    def lookup(self, key: str) -> Any:
        with self._lock:
            return self._store.get(key)

    def store(self, key: str, value: Any) -> None:
        with self._lock:
            if value is None:
                self._store.pop(key, None)
            else:
                self._store[key] = value
                if len(self._store) > self._MAX_ENTRIES:
                    # Evict the oldest (insertion-ordered) entry to bound memory in O(1).
                    oldest_key = next(iter(self._store))
                    if oldest_key == key and len(self._store) > 1:
                        oldest_key = next(k for k in self._store if k != key)
                    self._store.pop(oldest_key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def initialise(self) -> None:  # pragma: no cover - compatibility no-op
        pass

    @property
    def tz_db(self) -> None:  # pragma: no cover - interface compatibility
        return None

    @property
    def Cookie_db(self) -> None:  # pragma: no cover - interface compatibility
        return None


class _InMemoryCookieCache(_InMemoryYfCache):
    """Cookie-cache variant that mirrors ``yfinance.cache._CookieCache.lookup``.

    The real cookie cache returns ``{"cookie": <stored value>, "age": <timedelta>}``
    and ``YfData._load_cookie_curlCffi`` reads ``cookie_dict["cookie"]``. A raw
    return value would raise ``KeyError`` on every cached-cookie reuse, so this
    subclass wraps lookups in the same shape the yfinance consumer expects.
    """

    def lookup(self, key: str) -> Any:
        with self._lock:
            value = self._store.get(key)
        if value is None:
            return None

        return {"cookie": value, "age": timedelta(0)}


# Re-export logging filters and formatters
IMPORTANT_INFO_PATTERNS = (
    "REQ start",
    "REQ end",
    "api_news start",
    "api_analyze input",
    "News bundle refresh",
    "LangSearch used:",
    "DDGS fallback used:",
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
        return not (" 200 -" in msg and any(x in msg for x in ["GET /api/indices", "GET /api/health", "GET /api/stocks"]))


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
    yf_session_manager: Any
    stock_provider: Any
    stock_disk_cache: Any
    payload_disk_cache: Any
    sse_announcer: MessageAnnouncer
    sse_announcer_mode1: MessageAnnouncer
    sse_announcer_mode2: MessageAnnouncer
    sse_listener_limiter: SseListenerLimiter
    history_fetch_inflight: set[str]
    history_fetch_lock: threading.Lock
    info_fetch_inflight: set[str]
    info_fetch_lock: threading.Lock
    heatmap_fetch_inflight: set[str]
    heatmap_fetch_start_times: dict[str, float]
    heatmap_fetch_lock: threading.Lock

    def __init__(self):
        self.execution = ExecutionState()
        self.market = MarketDataState()
        self.ai = AIState()
        self.yf_session_manager = yf_session_manager
        from utils.caching import global_cache

        self.cache = global_cache
        self.shutdown_manager = ShutdownTokenManager()
        self.bootstrap_ready = threading.Event()
        self.history_fetch_inflight = set()
        self.history_fetch_lock = threading.Lock()
        self.info_fetch_inflight = set()
        self.info_fetch_lock = threading.Lock()
        self.heatmap_fetch_inflight = set()
        self.heatmap_fetch_start_times = {}
        self.heatmap_fetch_lock = threading.Lock()

        self.sse_announcer_mode1 = MessageAnnouncer()
        self.sse_announcer_mode2 = MessageAnnouncer()
        self.sse_announcer = self.sse_announcer_mode1
        self.sse_listener_limiter = SseListenerLimiter()
        self._extension_origins_cache: set[str] = set()
        self._extension_origins_cache_ts = 0.0
        self._extension_origins_cache_lock = threading.Lock()
        self._extension_manifest_status = {"ok": True, "error": ""}
        self.EXTENSION_MANIFEST_ERROR_LOGGED = False
        self._EXTENSION_ORIGINS_CACHE_TTL_SEC = 30.0
        self._yfinance_cache_dir: str | None = None
        self._yfinance_cache_lock = threading.Lock()

        # stock_provider, disk caches: initialized eagerly in __init__ without
        # file-system side effects (those are deferred to initialize_yfinance_cache).
        from services.fallback_provider import CompositeFallbackProvider
        from services.stock_provider import YFinanceProvider

        self.stock_provider = YFinanceProvider(self.market)
        self.fallback_provider = CompositeFallbackProvider()

        from config_store import APP_DATA_DIR
        from constants import STOCK_HISTORY_CACHE_MAXSIZE, STOCK_HISTORY_DISK_CACHE_TTL
        from utils.disk_cache import StockDiskCache

        # R3-1: keep the disk caches in the per-user runtime data directory
        # (APP_DATA_DIR), the same place as config / user_stocks / chat_history /
        # shutdown token / ai_portfolios. This keeps all runtime data in one
        # location and works in environments where the source tree root is not
        # writable. The previous BASE_DIR/.cache files are left untouched
        # (best-effort re-fetch on first miss after the move).
        self.stock_disk_cache = StockDiskCache(
            cache_dir=APP_DATA_DIR / ".cache" / "stock_history",
            max_entries=STOCK_HISTORY_CACHE_MAXSIZE,
            default_ttl=STOCK_HISTORY_DISK_CACHE_TTL,
        )
        self.payload_disk_cache = StockDiskCache(
            cache_dir=APP_DATA_DIR / ".cache" / "stock_payloads",
            max_entries=256,
            default_ttl=3600,
        )

    def initialize_yfinance_cache(self) -> None:
        """Configure yfinance cache isolation and disable SQLite DB writes.

        Extracted from __init__ to avoid file-system side effects at import
        time (which interfere with test isolation). Call once explicitly from
        app startup (create_app) rather than at construction time.

        Mitigates sqlite3 locking issues (OperationalError: database is locked)
        on cookies.db, tkr-tz.db, and isin-tkr.db in parallel environments:
        - Replaces tz, cookie, and ISIN cache instances with dummy/in-memory caches
        - Sets a process-specific temp directory as fallback
        """
        with self._yfinance_cache_lock:
            try:
                import tempfile

                import yfinance as yf
                import yfinance.cache as yfc

                yf_version = getattr(yf, "__version__", "unknown")
                if not str(yf_version).startswith("1.5"):
                    logger.info(
                        "yfinance %s differs from the version this build was verified "
                        "against (1.5.x); the in-memory cache patching and crumb-reset "
                        "logic in session_manager may need review.",
                        yf_version,
                    )

                self._cleanup_yfinance_cache()
                custom_cache_dir = tempfile.mkdtemp(prefix="py-yfinance-mns-")
                self._yfinance_cache_dir = custom_cache_dir
                yf.set_tz_cache_location(custom_cache_dir)

                # Replace yfinance's SQLite-backed caches with in-memory ones.
                # This completely avoids sqlite3.OperationalError: database is
                # locked failures under concurrent background/worker requests
                # while still memoizing timezone/cookie/ISIN lookups in memory.
                # (The previous _*Dummy caches never stored anything, so every
                # Ticker construction re-fetched the exchange timezone over the
                # network — one v8/finance/chart request per ticker.)
                yfc._TzCacheManager._tz_cache = _InMemoryYfCache()
                yfc._CookieCacheManager._Cookie_cache = _InMemoryCookieCache()
                yfc._ISINCacheManager._isin_cache = _InMemoryYfCache()
                logger.info(
                    "Set yfinance timezone cache location to %s and replaced SQLite caches "
                    "with in-memory ones (yfinance %s)",
                    custom_cache_dir,
                    yf_version,
                )

                # Force reset of any in-memory cached crumbs/cookies for a clean startup state
                from session_manager import reset_yfinance_auth

                reset_yfinance_auth()
            except Exception as e:
                logger.warning("Failed to configure process-isolated yfinance cache: %s", e)

    def _cleanup_yfinance_cache(self) -> None:
        """Remove the private yfinance cache directory after the process stops."""
        cache_dir = self._yfinance_cache_dir
        self._yfinance_cache_dir = None
        if not cache_dir:
            return
        try:
            shutil.rmtree(cache_dir, ignore_errors=True)
        except OSError as exc:
            logger.debug("Failed to remove yfinance cache directory: %s", exc)

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

        with self._yfinance_cache_lock:
            self._cleanup_yfinance_cache()

        try:
            if hasattr(self, "sse_announcer_mode1"):
                self.sse_announcer_mode1.close()
            if hasattr(self, "sse_announcer_mode2"):
                self.sse_announcer_mode2.close()
        except Exception as e:
            logger.debug("Error closing SSE announcers: %s", e)

        try:
            # TradingView WS ・Yahoo JP スクレイパー・PTS ループを停止する（R12）。
            # 遅延 import: app_state と互いにトップレベル import しないことで循環を回避。
            from services.realtime_engine import realtime_market_engine

            realtime_market_engine.stop()
        except Exception as e:
            logger.debug("Error stopping realtime market engine: %s", e)

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

        try:
            if hasattr(self, "ai") and hasattr(self.ai, "chat_history") and hasattr(self.ai.chat_history, "close_all"):
                self.ai.chat_history.close_all()
        except Exception as e:
            logger.debug("Error closing chat history connections: %s", e)

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
