# tests/test_stock_fetching_resilience.py
"""Tests for stock data fetching resilience and performance optimizations."""

import json
from unittest.mock import patch

from services.fallback_provider import (
    CompositeFallbackProvider,
    YahooJPScraperProvider,
    YahooWebScraperProvider,
    _extract_yahoo_jp_price,
)
from services.realtime_engine import TradingViewWSClient


def test_tradingview_parse_tv_messages_fast():
    """Verify TradingView WS fast message parser parses concatenated ~m~ messages correctly."""
    msg1 = json.dumps(
        {
            "m": "qsd",
            "p": [
                "session",
                {"n": "NASDAQ:AAPL", "v": {"lp": 180.5, "ch": 1.2, "chp": 0.67, "volume": 10000}},
            ],
        }
    )
    msg2 = json.dumps({"m": "ping", "p": [12345]})

    raw_stream = f"~m~{len(msg1)}~m~{msg1}~m~{len(msg2)}~m~{msg2}"

    parsed = TradingViewWSClient.parse_tv_messages(raw_stream)
    assert len(parsed) == 2
    assert parsed[0]["m"] == "qsd"
    assert parsed[0]["p"][1]["n"] == "NASDAQ:AAPL"
    assert parsed[1]["m"] == "ping"


def test_tradingview_parse_tv_messages_invalid():
    """Verify parser gracefully handles malformed frames."""
    raw_stream = "~m~invalid~m~{}"
    parsed = TradingViewWSClient.parse_tv_messages(raw_stream)
    assert parsed == []


def test_yahoo_jp_price_extraction_next_data():
    """Verify _extract_yahoo_jp_price parses __NEXT_DATA__ script content."""
    from bs4 import BeautifulSoup

    html = """
    <html>
      <head>
        <script id="__NEXT_DATA__" type="application/json">
          {"props": {"pageProps": {"priceData": {"price": 3250.0}}}}
        </script>
      </head>
      <body></body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    price = _extract_yahoo_jp_price(soup, html)
    assert price == "3250.0"


def test_yahoo_jp_price_extraction_selector_fallback():
    """Verify _extract_yahoo_jp_price falls back to CSS selectors when __NEXT_DATA__ is missing."""
    from bs4 import BeautifulSoup

    html = '<html><body><span data-testid="stock-price">1,450.5</span></body></html>'
    soup = BeautifulSoup(html, "html.parser")
    price = _extract_yahoo_jp_price(soup, html)
    assert price == "1,450.5"


def test_fetch_history_fallback_candle_anchored_to_utc_midnight():
    """R2: the synthetic fallback candle must use a tz-aware UTC index.

    A naive index would be interpreted in the server's local timezone by
    ``Timestamp.timestamp()``, shifting the chart x-axis by the TZ offset.
    """
    import datetime

    import pandas as pd

    from app_state import app_state
    from services import stock_service

    fixed = datetime.datetime(2026, 7, 3, 10, 30, 0, tzinfo=datetime.UTC)

    class _FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    fallback_quote = {
        "regularMarketOpen": 150.0,
        "regularMarketDayHigh": 155.0,
        "regularMarketDayLow": 149.0,
        "regularMarketPrice": 154.0,
        "regularMarketVolume": 100000,
    }

    with (
        patch.object(stock_service, "datetime", _FixedDateTime),
        patch.object(stock_service, "_history_with_timeout", return_value=pd.DataFrame()),
        patch.object(app_state.fallback_provider, "get_latest_quote", return_value=fallback_quote),
    ):
        result = stock_service.fetch_history_sync_impl("AAPL", "us", "1d")

    assert "error" not in result, result
    assert result["interval_used"] == "1d"
    expected_x = int(pd.Timestamp("2026-07-03", tz="UTC").timestamp() * 1000)
    assert result["history"][0]["x"] == expected_x


def test_fetch_history_nonfinite_provider_values_are_json_safe():
    """Provider NaN/Inf values must be normalized before API serialization."""
    import math

    import pandas as pd

    from services import stock_service

    index = pd.date_range("2026-01-05", periods=2, tz="UTC")
    history = pd.DataFrame(
        {
            "Open": [100.0, float("inf")],
            "High": [float("nan"), 105.0],
            "Low": [95.0, float("-inf")],
            "Close": [100.0, 102.0],
            "Volume": [float("inf"), 1500],
        },
        index=index,
    )

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=history),
        patch.object(stock_service, "safe_get_ticker", return_value=True),
    ):
        result = stock_service.fetch_history_sync_impl("NONFINITE", "us", "1d")

    assert "error" not in result, result
    json.dumps(result, allow_nan=False)
    for row in result["history"]:
        for key in ("o", "h", "l", "c"):
            assert math.isfinite(row[key])
    assert result["history"][0]["v"] == 0
    assert result["history"][1]["v"] == 1500


def test_fallback_providers_use_session():
    """Verify Yahoo scrapers support persistent HTTP sessions."""
    y_us = YahooWebScraperProvider()
    assert hasattr(y_us, "session")

    y_jp = YahooJPScraperProvider()
    assert hasattr(y_jp, "session")


def test_composite_fallback_provider():
    """Test CompositeFallbackProvider routing."""
    composite = CompositeFallbackProvider()

    with (
        patch.object(composite.alpha_vantage, "get_latest_quote", return_value=None),
        patch.object(
            composite.yahoo_jp, "get_latest_quote", return_value={"regularMarketPrice": 2500.0}
        ) as mock_jp,
    ):
        res = composite.get_latest_quote("7203.T")
        assert res is not None
        assert res["source"] == "yahoojp"
        assert res["regularMarketPrice"] == 2500.0
        mock_jp.assert_called_once_with("7203.T")


def test_fallback_future_timeout_logs_late_failure():
    """P6: a fallback Future that finishes after the wait() timeout must have
    its exception consumed and logged, not silently discarded."""
    import concurrent.futures
    import logging

    import pandas as pd

    import app_bg as _app_bg
    from bg.sync_worker import fetch_stocks_batch as real_fetch

    late_future = concurrent.futures.Future()
    late_future.set_exception(ValueError("boom after timeout"))

    recorded = []

    class _FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            return late_future

    # Batch download "succeeds" but extraction yields no usable history for
    # TEST1, so the per-symbol fallback path (which submits to the executor)
    # is reached.
    with (
        patch.object(_app_bg, "fetch_stocks_batch", real_fetch),
        patch("app_bg.app_state.execution.data_executor", _FakeExecutor()),
        patch("app_bg.acquire_yfinance_slot", return_value=True),
        patch(
            "app_bg.app_state.stock_provider.download_batch",
            return_value=pd.DataFrame({"Close": [1.0]}),
        ),
        patch("app_bg.extract_batch_history", return_value=pd.DataFrame()),
        patch("concurrent.futures.wait", return_value=(set(), {late_future})),
        patch.object(
            logging.getLogger("app_bg"), "warning", side_effect=lambda *a, **k: recorded.append(a)
        ),
    ):
        results = real_fetch([("TEST1", "Test", "us")])

    # wait() returned no done futures, so each item maps to None (not removable).
    assert results == [None]
    # The done-callback must consume the future's exception and log it.
    assert any("failed late" in str(r) for r in recorded), f"no late-failure log: {recorded}"

    # Restore the conftest stub so subsequent tests keep the no-network guarantee.
    _app_bg.fetch_stocks_batch = lambda items, snapshot_ts_ms=None, **kwargs: []  # type: ignore[assignment]


def test_semaphore_timeout_marked_as_transient():
    """Verify that TimeoutError on semaphore acquisition returns a transient error flag."""
    from services import stock_service

    with patch.object(
        stock_service,
        "_history_with_timeout",
        side_effect=TimeoutError("Timed out waiting for history semaphore (server overloaded)"),
    ):
        res = stock_service.fetch_history_sync_impl("MSFT", "us", "3mo")

    assert isinstance(res, dict)
    assert "error" in res
    assert res.get("transient") is True
    assert res["symbol"] == "MSFT"


def test_transient_error_not_stored_in_negative_cache():
    """Verify that transient semaphore timeout errors are NOT written into negative cache."""
    from services import stock_service
    from utils.caching import _get_cached_value, clear_cache_key

    cache_key = "hist_MSFT_us_3mo_test_transient"
    clear_cache_key(cache_key)

    transient_err = {
        "error": "一時的なエラー",
        "error_code": 1004,
        "symbol": "MSFT",
        "transient": True,
    }

    with patch.object(stock_service, "fetch_history_sync_impl", return_value=transient_err):
        stock_service.fetch_history_async_task("MSFT", "us", "3mo", cache_key, 60)

    # Negative cache should NOT be populated for transient error
    cached = _get_cached_value(cache_key, 60)
    assert cached is None


def test_permanent_error_stored_in_negative_cache():
    """Verify that non-transient errors ARE written into negative cache."""
    from services import stock_service
    from utils.caching import _get_cached_value, clear_cache_key

    cache_key = "hist_NONEXISTENT_us_3mo_test_perm"
    clear_cache_key(cache_key)

    permanent_err = {
        "error": "銘柄情報が取得できませんでした。",
        "error_code": 1004,
        "symbol": "NONEXISTENT",
        "transient": False,
    }

    with patch.object(stock_service, "fetch_history_sync_impl", return_value=permanent_err):
        stock_service.fetch_history_async_task("NONEXISTENT", "us", "3mo", cache_key, 60)

    # Negative cache SHOULD be populated for non-transient error
    cached = _get_cached_value(cache_key, 60)
    assert cached is not None
    assert cached.get("error") == "銘柄情報が取得できませんでした。"
    clear_cache_key(cache_key)


def test_history_semaphore_capacity_matches_constant():
    """Verify MarketDataState initializes yfinance_history_semaphore with HISTORY_SEMAPHORE_CAPACITY."""
    from constants import HISTORY_SEMAPHORE_CAPACITY
    from market_state import MarketDataState

    m = MarketDataState()
    assert m.yfinance_history_semaphore is not None
    # Acquire all slots to verify the semaphore count
    slots = []
    for _ in range(HISTORY_SEMAPHORE_CAPACITY):
        assert m.yfinance_history_semaphore.acquire(blocking=False) is True
        slots.append(True)
    # The next acquire must fail (all capacity exhausted)
    assert m.yfinance_history_semaphore.acquire(blocking=False) is False
    for _ in slots:
        m.yfinance_history_semaphore.release()
