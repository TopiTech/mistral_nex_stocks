"""Application state reset logic for test isolation."""

from __future__ import annotations

import math
import os
from pathlib import Path

from tests.test_support.temp_config import cleanup_temp_files


def _reset_ai_state(app_state: object) -> None:
    """Reset AI and LLM service states."""
    ai = getattr(app_state, "ai", None)
    if ai is None:
        return

    ai.mistral_429_streak = 0
    ai.mistral_next_allowed_ts = 0.0
    ai.mistral_last_call_ts = 0.0

    if hasattr(ai, "mistral_response_cache"):
        ai.mistral_response_cache.clear()
    if hasattr(ai, "mistral_clients"):
        ai.mistral_clients.clear()
    if hasattr(ai, "chat_history") and hasattr(ai.chat_history, "close_all"):
        try:
            ai.chat_history.close_all()
        except Exception:
            pass


def _reset_market_state(app_state: object) -> None:
    """Reset market data, rate limits, circuits, and default FX rates."""
    market = getattr(app_state, "market", None)
    if market is None:
        return

    market.first_sync_attempted = True
    market.is_yfinance_rate_limited = False
    market.yfinance_rate_limit_until = 0.0
    market.yfinance_last_request_ts = 0.0
    market.yfinance_429_streak = 0

    if hasattr(market, "invalid_symbol_streak"):
        market.invalid_symbol_streak.clear()

    try:
        from market_state import CircuitState

        market.circuit_states = {
            "mistral": CircuitState(status="CLOSED", timeout_streak=0, open_until=0.0),
            "langsearch": CircuitState(status="CLOSED", timeout_streak=0, open_until=0.0),
            "openai_compatible": CircuitState(status="CLOSED", timeout_streak=0, open_until=0.0),
        }
    except ImportError:
        pass

    if hasattr(market, "scraper_block_until"):
        market.scraper_block_until = 0.0
    if hasattr(market, "scraper_block_streak"):
        market.scraper_block_streak = 0
    if hasattr(market, "history_circuit_state"):
        market.history_circuit_state.clear()
    if hasattr(market, "previous_close_cache"):
        market.previous_close_cache.clear()

    try:
        default_usdjpy_rate = float(os.environ.get("MNS_DEFAULT_USDJPY", "150.00"))
    except (TypeError, ValueError):
        default_usdjpy_rate = 150.00
    if not math.isfinite(default_usdjpy_rate) or default_usdjpy_rate <= 0:
        default_usdjpy_rate = 150.00
    market.last_usdjpy_rate = default_usdjpy_rate
    market.last_usdjpy_rate_ts = 0.0

    # Reset snapshot caches
    cache = getattr(app_state, "cache", None)
    if cache is not None:
        try:
            with cache.sse_data_lock:
                market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
                market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
                market.current_indices_cache = {}
                market.target_indices_cache = {}
        except (AttributeError, RuntimeError):
            pass

    # Reset user-stocks persistence state
    if hasattr(market, "user_stocks_lock"):
        with market.user_stocks_lock:
            market.user_stocks_load_error = False
            market.user_us = {}
            market.user_jp = {}
            market.user_idx = {}

    # Reset sync locks and flags
    try:
        with market.is_syncing_lock:
            market.is_syncing = False
        with market.sync_schedule_lock:
            market.sync_pending = False
    except (AttributeError, RuntimeError):
        pass


def _reset_caches_and_sessions(app_state: object) -> None:
    """Clear in-memory and disk caches, SSE tickets, and session rate limits."""
    # YF Session Manager
    try:
        from app_state import yf_session_manager

        yf_session_manager.clear_rate_limit("yfinance")
        yf_session_manager.clear_rate_limit("default")
    except ImportError:
        pass

    # SSE ticket store
    try:
        from utils import networking as _networking

        with _networking._SSE_TICKETS_LOCK:
            _networking._SSE_TICKETS.clear()
    except (ImportError, AttributeError):
        pass

    # YFinance short cache
    if hasattr(app_state, "yfinance_short_cache"):
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.clear()

    # Disk cache lock timeout
    try:
        from utils import disk_cache as _disk_cache

        _disk_cache._last_lock_timeout_ts = 0.0
    except (ImportError, AttributeError):
        pass

    # Global cache
    try:
        from utils.caching import global_cache

        if hasattr(global_cache, "caches"):
            with global_cache.cache_lock:
                for dur in list(global_cache.caches.keys()):
                    global_cache.caches[dur].clear()
        if hasattr(global_cache, "fetch_events"):
            with global_cache.fetch_events_lock:
                global_cache.fetch_events.clear()
    except ImportError:
        pass

    if hasattr(app_state, "cache"):
        app_state.cache.reset_stats()

    # Route helpers rate limit store
    try:
        from route_helpers import _rate_limit_store

        _rate_limit_store.clear()
    except ImportError:
        pass

    # Analysis job caches
    try:
        from routes import api_analysis as _api_analysis

        _api_analysis.analyze_result_cache.clear()
        _api_analysis.analyze_fetch_inflight.clear()
        _api_analysis.chat_result_cache.clear()
        _api_analysis.chat_fetch_inflight.clear()
        with _api_analysis.news_fetch_lock:
            _api_analysis.news_fetch_inflight.clear()
    except (ImportError, AttributeError):
        pass

    # Disk caches
    for cache_attr in ("stock_disk_cache", "payload_disk_cache"):
        if hasattr(app_state, cache_attr):
            try:
                cache_obj = getattr(app_state, cache_attr)
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

    # Messaging listeners
    if hasattr(app_state, "messaging"):
        with app_state.messaging.listeners_lock:
            app_state.messaging.listeners.clear()

    # SSE listener limiter
    try:
        app_state.sse_listener_limiter.reset_for_testing()
    except AttributeError:
        pass

    # YFinance ticker instance cache
    try:
        if hasattr(app_state, "stock_provider") and hasattr(
            app_state.stock_provider, "clear_ticker_cache"
        ):
            app_state.stock_provider.clear_ticker_cache()
    except (ImportError, AttributeError):
        pass


def _reset_realtime_engine() -> None:
    """Reset RealtimeMarketEngine stores and dirty flags."""
    try:
        from services.realtime_engine import realtime_market_engine

        with realtime_market_engine.store_lock:
            realtime_market_engine.market_store.clear()
            realtime_market_engine.previous_store.clear()
            realtime_market_engine.pts_store.clear()
            realtime_market_engine.previous_pts_store.clear()
            realtime_market_engine._dirty_symbols.clear()
            realtime_market_engine._dirty_pts_symbols.clear()
    except (ImportError, AttributeError):
        pass


def _reset_sync_and_leader() -> None:
    """Restore default leader state and clear sync execution locks/files."""
    try:
        import threading

        import app_bg
        import bg.leader_election as _le
        import bg.sync_worker as _sw

        _le._set_is_sync_leader(True)
        _sw._set_sync_generation(0)
        _sw._set_sync_start_time(0.0)
        app_bg._sync_start_time = 0.0

        _lock_f = _le._get_leader_lock_file()
        if _lock_f is not None:
            try:
                _lock_f.close()
            except OSError:
                pass
            _le._set_leader_lock_file(None)

        lock = getattr(app_bg, "_sync_execution_lock", None)
        if lock is not None and lock.locked():
            app_bg._sync_execution_lock = threading.Lock()
    except (ImportError, AttributeError):
        pass

    try:
        import config_store

        leader_lock = Path(config_store.APP_DATA_DIR) / ".mns_sync_leader.lock"
        if leader_lock.exists():
            leader_lock.unlink(missing_ok=True)
    except Exception:
        pass


def reset_app_state_internals() -> None:
    """Reset global app_state and test-isolation boundaries to a clean slate.

    Coordinates cleanup across:
    - AI service caches, backoff streaks, and chat histories
    - Market state, circuits, rate limits, and snapshot caches
    - In-memory & disk caches, session limits, and SSE ticket stores
    - Realtime market engine quote and PTS stores
    - Background worker leader election states and locks
    - Tracked temporary configuration files
    """
    from app_state import app_state

    _reset_ai_state(app_state)
    _reset_market_state(app_state)
    _reset_caches_and_sessions(app_state)
    _reset_realtime_engine()
    _reset_sync_and_leader()
    cleanup_temp_files()
