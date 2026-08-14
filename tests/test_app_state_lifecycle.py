"""Lifecycle tests for process-isolated yfinance resources."""

import tempfile
from pathlib import Path

from app_state import app_state


def test_cleanup_yfinance_cache_removes_only_registered_directory():
    with tempfile.TemporaryDirectory() as parent:
        cache_dir = Path(parent) / "py-yfinance-mns-test"
        cache_dir.mkdir()
        (cache_dir / "cache.db").write_text("temporary", encoding="utf-8")
        original = app_state._yfinance_cache_dir
        try:
            app_state._yfinance_cache_dir = str(cache_dir)
            app_state._cleanup_yfinance_cache()
            assert not cache_dir.exists()
            assert app_state._yfinance_cache_dir is None
        finally:
            app_state._yfinance_cache_dir = original


def test_app_state_disk_caches_reside_under_app_data_dir():
    """R3-1: constructing a fresh AppState must place stock/payload disk
    caches under the per-user runtime data directory (APP_DATA_DIR) — the same
    place as config, user_stocks, chat_history, shutdown token and
    ai_portfolios — instead of the source tree root (BASE_DIR/.cache)."""
    import config_store
    from app_state import AppState

    # conftest points MNS_DATA_DIR at an isolated temp dir, so APP_DATA_DIR is
    # already that temp dir. Build a fresh instance to exercise AppState.__init__.
    runtime_dir = Path(config_store.APP_DATA_DIR).resolve()

    fresh = AppState()
    try:
        stock_dir = Path(fresh.stock_disk_cache._cache_dir).resolve()
        payload_dir = Path(fresh.payload_disk_cache._cache_dir).resolve()

        assert stock_dir == runtime_dir / ".cache" / "stock_history"
        assert payload_dir == runtime_dir / ".cache" / "stock_payloads"

        # Defense-in-depth: neither cache may point into the source tree root.
        from constants import BASE_DIR

        base_dir = Path(BASE_DIR).resolve()
        assert not stock_dir.is_relative_to(base_dir)
        assert not payload_dir.is_relative_to(base_dir)
    finally:
        fresh.shutdown_executors()


def test_app_state_disk_cache_writes_land_in_runtime_dir():
    """R3-1: a real cache write through a fresh AppState creates the JSON
    files under APP_DATA_DIR and never under the repository root .cache."""
    import config_store
    from app_state import AppState

    runtime_dir = Path(config_store.APP_DATA_DIR).resolve()
    fresh = AppState()
    try:
        fresh.stock_disk_cache.set("hist_R3TEST_us_3mo", {"symbol": "R3TEST"})
        fresh.payload_disk_cache.set("payload_R3TEST_us", {"symbol": "R3TEST"})

        stock_dir = Path(fresh.stock_disk_cache._cache_dir).resolve()
        payload_dir = Path(fresh.payload_disk_cache._cache_dir).resolve()
        assert stock_dir == runtime_dir / ".cache" / "stock_history"
        assert payload_dir == runtime_dir / ".cache" / "stock_payloads"

        # Entry files were actually created inside the runtime dir.
        stock_files = [p.name for p in stock_dir.glob("*.json")]
        payload_files = [p.name for p in payload_dir.glob("*.json")]
        assert any("hist_R3TEST_us_3mo" in n for n in stock_files)
        assert any("payload_R3TEST_us" in n for n in payload_files)
    finally:
        fresh.shutdown_executors()
