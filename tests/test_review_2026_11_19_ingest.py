# tests/test_review_2026_11_19_ingest.py
"""Regression tests for realtime ingestion defects found in the 2026-11-19 review."""

import json
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from bg.sse_interpolator import _fluctuate_indices, _interpolate_and_fluctuate_market
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.tv_client import TradingViewWSClient


@pytest.fixture()
def engine():
    eng = RealtimeMarketEngine()
    try:
        yield eng
    finally:
        eng._bg_executor.shutdown(wait=True)


def _tv_quote_frame(symbol: str, price: float) -> str:
    body = json.dumps({"m": "qsd", "p": ["qs_test", {"n": symbol, "v": {"lp": price}}]})
    return f"~m~{len(body)}~m~{body}"


def test_tv_parser_skips_oversized_and_non_ascii_length_tokens():
    """An upstream length token must never raise out of int() (Python 3.11+ limit)."""
    valid_frame = _tv_quote_frame("NASDAQ:AAPL", 225.5)
    oversized = "~m~" + ("9" * 5000) + "~m~" + valid_frame
    non_ascii = "~m~" + ("\u0663" * 80) + "~m~" + valid_frame

    parsed = TradingViewWSClient.parse_tv_messages(oversized)
    assert parsed == [json.loads(valid_frame.split("~m~")[2])]
    assert TradingViewWSClient.parse_tv_messages(non_ascii) == parsed


def test_tv_on_message_survives_malformed_frames_and_keeps_next_quote():
    received: list[dict[str, Any]] = []
    client = TradingViewWSClient(on_update_callback=received.append)

    # One truncated prefix, one oversized length token, then a valid quote.
    message = "~m~" + ("9" * 5000) + "~m~" + _tv_quote_frame("NASDAQ:NVDA", 120.0)
    client._on_message(MagicMock(), message)

    assert [item["symbol"] for item in received] == ["NASDAQ:NVDA", "NVDA"]


def test_unsubscribed_symbol_is_not_resurrected_by_inflight_quote():
    """remove_symbol() racing with in-flight quote handling must win."""
    received: list[dict[str, Any]] = []
    client = TradingViewWSClient(on_update_callback=received.append)
    client.symbols.add("NASDAQ:AAPL")
    client._subscriptions_managed = True

    class _ConcurrentRemovalDict(dict):
        """Simulate remove_symbol() completing right after the quote read."""

        def get(self, key, default=None):
            client.symbols.discard(key)
            return super().get(key, default)

    client._last_quotes = _ConcurrentRemovalDict()

    client._on_message(MagicMock(), _tv_quote_frame("NASDAQ:AAPL", 225.5))

    assert received == []
    assert "NASDAQ:AAPL" not in client._last_quotes


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"symbol": None},
        {"symbol": 123},
        {"symbol": "   "},
        {"symbol": "A" * 33},
        cast(Any, None),
        cast(Any, []),
    ],
)
def test_market_producer_drops_malformed_payloads(engine, payload):
    engine._handle_producer_update(payload)

    assert engine.market_store == {}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"symbol": None},
        {"symbol": ""},
        cast(Any, None),
        cast(Any, "7203.T"),
    ],
)
def test_pts_producer_drops_malformed_payloads(engine, payload):
    engine._handle_pts_update(payload)

    assert engine.pts_store == {}


def test_market_producer_stores_valid_payload(engine):
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=100.0):
        engine._handle_producer_update(
            {"symbol": "  AAPL  ", "price": 101.0, "volume": 10, "market": "us"}
        )

    stored = engine.market_store["AAPL"]
    assert stored["symbol"] == "AAPL"
    assert stored["price"] == 101.0
    assert stored["change"] == 1.0


def test_pts_producer_stores_valid_payload(engine):
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=2490.0):
        engine._handle_pts_update(
            {"symbol": "7203.T", "price": 2500.0, "volume": 5, "market": "jp"}
        )

    stored = engine.pts_store["7203.T"]
    assert stored["price"] == 2500.0
    assert stored["change"] == 10.0


def test_interpolated_market_change_matches_published_price():
    target = [{"symbol": "7203.T", "price": 2500.564, "change": 10.567, "currency": "JPY"}]

    with patch("bg.sse_interpolator._get_simulate_fluctuation", return_value=False):
        result = _interpolate_and_fluctuate_market(target, [], True, "jp")

    item = result[0]
    previous_close = 2500.564 - 10.567
    expected_change = item["price"] - previous_close
    assert item["price"] == 2500.56
    assert item["change"] == round(expected_change, 2)
    assert item["change"] == 10.56
    assert item["change_percent"] == round((expected_change / previous_close) * 100, 2)


def test_fluctuated_index_change_matches_published_price():
    indices = {"N225": {"price": 30000.564, "change": 123.456}}

    with (
        patch("bg.sse_interpolator._get_simulate_fluctuation", return_value=True),
        patch("bg.sse_interpolator.random.random", return_value=0.0),
        patch("bg.sse_interpolator.random.uniform", return_value=0.0),
    ):
        _fluctuate_indices(indices, us_open=False, jp_open=True)

    info = indices["N225"]
    previous_close = 30000.564 - 123.456
    expected_change = info["price"] - previous_close
    assert info["price"] == 30000.56
    assert info["change"] == round(expected_change, 2)
    assert info["change"] == 123.45
    assert info["percent"] == round((expected_change / previous_close) * 100, 2)
