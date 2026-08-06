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

    assert len(received) == 1
    assert received[0]["symbol"] == "NASDAQ:AAPL"
    assert received[0]["price"] == 225.5
    assert received[0]["source"] == "tradingview"


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

    # Missing price entirely
    no_lp_json = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:AAPL", "v": {"ch": 1.5}}]})
    client._on_message(MagicMock(), f"~m~{len(no_lp_json)}~m~{no_lp_json}")

    assert received == []

    # A valid quote still flows after the malformed ones
    ok_json = json.dumps({"m": "qsd", "p": ["qs_test", {"n": "NASDAQ:AAPL", "v": {"lp": 225.5}}]})
    client._on_message(MagicMock(), f"~m~{len(ok_json)}~m~{ok_json}")
    assert len(received) == 1
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


def test_is_pts_session():
    mon_pts = datetime(2026, 8, 3, 18, 0, tzinfo=JST)  # Monday 18:00 JST
    assert is_pts_session(mon_pts) is True

    mon_morning = datetime(2026, 8, 3, 10, 0, tzinfo=JST)  # Regular hours
    assert is_pts_session(mon_morning) is False

    sun_pts = datetime(2026, 8, 2, 18, 0, tzinfo=JST)  # Sunday
    assert is_pts_session(sun_pts) is False


def test_yahoo_jp_scraper_structure_change_detection():
    """Consecutive failures must surface a loud one-time warning (page change)."""
    scraper = YahooJPRealtimeScraper()

    with patch("services.realtime_engine.logger") as mock_logger:
        for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD):
            scraper._record_fetch_failure("7203.T")
        mock_logger.error.assert_called_once()

    # A successful scrape resets the counters; failures below the threshold
    # must not re-report.
    scraper._record_fetch_success("7203.T")
    with patch("services.realtime_engine.logger") as mock_logger:
        for _ in range(scraper.STRUCTURE_CHANGE_THRESHOLD - 1):
            scraper._record_fetch_failure("7203.T")
        mock_logger.error.assert_not_called()


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
    # The purge applies to the shared cursor AND the registered client cursor.
    assert "7203.T" not in engine.get_market_deltas()
    assert "7203.T" not in engine.get_market_deltas(cid)

    engine.unregister_client(cid)
