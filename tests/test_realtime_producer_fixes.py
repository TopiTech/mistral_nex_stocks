# tests/test_realtime_producer_fixes.py
"""Tests for the realtime-producer fixes (P0-P2):

- P0: TradingView WS connects around the clock (closed-market gate removed)
      and subscribed symbols are normalized to exchange-prefixed TV form.
- P1: Scraper structure-change streaks pause the symbol's polling until the
      recovery cooldown elapses, and scraper blocks cross-link into yfinance
      pacing (shared IP) and vice-versa.
- P2: The engine exposes its producer threads to the watchdog and can be
      restarted when one of them dies.
"""

import json
import threading
import time
import types
from unittest.mock import MagicMock, patch

from services.realtime_engine import (
    RealtimeMarketEngine,
    TradingViewWSClient,
    YahooJPRealtimeScraper,
    _is_yf_rate_limited,
    _normalize_tv_symbol,
    _tv_purge_key_variants,
)

# ---------------------------------------------------------------------------
# P0: TradingView symbol normalization
# ---------------------------------------------------------------------------


def test_normalize_tv_symbol():
    # Bare US tickers gain an exchange prefix.
    assert _normalize_tv_symbol("AAPL") == "NASDAQ:AAPL"
    # Index symbols already in TV form are preserved verbatim (never degraded
    # to NASDAQ:INDEX:SPX).
    assert _normalize_tv_symbol("INDEX:SPX") == "INDEX:SPX"
    assert _normalize_tv_symbol("INDEX:IUXX") == "INDEX:IUXX"
    # ^-prefixed indices resolve through the mapper (FOREXCOM:SPXUSD), not
    # kept verbatim as a raw ^GSPC that TradingView would not match.
    assert _normalize_tv_symbol("^GSPC") == "FOREXCOM:SPXUSD"
    # Dotted/dashed class shares resolve to the dotted TV form.
    assert _normalize_tv_symbol("BRK-B") == "NYSE:BRK.B"
    # Already-prefixed symbols pass through unchanged.
    assert _normalize_tv_symbol("NASDAQ:AAPL") == "NASDAQ:AAPL"
    assert _normalize_tv_symbol("TSE:7203") == "TSE:7203"
    # Empty symbols are a no-op.
    assert _normalize_tv_symbol("") == ""


def test_tv_purge_key_variants():
    variants = set(_tv_purge_key_variants("BRK-B"))
    assert {"BRK-B", "BRK.B", "NYSE:BRK.B"} <= variants

    variants_tsla = set(_tv_purge_key_variants("TSLA"))
    assert "TSLA" in variants_tsla
    assert "NASDAQ:TSLA" in variants_tsla


def test_register_symbol_normalizes_to_exchange_prefix():
    engine = RealtimeMarketEngine()
    engine.register_symbol("TSLA", "us")
    assert "NASDAQ:TSLA" in engine.tv_client.symbols
    assert "TSLA" not in engine.tv_client.symbols

    engine.register_symbol("INDEX:SPX", "us")
    assert "INDEX:SPX" in engine.tv_client.symbols

    engine.register_symbol("BRK-B", "us")
    assert "NYSE:BRK.B" in engine.tv_client.symbols


def test_register_symbols_batch_normalization():
    engine = RealtimeMarketEngine()
    engine.register_symbols(["AAPL", "INDEX:SPX", "BRK-B"], [])
    symbols = engine.tv_client.symbols
    assert "NASDAQ:AAPL" in symbols
    assert "INDEX:SPX" in symbols
    assert "NYSE:BRK.B" in symbols


def test_unregister_symbol_purges_prefixed_and_dotted_keys():
    engine = RealtimeMarketEngine()
    engine.register_symbol("BRK-B", "us")

    def _payload(symbol):
        return {
            "symbol": symbol,
            "price": 400.0,
            "change": 1.0,
            "change_percent": 0.25,
            "volume": 10,
            "source": "tradingview",
            "updated_at": time.time(),
        }

    engine._handle_producer_update(_payload("NYSE:BRK.B"))
    engine._handle_producer_update(_payload("BRK-B"))

    snapshot = engine.get_market_snapshot()
    assert "NYSE:BRK.B" in snapshot
    assert "BRK-B" in snapshot

    engine.unregister_symbol("BRK-B", "us")
    assert "NYSE:BRK.B" not in engine.tv_client.symbols
    snapshot = engine.get_market_snapshot()
    assert "NYSE:BRK.B" not in snapshot
    assert "BRK-B" not in snapshot
    assert "NYSE:BRK.B" not in engine.get_market_deltas()
    assert "BRK-B" not in engine.get_market_deltas()


def test_tv_ws_on_message_dispatches_dash_alias_for_class_shares():
    received = []

    def callback(payload):
        received.append(payload)

    client = TradingViewWSClient(on_update_callback=callback)
    qsd = {"m": "qsd", "p": ["qs", {"n": "NYSE:BRK.B", "v": {"lp": 400.0}}]}
    raw = f"~m~{len(json.dumps(qsd))}~m~{json.dumps(qsd)}"
    client._on_message(MagicMock(), raw)

    symbols = [p["symbol"] for p in received]
    assert "NYSE:BRK.B" in symbols
    assert "BRK.B" in symbols
    assert "BRK-B" in symbols


# ---------------------------------------------------------------------------
# P0: TradingView WS must connect even while the US market is closed
# ---------------------------------------------------------------------------


def test_tv_ws_connects_when_us_market_closed():
    """The closed-market gate was removed: the WS must connect around the clock.

    TradingView streams the last quote even while the US market is closed, so
    gating the connection on ``is_market_open`` previously silenced US realtime
    during JST daytime.
    """
    from services import realtime_engine as rt

    created = []

    class FakeWSApp:
        def __init__(self, *a, **kw):
            self.kw = kw
            # The real websocket-client library stores on_open as an attribute
            # that _run_ws assigns AFTER construction (``self.ws.on_open = ...``).
            self.on_open = kw.get("on_open")
            created.append(self)

        def send(self, *a, **kw):
            pass

        def run_forever(self, **kw):
            if self.on_open:
                self.on_open(self)
            while client.running:
                time.sleep(0.005)

    fake_ws = types.SimpleNamespace(WebSocketApp=FakeWSApp)
    client = rt.TradingViewWSClient(symbols=["NASDAQ:AAPL"])
    fake_state = types.SimpleNamespace(
        execution=types.SimpleNamespace(shutdown_event=threading.Event())
    )
    mock_market_open = MagicMock(return_value=False)
    with (
        patch.object(rt, "websocket", fake_ws),
        patch("app_state.app_state", fake_state),
        patch("utils.market_utils.is_market_open", mock_market_open),
        patch("services.realtime_engine.time.sleep", return_value=None),
    ):
        client.start()
        t = client.thread
        deadline = time.time() + 2.0
        while not client.connected and time.time() < deadline:
            time.sleep(0.005)
        # Assert while the connection is live: the worker resets ``connected``
        # to False when it shuts down below.
        assert client.connected is True
        assert created, "TradingView WS connection was never attempted"
        # The WS path must not be gated on the US market being open.
        assert mock_market_open.call_count == 0
        client.stop()
        if t is not None:
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# P1: Structure-change polling pauses with auto-recovery
# ---------------------------------------------------------------------------


def test_structure_change_pauses_symbol_polling():
    scraper = YahooJPRealtimeScraper()
    for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD):
        scraper._record_fetch_failure("7203.T")

    key = ("7203.T", "regular")
    assert scraper._pause_until.get(key, 0.0) > time.time()
    # Paused symbols are skipped by the active-symbol filter.
    assert scraper._active_symbols(["7203.T"]) == []
    # Other symbols are unaffected.
    assert scraper._active_symbols(["9984.T"]) == ["9984.T"]

    # A successful scrape lifts the pause and resumes polling.
    scraper._record_fetch_success("7203.T")
    assert scraper._pause_until.get(key, 0.0) == 0.0
    assert scraper._active_symbols(["7203.T"]) == ["7203.T"]


def test_structure_change_pause_is_reapplied_until_recovery():
    scraper = YahooJPRealtimeScraper()
    for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD):
        scraper._record_fetch_failure("7203.T")

    key = ("7203.T", "regular")
    first_pause = scraper._pause_until[key]
    # A subsequent failure while paused extends/re-applies the pause.
    scraper._record_fetch_failure("7203.T")
    assert scraper._pause_until[key] >= first_pause

    # remove_symbol clears the pause bookkeeping.
    scraper.remove_symbol("7203.T")
    assert key not in scraper._pause_until


def test_structure_change_pause_expiry_resumes_polling():
    scraper = YahooJPRealtimeScraper()
    for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD):
        scraper._record_fetch_failure("7203.T")

    assert scraper._active_symbols(["7203.T"]) == []

    # Once the recovery cooldown elapses the symbol is polled again.
    future = time.time() + scraper.RECOVERY_COOLDOWN_SECONDS + 1.0
    with patch("services.realtime_engine.time.time", return_value=future):
        assert scraper._active_symbols(["7203.T"]) == ["7203.T"]


def test_pts_structure_change_pauses_pts_kind_only():
    scraper = YahooJPRealtimeScraper()
    for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD):
        scraper._record_fetch_failure("7203.T", kind="pts")

    # The PTS pause does not affect regular polling of the same symbol.
    assert scraper._active_symbols(["7203.T"], kind="pts") == []
    assert scraper._active_symbols(["7203.T"], kind="regular") == ["7203.T"]


# ---------------------------------------------------------------------------
# P1: yfinance <-> scraper block cross-link (shared IP)
# ---------------------------------------------------------------------------


def test_scraper_block_marks_yfinance_rate_limited():
    """A Yahoo-hosted scraper block must also pause yfinance (shared IP)."""
    from app_state import app_state
    from session_manager import yf_session_manager

    market = app_state.market
    try:
        yf_session_manager.clear_rate_limit("yfinance")
        market.scraper_block_until = 0.0
        market.scraper_block_streak = 0

        market.mark_scraper_blocked(propagate_to_yfinance=True)
        assert market.is_scraper_blocked() is True
        assert yf_session_manager.is_rate_limited("yfinance") is True
    finally:
        market.scraper_block_until = 0.0
        market.scraper_block_streak = 0
        yf_session_manager.clear_rate_limit("yfinance")


def test_non_yahoo_scraper_block_does_not_touch_yfinance():
    """Kabutan/SBI/Minkabu blocks are site-local and must NOT pause yfinance.

    Third-party bot-protection 403s used to rotate UA + destroy the whole
    yfinance session pool (epoch bump + crumb reset) on every occurrence,
    destabilizing yfinance data fetching. Only Yahoo-hosted scrapers share
    Yahoo's rate-limit enforcement with yfinance.
    """
    from app_state import app_state
    from session_manager import yf_session_manager

    market = app_state.market
    try:
        yf_session_manager.clear_rate_limit("yfinance")
        market.scraper_block_until = 0.0
        market.scraper_block_streak = 0

        # Default (no propagation) is what Kabutan/SBI/Minkabu call sites use.
        market.mark_scraper_blocked()
        assert market.is_scraper_blocked() is True
        assert yf_session_manager.is_rate_limited("yfinance") is False
    finally:
        market.scraper_block_until = 0.0
        market.scraper_block_streak = 0
        yf_session_manager.clear_rate_limit("yfinance")


def test_yf_rate_limited_visible_to_scrapers():
    """_is_yf_rate_limited reflects the shared yfinance rate-limit state."""
    from session_manager import yf_session_manager

    try:
        yf_session_manager.clear_rate_limit("yfinance")
        assert _is_yf_rate_limited() is False
        yf_session_manager.mark_rate_limited("yfinance", 30)
        assert _is_yf_rate_limited() is True
    finally:
        yf_session_manager.clear_rate_limit("yfinance")


def test_worker_loop_skips_fetch_when_yf_rate_limited():
    """While yfinance is rate-limited, the scraper loop must not fetch."""
    scraper = YahooJPRealtimeScraper()
    scraper.symbols.add("7203.T")
    called = []
    scraper._fetch_regular_with_fallback = lambda sym: called.append(sym)
    scraper.running = True
    with (
        patch("services.realtime_engine._is_scraper_blocked", return_value=False),
        patch("services.realtime_engine._is_yf_rate_limited", return_value=True),
        patch("utils.market_utils.is_market_open", return_value=True),
        patch("services.realtime_engine.time.sleep", return_value=None),
    ):
        t = threading.Thread(target=scraper._worker_loop, daemon=True)
        t.start()
        time.sleep(0.05)
        scraper.running = False
        t.join(timeout=2.0)
    assert called == []


# ---------------------------------------------------------------------------
# P2: Engine producer threads, watchdog restart
# ---------------------------------------------------------------------------


def test_engine_worker_threads_reports_producers():
    engine = RealtimeMarketEngine()
    assert engine.worker_threads() == []

    engine.tv_client.thread = threading.Thread(name="TradingViewWSWorker")
    engine.yahoojp_scraper.thread = threading.Thread(name="YahooJPScraperWorker")
    engine.pts_thread = threading.Thread(name="JPPTSWorker")
    names = [t.name for t in engine.worker_threads()]
    assert "TradingViewWSWorker" in names
    assert "YahooJPScraperWorker" in names
    assert "JPPTSWorker" in names


def test_engine_restart_stops_and_starts():
    engine = RealtimeMarketEngine()
    with (
        patch.object(engine, "stop") as mock_stop,
        patch.object(engine, "start") as mock_start,
        patch("services.realtime_engine.time.sleep", return_value=None),
    ):
        engine.restart()
        mock_stop.assert_called_once()
        mock_start.assert_called_once()


def test_restart_keeps_symbol_registration_usable():
    """After a watchdog restart the background executor must be recreated, so
    ``register_symbol`` (which submits warm-up fetches) never raises
    ``RuntimeError: cannot schedule new futures after shutdown``.
    """
    engine = RealtimeMarketEngine()
    with (
        # Keep the restart producer-free: only the executor lifecycle is under
        # test here.
        patch.object(engine.tv_client, "start"),
        patch.object(engine.yahoojp_scraper, "start"),
        patch.object(engine, "_pts_worker_loop"),
        patch.object(engine.yahoojp_scraper, "_fetch_regular_with_fallback", return_value=None),
        patch.object(engine, "_fetch_pts_with_fallback", return_value=None),
        patch("services.realtime_engine.is_pts_session", return_value=False),
        patch("services.realtime_engine.time.sleep", return_value=None),
    ):
        engine.restart()
        # No RuntimeError: the executor was rebuilt by start().
        engine.register_symbol("7203.T", "jp")
        with engine.yahoojp_scraper.lock:
            assert "7203.T" in engine.yahoojp_scraper.symbols
        engine.register_symbols(["AAPL"], [])
        assert "NASDAQ:AAPL" in engine.tv_client.symbols
    # Cleanup without touching real producers.
    engine.running = False
    engine._bg_executor.shutdown(wait=False, cancel_futures=True)


def test_watchdog_restarts_dead_realtime_engine():
    import app_bg

    class FakeEngine:
        def __init__(self):
            self.restarted = False
            self.dead = threading.Thread(target=lambda: None)
            self.alive = threading.Thread(target=lambda: time.sleep(30), daemon=True)
            self.alive.start()

        def worker_threads(self):
            return [self.dead, self.alive]

        def restart(self):
            self.restarted = True

    fake = FakeEngine()
    restarted = app_bg._watchdog_restart_dead_realtime_engine(fake)
    assert restarted == [fake.dead.name]
    assert fake.restarted is True

    # All-healthy case: no restart.
    fake2 = FakeEngine()
    fake2.dead = fake2.alive
    restarted2 = app_bg._watchdog_restart_dead_realtime_engine(fake2)
    assert restarted2 == []
    assert fake2.restarted is False


def test_tv_ws_batched_heartbeat_and_qsd():
    """Verify that batched ~h~ heartbeat and qsd update in single WS frame does not drop qsd."""
    received = []
    client = TradingViewWSClient(on_update_callback=lambda data: received.append(data))
    mock_ws = MagicMock()

    hb_part = "~m~4~m~~h~1"
    qsd_body = json.dumps(
        {
            "m": "qsd",
            "p": [
                client.session_id,
                {"n": "NASDAQ:AAPL", "v": {"lp": 195.5, "ch": 2.5, "chp": 1.3}},
            ],
        }
    )
    qsd_part = f"~m~{len(qsd_body)}~m~{qsd_body}"
    batched_message = hb_part + qsd_part

    client._on_message(mock_ws, batched_message)

    mock_ws.send.assert_called_once_with(hb_part)
    assert len(received) == 2
    assert received[0]["symbol"] == "NASDAQ:AAPL"
    assert received[1]["symbol"] == "AAPL"
    assert received[0]["price"] == 195.5


def test_scraper_block_market_state_clears_in_no_nameerror():
    """Verify that _is_scraper_blocked / _is_yf_rate_limited cooldown path resolves market_state without NameError."""
    from services.realtime_engine import _scraper_market_state

    with patch("services.realtime_engine._is_scraper_blocked", return_value=True):
        market = _scraper_market_state()
        if market and hasattr(market, "scraper_block_clears_in"):
            remains = market.scraper_block_clears_in()
            assert isinstance(remains, (int, float))
