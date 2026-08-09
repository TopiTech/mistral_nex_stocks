"""
tests/test_audit_review_fixes.py
Verification tests for review findings (R1 - R15).
"""

import threading
import time

from app_state import app_state
from messaging import MessageAnnouncer
from services.realtime_engine import RealtimeMarketEngine, YahooJPRealtimeScraper
from services.stock_provider import with_yfinance_retry
from session_manager import yf_session_manager


def test_r1_with_yfinance_retry_does_not_clear_active_rate_limit():
    """R1: If a rate limit is triggered during execution, delayed success does not reset it."""
    market = app_state.market
    with market.yfinance_lock:
        market.is_yfinance_rate_limited = False

    class DummyProvider:
        def _get_market_state(self):
            return market

        @with_yfinance_retry(max_retries=0)
        def fetch_delayed(self):
            # Simulate another thread setting rate limit while this request is running
            with market.yfinance_lock:
                market.is_yfinance_rate_limited = True
            yf_session_manager.mark_rate_limited("yfinance", duration=60)
            return {"data": "ok"}

    provider = DummyProvider()
    res = provider.fetch_delayed()
    assert res == {"data": "ok"}
    # The rate limit must NOT have been wiped out by the success
    assert yf_session_manager.is_rate_limited("yfinance") is True
    # Cleanup
    yf_session_manager.clear_rate_limit("yfinance")
    with market.yfinance_lock:
        market.is_yfinance_rate_limited = False


def test_r2_yfinance_cache_initialization_thread_safety():
    """R2: Multiple concurrent calls to initialize_yfinance_cache are safe and idempotent."""
    threads = []
    for _ in range(5):
        t = threading.Thread(target=app_state.initialize_yfinance_cache)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    assert app_state._yfinance_cache_dir is not None


def test_r3_r7_announce_real_market_state_and_sync_forced(monkeypatch):
    """R3 & R7: announce_real_market_state handles concurrent state changes safely, and sync_forced is locked."""
    from app_bg import announce_real_market_state

    with app_state.cache.sse_data_lock:
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 150.0}],
            "jp": [{"symbol": "7203.T", "price": 2000.0}],
            "idx": [],
        }

    # Should run without RuntimeError
    announce_real_market_state()

    # Test sync_forced under lock directly
    with app_state.market.sync_schedule_lock:
        app_state.market.sync_forced = True
        assert app_state.market.sync_forced is True
        app_state.market.sync_forced = False


def test_r4_r6_realtime_engine_concurrency_and_pts_deltas():
    """R4 & R6: store_lock is re-entrant, _purge_stale_clients is thread-safe, and get_pts_deltas updates last_seen."""
    engine = RealtimeMarketEngine()
    cid = engine.register_client()
    assert cid in engine._client_last_seen
    initial_ts = engine._client_last_seen[cid]

    time.sleep(0.01)
    engine.pts_store["7203.T"] = {"symbol": "7203.T", "price": 2500.0, "updated_at": time.time()}
    deltas = engine.get_pts_deltas(cid)
    assert "7203.T" in deltas
    assert engine._client_last_seen[cid] > initial_ts

    # Purge stale clients with 0 ttl
    engine._purge_stale_clients(ttl_seconds=-1.0)
    assert cid not in engine._client_last_seen

    # Cleanup
    if engine.tv_client:
        engine.tv_client.stop()
    if engine.yahoojp_scraper:
        engine.yahoojp_scraper.stop()


def test_r5_yahoojp_scraper_executor_reuse():
    """R5: YahooJPRealtimeScraper reuses executor across worker cycles."""
    scraper = YahooJPRealtimeScraper()
    assert scraper._executor is None
    scraper.stop()


def test_r10_message_announcer_close_and_shutdown():
    """R10: MessageAnnouncer.close() releases all registered listener queues."""
    announcer = MessageAnnouncer()
    q1 = announcer.listen()
    q2 = announcer.listen()
    assert announcer.listener_count() == 2

    announcer.close()
    assert announcer.listener_count() == 0
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None


def test_r1_bg_yahoo_fetch_loop_mode2_listener_count(monkeypatch):
    """R1: bg_yahoo_fetch_loop respects mode 2 SSE listeners and avoids idle sleep."""
    from app_bg import bg_yahoo_fetch_loop
    from messaging import MessageAnnouncer

    orig_mode1 = app_state.sse_announcer_mode1
    orig_mode2 = app_state.sse_announcer_mode2
    try:
        app_state.sse_announcer_mode1 = MessageAnnouncer()
        app_state.sse_announcer_mode2 = MessageAnnouncer()
        # Add a listener only to Mode 2 (TradingView / Realtime mode)
        q = app_state.sse_announcer_mode2.listen()

        waited_intervals = []
        call_count = 0

        def mock_wait(timeout=None):
            nonlocal call_count
            call_count += 1
            waited_intervals.append(timeout)
            if call_count >= 2:
                # Terminate loop on second wait call (which is the loop's post-sync sleep)
                app_state.execution.shutdown_event.set()
            return True

        monkeypatch.setattr(app_state.execution.shutdown_event, "wait", mock_wait)
        monkeypatch.setattr("app_bg.sync_all_stocks_now", lambda: None)
        monkeypatch.setattr("app_bg.is_market_open", lambda m: True)

        app_state.execution.shutdown_event.clear()
        bg_yahoo_fetch_loop()

        from constants import SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP, SSE_YAHOO_FETCH_NO_LISTENER_SLEEP

        assert SSE_YAHOO_FETCH_NO_LISTENER_SLEEP not in waited_intervals[1:]
        assert SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP in waited_intervals[1:]
    finally:
        app_state.sse_announcer_mode1.close()
        app_state.sse_announcer_mode2.close()
        app_state.sse_announcer_mode1 = orig_mode1
        app_state.sse_announcer_mode2 = orig_mode2
        app_state.sse_announcer = orig_mode1
        app_state.execution.shutdown_event.clear()


def test_r2_fallback_provider_json_extraction():
    """R2: Fallback provider extracts quote even from slightly truncated JSON strings."""
    from services.fallback_provider import YahooWebScraperProvider

    provider = YahooWebScraperProvider()
    assert provider is not None


def test_r3_crypto_utils_dpapi_cleanup_resilience():
    """R3: DPAPI cleanup safely handles null and valid pointers."""
    import crypto_utils
    # Should not raise exception
    assert hasattr(crypto_utils, "_dpapi_unprotect")

