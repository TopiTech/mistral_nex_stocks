"""Regression tests for the 2026-10-21 review fix cycle.

Each test targets a defect that existed before the fix:

* a paused primary source removed the symbol from polling entirely, so the
  fallback providers were never consulted (K-02);
* the PTS worker used an uninterruptible ``time.sleep`` between symbols, so
  ``stop()`` could not join the worker (K-03);
* the TradingView client's symbol-variant fan-out is locked down so a quote
  without a dot is not dispatched twice (the code already deduped by requiring
  a dot for the dashed variant; this is a behaviour lock, not a fixed defect);
* the history circuit breaker was keyed by ``market:symbol`` in the route and
  service layers but by the bare symbol in ``YFinanceProvider`` (D-01);
* a missing market cap was replaced with ``price * volume`` (trading value),
  which is dimensionally wrong (D-03).
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest

import constants
import services.realtime.engine as engine_mod
from services.market_data_service import build_heatmap_payload
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.scrapers import YahooJPRealtimeScraper
from services.realtime.tv_client import TradingViewWSClient
from services.stock_provider import YFinanceProvider


class _FakeFallback:
    """Minimal fallback provider that records calls."""

    def __init__(self, payload: dict | None) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def fetch_quote(self, symbol: str) -> dict | None:
        self.calls.append(symbol)
        return dict(self.payload) if self.payload else None


# ---------------------------------------------------------------------------
# K-02: a paused primary must not disable the fallback chain
# ---------------------------------------------------------------------------


def test_paused_primary_still_uses_fallback(monkeypatch):
    scraper = YahooJPRealtimeScraper()
    fallback = _FakeFallback(
        {"symbol": "7203.T", "price": 2500.0, "change": 10.0, "change_percent": 0.4}
    )
    scraper.fallback_provider = fallback

    def _primary_must_not_run(symbol: str):
        raise AssertionError("paused primary source was called")

    monkeypatch.setattr(scraper, "fetch_jp_symbol", _primary_must_not_run)

    payload = scraper._fetch_regular_with_fallback("7203.T", use_primary=False)

    assert payload is not None
    assert payload["price"] == 2500.0
    assert fallback.calls == ["7203.T"]


def test_engine_pts_fallback_runs_when_primary_paused():
    engine = object.__new__(RealtimeMarketEngine)

    def _primary_must_not_run(symbol: str):
        raise AssertionError("paused PTS primary source was called")

    engine.yahoojp_scraper = SimpleNamespace(fetch_pts_symbol=_primary_must_not_run)
    engine.sbi_scraper = SimpleNamespace(
        fetch_pts_quote=lambda symbol: {"symbol": symbol, "price": 2500.0, "source": "sbi"}
    )
    engine.nikkei225jp_scraper = SimpleNamespace(fetch_pts_quote=lambda symbol: None)
    engine.minkabu_scraper = SimpleNamespace(fetch_pts_quote=lambda symbol: None)

    payload = engine._fetch_pts_with_fallback("7203.T", use_primary=False)

    assert payload is not None
    assert payload["source"] == "sbi"


# ---------------------------------------------------------------------------
# K-03: PTS worker must stop promptly while sleeping between symbols
# ---------------------------------------------------------------------------


def test_pts_worker_stops_promptly_during_inter_symbol_sleep(monkeypatch):
    engine = object.__new__(RealtimeMarketEngine)
    fetched = threading.Event()

    class _Scraper:
        lock: ClassVar[threading.Lock] = threading.Lock()
        symbols: ClassVar[set[str]] = {"7203.T"}

        @staticmethod
        def _is_startup_ready() -> bool:
            return True

        @staticmethod
        def _is_symbol_current(symbol: str) -> bool:
            return True

        @staticmethod
        def _active_symbols(symbols, kind: str = "regular"):
            return list(symbols)

    engine.yahoojp_scraper = _Scraper()
    engine.running = True
    engine._pts_epoch = 0
    engine._last_stale_client_purge = time.time()

    def _fake_fetch(symbol: str, **kwargs):
        fetched.set()
        return {"symbol": symbol, "price": 2500.0, "updated_at": time.time()}

    engine._fetch_pts_with_fallback = _fake_fetch
    engine._handle_pts_update = lambda payload: None
    engine._purge_stale_clients = lambda: None
    engine._pts_cached_payload = lambda symbol: None

    monkeypatch.setattr(engine_mod, "_dedupe_pts_symbols", lambda *a: ["7203.T"])
    monkeypatch.setattr(engine_mod, "is_pts_session", lambda: True)
    monkeypatch.setattr(engine_mod, "_is_scraper_blocked", lambda: False)
    monkeypatch.setattr(engine_mod, "_is_yf_rate_limited", lambda: False)
    monkeypatch.setattr(constants, "SCRAPER_REQUEST_STAGGER_SEC", 2.0)

    worker = threading.Thread(target=engine._pts_worker_loop, daemon=True)
    worker.start()
    assert fetched.wait(timeout=2.0), "PTS worker never fetched a quote"

    engine.running = False
    worker.join(timeout=1.0)

    assert not worker.is_alive(), "PTS worker did not stop within one second"


# ---------------------------------------------------------------------------
# K-05: one quote must produce one callback per distinct symbol form
# ---------------------------------------------------------------------------


def _tv_frame(payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"


def test_tv_quote_without_dot_dispatches_once_per_distinct_symbol():
    received: list[str] = []
    client = TradingViewWSClient(on_update_callback=lambda p: received.append(p["symbol"]))

    frame = _tv_frame(
        {
            "m": "qsd",
            "p": [
                "qs_test",
                {"n": "NASDAQ:AAPL", "v": {"lp": 123.45, "ch": 1.0, "chp": 0.8, "volume": 10}},
            ],
        }
    )
    client._on_message(None, frame)

    assert received == ["NASDAQ:AAPL", "AAPL"]


def test_tv_quote_with_dot_keeps_all_three_forms():
    received: list[str] = []
    client = TradingViewWSClient(on_update_callback=lambda p: received.append(p["symbol"]))

    frame = _tv_frame(
        {
            "m": "qsd",
            "p": [
                "qs_test",
                {"n": "NYSE:BRK.B", "v": {"lp": 400.0, "ch": 1.0, "chp": 0.2, "volume": 5}},
            ],
        }
    )
    client._on_message(None, frame)

    assert received == ["NYSE:BRK.B", "BRK.B", "BRK-B"]


# ---------------------------------------------------------------------------
# D-01: one circuit-key namespace across service, provider, and routes
# ---------------------------------------------------------------------------


def test_history_circuit_key_is_market_qualified():
    from utils.normalization import history_circuit_key

    assert history_circuit_key("7203.T", "jp") == "jp:7203.T"
    assert history_circuit_key("7203.T") == "jp:7203.T"
    assert history_circuit_key("7203") == "jp:7203"
    assert history_circuit_key("AAPL") == "us:AAPL"
    assert history_circuit_key("^N225") == "idx:^N225"


def test_provider_reads_the_same_circuit_key_the_service_reports():
    class _CircuitSpy:
        def __init__(self) -> None:
            self.keys: list[str | None] = []

        yfinance_lock = threading.Lock()

        def is_circuit_open(self, name: str, symbol: str | None = None) -> bool:
            self.keys.append(symbol)
            return symbol == "jp:7203.T"

        @staticmethod
        def is_yf_rate_limited() -> bool:
            return False

    market_state = _CircuitSpy()
    provider = YFinanceProvider(market_state=market_state)

    result = provider.get_history("7203.T", period="1mo")

    assert isinstance(result, pd.DataFrame)
    assert result.empty
    assert market_state.keys == ["jp:7203.T"]


# ---------------------------------------------------------------------------
# D-03: never substitute trading value for market capitalisation
# ---------------------------------------------------------------------------


def test_heatmap_excludes_symbols_without_a_market_cap():
    def _fetch(items, **kwargs):
        return [
            # No shares and no market cap: only trading value is derivable.
            {"symbol": "AAA", "price": 100.0, "volume": 5000},
            # Shares outstanding make the market cap derivable.
            {"symbol": "BBB", "price": 100.0, "sharesOutstanding": 1000},
        ]

    payload = build_heatmap_payload("us", ["AAA", "BBB"], fetch_batch_fn=_fetch)

    assert [row["symbol"] for row in payload["stocks"]] == ["BBB"]
    assert payload["stocks"][0]["market_cap"] == pytest.approx(100_000.0)


def test_heatmap_accepts_snake_case_shares_outstanding():
    """Payload rows carry ``shares_outstanding``; a cap must still be derived."""

    def _fetch(items, **kwargs):
        return [{"symbol": "CCC", "price": 50.0, "shares_outstanding": 200}]

    payload = build_heatmap_payload("us", ["CCC"], fetch_batch_fn=_fetch)

    assert [row["symbol"] for row in payload["stocks"]] == ["CCC"]
    assert payload["stocks"][0]["market_cap"] == pytest.approx(10_000.0)
