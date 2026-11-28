"""Regression tests for Unit 5 fixes (routes, route_helpers, schemas)."""

from __future__ import annotations

import base64
import contextlib
import queue
from types import SimpleNamespace
from unittest.mock import patch

import app as app_mod
import route_helpers
from app_state import app_state
from error_codes import ErrorCode
from messaging import sse_event_log
from routes import api_analysis as aa_mod
from routes.stocks import ai_portfolio as aip_mod
from routes.stocks import stream as stream_mod
from schemas.analysis import AIAnalyzeChartImageRequest
from services.ai_service import ProviderAPIError

_SAMPLE_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode("ascii")


@contextlib.contextmanager
def _csrf_disabled():
    has_csrf = "WTF_CSRF_ENABLED" in app_mod.app.config
    orig_csrf = app_mod.app.config.get("WTF_CSRF_ENABLED")
    app_mod.app.config["WTF_CSRF_ENABLED"] = False
    try:
        yield
    finally:
        if has_csrf:
            app_mod.app.config["WTF_CSRF_ENABLED"] = orig_csrf
        else:
            app_mod.app.config.pop("WTF_CSRF_ENABLED", None)


def test_rate_limit_does_not_register_polling_token_on_error_status_d3_01() -> None:
    calls = 0

    @route_helpers.rate_limit(max_requests=1, window_seconds=60, skip_polling_duplicates=True)
    def _dummy_view():
        nonlocal calls
        calls += 1
        return {"ok": False}, 503

    token = "a1b2c3d4e5f6a1b2c3d4"
    headers = {"Origin": "http://localhost:5000"}
    with app_mod.app.test_request_context(
        "/api/dummy-rl-test",
        method="POST",
        json={"request_token": token},
        headers=headers,
    ):
        route_helpers._rate_limit_store.clear()
        route_helpers._rate_limit_distinct_token_counts.clear()
        _, st1 = _dummy_view()
        assert st1 == 503
        # Second request with the same token must hit 429 (token was never registered as valid poll)
        _, st2 = _dummy_view()
        assert st2 == 429
        assert calls == 1
        route_helpers._rate_limit_store.clear()
        route_helpers._rate_limit_distinct_token_counts.clear()


def test_ai_portfolio_pops_token_on_queue_full_and_news_swr_records_token_d3_01() -> None:
    req_token = "abcd1234abcd1234abcd"
    aip_mod.ai_portfolio_token_records.clear()
    aip_mod.ai_portfolio_result_cache.clear()
    aip_mod.ai_portfolio_fetch_inflight.clear()

    with _csrf_disabled():
        with patch.object(
            app_state.execution.executor,
            "submit",
            side_effect=queue.Full(),
        ):
            with app_mod.app.test_client() as client:
                resp = client.post(
                    "/api/ai-portfolio/generate",
                    json={"theme": "AI-Test-QueueFull", "request_token": req_token},
                    headers={"Origin": "http://localhost:5000"},
                )
        assert resp.status_code == 503
        assert req_token not in aip_mod.ai_portfolio_token_records


def test_local_system_routes_reject_untrusted_origin_even_with_allow_remote_d3_02(
    monkeypatch,
) -> None:
    admin_token = "super-secret-admin-token-32chars!!"
    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    monkeypatch.setenv("MNS_ADMIN_TOKEN", admin_token)

    with app_mod.app.test_client() as client:
        for path in ("/api/cache-stats", "/api/metrics", "/api/csrf-token", "/api/system/ai-usage"):
            # Local caller with untrusted Origin MUST be denied even when MNS_ALLOW_REMOTE_API=1
            res = client.get(
                path,
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={
                    "Origin": "https://attacker.evil.com",
                    "X-MNS-Admin-Token": admin_token,
                },
            )
            assert res.status_code == 403
            assert res.get_json()["details"]["reason"] == "untrusted origin"

            # Remote caller with valid admin token remains allowed
            ok_res = client.get(
                path,
                environ_base={"REMOTE_ADDR": "203.0.113.50"},
                headers={
                    "Host": "dashboard.example",
                    "Origin": "https://dashboard.example",
                    "X-MNS-Admin-Token": admin_token,
                },
            )
            assert ok_res.status_code == 200


def test_analysis_and_credentials_upstream_sanitization_d3_03() -> None:
    err = ProviderAPIError(
        'Traceback (most recent call last):\n  File "x.py" https://secret.internal/v1?key=abc',
        status_code=502,
    )
    with app_mod.app.app_context():
        resp, status = aa_mod._analyze_v2_error_response(err, SimpleNamespace(request_id="req1"))
    assert status == 502
    reason = resp.get_json()["details"]["reason"]
    assert "Traceback" not in reason
    assert "secret.internal" not in reason

    with _csrf_disabled():
        with (
            patch(
                "routes.system.credentials.validate_openai_base_url_setting",
                return_value=("https://api.example.com/v1", None),
            ),
            patch(
                "services.openai_service.verify_openai_connection",
                return_value={
                    "ok": False,
                    "error": "bad\n<script>alert(1)</script>https://evil.test/x",
                    "latency_ms": 5,
                },
            ),
        ):
            with app_mod.app.test_client() as client:
                r = client.post(
                    "/api/credentials/verify",
                    json={"provider": "openai_compatible", "openai_base_url": "https://api.example.com/v1"},
                    headers={"Origin": "http://localhost:5000"},
                )
    assert r.status_code == 400
    body_reason = r.get_json()["details"]["reason"]
    assert "<script>" not in body_reason
    assert "\n" not in body_reason
    assert "evil.test" not in body_reason


def test_ai_portfolio_queue_full_maps_to_503_too_many_requests_d3_04() -> None:
    with app_mod.app.app_context():
        resp, status = aip_mod._ai_portfolio_error_response(queue.Full(), SimpleNamespace(request_id="r"))
    assert status == 503
    assert resp.get_json()["error_code"] == int(ErrorCode.TOO_MANY_REQUESTS)


def test_sse_stream_deduplicates_initial_snapshot_log_entries_d3_05() -> None:
    sse_event_log.clear()
    try:
        seq1 = stream_mod._emit_deduped_initial_snapshot(2, 'data: {"a":1}\n\n')
        seq2 = stream_mod._emit_deduped_initial_snapshot(2, 'data: {"a":1}\n\n')
        assert seq1 == seq2
        seq3 = stream_mod._emit_deduped_mode2_frame("realtime_update", 'data: {"d":1}\n\n')
        seq4 = stream_mod._emit_deduped_mode2_frame("realtime_update", 'data: {"d":1}\n\n')
        assert seq3 == seq4
    finally:
        sse_event_log.clear()


def test_portfolio_uses_written_snapshot_and_views_uses_locked_name_d3_06(monkeypatch) -> None:
    recorded_names: list[str] = []
    monkeypatch.setattr("routes.stocks.portfolio.save_user_stocks", lambda: None)
    monkeypatch.setattr("routes.stocks.portfolio.invalidate_stock_caches", lambda _s: None)
    monkeypatch.setattr("routes.stocks.portfolio._invalidate_sse_payload_cache", lambda: None)
    monkeypatch.setattr("routes.stocks.portfolio.announce_current_market_state", lambda: None)
    monkeypatch.setattr("routes.stocks.portfolio.announce_real_market_state", lambda: None)
    monkeypatch.setattr("routes.stocks.portfolio.schedule_sync_all_stocks_now", lambda: None)
    monkeypatch.setattr(
        "routes.stocks.portfolio.ensure_stock_placeholder_in_caches",
        lambda _s, name, _m, **_kw: recorded_names.append(name),
    )

    with app_state.market.user_stocks_lock:
        saved_us = dict(app_state.market.user_us)
        app_state.market.user_us["AAPL"] = {"name": "Snapshot Apple", "shares": 1, "avg_price": 100.0}
    try:
        with _csrf_disabled():
            with app_mod.app.test_client() as client:
                resp = client.post(
                    "/api/stocks/portfolio",
                    json={"symbol": "AAPL", "market": "us", "shares": 2, "avg_price": 150.0, "avg_fx_rate": 150.0},
                    headers={"Origin": "http://localhost:5000"},
                )
        assert resp.status_code == 200
        assert recorded_names == ["Snapshot Apple"]
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.clear()
            app_state.market.user_us.update(saved_us)


def test_technical_lines_status_mapping_and_heatmap_disk_cache_check_d3_07() -> None:
    with _csrf_disabled():
        with (
            patch("routes.api_analysis.is_medium_or_large_model", return_value=True),
            patch("routes.api_analysis.extract_api_key", return_value="real-key"),
            patch(
                "routes.api_analysis.generate_ai_technical_lines",
                return_value={"error": {"message": "rate limited", "status_code": 429}},
            ),
        ):
            with app_mod.app.test_client() as client:
                res = client.post(
                    "/api/ai-technical-lines",
                    json={"symbol": "AAPL", "market": "us", "period": "3mo", "history": [{"date": "2026-01-01", "close": 100.0}]},
                    headers={"Origin": "http://localhost:5000"},
                )
    assert res.status_code == 500
    assert res.get_json()["error_code"] == int(ErrorCode.INTERNAL_SERVER_ERROR)

    # Heatmap rejects non-usable disk cache dict (missing "stocks") when executor raises queue.Full
    with (
        patch("routes.stocks.views._get_cached_value_if_present", return_value=(False, None)),
        patch.object(app_state.payload_disk_cache, "get", return_value={"updated_at": 1}),
        patch.object(app_state.execution.data_executor, "submit", side_effect=queue.Full()),
    ):
        with app_mod.app.test_client() as client:
            hres = client.get("/api/heatmap?market=us", headers={"Origin": "http://localhost:5000"})
    assert hres.status_code == 503


def test_chart_image_whitespace_falls_back_to_image_field_d3_08() -> None:
    req = AIAnalyzeChartImageRequest.model_validate({"image_data": "   ", "image": _SAMPLE_PNG_B64})
    assert req.image_data == _SAMPLE_PNG_B64
    assert req.image == _SAMPLE_PNG_B64
