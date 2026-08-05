# tests/test_realtime_engine.py
"""Unit tests for Realtime Market Engine (TradingView WS, Yahoo JP, SBI Scraper)."""

import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from services.realtime_engine import (
    RealtimeMarketEngine,
    TradingViewWSClient,
    YahooJPRealtimeScraper,
    is_jp_market_open,
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
