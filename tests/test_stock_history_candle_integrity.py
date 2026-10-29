# tests/test_stock_history_candle_integrity.py
"""Regression coverage for the history-candle contract (DATA-1).

A non-finite OHLC value must never be turned into a fabricated ``0.0`` candle
or persisted to the disk cache.  Missing OHLC columns are a different case: the
bar is synthesized flat at the close.  The tests exercise the real serializer,
the real async persistence path, and the HTTP disk-cache read path.
"""

import json
from unittest.mock import patch

import pandas as pd

from app_state import app_state
from services import stock_service


def _frame(rows, columns=("Open", "High", "Low", "Close", "Volume")):
    index = pd.date_range("2026-01-05", periods=len(rows), tz="UTC")
    return pd.DataFrame(rows, columns=list(columns), index=index)


def test_fetch_history_drops_nonfinite_bars_and_keeps_valid_ones():
    history = _frame(
        [
            (100.0, 105.0, 95.0, 100.0, 1000.0),
            (float("inf"), 105.0, 95.0, 102.0, 2000.0),
            (101.0, float("nan"), 95.0, 103.0, 3000.0),
        ]
    )

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=history),
        patch.object(stock_service, "safe_get_ticker", return_value=object()),
    ):
        result = stock_service.fetch_history_sync_impl("CANDLE_MIXED", "us", "1mo", "1d")

    assert "error" not in result, result
    assert len(result["history"]) == 1
    candle = result["history"][0]
    assert candle == {"x": candle["x"], "o": 100.0, "h": 105.0, "l": 95.0, "c": 100.0, "v": 1000}
    assert candle["o"] != 0.0 and candle["h"] != 0.0
    json.dumps(result, allow_nan=False)


def test_fetch_history_missing_ohlc_columns_uses_flat_close_bar():
    history = _frame(
        [(100.0, 1000.0), (102.0, 2000.0)],
        columns=("Close", "Volume"),
    )

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=history),
        patch.object(stock_service, "safe_get_ticker", return_value=object()),
    ):
        result = stock_service.fetch_history_sync_impl("CANDLE_CLOSE_ONLY", "us", "1mo", "1d")

    assert "error" not in result, result
    assert len(result["history"]) == 2
    for candle in result["history"]:
        assert candle["o"] == candle["c"]
        assert candle["h"] == candle["c"]
        assert candle["l"] == candle["c"]


def test_fetch_history_all_invalid_bars_returns_error_and_persists_nothing():
    history = _frame(
        [
            (float("nan"), float("nan"), float("nan"), 100.0, 1000.0),
            (float("inf"), 105.0, -float("inf"), 102.0, 2000.0),
        ]
    )

    cache_key = "hist_CANDLE_ALL_BAD_us_1mo"
    app_state.stock_disk_cache.delete(cache_key)

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=history),
        patch.object(stock_service, "safe_get_ticker", return_value=object()),
    ):
        result = stock_service.fetch_history_sync_impl("CANDLE_ALL_BAD", "us", "1mo", "1d")
        assert "error" in result, result
        stock_service.fetch_history_async_task("CANDLE_ALL_BAD", "us", "1mo", cache_key, 3600)

    assert app_state.stock_disk_cache.get(cache_key) is None


def test_real_fetch_then_disk_roundtrip_contains_only_valid_candles():
    history = _frame(
        [
            (100.0, 105.0, 95.0, 100.0, 1000.0),
            (float("inf"), 105.0, 95.0, 102.0, 2000.0),
        ]
    )
    cache_key = "hist_CANDLE_ROUNDTRIP_us_1mo"
    app_state.stock_disk_cache.delete(cache_key)

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=history),
        patch.object(stock_service, "safe_get_ticker", return_value=object()),
    ):
        stock_service.fetch_history_async_task(
            "CANDLE_ROUNDTRIP", "us", "1mo", cache_key, 3600
        )

    stored = app_state.stock_disk_cache.get(cache_key)
    assert isinstance(stored, dict), stored
    assert len(stored["history"]) == 1
    candle = stored["history"][0]
    assert candle["o"] == 100.0 and candle["c"] == 100.0
    assert candle["o"] != 0.0
    json.dumps(stored, allow_nan=False)


def test_async_writer_rejects_poisoned_payload_from_a_future_producer():
    """The shared pre-cache choke point must sanitize, not merely trust callers."""
    cache_key = "hist_CANDLE_FUTURE_us_1mo"
    app_state.stock_disk_cache.delete(cache_key)
    poisoned = {
        "symbol": "CANDLE_FUTURE",
        "history": [
            {"x": 1, "o": 0.0, "h": 0.0, "l": 0.0, "c": 0.0, "v": 0},
            {"x": 2, "o": 10.0, "h": 12.0, "l": 9.0, "c": 11.0, "v": 100},
        ],
    }

    with patch.object(stock_service, "fetch_history_sync_impl", return_value=poisoned):
        stock_service.fetch_history_async_task("CANDLE_FUTURE", "us", "1mo", cache_key, 3600)

    stored = app_state.stock_disk_cache.get(cache_key)
    assert isinstance(stored, dict), stored
    assert stored["history"] == [{"x": 2, "o": 10.0, "h": 12.0, "l": 9.0, "c": 11.0, "v": 100}]


def test_route_drops_preexisting_poisoned_disk_cache(client):
    cache_key = "hist_CANDLE_POISON_us_1mo"
    app_state.stock_disk_cache.set(
        cache_key,
        {
            "symbol": "CANDLE_POISON",
            "history": [{"x": 1, "o": 0.0, "h": 0.0, "l": 0.0, "c": 0.0, "v": 0}],
        },
    )

    with patch("routes.stocks.quotes._submit_async_history_fetch", return_value=True):
        response = client.get("/api/stock-history?symbol=CANDLE_POISON&market=us&period=1mo")

    assert response.status_code == 200
    data = response.get_json()
    assert data["history"] == []
    assert data.get("fetching") is True
    assert app_state.stock_disk_cache.get(cache_key) is None
