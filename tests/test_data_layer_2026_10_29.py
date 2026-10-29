"""Regression coverage for the 2026-10-29 data-layer fixes.

* DATA-2: ``period_requested`` reports the caller's period, not the adjusted
  period used for the yfinance fetch (and legacy cache entries are re-stamped).
* DATA-4: a valid short-cache entry is served even while yfinance is
  rate-limited or the history circuit is open; the gates still block the
  network path. The same ordering is applied to ``get_fast_info``.
* DATA-5: a non-empty merged trends list is reported as success even when one
  source failed; per-source detail is kept in ``trend_source_status``.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pandas as pd

from services.news_service import NewsService
from services.stock.provider import YFinanceProvider
from services.stock_service import fetch_history_sync_impl
from utils.caching import history_short_cache_key, history_short_payload_cache_key


def _daily_frame() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "Open": [1.0, 2.0, 3.0],
            "High": [2.0, 3.0, 4.0],
            "Low": [0.5, 1.5, 2.5],
            "Close": [1.5, 2.5, 3.5],
            "Volume": [10, 20, 30],
        },
        index=index,
    )


def test_period_requested_reports_requested_period_not_adjusted_fetch_period() -> None:
    """1y/5m is fetched as 1mo/5m but the response must still say 1y."""
    payload_key = history_short_payload_cache_key("AAPL", "1y", "5m")
    try:
        with (
            patch("services.stock_service.safe_get_ticker", return_value=MagicMock()),
            patch(
                "services.stock_service._history_with_timeout", return_value=_daily_frame()
            ) as history_mock,
        ):
            result = fetch_history_sync_impl("AAPL", "us", "1y", "5m")
    finally:
        from app_state import app_state

        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(payload_key, None)

    assert result["period_requested"] == "1y"
    assert result["interval_used"] == "5m"
    # The compatibility adjustment still governs the actual yfinance request.
    assert history_mock.call_args.args[0] == "1mo"


def test_period_requested_restamps_legacy_cache_entries() -> None:
    """An entry written with the adjusted value must not leak it to clients."""
    from app_state import app_state

    payload_key = history_short_payload_cache_key("MSFT", "1y", "5m")
    legacy_payload = {
        "symbol": "MSFT",
        "incorrect_but_legacy_field": "1mo",
        "period_requested": "1mo",
        "interval_used": "5m",
        "history": [
            {"x": 1793145600000, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000}
        ],
    }
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache[payload_key] = legacy_payload
    try:
        result = fetch_history_sync_impl("MSFT", "us", "1y", "5m")
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(payload_key, None)

    assert result["period_requested"] == "1y"
    # Sanitization still copies: mutating the response must not touch the cache.
    assert legacy_payload["period_requested"] == "1mo"


def test_history_unavailable_payload_reports_requested_period() -> None:
    """The empty-history payload must also carry the caller's period."""
    payload_key = history_short_payload_cache_key("TSLA", "1y", "5m")
    try:
        with (
            patch("services.stock_service.safe_get_ticker", return_value=MagicMock()),
            patch(
                "services.stock_service._history_with_timeout",
                return_value=pd.DataFrame(),
            ),
        ):
            result = fetch_history_sync_impl("TSLA", "us", "1y", "5m")
    finally:
        from app_state import app_state

        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(payload_key, None)

    assert "error" in result
    assert result["period_requested"] == "1y"


class _FakeMarket:
    def __init__(self, *, circuit_open: bool, rate_limited: bool, cache: dict) -> None:
        self._circuit_open = circuit_open
        self._rate_limited = rate_limited
        self.yfinance_short_cache = cache
        self.yfinance_short_cache_lock = threading.Lock()
        self.history_circuit_state: dict = {}

    def is_circuit_open(self, *_args, **_kwargs) -> bool:
        return self._circuit_open

    def is_yf_rate_limited(self) -> bool:
        return self._rate_limited


def test_provider_serves_cached_history_while_rate_limited_and_circuit_open() -> None:
    """A cache hit performs no network I/O, so availability gates must not hide it."""
    cache_key = history_short_cache_key("AAPL", "3mo", "1d")
    cached = pd.DataFrame({"Close": [1.0, 2.0]})
    fake_market = _FakeMarket(
        circuit_open=True, rate_limited=True, cache={cache_key: cached}
    )
    provider = YFinanceProvider(market_state=fake_market)

    result = provider.get_history("AAPL", "3mo", "1d")

    assert isinstance(result, pd.DataFrame)
    assert result.equals(cached)
    # A copy, not the cached object itself.
    assert result is not cached


def test_provider_still_fails_fast_without_cache() -> None:
    """The gates keep their fail-fast role for uncached symbols."""
    fake_market = _FakeMarket(circuit_open=True, rate_limited=True, cache={})
    provider = YFinanceProvider(market_state=fake_market)

    result = provider.get_history("AAPL", "3mo", "1d")

    assert isinstance(result, pd.DataFrame)
    assert result.empty


def test_fast_info_serves_cached_metadata_while_rate_limited() -> None:
    cached = {"previous_close": 100.0, "currency": "USD"}
    fake_market = _FakeMarket(
        circuit_open=False, rate_limited=True, cache={"fastinfo_AAPL": cached}
    )
    provider = YFinanceProvider(market_state=fake_market)

    assert provider.get_fast_info("AAPL") == cached


def test_fast_info_fails_fast_without_cache() -> None:
    fake_market = _FakeMarket(circuit_open=False, rate_limited=True, cache={})
    provider = YFinanceProvider(market_state=fake_market)

    assert provider.get_fast_info("AAPL") == {}


def _news_patches(trends_side_effect):
    def _context(market, **_kwargs):
        return f"{market} context"

    def _cached_context(_key, loader, *_args):
        return loader()

    def _chat(*_args, **_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"us":"us summary","jp":"jp summary","trends":"trend summary"}'
                    }
                }
            ]
        }

    return (
        patch("services.news_service.collect_market_news_context", side_effect=_context),
        patch(
            "services.news_service.get_cached_context_with_negative_cache",
            side_effect=_cached_context,
        ),
        patch(
            "services.news_service.collect_market_trending_titles",
            side_effect=trends_side_effect,
        ),
        patch("services.news_service.get_cached", side_effect=lambda _k, fn, **_kw: fn()),
        patch("services.news_service.call_mistral_chat", side_effect=_chat),
    )


def test_trends_success_when_one_source_fails_but_the_other_has_items() -> None:
    def trends(market, *_args, **_kwargs):
        if market == "us":
            raise RuntimeError("us trends unavailable")
        return ["JP trend"]

    patches = _news_patches(trends)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = NewsService().get_synchronized_market_news("key", "", "")

    assert result["trending_raw"] == ["JP trend"]
    assert result["retrieve_status"]["trends"] == "success"
    assert result["trend_source_status"] == {"us": "error", "jp": "success"}


def test_trends_error_when_no_source_produces_items() -> None:
    def trends(*_args, **_kwargs):
        raise RuntimeError("trend provider unavailable")

    patches = _news_patches(trends)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = NewsService().get_synchronized_market_news("key", "", "")

    assert result["trending_raw"] == []
    assert result["retrieve_status"]["trends"] == "error"
    assert result["trend_source_status"] == {"us": "error", "jp": "error"}
