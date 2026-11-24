"""Regression and edge-case tests for Unit 3 routes & API boundary fixes (2026-11-25)."""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

import app as app_module
import route_helpers
from app_state import app_state
from routes import api_analysis
from routes.stocks import views as stock_views
from routes.system import credentials as creds_route
from services.news_service import news_service


@pytest.fixture
def client():
    prev_testing = app_module.app.config.get("TESTING", False)
    prev_csrf = app_module.app.config.get("WTF_CSRF_ENABLED", True)
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    try:
        with app_module.app.test_client() as test_client:
            yield test_client
    finally:
        app_module.app.config["TESTING"] = prev_testing
        app_module.app.config["WTF_CSRF_ENABLED"] = prev_csrf


def test_route_01_half_open_probe_ttl_reclamation_is_called(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-01: HALF_OPEN must call try_claim_circuit_probe even when state['probing'] is True."""
    sym = "UNIT3ROUTE01"
    circuit_key = f"us:{sym}"
    with app_state.market.history_circuit_lock:
        app_state.market.history_circuit_state[circuit_key] = {
            "status": "HALF_OPEN",
            "timeout_streak": 0,
            "open_until": 0.0,
            "probing": True,
            "probing_since": time.monotonic() - 999.0,
        }

    try:
        monkeypatch.setattr(
            "routes.stocks.quotes.require_trusted_or_admin", lambda *_a, **_kw: (True, "")
        )
        monkeypatch.setattr(
            "routes.stocks.quotes._get_cached_value_if_present",
            lambda *_a, **_kw: (False, None),
        )
        monkeypatch.setattr(
            "routes.stocks.quotes._submit_async_history_fetch", lambda *_a, **_kw: True
        )

        with patch.object(
            app_state.market,
            "try_claim_circuit_probe",
            wraps=app_state.market.try_claim_circuit_probe,
        ) as claim_spy:
            resp = client.get(f"/api/stock-history?symbol={sym}&market=us&period=1mo")

        assert resp.status_code == 200
        claim_spy.assert_called_once_with("yfinance_history", symbol=circuit_key)
    finally:
        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_state.pop(circuit_key, None)


def test_route_02_non_cacheable_news_bundle_delivers_once_and_stops_polling_bypass(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-02: Non-cacheable news bundle stays out of news_result_cache, delivers once, and closes polling bypass."""
    monkeypatch.setattr("routes.api_analysis.require_trusted_or_admin", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr("routes.api_analysis.extract_api_key", lambda *_a, **_kw: "test-mistral-key-12345")
    monkeypatch.setattr("routes.api_analysis.extract_langsearch_api_key", lambda *_a, **_kw: "")
    monkeypatch.setattr("routes.api_analysis.extract_tavily_api_key", lambda *_a, **_kw: "")
    monkeypatch.setattr("routes.api_analysis.NEWS_PREPARE_WAIT_SEC", 0.0)
    monkeypatch.setattr("routes.api_analysis.get_cached", lambda *_a, **_kw: None)

    partial_bundle = {
        "us": {"status": "error", "content": "解析エラー: upstream"},
        "jp": {"status": "ok", "content": "ok"},
        "trends": {"status": "ok", "content": "ok"},
    }
    monkeypatch.setattr(
        news_service, "get_synchronized_market_news", lambda **_kw: partial_bundle
    )

    submitted_jobs: list = []

    class _DeferredExecutor:
        def submit(self, fn):
            submitted_jobs.append(fn)
            return MagicMock()

    monkeypatch.setattr(app_state.execution, "news_executor", _DeferredExecutor())

    with api_analysis.news_fetch_lock:
        api_analysis.news_fetch_inflight.clear()
        api_analysis.news_result_cache.clear()
        api_analysis.news_completed_tokens.clear()
        api_analysis.news_uncacheable_results.clear()

    tok = "unit3_route02_token_1234"
    cache_key = f"news_ddgs_only_force:{tok}"

    resp1 = client.post(
        "/api/news?force=true",
        json={"force": True, "request_token": tok},
        headers={"Origin": "http://127.0.0.1:5000"},
    )
    assert resp1.status_code == 200
    assert resp1.get_json().get("fetching") is True
    assert len(submitted_jobs) == 1
    assert route_helpers._is_polling_token_inflight(tok) is True

    # Background job finishes with non-cacheable partial bundle
    submitted_jobs[0]()

    with api_analysis.news_fetch_lock:
        assert cache_key not in api_analysis.news_result_cache
        assert cache_key in api_analysis.news_completed_tokens

    assert route_helpers._is_polling_token_inflight(tok) is False

    calls = {"n": 0}
    orig_get_news = news_service.get_synchronized_market_news

    def _counting_news(**kwargs):
        calls["n"] += 1
        return orig_get_news(**kwargs)

    monkeypatch.setattr(news_service, "get_synchronized_market_news", _counting_news)

    resp2 = client.post(
        "/api/news?force=true",
        json={"force": True, "request_token": tok},
        headers={"Origin": "http://127.0.0.1:5000"},
    )
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert data2.get("fetching") is not True
    assert data2.get("us", {}).get("status") == "error"
    assert calls["n"] == 0


def test_route_03_reset_stocks_preserves_default_stocks_in_realtime_engine(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-03: POST /api/stocks/reset must not unregister default symbols that had portfolio entries."""
    monkeypatch.setattr(
        "routes.stocks.views.require_trusted_or_admin", lambda *_a, **_kw: (True, "")
    )
    with app_state.market.user_stocks_lock:
        prev_us = dict(app_state.market.user_us)
        prev_jp = dict(app_state.market.user_jp)
        prev_idx = dict(app_state.market.user_idx)
        app_state.market.user_us = {"AAPL": {"shares": 1}, "CUSTOMX": {"shares": 2}}
        app_state.market.user_jp = {}
        app_state.market.user_idx = {}

    try:
        monkeypatch.setattr(stock_views, "save_user_stocks", lambda: None)
        monkeypatch.setattr(stock_views, "schedule_sync_all_stocks_now", lambda: None)
        monkeypatch.setattr("routes.stocks.common._announce_watchlist_state", lambda: None)
        monkeypatch.setattr(
            "routes.stocks.common._default_stock_names",
            lambda m: {"AAPL"} if m == "us" else set(),
        )

        unregistered: list[tuple[str, str, bool]] = []
        monkeypatch.setattr(
            "routes.stocks.common._sync_realtime_symbol",
            lambda sym, mkt, register=False: unregistered.append((sym, mkt, register)),
        )

        resp = client.post(
            "/api/stocks/reset",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 200
        assert ("CUSTOMX", "us", False) in unregistered
        assert ("AAPL", "us", False) not in unregistered
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = prev_us
            app_state.market.user_jp = prev_jp
            app_state.market.user_idx = prev_idx


def test_route_04_credentials_verify_validates_agent_id_and_sanitizes_logs(
    client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ROUTE-04: /api/credentials/verify must reject bad mistral_agent_id and sanitize SDK errors."""
    monkeypatch.setattr(creds_route, "require_trusted_or_admin", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr(creds_route, "is_allowed_trusted_origin", lambda *_a: True)

    fake_client = MagicMock()
    monkeypatch.setattr(app_state.ai, "get_or_create_mistral_client", lambda _k: fake_client)

    resp_bad_type = client.post(
        "/api/credentials/verify",
        json={
            "api_key": "mistral_real_key_123456789012345",
            "mistral_api_mode": "agents",
            "mistral_agent_id": 123,
        },
        headers={"Origin": "http://127.0.0.1:5000"},
    )
    assert resp_bad_type.status_code == 400

    resp_bad_fmt = client.post(
        "/api/credentials/verify",
        json={
            "api_key": "mistral_real_key_123456789012345",
            "mistral_api_mode": "agents",
            "mistral_agent_id": "../../models",
        },
        headers={"Origin": "http://127.0.0.1:5000"},
    )
    assert resp_bad_fmt.status_code == 400
    fake_client.beta.agents.get.assert_not_called()

    secret = "sk-secret-token-leak-value-99999"
    fake_client.beta.agents.get.side_effect = RuntimeError(f"Bearer {secret}\nforged line")
    monkeypatch.setattr(creds_route, "HAS_AGENTS_SDK", False)
    with caplog.at_level(logging.DEBUG):
        resp_fail = client.post(
            "/api/credentials/verify",
            json={
                "api_key": "mistral_real_key_123456789012345",
                "mistral_api_mode": "agents",
                "mistral_agent_id": "ag_valid_id_123",
            },
            headers={"Origin": "http://127.0.0.1:5000"},
        )
    assert resp_fail.status_code == 400
    assert secret not in caplog.text


def test_route_05_technical_lines_and_search_sanitize_logs(
    client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ROUTE-05: /api/ai-technical-lines and /api/search must sanitize upstream errors and query."""
    monkeypatch.setattr("routes.api_analysis.require_trusted_or_admin", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr("routes.api_analysis.extract_api_key", lambda *_a, **_kw: "test-mistral-key-12345")
    secret = "super-secret-api-key-abcdef123456"
    monkeypatch.setattr(
        "routes.api_analysis.generate_ai_technical_lines",
        lambda *_a, **_kw: {"error": f"https://api.example.com?api_key={secret}\nforged"},
    )
    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/api/ai-technical-lines",
            json={
                "symbol": "AAPL",
                "market": "us",
                "period": "1mo",
                "history": [{"date": "2026-11-24", "close": 100.0}],
            },
            headers={"Origin": "http://127.0.0.1:5000"},
        )
    assert resp.status_code == 500
    assert secret not in caplog.text

    caplog.clear()
    monkeypatch.setattr("routes.stocks.quotes.require_trusted_or_admin", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr("routes.stocks.quotes.get_cached", lambda _k, fn, **_kw: fn())
    monkeypatch.setattr(
        app_state.stock_provider,
        "search",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            requests.RequestException(f"Authorization: Bearer {secret}")
        ),
    )
    with caplog.at_level(logging.ERROR):
        resp_s = client.get("/api/search?q=AAPL%0AFORGED_LOG")
    assert resp_s.status_code == 502
    assert secret not in caplog.text
    assert "FORGED_LOG\n" not in caplog.text


def test_route_07_cancelled_heatmap_callback_does_not_clear_newer_inflight(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROUTE-07: Stale cancelled heatmap future must not remove a newer request's inflight entry."""
    monkeypatch.setattr("routes.stocks.views.require_trusted_or_admin", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr(
        "routes.stocks.views._get_cached_value_if_present", lambda *_a, **_kw: (False, None)
    )
    monkeypatch.setattr(app_state.payload_disk_cache, "get", lambda *_a, **_kw: None)

    callbacks: list = []

    class _FakeFuture:
        def cancelled(self) -> bool:
            return True

        def add_done_callback(self, cb) -> None:
            callbacks.append(cb)

    monkeypatch.setattr(
        app_state.execution.data_executor, "submit", lambda *_a, **_kw: _FakeFuture()
    )

    with app_state.heatmap_fetch_lock:
        app_state.heatmap_fetch_inflight.clear()
        app_state.heatmap_fetch_start_times.clear()

    resp = client.get("/api/heatmap?market=us")
    assert resp.status_code == 200
    assert len(callbacks) == 1

    with app_state.heatmap_fetch_lock:
        app_state.heatmap_fetch_start_times["heatmap_us"] = 999999999.0

    callbacks[0](_FakeFuture())

    with app_state.heatmap_fetch_lock:
        assert "heatmap_us" in app_state.heatmap_fetch_inflight
        assert app_state.heatmap_fetch_start_times["heatmap_us"] == 999999999.0
        app_state.heatmap_fetch_inflight.clear()
        app_state.heatmap_fetch_start_times.clear()
