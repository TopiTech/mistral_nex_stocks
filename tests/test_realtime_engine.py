# tests/test_realtime_engine.py
"""Unit tests for Realtime Market Engine (TradingView WS, Yahoo JP)."""

import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from services.realtime_engine import (
    RealtimeMarketEngine,
    SBISecuritiesScraper,
    TradingViewWSClient,
    YahooJPRealtimeScraper,
    is_jp_market_open,
    is_pts_session,
)

JST = ZoneInfo("Asia/Tokyo")


def test_is_jp_market_open():
    # Test open hours (Monday 10:00 JST)
    mon_open = datetime(2026, 8, 3, 10, 0, tzinfo=JST)
    assert is_jp_market_open(mon_open) is True

    # Test weekend (Sunday 10:00 JST)
    sun = datetime(2026, 8, 2, 10, 0, tzinfo=JST)
    assert is_jp_market_open(sun) is False

    # Test night closed hours (Monday 20:00 JST)
    mon_night = datetime(2026, 8, 3, 20, 0, tzinfo=JST)
    assert is_jp_market_open(mon_night) is False


def test_tradingview_ws_format_and_parse():
    formatted = TradingViewWSClient.format_tv_message("quote_add_symbols", ["qs_test", "NASDAQ:AAPL"])
    assert formatted.startswith("~m~")
    assert "quote_add_symbols" in formatted

    payload_json = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:AAPL", "v": {"lp": 225.5, "ch": 1.5}}]})
    raw_msg = f"~m~{len(payload_json)}~m~{payload_json}"
    parsed = TradingViewWSClient.parse_tv_messages(raw_msg)
    assert len(parsed) == 1
    assert parsed[0]["m"] == "qsd"
    assert parsed[0]["p"][1]["n"] == "NASDAQ:AAPL"
    assert parsed[0]["p"][1]["v"]["lp"] == 225.5


def test_tradingview_ws_client_on_message():
    received = []

    def callback(payload):
        received.append(payload)

    client = TradingViewWSClient(on_update_callback=callback)
    payload_json = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:AAPL", "v": {"lp": 225.5, "ch": 1.5, "chp": 0.67, "volume": 10000}}]})
    raw_msg = f"~m~{len(payload_json)}~m~{payload_json}"
    mock_ws = MagicMock()
    client._on_message(mock_ws, raw_msg)

    assert len(received) == 2
    assert received[0]["symbol"] == "NASDAQ:AAPL"
    assert received[0]["price"] == 225.5
    assert received[0]["change"] == 1.5
    assert received[0]["source"] == "tradingview"
    assert received[1]["symbol"] == "AAPL"
    assert received[1]["price"] == 225.5


def test_tradingview_ws_client_partial_updates_preserve_state():
    """Partial updates (e.g. volume or lp only) must preserve previously seen price/change metrics."""
    received = []

    def callback(payload):
        received.append(payload)

    client = TradingViewWSClient(on_update_callback=callback)
    # Frame 1: Full payload
    f1 = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:NVDA", "v": {"lp": 120.0, "ch": 2.5, "chp": 2.12}}]})
    client._on_message(MagicMock(), f"~m~{len(f1)}~m~{f1}")

    # Frame 2: Partial payload with volume update only (no lp/ch/chp)
    f2 = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:NVDA", "v": {"volume": 50000}}]})
    client._on_message(MagicMock(), f"~m~{len(f2)}~m~{f2}")

    assert len(received) == 4  # 2 frames x 2 (prefixed + bare symbol)
    last_payload = received[-1]
    assert last_payload["symbol"] == "NVDA"
    assert last_payload["price"] == 120.0
    assert last_payload["change"] == 2.5
    assert last_payload["change_percent"] == 2.12
    assert last_payload["volume"] == 50000


def test_tradingview_ws_client_skips_malformed_quote():
    """A single bad quote (non-numeric price) must not crash or emit a payload."""
    received = []

    def callback(payload):
        received.append(payload)

    client = TradingViewWSClient(on_update_callback=callback)

    # Non-numeric price
    bad_json = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:AAPL", "v": {"lp": "--", "ch": 1.5}}]})
    client._on_message(MagicMock(), f"~m~{len(bad_json)}~m~{bad_json}")

    # Non-finite price (NaN / Infinity)
    nan_json = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:AAPL", "v": {"lp": "NaN"}}]})
    client._on_message(MagicMock(), f"~m~{len(nan_json)}~m~{nan_json}")

    assert received == []

    # A valid quote still flows after the malformed ones
    ok_json = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:AAPL", "v": {"lp": 225.5}}]})
    client._on_message(MagicMock(), f"~m~{len(ok_json)}~m~{ok_json}")
    assert len(received) == 2
    assert received[0]["price"] == 225.5



def test_yahoo_jp_scraper_fetch():
    scraper = YahooJPRealtimeScraper()
    mock_html = '{"price": "3500.0", "priceChange": "50.0", "priceChangePercent": "1.45"}'

    with patch.object(scraper.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html
        mock_get.return_value = mock_resp

        payload = scraper.fetch_jp_symbol("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 3500.0
        assert payload["source"] == "yahoojp"


def test_yahoo_jp_scraper_parses_current_escaped_json():
    """Current Yahoo JP pages embed quote data as escaped JSON."""
    scraper = YahooJPRealtimeScraper()
    plain = (
        '{"price":{"value":"2,983.5"},'
        '"priceChange":{"value":"69"},'
        '"priceChangeRate":{"value":"2.37"}}'
    )
    # Yahoo embeds the JSON inside a JS string, so every quote is backslash-escaped.
    mock_html = plain.replace('"', '\\"')

    with patch.object(scraper.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html
        mock_get.return_value = mock_resp

        payload = scraper.fetch_jp_symbol("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 2983.5
        assert payload["change"] == 69.0
        assert payload["change_percent"] == 2.37
        assert payload["source"] == "yahoojp"


def test_yahoo_jp_scraper_fetch_pts_symbol():
    """PTS quotes must be parsed from the Yahoo JP PTS tab page."""
    scraper = YahooJPRealtimeScraper()
    plain = (
        '...ptsTradingFlag":true,ptsPriceData":{"price":"2,973.9",'
        '"priceTime":"17:03","changePrice":"-9.6","changeRate":"-0.32",'
        '"volume":"600","displayTimeFlag":true}}]]}]...'
    )
    mock_html = plain.replace('"', '\\"')

    with patch.object(scraper.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html
        mock_get.return_value = mock_resp

        payload = scraper.fetch_pts_symbol("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 2973.9
        assert payload["change"] == -9.6
        assert payload["change_percent"] == -0.32
        assert payload["volume"] == 600
        assert payload["pts"] is True
        assert payload["pts_trading"] is True
        assert payload["source"] == "yahoojp_pts"


def test_yahoo_jp_scraper_fallback_to_sbi():
    """When Yahoo JP returns no quote, the fallback provider must be used."""
    scraper = YahooJPRealtimeScraper()
    scraper.fallback_provider = MagicMock()
    scraper.fallback_provider.fetch_quote.return_value = {
        "symbol": "7203.T",
        "price": 2900.0,
        "change": 0.0,
        "change_percent": 0.0,
        "volume": 0,
        "source": "sbi",
        "updated_at": time.time(),
    }

    with patch.object(scraper.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>no price marker here</html>"
        mock_get.return_value = mock_resp

        payload = scraper._fetch_regular_with_fallback("7203.T")
        assert payload is not None
        assert payload["source"] == "sbi"
        scraper.fallback_provider.fetch_quote.assert_called_once_with("7203.T")

    # When Yahoo succeeds, the fallback must NOT be consulted.
    scraper2 = YahooJPRealtimeScraper()
    scraper2.fallback_provider = MagicMock()
    mock_html = '{"price": "3500.0"}'
    with patch.object(scraper2.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html
        mock_get.return_value = mock_resp
        payload = scraper2._fetch_regular_with_fallback("7203.T")
        assert payload is not None
        assert payload["source"] == "yahoojp"
        scraper2.fallback_provider.fetch_quote.assert_not_called()


def test_sbi_scraper_fetch_quote():
    """SBI fallback must parse 現在値 / 前日比 from the Windows-31J page."""
    scraper = SBISecuritiesScraper()
    mock_html = (
        "<table><tr><td>現在値</td><td><strong>2,983.5</strong></td></tr>"
        "<tr><td>前日比</td><td>-9.6</td></tr></table>"
    )

    with patch.object(scraper.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = mock_html.encode("cp932")
        mock_get.return_value = mock_resp

        payload = scraper.fetch_quote("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 2983.5
        assert payload["change"] == -9.6
        assert payload["source"] == "sbi"


def test_sbi_scraper_cooldown_skips_retries():
    """Repeated SBI failures must enter a cooldown that skips network retries."""
    scraper = SBISecuritiesScraper()
    scraper._record_fetch_failure("7203.T")

    # While in cooldown, the fetch must be skipped entirely (no network call).
    with patch.object(scraper, "_fetch_page") as mock_fetch:
        payload = scraper.fetch_quote("7203.T")
        assert payload is None
        mock_fetch.assert_not_called()

    # After the cooldown window expires, fetching is attempted again.
    future = time.time() + scraper.FALLBACK_COOLDOWN_SECONDS + 1
    with patch("services.realtime_engine.time.time", return_value=future):
        with patch.object(scraper, "_fetch_page", return_value=None) as mock_fetch2:
            payload = scraper.fetch_quote("7203.T")
            assert payload is None
            mock_fetch2.assert_called_once_with("7203.T")


def test_sbi_scraper_fetch_pts_quote():
    """SBI PTS fallback must parse the PTS section price."""
    scraper = SBISecuritiesScraper()
    mock_html = (
        "<table><tr><td>現在値</td><td>2,983.5</td></tr></table>"
        "<div>PTS</div><div class='pts-price'>2,900.0</div>"
    )

    with patch.object(scraper.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = mock_html.encode("cp932")
        mock_get.return_value = mock_resp

        payload = scraper.fetch_pts_quote("7203.T")
        assert payload is not None
        assert payload["price"] == 2900.0
        assert payload["pts"] is True
        assert payload["source"] == "sbi_pts"


def test_minkabu_scraper_fetch_quote_and_pts():
    """Minkabu fallback must parse stock_price from minkabu HTML."""
    from services.realtime_engine import MinkabuScraper
    scraper = MinkabuScraper()
    mock_html = '<div class="stock_price">2,983.5</div>'

    with patch.object(scraper.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html
        mock_get.return_value = mock_resp

        payload = scraper.fetch_quote("7203.T")
        assert payload is not None
        assert payload["price"] == 2983.5
        assert payload["source"] == "minkabu"

        pts_payload = scraper.fetch_pts_quote("7203.T")
        assert pts_payload is not None
        assert pts_payload["price"] == 2983.5
        assert pts_payload["pts"] is True
        assert pts_payload["source"] == "minkabu_pts"


def test_is_pts_session():
    mon_pts = datetime(2026, 8, 3, 18, 0, tzinfo=JST)  # Monday Night PTS (16:30 - 23:59)
    assert is_pts_session(mon_pts) is True

    mon_day_pts = datetime(2026, 8, 3, 10, 0, tzinfo=JST)  # Monday Daytime PTS (08:20 - 16:00)
    assert is_pts_session(mon_day_pts) is True

    mon_early = datetime(2026, 8, 3, 7, 0, tzinfo=JST)  # Before PTS session
    assert is_pts_session(mon_early) is False

    sun_pts = datetime(2026, 8, 2, 18, 0, tzinfo=JST)  # Sunday
    assert is_pts_session(sun_pts) is False


def test_yahoo_jp_scraper_structure_change_detection():
    """Consecutive failures must surface an info notification (page change)."""
    scraper = YahooJPRealtimeScraper()

    with patch("services.realtime_engine.logger") as mock_logger:
        for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD):
            scraper._record_fetch_failure("7203.T")
        mock_logger.info.assert_called_once()

    # A successful scrape resets the counters; failures below the threshold
    # must not re-report.
    scraper._record_fetch_success("7203.T")
    with patch("services.realtime_engine.logger") as mock_logger:
        for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD - 1):
            scraper._record_fetch_failure("7203.T")
        mock_logger.info.assert_not_called()


def test_yahoo_jp_scraper_auto_recovery():
    """After recovery cooldown passes, failure tracking resets for auto-recovery."""
    scraper = YahooJPRealtimeScraper()
    for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD):
        scraper._record_fetch_failure("7203.T")

    key = ("7203.T", "regular")
    assert key in scraper._structure_change_reported

    future = time.time() + scraper.RECOVERY_COOLDOWN_SECONDS + 10.0
    with patch("services.realtime_engine.time.time", return_value=future):
        with patch("services.realtime_engine.logger") as mock_logger:
            scraper._record_fetch_failure("7203.T")
            assert key not in scraper._structure_change_reported
            mock_logger.info.assert_not_called()


def test_tradingview_ws_opcode_8_handled_as_info():
    """Opcode 8 close frames (1000 goodbye) must be logged as clean close at INFO level."""
    client = TradingViewWSClient()
    with patch("services.realtime_engine.logger") as mock_logger:
        client._on_ws_error(MagicMock(), "fin=1 opcode=8 data=b'\\x03\\xe8' - goodbye")
        mock_logger.info.assert_called_once()
        assert "clean close" in mock_logger.info.call_args[0][0]


def test_yahoo_jp_scraper_startup_ready_gate():
    """Scrapers must wait for initial yfinance sync completion and rendering window."""
    scraper = YahooJPRealtimeScraper()

    mock_market = MagicMock()
    mock_market.first_sync_attempted = False
    mock_market.first_sync_completed_at = 0.0

    mock_app_state = MagicMock()
    mock_app_state.market = mock_market

    with patch.dict("sys.modules", {"pytest": None}):
        with patch("app_state.app_state", mock_app_state):
            assert scraper._is_startup_ready(force_check=True) is False

            mock_market.first_sync_attempted = True
            mock_market.first_sync_completed_at = time.time() - 5.0
            assert scraper._is_startup_ready(force_check=True) is True


def test_yahoo_jp_scraper_poll_interval_uses_market_state():
    """Smart polling must switch on the live JP market state (holiday-aware)."""
    scraper = YahooJPRealtimeScraper()

    with patch("utils.market_utils.is_market_open", return_value=True):
        assert scraper._poll_interval() == scraper.POLL_INTERVAL_OPEN

    with patch("utils.market_utils.is_market_open", return_value=False):
        assert scraper._poll_interval() == scraper.POLL_INTERVAL_CLOSED




def test_realtime_market_engine_snapshot_and_deltas():
    engine = RealtimeMarketEngine()

    # Initial update
    payload1 = {
        "symbol": "AAPL",
        "price": 220.0,
        "change": 1.0,
        "change_percent": 0.45,
        "volume": 5000,
        "source": "tradingview",
        "updated_at": time.time(),
    }
    engine._handle_producer_update(payload1)

    snapshot = engine.get_market_snapshot()
    assert "AAPL" in snapshot
    assert snapshot["AAPL"]["price"] == 220.0

    # Get deltas (first call should return AAPL)
    deltas1 = engine.get_market_deltas()
    assert "AAPL" in deltas1

    # Second call without price change should return empty deltas
    deltas2 = engine.get_market_deltas()
    assert "AAPL" not in deltas2

    # Update price and check deltas again
    payload2 = dict(payload1)
    payload2["price"] = 222.0
    engine._handle_producer_update(payload2)

    deltas3 = engine.get_market_deltas()
    assert "AAPL" in deltas3
    assert deltas3["AAPL"]["price"] == 222.0


def test_realtime_market_engine_register_symbol():
    """Watchlist additions after startup must reach the right producer."""
    engine = RealtimeMarketEngine()

    engine.register_symbol("TSLA", "us")
    assert "TSLA" in engine.tv_client.symbols

    engine.register_symbol("7203.T", "jp")
    with engine.yahoojp_scraper.lock:
        assert "7203.T" in engine.yahoojp_scraper.symbols

    # Unrelated markets are untouched
    assert "7203.T" not in engine.tv_client.symbols
    with engine.yahoojp_scraper.lock:
        assert "TSLA" not in engine.yahoojp_scraper.symbols


def test_realtime_market_engine_unregister_purges_state():
    """Removing a symbol must unsubscribe and purge its stored quotes."""
    engine = RealtimeMarketEngine()
    engine.register_symbol("TSLA", "us")
    engine.register_symbol("7203.T", "jp")

    tsla_payload = {
        "symbol": "NASDAQ:TSLA",
        "price": 300.0,
        "change": 2.0,
        "change_percent": 0.67,
        "volume": 1000,
        "source": "tradingview",
        "updated_at": time.time(),
    }
    jp_payload = {
        "symbol": "7203.T",
        "price": 3500.0,
        "change": 50.0,
        "change_percent": 1.45,
        "volume": 0,
        "source": "yahoojp",
        "updated_at": time.time(),
    }
    engine._handle_producer_update(tsla_payload)
    engine._handle_producer_update(jp_payload)

    # Unregister the US symbol: subscription removed AND prefixed TV key purged.
    engine.unregister_symbol("TSLA", "us")
    assert "TSLA" not in engine.tv_client.symbols
    snapshot = engine.get_market_snapshot()
    assert "NASDAQ:TSLA" not in snapshot

    # Unregister the JP symbol: subscription removed AND state purged.
    engine.unregister_symbol("7203.T", "jp")
    with engine.yahoojp_scraper.lock:
        assert "7203.T" not in engine.yahoojp_scraper.symbols
    snapshot = engine.get_market_snapshot()
    assert "7203.T" not in snapshot

    # Purged symbols must not reappear in subsequent delta generation.
    assert "NASDAQ:TSLA" not in engine.get_market_deltas()
    assert "7203.T" not in engine.get_market_deltas()


def test_realtime_market_engine_pts_store_and_deltas():
    """PTS quotes live in a separate store and produce their own deltas."""
    engine = RealtimeMarketEngine()
    payload = {
        "symbol": "7203.T",
        "price": 2973.9,
        "change": -9.6,
        "change_percent": -0.32,
        "volume": 600,
        "source": "yahoojp_pts",
        "pts": True,
        "pts_trading": True,
        "pts_time": "17:03",
        "updated_at": time.time(),
    }
    engine._handle_pts_update(payload)

    snapshot = engine.get_pts_snapshot()
    assert "7203.T" in snapshot
    assert snapshot["7203.T"]["pts"] is True

    # First delta pass returns the new quote; unchanged quotes do not repeat.
    deltas1 = engine.get_pts_deltas()
    assert "7203.T" in deltas1
    deltas2 = engine.get_pts_deltas()
    assert "7203.T" not in deltas2

    # Price change produces a new delta.
    payload2 = dict(payload)
    payload2["price"] = 2975.0
    engine._handle_pts_update(payload2)
    deltas3 = engine.get_pts_deltas()
    assert deltas3["7203.T"]["price"] == 2975.0


def test_realtime_market_engine_pts_fallback_chain():
    """PTS fetch must try Yahoo JP first, then fall back to SBI."""
    engine = RealtimeMarketEngine()
    sbi_payload = {
        "symbol": "7203.T",
        "price": 2900.0,
        "change": 0.0,
        "change_percent": 0.0,
        "volume": 0,
        "source": "sbi_pts",
        "pts": True,
        "pts_trading": False,
        "pts_time": "",
        "updated_at": time.time(),
    }
    with (
        patch.object(engine.yahoojp_scraper, "fetch_pts_symbol", return_value=None) as mock_yahoo,
        patch.object(engine.sbi_scraper, "fetch_pts_quote", return_value=sbi_payload) as mock_sbi,
    ):
        payload = engine._fetch_pts_with_fallback("7203.T")
        assert payload is not None
        assert payload["source"] == "sbi_pts"
        mock_yahoo.assert_called_once_with("7203.T")
        mock_sbi.assert_called_once_with("7203.T")

    # Yahoo PTS succeeds → SBI must NOT be consulted.
    yahoo_payload = {
        "symbol": "7203.T",
        "price": 2973.9,
        "change": 0.0,
        "change_percent": 0.0,
        "volume": 0,
        "source": "yahoojp_pts",
        "pts": True,
        "pts_trading": True,
        "pts_time": "17:03",
        "updated_at": time.time(),
    }
    with (
        patch.object(engine.yahoojp_scraper, "fetch_pts_symbol", return_value=yahoo_payload),
        patch.object(engine.sbi_scraper, "fetch_pts_quote") as mock_sbi2,
    ):
        payload = engine._fetch_pts_with_fallback("7203.T")
        assert payload["source"] == "yahoojp_pts"
        mock_sbi2.assert_not_called()


def test_realtime_market_engine_unregister_purges_pts():
    """Removing a JP symbol must also purge its stored PTS quote."""
    engine = RealtimeMarketEngine()
    engine.register_symbol("7203.T", "jp")
    engine._handle_pts_update(
        {
            "symbol": "7203.T",
            "price": 2973.9,
            "change": 0.0,
            "change_percent": 0.0,
            "volume": 0,
            "source": "yahoojp_pts",
            "pts": True,
            "pts_trading": True,
            "pts_time": "17:03",
            "updated_at": time.time(),
        }
    )
    assert "7203.T" in engine.get_pts_snapshot()

    engine.unregister_symbol("7203.T", "jp")
    assert "7203.T" not in engine.get_pts_snapshot()
    assert "7203.T" not in engine.get_pts_deltas()


def test_realtime_market_engine_deltas_are_per_client():
    """Each SSE client cursor must receive every delta independently (R3).

    Previously a single shared ``previous_store`` meant only whichever client
    polled first consumed a price change; the others saw nothing. With
    per-client cursors both connections must get the update.
    """
    engine = RealtimeMarketEngine()
    engine._handle_producer_update(
        {
            "symbol": "AAPL",
            "price": 220.0,
            "change": 1.0,
            "change_percent": 0.45,
            "volume": 5000,
            "source": "tradingview",
            "updated_at": time.time(),
        }
    )

    c1 = engine.register_client()
    c2 = engine.register_client()

    # Both clients see the initial quote on their first poll.
    assert "AAPL" in engine.get_market_deltas(c1)
    assert "AAPL" in engine.get_market_deltas(c2)

    # A price change must be delivered to BOTH clients, not just the first poller.
    engine._handle_producer_update(
        {
            "symbol": "AAPL",
            "price": 221.0,
            "change": 2.0,
            "change_percent": 0.91,
            "volume": 6000,
            "source": "tradingview",
            "updated_at": time.time(),
        }
    )
    d1 = engine.get_market_deltas(c1)
    d2 = engine.get_market_deltas(c2)
    assert d1["AAPL"]["price"] == 221.0
    assert d2["AAPL"]["price"] == 221.0

    # No further change: empty for both.
    assert "AAPL" not in engine.get_market_deltas(c1)
    assert "AAPL" not in engine.get_market_deltas(c2)

    # The default (no client_id) cursor still behaves as before: it consumes
    # its own deltas independently (the initial quote was never seen by it).
    d_default = engine.get_market_deltas()
    assert d_default["AAPL"]["price"] == 221.0
    assert "AAPL" not in engine.get_market_deltas()

    engine.unregister_client(c1)
    engine.unregister_client(c2)


def test_realtime_market_engine_pts_deltas_are_per_client():
    """PTS deltas are also delivered to every registered client (R3)."""
    engine = RealtimeMarketEngine()

    def _pts_payload(price):
        return {
            "symbol": "7203.T",
            "price": price,
            "change": -9.6,
            "change_percent": -0.32,
            "volume": 600,
            "source": "yahoojp_pts",
            "pts": True,
            "pts_trading": True,
            "pts_time": "17:03",
            "updated_at": time.time(),
        }

    engine._handle_pts_update(_pts_payload(2973.9))
    c1 = engine.register_client()
    c2 = engine.register_client()

    assert "7203.T" in engine.get_pts_deltas(c1)
    assert "7203.T" in engine.get_pts_deltas(c2)

    engine._handle_pts_update(_pts_payload(2970.0))
    assert engine.get_pts_deltas(c1)["7203.T"]["price"] == 2970.0
    assert engine.get_pts_deltas(c2)["7203.T"]["price"] == 2970.0

    engine.unregister_client(c1)
    engine.unregister_client(c2)


def test_realtime_market_engine_unregister_client_releases_cursor():
    """Unregistered clients must release their cursors (no leak)."""
    engine = RealtimeMarketEngine()
    cid = engine.register_client()
    assert cid in engine._client_states
    assert cid in engine._client_pts_states

    engine.unregister_client(cid)
    assert cid not in engine._client_states
    assert cid not in engine._client_pts_states
    # Polling with a dropped cursor yields no deltas (client no longer exists).
    assert engine.get_market_deltas(cid) == {}
    assert engine.get_pts_deltas(cid) == {}


def test_realtime_market_engine_unregister_symbol_purges_client_cursors():
    """Removing a symbol must purge it from every client cursor too (R3)."""
    engine = RealtimeMarketEngine()
    engine.register_symbol("7203.T", "jp")
    cid = engine.register_client()

    engine._handle_producer_update(
        {
            "symbol": "7203.T",
            "price": 3500.0,
            "change": 50.0,
            "change_percent": 1.45,
            "volume": 0,
            "source": "yahoojp",
            "updated_at": time.time(),
        }
    )
    assert "7203.T" in engine.get_market_deltas(cid)

    engine.unregister_symbol("7203.T", "jp")
    engine.unregister_client(cid)


def test_kabutan_scraper_fetch():
    """Test Kabutan scraper parsing with mocked HTML response."""
    scraper = YahooJPRealtimeScraper()
    html = """
    <div class="si_i1_2">
        <span class="kabuka">2,983.5円</span>
        <dl class="si_i1_dl1">
            <dt>前日比</dt>
            <dd><span class="up">+69.0</span></dd>
            <dd><span class="up">+2.37</span>%</dd>
        </dl>
    </div>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch.object(scraper.session, "get", return_value=mock_resp):
        payload = scraper.fetch_jp_symbol("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 2983.5
        assert payload["change"] == 69.0
        assert payload["change_percent"] == 2.37
        assert payload["source"] == "kabutan"


def test_kabutan_pts_scraper_fetch():
    """Test Kabutan PTS quote parsing with mocked HTML response."""
    scraper = YahooJPRealtimeScraper()
    html = """
    <div class="si_i1_3">
        <div class="kabuka1">PTS</div>
        <div class="kabuka2">2,986円</div>
        <div class="kabuka3">23:23 08/06</div>
    </div>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch.object(scraper.session, "get", return_value=mock_resp):
        payload = scraper.fetch_pts_symbol("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 2986.0
        assert payload["pts"] is True
        assert payload["pts_trading"] is True
        assert payload["pts_time"] == "23:23 08/06"
        assert payload["source"] == "kabutan_pts"


def test_realtime_engine_registers_saved_user_symbols_on_startup():
    """R2: startup must register saved user symbols with the realtime engine.

    ``load_user_stocks()`` populates ``app_state.market.user_*`` and returns
    ``None``; the startup code previously read the return value, so saved
    symbols were never registered after a restart.
    """
    import app_bg
    from app_state import app_state
    from services.realtime_engine import realtime_market_engine

    with app_state.market.user_stocks_lock:
        original_us = dict(app_state.market.user_us)
        original_jp = dict(app_state.market.user_jp)
        app_state.market.user_us = {"AAPL": "Apple", "MSFT": "Microsoft"}
        app_state.market.user_jp = {"7203.T": "Toyota"}

    registered_tv = []
    registered_jp = []

    def fake_register_symbols(tv_symbols, jp_symbols):
        registered_tv.extend(tv_symbols)
        registered_jp.extend(jp_symbols)

    try:
        with (
            patch.object(app_bg, "load_user_stocks", return_value=None),
            # Prevent _start_background_threads from spawning real daemon
            # threads; only the realtime registration path is under test.
            patch.object(app_bg.threading, "Thread", autospec=True),
            patch.object(
                realtime_market_engine, "register_symbols", side_effect=fake_register_symbols
            ),
            patch.object(realtime_market_engine, "start"),
        ):
            app_bg._start_background_threads()
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = original_us
            app_state.market.user_jp = original_jp

    assert "AAPL" in registered_tv
    assert "MSFT" in registered_tv
    assert "7203.T" in registered_jp


def test_realtime_market_engine_register_symbol_priority_fetch():
    """register_symbol must execute priority fetch without AttributeError."""
    engine = RealtimeMarketEngine()
    mock_payload = {
        "symbol": "7203.T",
        "price": 3500.0,
        "change": 50.0,
        "change_percent": 1.45,
        "volume": 0,
        "source": "yahoojp",
        "updated_at": time.time(),
    }
    with patch.object(engine.yahoojp_scraper, "_fetch_regular_with_fallback", return_value=mock_payload) as mock_fetch:
        engine.register_symbol("7203.T", "jp")
        time.sleep(0.2)
        mock_fetch.assert_called_once_with("7203.T")
        snapshot = engine.get_market_snapshot()
        assert "7203.T" in snapshot
        assert snapshot["7203.T"]["price"] == 3500.0


