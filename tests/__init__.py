"""Test package for unittest discovery.

Common test utilities shared across test modules.
"""

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


def create_temp_config(
    overrides: Optional[Dict[str, Any]] = None,
    api_credentials: Optional[Dict[str, Any]] = None,
) -> Path:
    """Create a temporary config file for testing.

    Args:
        overrides: Dict of config values to override defaults.
        api_credentials: Dict of API credentials to inject.

    Returns:
        Path to the created config file.
    """
    config: Dict[str, Any] = {
        "mistral_model": "mistral-small-latest",
        "model_badge": "mistral-small",
        "api_credentials": api_credentials or {},
    }
    if overrides:
        config.update(overrides)

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(config, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    return Path(tmp.name)


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
        if hasattr(app_state.market, "history_circuit_state"):
            app_state.market.history_circuit_state.clear()

    # Clear yf_session_manager rate limit state (singleton persists across tests)
    try:
        from app_state import yf_session_manager

        yf_session_manager.clear_rate_limit("yfinance")
        yf_session_manager.clear_rate_limit("default")
    except ImportError:
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

    try:
        from route_helpers import _rate_limit_store

        _rate_limit_store.clear()
    except ImportError:
        pass

    # Clear the /api/analyze-v2 background-job result/inflight caches so a
    # completed analysis from one test cannot leak into another (the cache is
    # module-level and keyed by symbol+market, intentionally surviving across
    # normal re-polls in production but stale for test isolation).
    try:
        from routes import api_analysis as _api_analysis

        _api_analysis.analyze_result_cache.clear()
        _api_analysis.analyze_fetch_inflight.clear()
        _api_analysis.chat_result_cache.clear()
        _api_analysis.chat_fetch_inflight.clear()
    except (ImportError, AttributeError):
        pass

    if hasattr(app_state, "stock_disk_cache"):
        try:
            app_state.stock_disk_cache.clear()
        except Exception:
            pass
