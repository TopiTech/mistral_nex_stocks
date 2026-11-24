"""Regression and edge-case tests for Unit 2 utils & services fixes (2026-11-25)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from requests.exceptions import HTTPError

from app_state import app_state
from services import ai_service
from services.realtime.engine import RealtimeMarketEngine
from services.stock.provider import YFinanceProvider
from utils.tradingview_mapper import _TICKER_EXCHANGE_CACHE, get_ticker_exchange


def test_bg_util_01_fallback_nasdaq_does_not_poison_ticker_exchange_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BG-UTIL-01: Missing info['exchange'] must not register fallback NASDAQ into exchange cache."""
    from utils.stock_payload import build_stock_payload

    sym = "ZZZZUNIT2TEST"
    _TICKER_EXCHANGE_CACHE.pop(sym, None)
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.pop(f"info_short_{sym}", None)
    monkeypatch.setattr(app_state.stock_disk_cache, "get", lambda *_a, **_kw: None)

    hist = pd.DataFrame(
        {"Open": [10.0], "High": [11.0], "Low": [9.5], "Close": [10.5], "Volume": [1000]},
        index=pd.to_datetime(["2026-11-24"]),
    )
    payload = build_stock_payload(
        sym,
        sym,
        "us",
        hist,
        lightweight=True,
    )
    assert payload is not None
    assert get_ticker_exchange(sym) is None


def test_svc_rev_01_start_preserves_prestart_registrations_and_stop_clears() -> None:
    """SVC-REV-01: register_symbols() before start() must remain valid until stop()."""
    engine = RealtimeMarketEngine()
    engine.tv_client = MagicMock()
    engine.yahoojp_scraper = MagicMock()
    engine._pts_worker_loop = MagicMock()
    engine._bg_executor = MagicMock()
    engine._bg_executor._shutdown = False

    engine.register_symbols(["AAPL"], ["7203.T"])
    engine.register_symbol("AAPL", "us")
    assert ("us", "AAPL") in engine._registration_tokens
    assert ("jp", "7203.T") in engine._registration_tokens

    engine.start()
    assert ("us", "AAPL") in engine._registration_tokens
    assert ("jp", "7203.T") in engine._registration_tokens

    engine.stop()
    assert engine._registration_tokens == {}


def test_svc_rev_02_and_03_mistral_chat_probe_and_fallback_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SVC-REV-02 & SVC-REV-03: No double release_circuit_probe and fallback respects semaphore."""
    release_calls: list[str] = []
    report_calls: list[tuple[str, bool]] = []
    sem_entries = {"count": 0}

    class _FakeSem:
        def __enter__(self):
            sem_entries["count"] += 1
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(app_state.ai, "mistral_call_semaphore", _FakeSem())
    monkeypatch.setattr(
        app_state.market,
        "enter_circuit_call",
        lambda _svc: (True, True, ""),
    )
    monkeypatch.setattr(
        app_state.market,
        "release_circuit_probe",
        lambda svc: release_calls.append(svc),
    )
    monkeypatch.setattr(
        app_state.market,
        "report_circuit_result",
        lambda svc, success=False, **_kw: report_calls.append((svc, success)),
    )
    monkeypatch.setattr(ai_service, "get_model_name", lambda: "mistral-large-latest")

    # Case 1 (SVC-REV-02): Non-circuit 401 error releases probe ONCE (not twice via finally)
    import httpx

    req_401 = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    resp_401 = httpx.Response(401, request=req_401, json={"message": "unauthorized"})
    err_401 = httpx.HTTPStatusError("unauthorized", request=req_401, response=resp_401)

    fake_client = MagicMock()
    fake_client.chat.complete.side_effect = err_401
    monkeypatch.setattr(ai_service, "_get_mistral_client", lambda _key: fake_client)

    res = ai_service.call_mistral_chat(
        "mistral_real_key_1234567890", [{"role": "user", "content": "hi"}], use_cache=False
    )
    assert "error" in res
    assert release_calls == ["mistral"]
    assert report_calls == []

    # Case 2 (SVC-REV-03): Fallback tier-restriction retry acquires mistral_call_semaphore
    sem_entries["count"] = 0
    release_calls.clear()

    req_403 = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    resp_403 = httpx.Response(403, request=req_403, json={"message": "tier restricted"})
    err_403 = httpx.HTTPStatusError("tier restricted", request=req_403, response=resp_403)

    call_idx = {"n": 0}

    def _tier_then_ok(**_kwargs):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            raise err_403
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="fallback ok"))],
            model_dump=lambda: {
                "choices": [{"message": {"content": "fallback ok"}}],
                "usage": None,
            },
        )

    fake_client2 = MagicMock()
    fake_client2.chat.complete.side_effect = _tier_then_ok
    monkeypatch.setattr(ai_service, "_get_mistral_client", lambda _key: fake_client2)

    res2 = ai_service.call_mistral_chat(
        "mistral_real_key_1234567890", [{"role": "user", "content": "hi"}], use_cache=False
    )
    assert res2.get("fallback_applied") is True
    assert sem_entries["count"] == 2


def test_svc_rev_04_fast_info_cache_hit_preserves_429_streak_and_5xx_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SVC-REV-04: Cache hit preserves 429 streak and 5xx HTTPError is retried."""
    provider = YFinanceProvider(app_state.market)
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache["fastinfo_UNIT2SYM"] = {
            "marketCap": 1000,
            "regularMarketPreviousClose": 10.0,
        }

    with app_state.market.yfinance_lock:
        app_state.market.yfinance_429_streak = 2
        app_state.market.is_yfinance_rate_limited = False

    res = provider.get_fast_info("UNIT2SYM")
    assert res["marketCap"] == 1000
    assert app_state.market.yfinance_429_streak == 2

    # 5xx HTTPError in get_info must be retried by @with_yfinance_retry
    attempts = {"n": 0}

    class _FakeTicker503:
        @property
        def info(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise HTTPError("503 Service Unavailable", response=SimpleNamespace(status_code=503))
            return {"shortName": "Retry Success", "currency": "USD"}

    monkeypatch.setattr(provider, "get_ticker", lambda _sym: _FakeTicker503())
    monkeypatch.setattr(app_state.market, "is_negative_cached_symbol", lambda _sym: False)
    info = provider.get_info("UNIT2RETRY")
    assert attempts["n"] == 2
    assert info["shortName"] == "Retry Success"
