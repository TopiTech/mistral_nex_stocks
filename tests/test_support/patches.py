"""Test isolation monkeypatches and background service stubs."""

from __future__ import annotations

from pathlib import Path

from tests.test_support.executors import SynchronousExecutor


def apply_yfinance_cache_isolation() -> None:
    """Mirror production yfinance cache isolation to prevent SQLite database leaks.

    Replaces yfinance sqlite-backed caches with in-memory drop-in caches.
    """
    try:
        import yfinance.cache as _yfc

        from app_state import _InMemoryCookieCache, _InMemoryYfCache

        _yfc._TzCacheManager._tz_cache = _InMemoryYfCache()
        _yfc._CookieCacheManager._Cookie_cache = _InMemoryCookieCache()
        _yfc._ISINCacheManager._isin_cache = _InMemoryYfCache()
    except (ImportError, AttributeError):  # pragma: no cover - defensive
        pass


def apply_storage_and_config_patches(temp_path: Path) -> None:
    """Redirect all persistent disk storage paths into an isolated temporary directory."""
    from app_state import app_state
    from utils.disk_cache import StockDiskCache

    # Shutdown token files
    app_state.shutdown_manager.token_file = temp_path / ".mns_shutdown_token"
    app_state.shutdown_manager.used_marker = temp_path / ".mns_shutdown_token.used"

    # User stocks file
    import utils.storage

    utils.storage.USER_STOCKS_FILE = str(temp_path / "user_stocks.json")

    # Config store paths
    import config_store

    config_store.APP_DATA_DIR = temp_path
    config_store.CONFIG_FILE = temp_path / "config.json"
    config_store.USER_STOCKS_FILE = temp_path / "user_stocks.json"

    # AI portfolio service paths
    try:
        import services.ai_portfolio_service as _ai_port_svc

        _ai_port_svc.APP_DATA_DIR = temp_path
        _ai_port_svc.AI_PORTFOLIO_STORAGE_FILE = temp_path / "ai_portfolios.json"
    except (ImportError, AttributeError):
        pass

    # Disk cache directories
    app_state.stock_disk_cache = StockDiskCache(
        cache_dir=temp_path / ".cache" / "stock_history",
        max_entries=256,
        default_ttl=300,
    )
    app_state.payload_disk_cache = StockDiskCache(
        cache_dir=temp_path / ".cache" / "stock_payloads",
        max_entries=256,
        default_ttl=300,
    )


def apply_synchronous_executors() -> None:
    """Replace thread pool executors with synchronous mocks to prevent race conditions and hangs."""
    from app_state import app_state

    sync_exec = SynchronousExecutor()
    app_state.execution.executor = sync_exec  # type: ignore[assignment]
    app_state.execution.data_executor = sync_exec  # type: ignore[assignment]
    app_state.execution.news_executor = sync_exec  # type: ignore[assignment]
    app_state.execution.sync_refresh_executor = sync_exec  # type: ignore[assignment]

    try:
        import trend_sources as _ts

        _ts._EXECUTOR = sync_exec  # type: ignore[assignment]
        _ts._GOOGLE_TRENDS_MIN_INTERVAL = 0.0
    except (ImportError, AttributeError):
        pass


def apply_background_stubs() -> None:
    """Stub network-fetching background loops and scheduled tasks to isolate unit tests."""
    import app_bg as _app_bg

    _app_bg.schedule_sync_all_stocks_now = lambda *a, **kw: False  # type: ignore[assignment]
    _app_bg.announce_current_market_state = lambda: None  # type: ignore[assignment]
    _app_bg.fetch_stocks_batch = lambda items, snapshot_ts_ms=None, **kwargs: []  # type: ignore[assignment]
    _app_bg.fetch_index_data = lambda *a, **kw: None  # type: ignore[assignment]

    try:
        import app as _app_mod

        _app_mod.schedule_news_warmup = lambda *a, **kw: None  # type: ignore[assignment]
    except (ImportError, AttributeError):
        pass

    # Stub research context collector in AI portfolio service to prevent live HTTP search latency
    try:
        import services.ai_portfolio_service as _ai_port_svc

        _ai_port_svc.collect_symbol_research_context = lambda *a, **kw: ""
    except (ImportError, AttributeError):
        pass


def apply_all_test_patches(temp_path: Path) -> None:
    """Convenience helper to apply all standard test monkeypatches."""
    apply_yfinance_cache_isolation()
    apply_storage_and_config_patches(temp_path)
    apply_synchronous_executors()
    apply_background_stubs()
