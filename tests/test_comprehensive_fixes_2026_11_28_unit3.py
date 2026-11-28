"""Unit 3 comprehensive review fixes regression tests (2026-11-28)."""

from __future__ import annotations

import builtins
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def test_stock_service_allows_raw_cache_and_fallback_during_yf_rate_limit() -> None:
    from app_state import app_state
    from services import stock_service

    # 1) Raw DataFrame in yfinance_short_cache is served during rate-limit cooldown
    df = pd.DataFrame(
        {
            "Open": [100.0, 102.0],
            "High": [103.0, 105.0],
            "Low": [99.0, 101.0],
            "Close": [102.0, 104.0],
            "Volume": [1000, 1200],
        },
        index=pd.to_datetime(["2026-11-26", "2026-11-27"], utc=True),
    )
    raw_key = stock_service._history_short_cache_key("AAPL", "1d", "5m")
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()
        app_state.yfinance_short_cache[raw_key] = df

    try:
        with (
            patch.object(stock_service, "safe_get_ticker", return_value=None),
            patch.object(app_state.market, "is_negative_cached_symbol", return_value=False),
            patch.object(app_state.market, "is_yf_rate_limited", return_value=True),
            patch.object(app_state.market, "is_circuit_open", return_value=False),
        ):
            res = stock_service.fetch_history_sync_impl("AAPL", "us", "1d", "5m")
        assert "error" not in res
        assert res["symbol"] == "AAPL"
        assert len(res["history"]) == 2
        assert res["history"][-1]["c"] == 104.0
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.clear()

    # 2) Cache miss during rate limit falls through to fallback_provider.get_latest_quote for 1d
    with (
        patch.object(stock_service, "safe_get_ticker", return_value=None),
        patch.object(app_state.market, "is_negative_cached_symbol", return_value=False),
        patch.object(app_state.market, "is_yf_rate_limited", return_value=True),
        patch.object(
            app_state.fallback_provider,
            "get_latest_quote",
            return_value={
                "regularMarketPrice": 150.25,
                "regularMarketOpen": 149.0,
                "regularMarketDayHigh": 151.0,
                "regularMarketDayLow": 148.5,
                "regularMarketVolume": 5000,
            },
        ),
    ):
        res_fb = stock_service.fetch_history_sync_impl("MSFT", "us", "1d", "auto")
    assert "error" not in res_fb
    assert res_fb["symbol"] == "MSFT"
    assert res_fb["history"][-1]["c"] == 150.25


def test_stock_provider_imports_survive_missing_yfinance_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yfinance.exceptions":
            raise ImportError("simulated older yfinance without exceptions module")
        return real_import(name, globals, locals, fromlist, level)

    saved_errors = sys.modules.pop("services.stock.errors", None)
    saved_provider = sys.modules.pop("services.stock_provider", None)
    try:
        monkeypatch.setattr(builtins, "__import__", _guarded_import)
        errors_mod = importlib.import_module("services.stock.errors")
        provider_mod = importlib.import_module("services.stock_provider")
        assert provider_mod.YFPricesMissingError is errors_mod.YFPricesMissingError
        assert provider_mod.YFTickerMissingError is errors_mod.YFTickerMissingError
    finally:
        monkeypatch.undo()
        sys.modules.pop("services.stock_provider", None)
        sys.modules.pop("services.stock.errors", None)
        if saved_errors is not None:
            sys.modules["services.stock.errors"] = saved_errors
        if saved_provider is not None:
            sys.modules["services.stock_provider"] = saved_provider


def test_fallback_provider_yahoo_block_marks_session_manager_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import session_manager as sm
    from app_state import app_state
    from services.fallback_provider import _mark_yahoo_block

    mark_calls: list[str] = []
    monkeypatch.setattr(
        sm.yf_session_manager,
        "mark_rate_limited",
        lambda *_a, **_kw: mark_calls.append("rate_limited"),
    )
    with app_state.market.scraper_block_lock:
        app_state.market.scraper_block_until = 0.0
        app_state.market.scraper_block_streak = 0
    with app_state.market.yfinance_lock:
        app_state.market.yfinance_rate_limit_until = 0.0
        app_state.market.yfinance_429_streak = 0

    resp = SimpleNamespace(headers={"Retry-After": "12"})
    _mark_yahoo_block(429, "Too Many Requests", is_yahoo_host=True, response=resp)
    assert len(mark_calls) == 1

    with app_state.market.scraper_block_lock:
        app_state.market.scraper_block_until = 0.0
        app_state.market.scraper_block_streak = 0
    with app_state.market.yfinance_lock:
        app_state.market.yfinance_rate_limit_until = 0.0
        app_state.market.yfinance_429_streak = 0


def test_realtime_scrapers_forward_response_for_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.realtime import scrapers

    recorded_responses: list[object] = []

    def _capture_block(*_args, **kwargs):
        recorded_responses.append(kwargs.get("response"))

    monkeypatch.setattr(scrapers, "_mark_scraper_blocked_from_status", _capture_block)
    monkeypatch.setattr(scrapers, "_is_scraper_blocked", lambda: False)

    fake_resp = MagicMock()
    fake_resp.status_code = 429
    fake_resp.headers = {"Retry-After": "90"}
    fake_sess = SimpleNamespace(get=lambda *_a, **_kw: fake_resp)

    sbi = scrapers.SBISecuritiesScraper()
    monkeypatch.setattr(sbi, "_get_session", lambda: fake_sess)
    assert sbi._fetch_page("7203.T") is None

    nikkei = scrapers.Nikkei225JPScraper()
    monkeypatch.setattr(nikkei, "_get_session", lambda: fake_sess)
    nikkei._adr_cache_time = 0.0
    nikkei._refresh_adr_cache()
    monkeypatch.setattr(nikkei, "_refresh_adr_cache", lambda max_age=10.0: {})
    assert nikkei.fetch_quote("7203.T") is None

    minkabu = scrapers.MinkabuScraper()
    monkeypatch.setattr(minkabu, "_get_session", lambda: fake_sess)
    assert minkabu.fetch_quote("7203.T") is None

    yahoo_jp = scrapers.YahooJPRealtimeScraper()
    monkeypatch.setattr(yahoo_jp, "_get_session", lambda: fake_sess)
    assert yahoo_jp._fetch_kabutan_symbol("7203.T") is None
    assert yahoo_jp._fetch_kabutan_pts_symbol("7203.T") is None

    assert len(recorded_responses) == 6
    assert all(r is fake_resp for r in recorded_responses)
