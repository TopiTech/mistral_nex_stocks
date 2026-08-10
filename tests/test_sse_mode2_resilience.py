"""
tests/test_sse_mode2_resilience.py

Unit tests for:
1. yfinance-based previous close calculations for realtime deltas
2. MessageAnnouncer ring-buffer queue management without dropping listeners
3. get_stock_previous_close helper functionality
4. YahooJPRealtimeScraper graduated failure backoff
"""

import time

from app_state import app_state
from services.realtime_engine import RealtimeMarketEngine, YahooJPRealtimeScraper
from utils.stock_payload import get_stock_previous_close


def test_yfinance_previous_close_delta_calculation():
    """Verify RealtimeMarketEngine calculates change and change_percent based on yfinance previous close."""
    engine = RealtimeMarketEngine()

    # Seed target_stocks_cache with a stock having price=1000, change=50 (prev_close=950)
    with app_state.cache.sse_data_lock:
        app_state.market.target_stocks_cache = {
            "jp": [
                {
                    "symbol": "7203.T",
                    "price": 1000.0,
                    "change": 50.0,
                    "change_percent": 5.26,
                    "previous_close": 950.0,
                }
            ],
            "us": [
                {
                    "symbol": "AAPL",
                    "price": 200.0,
                    "change": 10.0,
                    "change_percent": 5.26,
                    "previous_close": 190.0,
                }
            ],
            "idx": [],
        }
    # Mirror the sync path: seed the lock-free previous-close cache too.
    app_state.market.update_previous_close_cache("7203.T", 950.0)
    app_state.market.update_previous_close_cache("AAPL", 190.0)

    # Verify get_stock_previous_close resolves correctly
    assert get_stock_previous_close("7203.T") == 950.0
    assert get_stock_previous_close("AAPL") == 190.0

    # Producer incoming update for 7203.T with new price 970.0 (scraped raw change might be 0)
    payload_jp = {
        "symbol": "7203.T",
        "price": 970.0,
        "change": 0.0,
        "change_percent": 0.0,
        "source": "yahoojp",
        "updated_at": time.time(),
    }
    engine._handle_producer_update(payload_jp)

    stored_jp = engine.market_store["7203.T"]
    # Change should be 970.0 - 950.0 = +20.0, change_percent = (20 / 950) * 100 = 2.11%
    assert stored_jp["price"] == 970.0
    assert stored_jp["change"] == 20.0
    assert round(stored_jp["change_percent"], 2) == 2.11
    assert stored_jp["previous_close"] == 950.0

    # Producer incoming update for AAPL with new price 195.0
    payload_us = {
        "symbol": "AAPL",
        "price": 195.0,
        "change": 0.0,
        "change_percent": 0.0,
        "source": "tradingview",
        "updated_at": time.time(),
    }
    engine._handle_producer_update(payload_us)

    stored_us = engine.market_store["AAPL"]
    # Change should be 195.0 - 190.0 = +5.0, change_percent = (5 / 190) * 100 = 2.63%
    assert stored_us["price"] == 195.0
    assert stored_us["change"] == 5.0
    assert round(stored_us["change_percent"], 2) == 2.63
    assert stored_us["previous_close"] == 190.0


def test_realtime_engine_mode2_client_deltas():
    """Verify RealtimeMarketEngine deltas and client state management for SSE mode 2."""
    engine = RealtimeMarketEngine()
    cid = engine.register_client()

    # Initial get_market_deltas on empty engine returns empty dict
    assert engine.get_market_deltas(cid) == {}

    # Register an update
    payload = {
        "symbol": "NVDA",
        "price": 120.0,
        "change": 2.0,
        "change_percent": 1.69,
        "source": "tradingview",
        "updated_at": time.time(),
    }
    engine._handle_producer_update(payload)

    deltas = engine.get_market_deltas(cid)
    assert "NVDA" in deltas
    assert deltas["NVDA"]["price"] == 120.0

    # Second call returns empty because client already consumed it
    assert engine.get_market_deltas(cid) == {}

    engine.unregister_client(cid)


def test_previous_close_cache_resolves_without_sse_lock():
    """The lock-free previous-close cache resolves values without sse_data_lock.

    Regression for the per-qsd lock contention: get_stock_previous_close must
    resolve from the dedicated cache (mirroring the sync-path write) and only
    fall back to the sse_data_lock-guarded stores on a cache miss.
    """
    engine = RealtimeMarketEngine()
    app_state.market.update_previous_close_cache("MSFT", 410.25)

    assert get_stock_previous_close("MSFT") == 410.25
    assert get_stock_previous_close("410.25") is None  # not a symbol

    # Alias resolution: .T and bare forms share the same cache entry.
    app_state.market.update_previous_close_cache("7203", 3000.0)
    assert get_stock_previous_close("7203.T") == 3000.0
    assert get_stock_previous_close("7203") == 3000.0

    # Producer updates seed the cache so later deltas skip the fallback scan.
    payload = {
        "symbol": "MSFT",
        "price": 415.5,
        "change": 0.0,
        "change_percent": 0.0,
        "source": "tradingview",
        "updated_at": time.time(),
    }
    engine._handle_producer_update(payload)
    assert app_state.market.get_previous_close_cached("MSFT") == 410.25
    assert engine.market_store["MSFT"]["change"] == 5.25


def test_previous_close_unrounded_in_payload():
    """build_stock_payload keeps previous_close as a raw float (R6)."""
    from unittest.mock import patch

    import pandas as pd

    from utils.stock_payload import build_stock_payload

    idx = pd.date_range("2026-08-07", periods=2, freq="D")
    hist = pd.DataFrame(
        {
            "Open": [100.0, 105.5],
            "High": [101.0, 106.0],
            "Low": [99.0, 104.5],
            "Close": [100.0, 105.55],
            "Volume": [1000, 1200],
        },
        index=idx,
    )
    with patch("utils.stock_payload.get_stock_info_cached", return_value={}):
        payload = build_stock_payload("TEST", "Test Co", "us", hist)
    assert payload is not None
    # price 105.55 - change 5.55 => previous_close 100.00 (raw float, not
    # 2-decimal-rounded from the rounded change value).
    assert payload["previous_close"] == 100.0
    assert isinstance(payload["previous_close"], float)


def test_sync_path_seeds_previous_close_cache():
    """_process_fetched_stocks seeds the lock-free previous-close cache (R2).

    The sync path is the primary writer for the cache; realtime producers
    then resolve prev_close without sse_data_lock on every TV WS message.
    """
    import app_bg

    with app_state.cache.sse_data_lock:
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.previous_close_cache.clear()

    fetched = [
        {"symbol": "AAPL", "market": "us", "price": 200.0, "change": 10.0,
         "change_percent": 5.26, "previous_close": 190.0},
        {"symbol": "7203.T", "market": "jp", "price": 970.0, "change": 20.0,
         "change_percent": 2.11, "previous_close": 950.0},
    ]
    generation = app_bg._sync_generation
    us_res, jp_res, _idx_res = app_bg._process_fetched_stocks(fetched, sync_generation=generation)

    assert us_res and jp_res
    assert app_state.market.get_previous_close_cached("AAPL") == 190.0
    assert app_state.market.get_previous_close_cached("7203.T") == 950.0

    # Derived fallback: a row without previous_close derives price - change.
    with app_state.cache.sse_data_lock:
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.previous_close_cache.clear()
    fetched2 = [
        {"symbol": "MSFT", "market": "us", "price": 415.5, "change": 5.25,
         "change_percent": 1.28},
    ]
    app_bg._process_fetched_stocks(fetched2, sync_generation=app_bg._sync_generation)
    assert app_state.market.get_previous_close_cached("MSFT") == 410.25


def test_producer_update_not_blocked_by_sse_data_lock():
    """Producer deltas must not block on sse_data_lock (R2 regression).

    Before the lock-free previous-close cache, every TradingView qsd message
    took sse_data_lock to scan target_stocks_cache; while an SSE stream held
    the lock (initial snapshot serialization) the producer stalled for up to
    ~0.9s. With the cache seeded, _handle_producer_update must complete even
    while sse_data_lock is held by another thread.
    """
    import threading

    engine = RealtimeMarketEngine()
    app_state.market.update_previous_close_cache("NVDA", 120.0)

    result = {}
    release = threading.Event()

    def holder():
        with app_state.cache.sse_data_lock:
            release.wait(2.0)
        result["holder_done"] = True

    t1 = threading.Thread(target=holder)
    t1.start()
    time.sleep(0.05)  # let the holder acquire sse_data_lock

    payload = {
        "symbol": "NVDA",
        "price": 122.5,
        "change": 0.0,
        "change_percent": 0.0,
        "source": "tradingview",
        "updated_at": time.time(),
    }
    start = time.time()
    engine._handle_producer_update(payload)
    elapsed = time.time() - start

    release.set()
    t1.join(timeout=2.0)
    assert elapsed < 0.2, f"producer blocked {elapsed:.2f}s on sse_data_lock"
    assert engine.market_store["NVDA"]["change"] == 2.5


def test_scraper_graduated_failure_cooldown():
    """Verify YahooJPRealtimeScraper uses graduated exponential pause on consecutive failures."""
    scraper = YahooJPRealtimeScraper(symbols=["9999.T"])
    symbol = "9999.T"

    # 4 failures should not pause yet
    for _ in range(4):
        scraper._record_fetch_failure(symbol)
    assert symbol in scraper._active_symbols([symbol])

    # 5th failure triggers initial graduated pause (15s)
    scraper._record_fetch_failure(symbol)
    assert symbol not in scraper._active_symbols([symbol])

    key = (symbol, "regular")
    pause_until = scraper._pause_until.get(key, 0.0)
    assert pause_until > time.time()
    # Pause duration should be around 15s, not 600s
    assert (pause_until - time.time()) <= 25.0

    # Successful fetch resets failures immediately
    scraper._record_fetch_success(symbol)
    assert symbol in scraper._active_symbols([symbol])
    assert scraper._consecutive_failures.get(key, 0) == 0
