"""Regression tests: raw history disk cache must be keyed by every query dimension.

The fallback path in ``YFinanceProvider.get_history`` serves a disk frame when
the circuit is open, the provider is rate-limited, or the ticker is missing.
That key previously omitted ``interval``, so an intraday request could be
answered with daily candles written by the sync worker (which only fetches
``1d``) while the payload still reported the intraday interval.
"""

from __future__ import annotations

import threading

import pandas as pd
import pytest

from app import app
from app_state import app_state
from services.stock.provider import YFinanceProvider
from utils.caching import history_frame_disk_key, legacy_history_frame_disk_key


class _BlockedMarketState:
    """Market state where the network is unavailable but the cache is usable."""

    def __init__(self) -> None:
        self.yfinance_short_cache: dict[str, object] = {}
        self.yfinance_short_cache_lock = threading.RLock()
        self.yfinance_lock = threading.RLock()
        self.is_yfinance_rate_limited = False
        self.yfinance_429_streak = 0

    def is_circuit_open(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def is_yf_rate_limited(self) -> bool:
        return False


def _frame(close: float) -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": [close], "Volume": [1_000]},
        index=pd.date_range("2026-01-05", periods=1),
    )


def _seed(key: str, close: float) -> None:
    app_state.stock_disk_cache.set(
        key,
        {
            "type": "dataframe",
            "json_data": _frame(close).to_json(orient="split", date_format="iso"),
        },
    )


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> YFinanceProvider:
    ctx = app.app_context()
    ctx.push()
    try:
        instance = YFinanceProvider()
        blocked = _BlockedMarketState()
        monkeypatch.setattr(instance, "_get_market_state", lambda: blocked)
        yield instance
    finally:
        ctx.pop()


def test_intraday_request_is_not_served_the_legacy_daily_frame(
    provider: YFinanceProvider,
) -> None:
    _seed(legacy_history_frame_disk_key("AAPL", "3mo"), 150.0)

    assert provider.get_history("AAPL", "3mo", "5m").empty


def test_daily_request_still_reads_the_legacy_daily_frame(provider: YFinanceProvider) -> None:
    _seed(legacy_history_frame_disk_key("MSFT", "3mo"), 151.0)

    frame = provider.get_history("MSFT", "3mo", "1d")

    assert not frame.empty
    assert float(frame["Close"].iloc[0]) == pytest.approx(151.0)


def test_interval_scoped_entries_are_not_shared_between_intervals(
    provider: YFinanceProvider,
) -> None:
    _seed(history_frame_disk_key("NVDA", "3mo", "5m"), 120.0)

    intraday = provider.get_history("NVDA", "3mo", "5m")
    daily = provider.get_history("NVDA", "3mo", "1d")

    assert not intraday.empty
    assert float(intraday["Close"].iloc[0]) == pytest.approx(120.0)
    assert daily.empty

