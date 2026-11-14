"""2026-11-14 root-cause review — regression tests for the fixes in this pass.

Each test pins a concrete failure mode that was verified against the running
code, not a stylistic preference:

* portfolio ``avg_fx_rate`` omission vs explicit null
* chat provider error status preservation (429/5xx)
* stock-history error code / cache headers
* /api/indices skip-bootstrap fast path
* master-key fail-closed for a non-dict entry
* Agents-SDK circuit classification
* AlphaVantage local cooldown (no global scraper pause)
* realtime ``register_symbols`` idempotency
* embeddings pacing reservation rollback token
* market-news token matching (all tokens, not any)
* index default currency
* validator OverflowError on huge LLM integers
* extension token negative timestamp self-heal
* disk-cache backward clock step
* X-RateLimit-Reset unit normalization
* fallback live-price marker must not fake OHLC
* screener mixed-market market-cap normalization
* frontend wiring checks for the UI fixes
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import app
from app_state import app_state

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_watchlist():
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.clear()
        app_state.market.user_jp.clear()
        app_state.market.user_idx.clear()
    app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
    yield


def _portfolio_post(client, payload):
    return client.post(
        "/api/stocks/portfolio",
        data=json.dumps(payload),
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )


def test_portfolio_omitted_avg_fx_rate_preserves_stored_value(client):
    with app_state.market.user_stocks_lock:
        app_state.market.user_us["AAPL"] = {
            "name": "Apple",
            "shares": 1.0,
            "avg_price": 100.0,
            "avg_fx_rate": 150.0,
        }

    with patch("routes.stocks.portfolio.save_user_stocks", return_value=None):
        resp = _portfolio_post(
            client,
            {"symbol": "AAPL", "market": "us", "shares": 2.0, "avg_price": 110.0},
        )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    with app_state.market.user_stocks_lock:
        assert app_state.market.user_us["AAPL"]["avg_fx_rate"] == pytest.approx(150.0)


def test_portfolio_explicit_null_avg_fx_rate_clears_stored_value(client):
    with app_state.market.user_stocks_lock:
        app_state.market.user_us["AAPL"] = {
            "name": "Apple",
            "shares": 1.0,
            "avg_price": 100.0,
            "avg_fx_rate": 150.0,
        }

    with patch("routes.stocks.portfolio.save_user_stocks", return_value=None):
        resp = _portfolio_post(
            client,
            {
                "symbol": "AAPL",
                "market": "us",
                "shares": 2.0,
                "avg_price": 110.0,
                "avg_fx_rate": None,
            },
        )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    with app_state.market.user_stocks_lock:
        assert "avg_fx_rate" not in app_state.market.user_us["AAPL"]


def test_reset_rollback_reloads_when_concurrent_save_committed(client):
    from routes.stocks import views
    from utils.storage import UserStocksPersistError

    with app_state.market.user_stocks_lock:
        app_state.market.user_us["AAPL"] = {"name": "Apple", "shares": 1.0}
        app_state.market.user_stocks_rev += 1

    def _fail_after_concurrent_save():
        # Simulate a concurrent delete/add that persisted successfully while
        # the reset's own save was failing.
        with app_state.market.user_stocks_lock:
            app_state.market.user_stocks_rev += 1
            app_state.market.user_us = {"MSFT": {"name": "Microsoft", "shares": 1.0}}
        raise UserStocksPersistError("disk full")

    with (
        patch.object(views, "save_user_stocks", side_effect=_fail_after_concurrent_save),
        patch.object(views, "load_user_stocks") as reload_mock,
    ):
        resp = client.post(
            "/api/stocks/reset",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )

    assert resp.status_code == 503
    reload_mock.assert_called_once_with(force=True)


def test_chat_error_response_preserves_provider_429():
    from flask import g

    from routes.api_analysis import _chat_error_response
    from services.ai_service import ProviderAPIError

    with app.test_request_context():
        g.request_id = "test-429"
        resp, status = _chat_error_response(
            ProviderAPIError("rate limited", status_code=429), g
        )

    assert status == 429
    body = resp.get_json()
    assert body["error_code"] == "ERR_CHAT_429"
    assert "429" in body["message"]


def test_stock_history_transient_error_is_not_symbol_not_found(client):
    from routes.stocks import quotes as quotes_mod

    with (
        patch.object(
            quotes_mod,
            "_get_cached_value_if_present",
            return_value=(
                True,
                {"error": "temporary upstream failure", "transient": True},
            ),
        ),
        patch.object(quotes_mod, "_submit_async_history_fetch", return_value=False),
        patch.object(quotes_mod.app_state.market, "is_circuit_open", return_value=False),
        patch.object(quotes_mod.app_state.market, "is_yf_rate_limited", return_value=False),
    ):
        resp = client.get("/api/stock-history?symbol=AAPL&market=us&period=1mo")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["error_code"] == 1101  # FETCH_FAILED, not 1102 SYMBOL_NOT_FOUND
    assert resp.headers.get("Cache-Control") == "no-store"


def test_indices_skip_bootstrap_does_not_wait_for_snapshot(client):
    from routes.stocks import quotes as quotes_mod

    with (
        patch.object(quotes_mod, "_stocks_env_bool", return_value=True),
        patch.object(quotes_mod, "_stocks_is_testing", return_value=False),
        patch.object(quotes_mod.app_state.bootstrap_ready, "is_set", return_value=False),
        patch.object(quotes_mod, "_wait_for_initial_market_snapshot") as wait,
    ):
        resp = client.get("/api/indices")

    assert resp.status_code == 200
    assert resp.get_json()["fetching"] is True
    wait.assert_not_called()


def test_master_key_non_dict_entry_fails_closed(monkeypatch):
    from config import master_key as master_key_mod

    monkeypatch.delenv("MNS_MASTER_KEY", raising=False)
    with (
        patch.object(
            master_key_mod.config_store,
            "load_config",
            return_value={"mns_master_key": "legacy-base64-string"},
        ),
        patch("utils.env_helpers._is_production_env", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="could not be decoded"):
            master_key_mod.get_or_create_master_key()


def test_agent_stream_circuit_classification_excludes_client_errors():
    from services.ai_service import ProviderAPIError, _should_trip_mistral_circuit

    for status in (400, 401, 403, 404, 422, 429):
        assert (
            _should_trip_mistral_circuit(
                ProviderAPIError("client error", status_code=status), status, None
            )
            is False
        )
    assert (
        _should_trip_mistral_circuit(
            ProviderAPIError("server error", status_code=503), 503, None
        )
        is True
    )
    # Agents-SDK errors arrive as bare exceptions with no status. A transport
    # style RuntimeError must still be circuit-accounted.
    assert (
        _should_trip_mistral_circuit(
            RuntimeError("connection reset by peer"), 0, None, unknown_is_failure=True
        )
        is True
    )
    assert (
        _should_trip_mistral_circuit(
            RuntimeError("connection reset by peer"), 0, None
        )
        is False
    )


def test_alphavantage_quota_does_not_pause_global_scrapers():
    from services.fallback_provider import AlphaVantageProvider

    provider = AlphaVantageProvider()
    rate_resp = SimpleNamespace(
        status_code=429,
        text="too many requests",
        headers={"Retry-After": "60"},
        raise_for_status=lambda: (_ for _ in ()).throw(requests.HTTPError("429")),
    )
    with (
        patch("services.fallback_provider.get_alphavantage_api_key", return_value="key"),
        patch("requests.get", return_value=rate_resp) as get_mock,
        patch("services.fallback_provider._mark_yahoo_block") as mark_global,
    ):
        assert provider.get_latest_quote("AAPL") is None
        # Second call is served by the provider-local cooldown, not the network.
        assert provider.get_latest_quote("AAPL") is None

    assert get_mock.call_count == 1
    mark_global.assert_not_called()


def test_register_symbols_is_idempotent():
    from services.realtime.engine import RealtimeMarketEngine

    engine = object.__new__(RealtimeMarketEngine)
    engine.store_lock = threading.RLock()
    engine._registration_tokens = {}
    engine._engine_epoch = 0
    engine.tv_client = MagicMock()
    engine.yahoojp_scraper = MagicMock()
    engine._bg_executor = MagicMock()
    engine._pts_cached_payload = MagicMock(return_value=None)

    engine.register_symbols([], ["7203.T"])
    engine.register_symbols([], ["7203.T"])

    assert engine._bg_executor.submit.call_count == 1
    assert engine.yahoojp_scraper.add_symbol.call_count == 1


def test_embeddings_abort_rolls_back_with_reservation_token():
    from services import embeddings_service

    client = MagicMock()
    with (
        patch.object(embeddings_service, "_EMBEDDINGS_CACHE", {}),
        patch("credential_manager.is_openai_provider", return_value=False),
        patch("credential_manager.is_preview_api_key", return_value=False),
        patch.object(embeddings_service, "_get_client", return_value=client),
        patch.object(
            app_state.market,
            "get_circuit_state",
            return_value={"status": "CLOSED"},
        ),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch(
            "services.ai_service._acquire_mistral_call_slot_with_reservation",
            return_value=(0.0, 123.0),
        ),
        patch(
            "services.ai_service._wait_for_rate_limit_slot",
            return_value="rate_limited",
        ),
        patch("services.ai_service._release_mistral_call_slot") as release,
    ):
        embeddings_service.get_mistral_embeddings_batch(
            ["unique text for reservation regression"], "api-key"
        )

    release.assert_called_once_with(123.0)


def test_market_news_requires_every_query_token():
    from services.ai_tools import _tool_get_market_news

    items = [
        {
            "title": "ソニー、通期決算を上方修正",
            "snippet": "市場予想を上回る",
            "source": "Reuters",
        }
    ]
    with patch("trend_sources.collect_market_news_items_fast", return_value=items):
        result = _tool_get_market_news({"query": "トヨタ 決算"})

    assert result["count"] == 0
    assert result["matched_query"] is False


def test_index_default_currency_uses_symbol_not_usd():
    from utils.stock_payload import _default_index_currency

    assert _default_index_currency("^N225") == "JPY"
    assert _default_index_currency("^GSPC") == "USD"
    assert _default_index_currency("^UNKNOWN") == "JPY"


def test_validators_reject_huge_integer_without_overflow():
    from utils.validators import _normalize_analysis_aliases, validate_analysis_result

    ok, reason = validate_analysis_result(
        {"analysis_summary": "x", "target_price_3m": 10**400}
    )
    assert ok is False
    assert "finite" in reason

    normalized = _normalize_analysis_aliases(
        {"analysis_summary": "x", "upside": 10**400}
    )
    assert isinstance(normalized["upside_3m"], str)


def test_extension_token_negative_timestamp_self_heals():
    from credential_manager import get_or_create_extension_api_token

    cfg = {
        "extension_api_token": {"scheme": "plain", "value": "x"},
        "extension_api_token_created": -1,
    }
    with (
        patch("config_store.get_or_create_master_key", return_value="master-key"),
        patch("config_store.config_update_lock", return_value=contextlib.nullcontext()),
        patch("config_store.load_config", return_value=cfg),
        patch("crypto_utils.unprotect_data", return_value="x" * 40),
        patch("config_store.save_config") as save,
    ):
        secret = get_or_create_extension_api_token()

    assert secret == "x" * 40
    save.assert_called_once()
    saved_cfg = save.call_args.args[0]
    assert saved_cfg["extension_api_token_created"] > 0


def test_disk_cache_future_mtime_is_expired(tmp_path):
    from utils.disk_cache import _entry_age_seconds

    path = tmp_path / "entry.json"
    path.write_text("{}", encoding="utf-8")
    future = time.time() + 3600
    os.utime(path, (future, future))

    age = _entry_age_seconds({"stored_at": future}, path)
    assert math.isinf(age)


def test_rate_limit_reset_milliseconds_are_normalized():
    from services.ai_service import _extract_mistral_wait_seconds

    reset_ms = int((time.time() + 120) * 1000)
    wait = _extract_mistral_wait_seconds(
        {"headers": {"X-RateLimit-Reset": str(reset_ms)}}
    )
    assert 100.0 <= wait <= 130.0


def test_live_price_marker_does_not_fake_ohlc():
    from services.fallback_provider import YahooWebScraperProvider

    quote = YahooWebScraperProvider._parse_live_price_marker(
        '<div data-testid="qsp-price">150.5</div>', "AAPL"
    )
    assert quote is not None
    assert quote["regularMarketPrice"] == 150.5
    assert quote["regularMarketOpen"] is None
    assert quote["regularMarketDayHigh"] is None
    assert quote["regularMarketDayLow"] is None


def test_screener_mixed_market_cap_is_normalized_to_jpy(client):
    from routes.stocks import views

    rows = [
        {
            "symbol": "AAPL",
            "name": "Apple",
            "market": "us",
            "price": 100.0,
            "market_cap": 3.5e12,
            "change_percent": 1.0,
            "volume": 1,
            "sector": "Tech",
            "pe_ratio": 20.0,
        },
        {
            "symbol": "7203.T",
            "name": "Toyota",
            "market": "jp",
            "price": 2000.0,
            "market_cap": 50e12,
            "change_percent": 1.0,
            "volume": 1,
            "sector": "Auto",
            "pe_ratio": 10.0,
        },
    ]
    with (
        patch.object(views, "require_trusted_or_admin", return_value=(True, None)),
        patch.object(
            views, "resolve_stocks_for_response", return_value={"us": [], "jp": [], "idx": []}
        ),
        patch.object(views, "build_screener_base_rows_dispatch", return_value=rows),
        patch.object(views, "build_popular_symbol_items_dispatch", return_value=[]),
        patch.object(views, "get_current_usdjpy_rate", return_value=(150.0, False)),
    ):
        resp = client.get("/api/screener?market=all&sort_by=market_cap")

    assert resp.status_code == 200
    body = resp.get_json()
    # 3.5T USD * 150 = 525T JPY > 50T JPY; the raw comparison ranked Toyota first.
    assert body["stocks"][0]["symbol"] == "AAPL"


def test_frontend_fixes_are_wired():
    api_src = (ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
    state_src = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")
    realtime_src = (ROOT / "static" / "js" / "realtime_client.js").read_text(encoding="utf-8")
    ai_dive_src = (ROOT / "static" / "js" / "experimental" / "ai-dive-controller.js").read_text(
        encoding="utf-8"
    )
    settings_src = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    ai_portfolio_src = (ROOT / "static" / "js" / "ai_portfolio.js").read_text(encoding="utf-8")

    assert "marketsToReconcile" in api_src
    assert "hiddenUpdatesPending" in api_src
    assert "flushPendingRealtimeDeltas" in api_src
    assert "_indicesFetchGeneration++" in api_src
    assert "fallbackPollInFlight" in api_src
    assert "Build the payload inside the try" in api_src
    assert "flushPendingDeltas" in realtime_src
    assert "createRequestToken" in ai_dive_src
    assert 'ai_provider: "mistral"' in settings_src
    assert "OpenAI互換のBase URL" in settings_src
    assert "Math.round(simulatedPlJpy).toLocaleString()" in ai_portfolio_src
    assert "startsWith(`${stockKey}|`)" in state_src
    assert "stockDetailsCacheAt" in state_src
    assert "isStockDetailsCacheFresh" in (ROOT / "static" / "js" / "ui.js").read_text(
        encoding="utf-8"
    )
    chart_src = (ROOT / "static" / "js" / "chart.js").read_text(encoding="utf-8")
    ai_lines_idx = chart_src.index("chart.$aiTechnicalLines = aiLines.lines;")
    assert 'chart.update("none")' in chart_src[ai_lines_idx : ai_lines_idx + 400]
    assert "updateIndicesBar(state.indices)" in (
        ROOT / "static" / "js" / "index_main.js"
    ).read_text(encoding="utf-8")
    assert "[Mode0 polling]" in api_src


def test_config_first_run_initialization_uses_update_lock():
    source = (ROOT / "config" / "io.py").read_text(encoding="utf-8")
    marker = source.index("First-run initialization must be serialized")
    lock = source.index("with config_store.config_update_lock():", marker)
    save = source.index("config_store.save_config(config_store.DEFAULT_CONFIG)", marker)
    assert lock < save
