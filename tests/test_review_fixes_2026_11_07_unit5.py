"""Regression tests for the 2026-11-07 exhaustive review fixes (unit 5).

Provider/data fixes pinned here:

- H5: yfinance >= 1.6 returns history metadata as a HistoryMetadata Mapping,
  not a dict — every isinstance(metadata, dict) guard (currency/exchange
  extraction in services/stock/provider.py, live market-state detection in
  utils/market_utils.py) was dead in production, so live market detection
  silently degraded to the static heuristic and holiday closures were
  misreported as REGULAR.
- M20: is_market_open never negative-cached a failed live fetch, so the SSE
  interpolator's 2-second loops re-fired a yfinance probe per call during a
  Yahoo outage — one Google probe landing in the app-wide limiter locks out
  real fetches.
- M21: fallback _to_int/_quote_int raised OverflowError for extreme JSON
  integers, aborting an otherwise usable quote.
- M22: the AlphaVantage and Yahoo US fallback paths faked unknown OHLC as
  the current price, turning stale fallback reads into a fabricated 0.00%
  "flat market" (the JP-tier providers deliberately return None).
- M24: fetch_stocks_batch keyed results by bare symbol though items are
  (symbol, name, market) triples — the same symbol scheduled under two
  markets collided and one market's payload was silently replaced.
- M39: bg/sync's yfinance error handler drifted from the canonical
  classifier; unrecognized rate-limit markers fell to the else branch and
  reset the graduated 429 streak.
- M38: the yfinance error shim only protected YFRateLimitError; a yfinance
  build lacking the older ticker/prices classes would fail module import.
- M12: a new TrendReq client (live cookie fetch) was built per keyword with
  a retry budget far exceeding the caller's 12-second collection window.
- M15: the SBI scraper could not parse Japanese ▲/▼ negative change marks,
  reading a negative day-change as its positive magnitude.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from app import app_state

# ---------------------------------------------------------------------------
# H5: Mapping-typed yfinance history metadata
# ---------------------------------------------------------------------------


class _MappingMetadata(Mapping):
    """Stub mirroring yfinance's HistoryMetadata: a Mapping, never a dict."""

    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


def test_market_state_from_metadata_accepts_yf_mapping_type():
    """Live market-state detection must read the Mapping wrapper."""
    from utils.market_utils import _market_state_from_metadata

    meta = _MappingMetadata({"marketState": "CLOSED"})
    assert not isinstance(meta, dict), "fixture must mirror the production non-dict type"
    assert isinstance(meta, Mapping)
    assert _market_state_from_metadata(meta) == "CLOSED"
    assert _market_state_from_metadata(_MappingMetadata({"marketState": "REGULAR"})) == "REGULAR"
    assert _market_state_from_metadata(None) is None


def test_provider_metadata_guards_accept_mapping_type():
    """Every yfinance metadata guard in the provider must accept Mapping.

    yfinance >= 1.6 wraps history metadata in a HistoryMetadata Mapping that
    is NOT a dict; the previous plain-dict guards made the currency/exchange
    fallbacks permanently inert (fixture mirrors the production type).
    """
    import re as _re

    from services.stock import provider

    source = Path(provider.__file__).read_text(encoding="utf-8")
    guards = _re.findall(r"isinstance\(metadata, (\([^)]*\))\)", source)
    assert guards, "expected metadata isinstance guards in provider.py"
    for guard in guards:
        assert "Mapping" in guard, f"guard {guard!r} must accept Mapping"

    meta = _MappingMetadata({"currency": "EUR"})
    assert isinstance(meta, (dict, Mapping))
    assert not isinstance(meta, dict)


# ---------------------------------------------------------------------------
# M20: negative cache for failed live market-state probes
# ---------------------------------------------------------------------------


def test_is_market_open_negative_caches_failed_probe(monkeypatch):
    """A failed live fetch must not re-probe on every 2-second tick."""
    from utils import market_utils

    calls = {"n": 0}

    def _failing_fetch(market_type):
        calls["n"] += 1

    monkeypatch.setattr(market_utils, "_fetch_live_market_state", _failing_fetch)
    # Prime/clear the caches for this market.
    from utils.caching import global_cache

    with global_cache.cache_lock:
        for bucket in list(global_cache.caches.values()):
            bucket.pop("market_state_us__negative", None)
            bucket.pop("market_state_us", None)

    for _ in range(3):
        market_utils.is_market_open("us", ignore_weekend=True)

    assert calls["n"] == 1, "failed probes must be negative-cached (60s TTL)"
    # The time-based heuristic still answers.
    assert isinstance(market_utils.is_market_open("us", ignore_weekend=True), bool)


def test_is_market_open_success_probe_cached_under_valid_key(monkeypatch):
    from utils import market_utils

    calls = {"n": 0}

    def _ok_fetch(market_type):
        calls["n"] += 1
        return "REGULAR"

    monkeypatch.setattr(market_utils, "_fetch_live_market_state", _ok_fetch)
    from utils.caching import global_cache

    with global_cache.cache_lock:
        for bucket in list(global_cache.caches.values()):
            bucket.pop("market_state_us__negative", None)
            bucket.pop("market_state_us", None)

    assert market_utils.is_market_open("us", ignore_weekend=True) is True
    assert market_utils.is_market_open("us", ignore_weekend=True) is True
    assert calls["n"] == 1


def test_is_market_open_does_not_negative_cache_in_flight_fetch(monkeypatch):
    """CACHE_FETCHING means another thread is fetching, not that the probe failed.

    Negative-caching the sentinel suppressed the live result the concurrent
    fetch was about to store for the whole 60s TTL (same class as R3 in
    get_cached_context_with_negative_cache).
    """
    from utils import market_utils
    from utils.caching import CACHE_FETCHING

    monkeypatch.setattr(market_utils, "_has_cached_key", lambda *_a, **_k: False)
    monkeypatch.setattr(market_utils, "get_cached", lambda *_a, **_k: CACHE_FETCHING)
    writes = []
    monkeypatch.setattr(
        market_utils, "_set_cached_value", lambda key, value, duration=None: writes.append(key)
    )

    result = market_utils.is_market_open("us", ignore_weekend=True)

    assert isinstance(result, bool), "the time-based heuristic must still answer"
    assert "market_state_us__negative" not in writes


def test_is_market_open_still_negative_caches_genuine_failure(monkeypatch):
    from utils import market_utils

    monkeypatch.setattr(market_utils, "_has_cached_key", lambda *_a, **_k: False)
    monkeypatch.setattr(market_utils, "get_cached", lambda *_a, **_k: None)
    writes = []
    monkeypatch.setattr(
        market_utils, "_set_cached_value", lambda key, value, duration=None: writes.append(key)
    )

    market_utils.is_market_open("us", ignore_weekend=True)

    assert "market_state_us__negative" in writes


# ---------------------------------------------------------------------------
# M21/M22: fallback provider numeric hardening and no-fake OHLC
# ---------------------------------------------------------------------------


def _alphavantage_quote(quote_fields: dict) -> dict | None:
    import services.fallback_provider as fp

    provider_instance = fp.AlphaVantageProvider()

    class _Resp:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {"Global Quote": quote_fields}

        @staticmethod
        def raise_for_status():
            pass

    with (
        patch.object(fp, "get_alphavantage_api_key", return_value="AV-TEST-KEY"),
        patch("requests.get", return_value=_Resp()),
    ):
        return provider_instance.get_latest_quote("TEST")


def test_alphavantage_overflow_volume_degrades_instead_of_aborting():
    """An extreme JSON integer in one field must not discard the quote."""
    quote = _alphavantage_quote(
        {
            "05. price": "123.45",
            "06. volume": "1" + "0" * 400,
            "08. previous close": "120.00",
        }
    )
    assert quote is not None, "valid price must survive an overflow volume field"
    assert quote["regularMarketPrice"] == 123.45
    assert quote["regularMarketVolume"] == 0


def test_alphavantage_missing_prev_close_returns_none_not_price():
    """Unknown previous close must be None, never a copy of the price."""
    quote = _alphavantage_quote({"05. price": "123.45", "06. volume": "100"})
    assert quote is not None
    assert quote["regularMarketPrice"] == 123.45
    assert quote["regularMarketPreviousClose"] is None
    assert quote["regularMarketOpen"] is None
    assert quote["regularMarketDayHigh"] is None
    assert quote["regularMarketDayLow"] is None


def test_alphavantage_present_prev_close_kept():
    quote = _alphavantage_quote(
        {"05. price": "123.45", "08. previous close": "120.00"}
    )
    assert quote is not None
    assert quote["regularMarketPreviousClose"] == 120.00


# ---------------------------------------------------------------------------
# M24: fetch_stocks_batch keys results per (symbol, market)
# ---------------------------------------------------------------------------


def test_fetch_stocks_batch_dual_market_same_symbol(monkeypatch):
    """A symbol scheduled under two markets must keep BOTH payloads."""
    from bg.sync import fetch as fetch_mod

    def _fake_batch_download(symbols, period="3mo", lightweight=False):
        import pandas as pd

        return pd.DataFrame({"Close": [1.0, 1.5]})

    def _fake_extract(downloaded, symbol, single_symbol=False):
        import pandas as pd

        return pd.DataFrame({"Close": [1.0, 1.5], "Volume": [10, 20]})

    def _fake_build(symbol, name, market, hist, **kwargs):
        return {"symbol": symbol, "market": market, "payload": True}

    monkeypatch.setattr(fetch_mod, "extract_batch_history", _fake_extract)
    monkeypatch.setattr(fetch_mod, "build_stock_payload", _fake_build)
    monkeypatch.setattr(
        app_state.stock_provider, "download_batch", _fake_batch_download
    )
    # The conftest bg bridge stubs several fetch entry points; exercise the
    # real module body directly (the unit under test is the results_map
    # keying, not the bridge).
    monkeypatch.setattr(fetch_mod, "_get_app_bg_attr", lambda name, default=None: default)
    monkeypatch.setattr("utils.market_utils.acquire_yfinance_slot", lambda: True)
    monkeypatch.setattr("utils.disk_cache.is_disk_cache_degraded", lambda **kwargs: False)
    items = [
        ("7203.T", "Toyota", "jp"),
        ("7203.T", "Toyota IDX-watch", "idx"),
    ]
    with patch.object(app_state.market, "is_yf_rate_limited", return_value=False):
        results = fetch_mod.fetch_stocks_batch(items, lightweight=True)

    assert len(results) == 2
    assert results[0] is not None and results[0]["market"] == "jp"
    assert results[1] is not None and results[1]["market"] == "idx"


# ---------------------------------------------------------------------------
# M39: canonical classifier alignment in bg sync
# ---------------------------------------------------------------------------


def test_handle_yfinance_error_uses_canonical_markers(monkeypatch):
    """A drift-classified rate-limit marker must back off, not reset streak."""
    from bg.sync import fetch as fetch_mod

    marked = []

    def _fake_mark(retry_after=None):
        marked.append(retry_after)
        return 30.0

    monkeypatch.setattr(app_state.market, "mark_yf_429", _fake_mark)
    app_state.market.yfinance_429_streak = 2

    # Canonical marker not present in the old local list.
    fetch_mod._handle_yfinance_error(
        RuntimeError("403 Forbidden: your request was denied.")
    )
    assert marked, "canonical classifier must mark 403-denied as rate limited"


def test_handle_yfinance_error_nonrate_errors_reset_streak(monkeypatch):
    from bg.sync import fetch as fetch_mod

    app_state.market.yfinance_429_streak = 2
    with patch.object(app_state.market, "mark_yf_429") as mark:
        fetch_mod._handle_yfinance_error(ValueError("unexpected shape"))
    mark.assert_not_called()
    assert app_state.market.yfinance_429_streak == 0


# ---------------------------------------------------------------------------
# M38: the yfinance error shim must cover all three classes
# ---------------------------------------------------------------------------


def test_stock_errors_import_survives_missing_yf_exception_classes(monkeypatch):
    """An old yfiction build lacking the exception classes must not kill boot."""
    import importlib

    import services.stock.errors as errors_mod

    original_exc_mod = sys.modules.get("yfinance.exceptions")
    fake_exc_mod = types.ModuleType("yfinance.exceptions")
    # No attributes at all — every `from yfinance.exceptions import X` fails.
    monkeypatch.setitem(sys.modules, "yfinance.exceptions", fake_exc_mod)

    reloaded = importlib.reload(errors_mod)
    try:
        assert hasattr(reloaded, "YFRateLimitError")
        assert hasattr(reloaded, "YFPricesMissingError")
        assert hasattr(reloaded, "YFTickerMissingError")
        # The text heuristic classifier still works with sentinels in place.
        assert reloaded._is_yfinance_rate_limit_error(RuntimeError("429 Too Many Requests"))
    finally:
        # Restore the *original* module object before reloading. Deleting the
        # entry instead made the reload import a fresh module whose exception
        # classes were not the ones every other test imports, so isinstance
        # checks in the reloaded module silently stopped matching (the
        # monkeypatch teardown then put the original module back).
        if original_exc_mod is not None:
            sys.modules["yfinance.exceptions"] = original_exc_mod
        else:
            sys.modules.pop("yfinance.exceptions", None)
        importlib.reload(errors_mod)


# ---------------------------------------------------------------------------
# M12: TrendReq client reuse and retry budget
# ---------------------------------------------------------------------------


def test_trend_client_reused_per_market(monkeypatch):
    """One client per market must be reused across keyword lookups."""
    import trend_sources

    constructions = []

    class _FakeTrendReq:
        def __init__(self, *args, **kwargs):
            constructions.append((args, kwargs))

    monkeypatch.setattr(trend_sources, "TrendReq", _FakeTrendReq)
    trend_sources._GOOGLE_TRENDS_CLIENTS.clear()

    trend_sources._google_trends_client("us")
    trend_sources._google_trends_client("us")
    trend_sources._google_trends_client("jp")
    assert len(constructions) == 2, "clients must be cached per market"

    # Failure invalidates the cached client for that market only.
    trend_sources._invalidate_google_trends_client("us")
    trend_sources._google_trends_client("us")
    assert len(constructions) == 3

    kwargs = constructions[0][1]
    assert kwargs.get("retries", 5) <= 2, "retry budget must fit the 12s collection window"


def test_trend_client_used_for_lookups(monkeypatch):
    import trend_sources

    class _FakeTrendReq:
        def __init__(self, *args, **kwargs):
            self.suggestions_calls = []

        def suggestions(self, keyword):
            return [{"title": f"{keyword} suggestion"}]

    monkeypatch.setattr(trend_sources, "TrendReq", _FakeTrendReq)
    trend_sources._GOOGLE_TRENDS_CLIENTS.clear()
    out_a = trend_sources._trend_queries_for_keyword("alpha", "us")
    out_b = trend_sources._trend_queries_for_keyword("beta", "us")
    assert out_a == ["alpha suggestion"]
    assert out_b == ["beta suggestion"]
    # The same client instance served both keywords.
    assert len(trend_sources._GOOGLE_TRENDS_CLIENTS) == 1


# ---------------------------------------------------------------------------
# M15: SBI scraper parses Japanese negative change marks
# ---------------------------------------------------------------------------


def test_sbi_change_parses_triangle_minus_mark():
    """前日比 rendered as ▲ must parse as a negative change."""
    from services.realtime.scrapers import SBISecuritiesScraper

    scraper = SBISecuritiesScraper()
    html = (
        "<td>現在値 5,000</td>"
        "<td>前日比 ▲ 9.6 -0.19%</td>"  # ▲ = minus in JP market notation
    )
    change = scraper._number_after_label(html, "前日比", signed=True)
    assert change == -9.6


def test_sbi_change_keeps_ascii_minus_semantics():
    from services.realtime.scrapers import SBISecuritiesScraper

    scraper = SBISecuritiesScraper()
    html = "<td>現在値 5,000</td><td>前日比 -9.6 -0.19%</td>"
    change = scraper._number_after_label(html, "前日比", signed=True)
    assert change == -9.6
