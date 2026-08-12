"""Test package for unittest discovery.

Common test utilities shared across test modules.
"""

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

_CREATED_TEMP_FILES: set[Path] = set()


def create_temp_config(
    overrides: dict[str, Any] | None = None,
    api_credentials: dict[str, Any] | None = None,
    register_for_cleanup: bool = True,
) -> Path:
    """Create a temporary config file for testing.

    Args:
        overrides: Dict of config values to override defaults.
        api_credentials: Dict of API credentials to inject.
        register_for_cleanup: If True, tracks the file for automatic deletion.

    Returns:
        Path to the created config file.
    """
    config: dict[str, Any] = {
        "mistral_model": "mistral-small-latest",
        "api_credentials": api_credentials or {},
    }
    if overrides:
        config.update(overrides)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(config, tmp, ensure_ascii=False, indent=2)
        tmp_name = tmp.name

    path = Path(tmp_name)
    if register_for_cleanup:
        _CREATED_TEMP_FILES.add(path)
    return path


def cleanup_temp_files() -> None:
    """Clean up any temporary config files tracked by create_temp_config."""
    while _CREATED_TEMP_FILES:
        path = _CREATED_TEMP_FILES.pop()
        try:
            if path.exists():
                path.unlink(missing_ok=True)
        except Exception:
            pass


def reset_app_state_internals():
    """Reset the global app_state to a clean slate for testing."""
    from app_state import app_state

    if hasattr(app_state, "ai"):
        app_state.ai.mistral_429_streak = 0
        app_state.ai.mistral_next_allowed_ts = 0.0
        app_state.ai.mistral_last_call_ts = 0.0
        if hasattr(app_state.ai, "mistral_response_cache"):
            app_state.ai.mistral_response_cache.clear()

    if hasattr(app_state, "market"):
        app_state.market.is_yfinance_rate_limited = False
        app_state.market.yfinance_rate_limit_until = 0.0
        app_state.market.yfinance_last_request_ts = 0.0
        app_state.market.yfinance_429_streak = 0
        if hasattr(app_state.market, "invalid_symbol_streak"):
            app_state.market.invalid_symbol_streak.clear()
        from market_state import CircuitState

        app_state.market.circuit_states = {
            "mistral": CircuitState(status="CLOSED", timeout_streak=0, open_until=0.0),
            "langsearch": CircuitState(status="CLOSED", timeout_streak=0, open_until=0.0),
        }
        if hasattr(app_state.market, "scraper_block_until"):
            app_state.market.scraper_block_until = 0.0
        if hasattr(app_state.market, "scraper_block_streak"):
            app_state.market.scraper_block_streak = 0
        if hasattr(app_state.market, "history_circuit_state"):
            app_state.market.history_circuit_state.clear()
        if hasattr(app_state.market, "previous_close_cache"):
            app_state.market.previous_close_cache.clear()
        # FX is mutable singleton state just like the caches above.  Leaving a
        # rate written by one test makes a later portfolio calculation depend
        # on collection order, particularly when its timestamp makes it look
        # fresh.  Match MarketDataState's configured default while rejecting a
        # non-finite test environment value.
        try:
            default_usdjpy_rate = float(os.environ.get("MNS_DEFAULT_USDJPY", "150.00"))
        except (TypeError, ValueError):
            default_usdjpy_rate = 150.00
        if not math.isfinite(default_usdjpy_rate) or default_usdjpy_rate <= 0:
            default_usdjpy_rate = 150.00
        app_state.market.last_usdjpy_rate = default_usdjpy_rate
        app_state.market.last_usdjpy_rate_ts = 0.0

    # Clear yf_session_manager rate limit state (singleton persists across tests)
    try:
        from app_state import yf_session_manager

        yf_session_manager.clear_rate_limit("yfinance")
        yf_session_manager.clear_rate_limit("default")
    except ImportError:
        pass

    # Clear the SSE ticket store so tickets issued by one test cannot be
    # consumed by another (module-level store in utils.networking).
    try:
        from utils import networking as _networking

        with _networking._SSE_TICKETS_LOCK:
            _networking._SSE_TICKETS.clear()
    except (ImportError, AttributeError):
        pass

    if hasattr(app_state, "yfinance_short_cache"):
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.clear()

    # Clear all global cache entries (not just stats)
    from utils.caching import global_cache

    if hasattr(global_cache, "caches"):
        with global_cache.cache_lock:
            for dur in list(global_cache.caches.keys()):
                global_cache.caches[dur].clear()
    if hasattr(global_cache, "fetch_events"):
        with global_cache.fetch_events_lock:
            global_cache.fetch_events.clear()

    if hasattr(app_state, "cache"):
        app_state.cache.reset_stats()

    # Reset the market snapshot caches.
    if hasattr(app_state, "market"):
        try:
            with app_state.cache.sse_data_lock:
                app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
                app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
                app_state.market.current_indices_cache = {}
                app_state.market.target_indices_cache = {}
        except (AttributeError, RuntimeError):
            pass

    try:
        from route_helpers import _rate_limit_store

        _rate_limit_store.clear()
    except ImportError:
        pass

    # Clear the /api/analyze-v2 background-job result/inflight caches
    try:
        from routes import api_analysis as _api_analysis

        _api_analysis.analyze_result_cache.clear()
        _api_analysis.analyze_fetch_inflight.clear()
        _api_analysis.chat_result_cache.clear()
        _api_analysis.chat_fetch_inflight.clear()
    except (ImportError, AttributeError):
        pass

    # High-speed disk cache optimization: bypass expensive disk rmtree when cache directory is empty
    if hasattr(app_state, "stock_disk_cache"):
        try:
            cache_obj = app_state.stock_disk_cache
            if hasattr(cache_obj, "_memory_cache"):
                cache_obj._memory_cache.clear()
            if hasattr(cache_obj, "cache_dir") and cache_obj.cache_dir.exists():
                try:
                    if any(cache_obj.cache_dir.iterdir()):
                        cache_obj.clear()
                except Exception:
                    cache_obj.clear()
            else:
                cache_obj.clear()
        except Exception:
            pass

    # Reset user-stocks persistence state.
    if hasattr(app_state, "market"):
        with app_state.market.user_stocks_lock:
            app_state.market.user_stocks_load_error = False
            app_state.market.user_us = {}
            app_state.market.user_jp = {}
            app_state.market.user_idx = {}

    if hasattr(app_state, "messaging"):
        with app_state.messaging.listeners_lock:
            app_state.messaging.listeners.clear()

    # SSE admission is process-wide across mode-1 and mode-2 announcers.
    # Clear reservations between tests just as queue listeners are reset above.
    try:
        app_state.sse_listener_limiter.reset_for_testing()
    except AttributeError:
        pass

    # Drop cached yfinance Ticker instances so a Ticker created under a mocked
    # ``yf.Ticker`` in one test can never leak into another test (each cached
    # entry embeds a session that may belong to a previous test run).
    try:
        if hasattr(app_state, "stock_provider") and hasattr(
            app_state.stock_provider, "clear_ticker_cache"
        ):
            app_state.stock_provider.clear_ticker_cache()
    except (ImportError, AttributeError):
        pass

    cleanup_temp_files()
