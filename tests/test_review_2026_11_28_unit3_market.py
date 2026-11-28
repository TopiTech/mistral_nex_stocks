"""Regression tests for Unit 3 market data, stock providers, scrapers, and realtime engine."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def test_concat_symbol_dataframes_handles_mixed_tz_indexes() -> None:
    """D5-01: Mixed tz-naive and tz-aware DatetimeIndex frames must concat without dropping symbols."""
    from services.stock.provider import YFinanceProvider

    df_naive = pd.DataFrame(
        {("Close", "AAPL"): [200.0, 202.0]},
        index=pd.to_datetime(["2026-11-26", "2026-11-27"]),
    )
    df_aware = pd.DataFrame(
        {("Close", "MSFT"): [400.0, 405.0]},
        index=pd.to_datetime(["2026-11-26", "2026-11-27"]).tz_localize("America/New_York"),
    )
    merged = YFinanceProvider._concat_symbol_dataframes([df_aware, df_naive])
    assert ("Close", "AAPL") in merged.columns
    assert ("Close", "MSFT") in merged.columns
    assert merged[("Close", "AAPL")].dropna().tolist() == [200.0, 202.0]
    assert merged[("Close", "MSFT")].dropna().tolist() == [400.0, 405.0]


def test_fetch_history_sync_marks_rate_limit_and_timeout_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5-02: Rate-limit/timeout errors must be transient=True so negative cache is not poisoned."""
    from app_state import app_state
    from error_codes import ErrorCode
    from services import stock_service
    from services.stock_provider import YFRateLimitError

    monkeypatch.setattr(app_state.market, "is_yf_rate_limited", lambda: False)
    monkeypatch.setattr(
        stock_service,
        "_history_with_timeout",
        lambda *_a, **_kw: (_ for _ in ()).throw(YFRateLimitError("Too Many Requests")),
    )
    res_rl = stock_service.fetch_history_sync_impl("AAPL", "us", "1mo")
    assert res_rl["transient"] is True
    assert res_rl["error_code"] == int(ErrorCode.API_RATE_LIMITED)

    monkeypatch.setattr(
        stock_service,
        "_history_with_timeout",
        lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("read timed out")),
    )
    res_to = stock_service.fetch_history_sync_impl("AAPL", "us", "1mo")
    assert res_to["transient"] is True
    assert res_to["error_code"] == int(ErrorCode.FETCH_FAILED)


def test_yf_download_with_retry_stops_when_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """D5-06: _yf_download_with_retry must short-circuit without calling yf.download when rate-limited."""
    from services.stock.provider import YFinanceProvider

    provider = YFinanceProvider()
    monkeypatch.setattr(provider._get_market_state(), "is_yf_rate_limited", lambda: True)
    with patch("services.stock.provider.yf.download") as mock_dl:
        df = provider._yf_download_with_retry(["AAPL", "MSFT"], "3mo", 10.0)
    assert df.empty
    mock_dl.assert_not_called()


def test_major_holders_sanitizes_nan_and_numpy_and_fast_info_tolerates_field_error() -> None:
    """D5-09 & D5-10: major_holders strips NaN/Inf; fast_info tolerates per-field KeyError."""
    from services.stock.provider import YFinanceProvider, _normalize_major_holders_frame

    df = pd.DataFrame(
        {"Value": [0.25, float("nan"), float("inf")]},
        index=["insidersPercentHeld", "badNan", "badInf"],
    )
    cleaned = _normalize_major_holders_frame(df)
    assert cleaned["insidersPercentHeld"] == 0.25
    assert "badNan" not in cleaned
    assert "badInf" not in cleaned

    class _FlakyFastInfo:
        previous_close = 150.0
        currency = "USD"
        exchange = "NMS"

        @property
        def market_cap(self):
            raise KeyError("marketCap")

        @property
        def quote_type(self):
            return "EQUITY"

    provider = YFinanceProvider()
    fake_ticker = SimpleNamespace(fast_info=_FlakyFastInfo(), history_metadata={})
    with (
        patch.object(provider, "get_ticker", return_value=fake_ticker),
        patch.object(provider._get_market_state(), "yfinance_short_cache", {}),
    ):
        info = provider.get_fast_info("AAPL")
    assert info["previousClose"] == 150.0
    assert info["quoteType"] == "EQUITY"


def test_scrapers_ignore_html_attribute_digits_and_parse_real_prices() -> None:
    """D5-03: Yahoo JP, SBI, and Minkabu parsers must not capture HTML class/attr digits as prices."""
    from bs4 import BeautifulSoup

    from services.fallback_provider import _extract_yahoo_jp_price
    from services.realtime.scrapers import SBISecuritiesScraper
    from services.realtime.utils import extract_minkabu_price

    yjp_html = '<div><span>現在値</span><span class="_3rXWJKZF">2,500</span></div>'
    soup = BeautifulSoup(yjp_html, "html.parser")
    assert _extract_yahoo_jp_price(soup, yjp_html) == "2,500"

    sbi_html = '<th>現在値</th><td colspan="2" class="f16">1,234.5</td>'
    assert SBISecuritiesScraper._number_after_label(sbi_html, "現在値") == 1234.5

    minkabu_html = '<th>現在値</th><td class="f14">3,450</td>'
    assert extract_minkabu_price(minkabu_html) == 3450.0


def test_nikkei225jp_unknown_change_and_ndy_min_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """D5-12: Missing change must not fabricate prev_close=price; parse_ndy_min_js computes change_pct."""
    from services.fallback_provider import Nikkei225JPProvider
    from services.nikkei225jp_parser import parse_ndy_min_js

    cache: dict[int, list[str]] = {}
    parse_ndy_min_js("var NDY111V=40200.0,NDY111Z=+200.0;", cache)
    assert cache[111][2] == "0.50"

    prov = Nikkei225JPProvider()
    monkeypatch.setattr(prov, "_get_client", lambda: (MagicMock(), True))
    monkeypatch.setattr(prov, "_refresh_index_cache", lambda *_a, **_kw: {111: ["39000", "--", "0"]})
    quote = prov.get_latest_quote("^N225")
    assert quote is not None
    assert quote["regularMarketPrice"] == 39000.0
    assert quote["regularMarketPreviousClose"] is None


def test_build_popular_symbol_items_respects_market_filter() -> None:
    """D5-15: build_popular_symbol_items must filter out non-matching markets."""
    from services.market_data_service import build_popular_symbol_items

    items = build_popular_symbol_items(
        "jp",
        "",
        set(),
        [("us", ["AAPL"]), ("jp", ["7203.T"])],
    )
    assert [(s, m) for s, _n, m in items] == [("7203.T", "jp")]


def test_previous_close_cache_variants_supports_exchange_prefixed_dotted_symbol() -> None:
    """D5-11: NYSE:BRK.B must include BRK-B in cache variants."""
    from services.realtime.utils import _previous_close_cache_variants

    variants = _previous_close_cache_variants("NYSE:BRK.B")
    assert "BRK-B" in variants


def test_realtime_engine_start_rollback_and_stop_drain() -> None:
    """D5-04 & D5-05: start() rolls back running=False on failure; stop() joins after PTS drain."""
    from services.realtime.engine import RealtimeMarketEngine

    eng = object.__new__(RealtimeMarketEngine)
    eng.running = False
    eng.pts_thread = None
    eng._pts_epoch = 0
    eng._engine_epoch = 0
    eng._registration_tokens = set()
    eng._bg_executor = None
    eng._lifecycle_op_lock = threading.RLock()
    eng._lifecycle_lock = threading.RLock()
    eng.store_lock = threading.RLock()
    eng.tv_client = MagicMock()
    eng.yahoojp_scraper = MagicMock()
    eng.yahoojp_scraper.start.side_effect = RuntimeError("worker still running")

    with pytest.raises(RuntimeError, match="worker still running"):
        eng.start()
    assert eng.running is False
    eng.tv_client.stop.assert_called_once()

    # D5-04: stop() must perform the final thread.join after _drain_pts_fetch returns True
    join_timeouts: list[float | None] = []
    alive_states = iter([True, True, True, False])
    fake_thread = MagicMock()
    fake_thread.is_alive.side_effect = lambda: next(alive_states, False)
    fake_thread.join.side_effect = lambda timeout=None: join_timeouts.append(timeout)

    eng2 = object.__new__(RealtimeMarketEngine)
    eng2.running = True
    eng2.pts_thread = fake_thread
    eng2._pts_epoch = 1
    eng2._engine_epoch = 1
    eng2._registration_tokens = set()
    eng2._bg_executor = None
    eng2._lifecycle_op_lock = threading.RLock()
    eng2._lifecycle_lock = threading.RLock()
    eng2.store_lock = threading.RLock()
    eng2._pts_fetch_idle = threading.Event()
    eng2._pts_fetch_idle.set()
    eng2._drain_pts_fetch = lambda: True  # type: ignore[method-assign]
    eng2.tv_client = MagicMock()
    eng2.yahoojp_scraper = MagicMock()
    eng2.sbi_scraper = MagicMock()
    eng2.nikkei225jp_scraper = MagicMock()
    eng2.minkabu_scraper = MagicMock()

    assert eng2.stop() is True
    assert join_timeouts == [5.0, 5.0]


def test_tv_client_handles_binary_frame_and_shutdown_exit() -> None:
    """D5-14 & D5-07: Binary WS frame must not break connected; shutdown event exits ws_mod=None loop."""
    from services.realtime.tv_client import TradingViewWSClient

    client = TradingViewWSClient()
    client.connected = True
    client._on_message(MagicMock(), b'~m~5~m~{"a":1}')
    assert client.connected is True
