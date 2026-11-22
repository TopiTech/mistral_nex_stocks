"""Regression and edge-case tests for Unit 3 routes/schemas/error boundaries (2026-11-22)."""

from __future__ import annotations

import queue
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import app
from error_codes import ErrorCode


@pytest.fixture()
def client():
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c
    app.config["TESTING"] = orig_testing
    app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_csrf_error_preserves_csrf_token_phrase_while_bad_request_stays_none() -> None:
    from flask_wtf.csrf import CSRFError
    from werkzeug.exceptions import BadRequest

    with app.test_request_context("/api/credentials", method="POST"):
        csrf_handler = app.error_handler_spec[None][400][CSRFError]
        resp_csrf, status_csrf = csrf_handler(CSRFError("The CSRF session token is missing."))
        body_csrf = resp_csrf.get_json()
        assert status_csrf == 400
        assert re.search(r"csrf token", body_csrf["details"]["reason"], re.IGNORECASE)

        bad_handler = app.error_handler_spec[None][400][BadRequest]
        resp_bad, status_bad = bad_handler(BadRequest("Internal parse trace"))
        body_bad = resp_bad.get_json()
        assert status_bad == 400
        assert body_bad["details"]["reason"] is None


def test_analyze_v2_and_news_error_no_echo_and_uncacheable_news_skipped(client) -> None:
    from routes import api_analysis

    with app.test_request_context("/api/analyze-v2", method="POST"):
        from flask import g

        resp_net, code_net = api_analysis._analyze_v2_error_response(
            requests.ConnectionError("secret/internal_host.py: connection refused"),
            g,
        )
        assert code_net == 503
        assert "internal_host.py" not in resp_net.get_json()["details"]["reason"]

        resp_int, code_int = api_analysis._analyze_v2_error_response(
            ValueError("Traceback internal_bug_token"),
            g,
        )
        assert code_int == 500
        assert "internal_bug_token" not in resp_int.get_json()["details"]["reason"]

    # Chat SSE-to-poll ProviderAPIError retains status_code=429 rather than 500.
    with app.test_request_context("/api/chat", method="POST"):
        from flask import g

        from services.ai_service import ProviderAPIError

        resp_chat, code_chat = api_analysis._chat_error_response(
            ProviderAPIError("rate limited", status_code=429),
            g,
            "tok_1234567890123456",
        )
        assert code_chat == 429
        assert resp_chat.get_json()["code"] == 429

    # Empty / error news bundle must NOT be cached in news_result_cache.
    tok = "news-invalid-bundle-token-0001"
    bad_bundle = {
        "us": {"status": "error", "content": ""},
        "jp": {"status": "error", "content": ""},
        "trends": {"status": "error", "content": ""},
    }
    cache_key = f"news_ddgs_only_force:{tok}"
    with api_analysis.news_fetch_lock:
        api_analysis.news_result_cache.pop(cache_key, None)
    try:
        with (
            patch.object(api_analysis, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(api_analysis, "extract_api_key", return_value="real-mistral-key"),
            patch.object(api_analysis, "extract_langsearch_api_key", return_value=""),
            patch.object(api_analysis, "extract_tavily_api_key", return_value=""),
            patch.object(api_analysis, "_submit_in_app_context", lambda _ex, fn: fn()),
            patch.object(
                api_analysis.news_service, "get_synchronized_market_news", return_value=bad_bundle
            ),
        ):
            r = client.post(
                "/api/news?force=true",
                json={"force": True, "request_token": tok},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert r.status_code == 200
        with api_analysis.news_fetch_lock:
            assert cache_key not in api_analysis.news_result_cache
    finally:
        with api_analysis.news_fetch_lock:
            api_analysis.news_result_cache.pop(cache_key, None)


def test_ai_portfolio_generate_delivered_and_save_rejects_missing_items(client) -> None:
    from pydantic import ValidationError

    from routes.stocks import ai_portfolio as ap_mod
    from schemas.ai_portfolio import AIPortfolioCopyToMyRequest

    tok = "a pf_generate_token_123456".replace(" ", "_")
    sample_pf = {
        "id": "custom-gen1",
        "title": "Test",
        "items": [{"symbol": "AAPL", "market": "us", "weight_pct": 100.0}],
    }
    with ap_mod.ai_portfolio_fetch_lock:
        ap_mod.ai_portfolio_token_records.pop(tok, None)
        ap_mod.ai_portfolio_result_cache.clear()
    try:
        with (
            patch.object(ap_mod, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(ap_mod, "extract_api_key", return_value=""),
            patch.object(ap_mod, "generate_ai_portfolio_by_theme", return_value=sample_pf),
            patch("route_helpers._submit_in_app_context", lambda _ex, fn: fn()),
        ):
            # /generate intentionally keeps delivered=False across repeated polls,
            # while rejecting token reuse across differing themes with 409.
            res_gen = client.post(
                "/api/ai-portfolio/generate",
                json={"theme": "unique-test-delivered-theme", "request_token": tok},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert res_gen.status_code == 200
            with ap_mod.ai_portfolio_fetch_lock:
                assert ap_mod.ai_portfolio_token_records[tok][1] is False
            res_conflict = client.post(
                "/api/ai-portfolio/generate",
                json={"theme": "conflicting-other-theme", "request_token": tok},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert res_conflict.status_code == 409

            # /rebalance sets delivered=True on completed delivery.
            tok_reb = "apf_rebalance_token_123456"
            res_reb = client.post(
                "/api/ai-portfolio/rebalance",
                json={"theme": "unique-test-delivered-theme", "request_token": tok_reb},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert res_reb.status_code == 200
            with ap_mod.ai_portfolio_fetch_lock:
                assert ap_mod.ai_portfolio_token_records[tok_reb][1] is True
    finally:
        with ap_mod.ai_portfolio_fetch_lock:
            ap_mod.ai_portfolio_token_records.pop(tok, None)
            ap_mod.ai_portfolio_token_records.pop("apf_rebalance_token_123456", None)
            ap_mod.ai_portfolio_result_cache.clear()

    # MINOR-2: save without "items" key cannot save 0-item portfolio.
    with patch.object(ap_mod, "require_trusted_or_admin", return_value=(True, None)):
        res_empty = client.post(
            "/api/ai-portfolio/save",
            json={"portfolio": {"title": "No items portfolio"}},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
    assert res_empty.status_code == 400
    assert res_empty.get_json()["error_code"] == int(ErrorCode.MALFORMED_INPUT)

    # MINOR-1: copy-to-my rejects non-string symbol or market cleanly.
    with pytest.raises(ValidationError) as exc_info:
        AIPortfolioCopyToMyRequest.model_validate(
            {"items": [{"symbol": ["AAPL"], "market": 123, "weight_pct": 100.0}]}
        )
    assert "copy_bad_symbol_market|0" in str(exc_info.value)


def test_quotes_and_heatmap_submit_failures_release_probe_and_return_503(client) -> None:
    from app_state import app_state
    from market_state import CircuitState
    from routes.stocks import quotes as quotes_mod
    from routes.stocks import views as views_mod

    # MAJOR-2: HALF_OPEN local executor submit error releases probe (does NOT trip OPEN) and returns 503.
    circ_key = "us:AAPL"
    with app_state.market.history_circuit_lock:
        orig = app_state.market.history_circuit_states.get(circ_key)
        app_state.market.history_circuit_states[circ_key] = CircuitState(
            status="HALF_OPEN", probing=False, open_until=0.0
        )
    try:
        with (
            patch.object(quotes_mod, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
            patch.object(app_state.stock_disk_cache, "get", return_value=None),
            patch.object(
                app_state.execution.data_executor,
                "submit",
                side_effect=RuntimeError("executor down"),
            ),
        ):
            r_hist = client.get("/api/stock-history?symbol=AAPL&market=us&period=1mo")
        assert r_hist.status_code == 503
        with app_state.market.history_circuit_lock:
            st = app_state.market.history_circuit_states[circ_key]
            assert st["status"] == "HALF_OPEN"
            assert st["probing"] is False

        # cache_miss hitting queue.Full must still return 503 TOO_MANY_REQUESTS.
        import queue

        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_states[circ_key] = CircuitState(
                status="CLOSED", probing=False, open_until=0.0
            )
        with (
            patch.object(quotes_mod, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
            patch.object(app_state.stock_disk_cache, "get", return_value=None),
            patch.object(
                app_state.execution.data_executor,
                "submit",
                side_effect=queue.Full("full"),
            ),
        ):
            r_miss_full = client.get("/api/stock-history?symbol=AAPL&market=us&period=1mo")
        assert r_miss_full.status_code == 503
        assert r_miss_full.get_json()["error_code"] == int(ErrorCode.TOO_MANY_REQUESTS)
    finally:
        with app_state.market.history_circuit_lock:
            if orig is None:
                app_state.market.history_circuit_states.pop(circ_key, None)
            else:
                app_state.market.history_circuit_states[circ_key] = orig

    # MAJOR-3: /api/stock-details submit failure returns 503 instead of fake fetching=True.
    with (
        patch.object(quotes_mod, "require_trusted_or_admin", return_value=(True, None)),
        patch.object(quotes_mod, "get_cached", return_value=None),
        patch.dict(app_state.yfinance_short_cache, {}, clear=False),
        patch.object(
            app_state.execution.data_executor,
            "submit",
            side_effect=RuntimeError("executor down"),
        ),
    ):
        app_state.yfinance_short_cache.pop("info_short_ZZZZTEST", None)
        r_det = client.get("/api/stock-details?symbol=ZZZZTEST&market=us")
    assert r_det.status_code == 503

    # /api/heatmap non-queue submit failure without disk cache returns 503 & frees inflight.
    with app_state.heatmap_fetch_lock:
        app_state.heatmap_fetch_inflight.discard("heatmap_us")
        app_state.heatmap_fetch_start_times.pop("heatmap_us", None)
    with (
        patch.object(views_mod, "require_trusted_or_admin", return_value=(True, None)),
        patch.object(views_mod, "_get_cached_value_if_present", return_value=(False, None)),
        patch.object(app_state.payload_disk_cache, "get", return_value=None),
        patch.object(
            app_state.execution.data_executor,
            "submit",
            side_effect=RuntimeError("heatmap executor down"),
        ),
    ):
        r_hm = client.get("/api/heatmap?market=us")
    assert r_hm.status_code == 503
    assert "heatmap_us" not in app_state.heatmap_fetch_inflight


def test_credentials_verify_invalid_provider_and_observe_remote_manifest_error(client) -> None:
    from app_state import app_state
    from routes.system import credentials as cred_mod
    from routes.system import observe as obs_mod

    with patch.object(cred_mod, "require_trusted_or_admin", return_value=(True, None)):
        r_bad = client.post(
            "/api/credentials/verify",
            json={"ai_provider": "invalid_provider_name"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert r_bad.status_code == 400
        assert r_bad.get_json()["error_code"] == int(ErrorCode.INVALID_INPUT)

    with (
        patch.object(obs_mod, "_env_bool", return_value=True),
        patch.dict(app_state._extension_manifest_status, {"ok": False, "error": "/secret/path"}),
    ):
        r_health = client.get("/api/health")
    assert r_health.status_code == 200
    body = r_health.get_json()
    assert body["extension_manifest_ok"] is False
    assert "extension_manifest_error" not in body


def test_schemas_and_executor_cap_fixes(client) -> None:
    from pydantic import ValidationError

    from route_helpers import _parse_screener_request, _submit_in_app_context
    from schemas.analysis import AIAnalyzeChartImageRequest
    from schemas.stocks import ScreenerQueryRequest

    # MINOR-5: Uppercase HTTP:// image URI rejected cleanly.
    with pytest.raises(ValidationError) as exc_img:
        AIAnalyzeChartImageRequest.model_validate({"image_data": "HTTP://example.com/chart.png"})
    assert "image_url" in str(exc_img.value)

    # MINOR-6: Whitespace-only sector/market/sort fall back to defaults cleanly, min_change error mentions field.
    s_req = ScreenerQueryRequest(sector="   ", market="   ", sort_by="   ", sort_order="  ")
    assert s_req.sector == "all"
    assert s_req.market == "all"
    assert s_req.sort_by == "market_cap"
    assert s_req.sort_order == "desc"

    with app.test_request_context("/api/screener?min_change=-200"):
        from flask import request

        parsed, err = _parse_screener_request(request.args)
    assert parsed is None and err is not None
    err_resp, err_code = err
    assert err_code == 400
    assert "min_change" in err_resp.get_json()["details"]["reason"]

    # MINOR-9: _submit_in_app_context uses executor's own _max_queue_size when configured.
    ex_small = SimpleNamespace(
        _work_queue=SimpleNamespace(qsize=lambda: 5),
        _max_queue_size=5,
        submit=MagicMock(),
    )
    with pytest.raises(queue.Full):
        _submit_in_app_context(ex_small, lambda: None, app=app)

    ex_large = SimpleNamespace(
        _work_queue=SimpleNamespace(qsize=lambda: 20),
        _max_queue_size=50,
        submit=MagicMock(),
    )
    _submit_in_app_context(ex_large, lambda: None, app=app)
    assert ex_large.submit.call_count == 1


def test_screener_does_not_mutate_cached_enrichment_and_heatmap_uses_monotonic(client) -> None:
    from app_state import app_state
    from routes.stocks import views as views_mod

    shared_enriched = {
        "NVDA": {
            "symbol": "NVDA",
            "market": "us",
            "name": "NVIDIA",
            "price": 120.0,
            "change_percent": 1.5,
            "market_cap": 3_000_000_000_000,
            "sector": "Technology",
        }
    }
    with (
        patch.object(views_mod, "require_trusted_or_admin", return_value=(True, None)),
        patch.object(views_mod, "resolve_stocks_for_response", return_value=[]),
        patch.object(views_mod, "build_screener_base_rows_dispatch", return_value=[]),
        patch.object(
            views_mod,
            "build_popular_symbol_items_dispatch",
            return_value=[("NVDA", "NVIDIA", "us")],
        ),
        patch.object(views_mod, "get_cached_dispatch", return_value=shared_enriched),
        patch.object(views_mod, "get_current_usdjpy_rate", return_value=(150.0, False)),
    ):
        r_scr = client.get("/api/screener?market=all")
    assert r_scr.status_code == 200
    assert "market_cap_jpy" not in shared_enriched["NVDA"]

    # Heatmap inflight timeout uses time.monotonic(), resilient to time.time() jumps
    # and recovering cleanly when start_time is absent even below 30s uptime.
    with app_state.heatmap_fetch_lock:
        app_state.heatmap_fetch_inflight.discard("heatmap_us")
        app_state.heatmap_fetch_start_times.pop("heatmap_us", None)
        app_state.heatmap_fetch_inflight.add("heatmap_us")
        app_state.heatmap_fetch_start_times["heatmap_us"] = 995.0
    try:
        with (
            patch.object(views_mod, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(views_mod, "_get_cached_value_if_present", return_value=(False, None)),
            patch.object(app_state.payload_disk_cache, "get", return_value=None),
            patch.object(views_mod, "time", SimpleNamespace(monotonic=lambda: 1000.0)),
            patch.object(app_state.execution.data_executor, "submit") as mock_submit,
        ):
            r_active = client.get("/api/heatmap?market=us")
        assert r_active.status_code == 200
        mock_submit.assert_not_called()

        # Orphan entry without start_time recovers even if monotonic() < 30s
        with app_state.heatmap_fetch_lock:
            app_state.heatmap_fetch_inflight.add("heatmap_us")
            app_state.heatmap_fetch_start_times.pop("heatmap_us", None)
        with (
            patch.object(views_mod, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(views_mod, "_get_cached_value_if_present", return_value=(False, None)),
            patch.object(app_state.payload_disk_cache, "get", return_value=None),
            patch.object(views_mod, "time", SimpleNamespace(monotonic=lambda: 5.0)),
            patch.object(app_state.execution.data_executor, "submit") as mock_submit,
        ):
            r_orphan = client.get("/api/heatmap?market=us")
        assert r_orphan.status_code == 200
        mock_submit.assert_called_once()
        assert app_state.heatmap_fetch_start_times.get("heatmap_us") == 5.0
    finally:
        with app_state.heatmap_fetch_lock:
            app_state.heatmap_fetch_inflight.discard("heatmap_us")
            app_state.heatmap_fetch_start_times.pop("heatmap_us", None)
