"""Regression tests for Unit 3 (schemas, route helpers, API routes) fixes."""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from schemas.ai_portfolio import AIPortfolioGenerateRequest, AIPortfolioRebalanceRequest

_LOCAL_HEADERS = {
    "Origin": "http://localhost:5000",
    "Host": "localhost:5000",
}
_PNG_1X1_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _csrf_headers(client: Any) -> dict[str, str]:
    resp = client.get("/api/csrf-token", headers={"Host": "localhost:5000"})
    token = resp.get_json()["csrf_token"]
    return {**_LOCAL_HEADERS, "X-CSRFToken": token}


def test_u3_1_rebalance_null_theme_falls_back_to_tech_while_generate_still_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_mod

    reb = AIPortfolioRebalanceRequest.model_validate({"theme": None})
    assert reb.theme == "tech"

    with pytest.raises(ValidationError):
        AIPortfolioGenerateRequest.model_validate({"theme": None})

    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)
    with (
        patch("routes.stocks.ai_portfolio.extract_api_key", return_value="sk-test-123456"),
        patch(
            "routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme",
            return_value={
                "id": "tech",
                "title": "Tech",
                "theme": "tech",
                "items": [],
            },
        ) as mock_gen,
        app_mod.app.test_client() as client,
    ):
        headers = _csrf_headers(client)
        resp = client.post(
            "/api/ai-portfolio/rebalance",
            json={"theme": None},
            headers=headers,
        )
        assert resp.status_code == 200
        mock_gen.assert_called_once()
        assert mock_gen.call_args[0][0] == "tech"


def test_u3_2_credentials_rejects_oversized_and_control_char_openai_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_mod

    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)
    with (
        patch("routes.api_system.save_api_credentials") as mock_save,
        app_mod.app.test_client() as client,
    ):
        headers = _csrf_headers(client)
        for field_name in ("openai_model", "openai_embeddings_model"):
            resp_long = client.post(
                "/api/credentials",
                json={field_name: "m" * 201},
                headers=headers,
            )
            assert resp_long.status_code == 400
            assert resp_long.get_json()["ok"] is False

            resp_ctrl = client.post(
                "/api/credentials",
                json={field_name: "gpt-4o\r\nbad"},
                headers=headers,
            )
            assert resp_ctrl.status_code == 400
            assert resp_ctrl.get_json()["ok"] is False

        mock_save.assert_not_called()


def test_u3_3_cache_stats_and_metrics_aggregate_both_disk_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_mod
    from app_state import app_state

    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)

    stock_stats = {
        "disk_cache_entries": 40,
        "disk_cache_total_size_bytes": 4096,
        "disk_cache_max_entries": 500,
        "disk_cache_default_ttl": 3600,
        "disk_cache_last_cleanup_ts": 100.0,
    }
    payload_stats = {
        "disk_cache_entries": 2,
        "disk_cache_total_size_bytes": 512,
        "disk_cache_max_entries": 200,
        "disk_cache_default_ttl": 300,
        "disk_cache_last_cleanup_ts": 250.0,
    }

    with (
        patch.object(app_state.stock_disk_cache, "stats", return_value=stock_stats),
        patch.object(app_state.payload_disk_cache, "stats", return_value=payload_stats),
        app_mod.app.test_client() as client,
    ):
        cs_resp = client.get("/api/cache-stats", headers={"Host": "localhost:5000"})
        assert cs_resp.status_code == 200
        cs = cs_resp.get_json()["cache_stats"]
        assert cs["disk_cache_entries"] == 42
        assert cs["disk_cache_total_size_bytes"] == 4608
        assert cs["disk_cache_max_entries"] == 700
        assert cs["disk_cache_last_cleanup_ts"] == 250.0
        assert cs["stock_disk_cache"] == stock_stats
        assert cs["payload_disk_cache"] == payload_stats

        m_resp = client.get("/api/metrics", headers={"Host": "localhost:5000"})
        assert m_resp.status_code == 200
        m_cache = m_resp.get_json()["cache"]
        assert m_cache["disk_cache_entries"] == 42
        assert m_cache["disk_cache_total_size_bytes"] == 4608
        assert m_cache["stock_disk_cache"] == stock_stats
        assert m_cache["payload_disk_cache"] == payload_stats


def test_u3_4_api_news_swr_populates_joined_caller_token_cache_and_completed_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_mod
    from app_state import app_state
    from routes import api_analysis as aa_mod

    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)

    swr_started = threading.Event()
    swr_release = threading.Event()
    stale_bundle = {"us_news": [{"title": "Stale"}], "jp_news": [], "summary": "old"}
    fresh_uncacheable = {"error": False, "us_news": [], "jp_news": []}
    join_token = "joiner_token_0123456789abcdef"

    def _slow_sync_news(**_kwargs: Any) -> dict[str, Any]:
        swr_started.set()
        assert swr_release.wait(timeout=5.0)
        return fresh_uncacheable

    with aa_mod.news_fetch_lock:
        aa_mod.news_fetch_inflight.clear()
        aa_mod.news_result_cache.clear()
        aa_mod.news_completed_tokens.clear()
        aa_mod.news_uncacheable_results.clear()

    def _fake_get_cached(key: str, duration: int = 0, default: Any = None) -> Any:
        if key == "news_bundle_latest_ddgs_only":
            return stale_bundle if not swr_started.is_set() else None
        if key == "news_bundle_latest_ddgs_only_ts":
            return 0.0
        return default

    from concurrent.futures import ThreadPoolExecutor

    try:
        with (
            ThreadPoolExecutor(max_workers=2) as real_exec,
            patch("routes.api_analysis.extract_api_key", return_value="sk-real-key-123456"),
            patch("routes.api_analysis.credential_manager.is_preview_api_key", return_value=False),
            patch("routes.api_analysis.credential_manager.is_openai_provider", return_value=False),
            patch("routes.api_analysis._is_cacheable_news_bundle", return_value=False),
            patch("utils.caching._get_cached_value", side_effect=_fake_get_cached),
            patch("utils.caching._set_cached_value"),
            patch("routes.api_analysis.NEWS_PREPARE_WAIT_SEC", 0.05),
            patch.object(aa_mod.news_service, "get_synchronized_market_news", side_effect=_slow_sync_news),
            patch.object(app_state.execution, "news_executor", real_exec),
            app_mod.app.test_client() as client,
        ):
            headers = _csrf_headers(client)
            r1 = client.post("/api/news", json={"strategy": "balanced"}, headers=headers)
            assert r1.status_code == 200
            assert swr_started.wait(timeout=3.0)

            # Second request arrives after latest_bundle cleared while SWR job is still running
            r2 = client.post(
                "/api/news",
                json={"strategy": "balanced", "request_token": join_token},
                headers=headers,
            )
            assert r2.status_code == 200
            b2 = r2.get_json()
            assert b2["fetching"] is True
            assert b2["elapsed_sec"] >= 0.0

            swr_release.set()
            deadline = time.monotonic() + 3.0
            with aa_mod.news_fetch_lock:
                pass
            while time.monotonic() < deadline:
                with aa_mod.news_fetch_lock:
                    if not aa_mod.news_fetch_inflight:
                        break
                time.sleep(0.02)

            rc_key = f"news_ddgs_only:{join_token}"
            with aa_mod.news_fetch_lock:
                assert aa_mod.news_completed_tokens.get(rc_key) is True
                assert rc_key in aa_mod.news_uncacheable_results
    finally:
        swr_release.set()
        with aa_mod.news_fetch_lock:
            aa_mod.news_fetch_inflight.clear()
            aa_mod.news_result_cache.clear()
            aa_mod.news_completed_tokens.clear()
            aa_mod.news_uncacheable_results.clear()


def test_u3_5_system_read_endpoints_fail_closed_for_remote_without_admin_or_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_mod

    admin_token = "super-secret-admin-token-32chars!!"
    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    monkeypatch.delenv("MNS_PROXY_FIX", raising=False)
    monkeypatch.setenv("MNS_ADMIN_TOKEN", admin_token)

    with app_mod.app.test_client() as client:
        for path in (
            "/api/cache-stats",
            "/api/metrics",
            "/api/csrf-token",
            "/api/system/ai-usage",
        ):
            # Missing admin token in remote mode must fail closed.
            denied_resp = client.get(
                path,
                environ_base={"REMOTE_ADDR": "203.0.113.50"},
                headers={"Host": "dashboard.example"},
            )
            assert denied_resp.status_code in (401, 403), (
                f"{path} must deny unauthenticated remote request"
            )
            # Valid admin token in remote mode retains access.
            ok_resp = client.get(
                path,
                environ_base={"REMOTE_ADDR": "203.0.113.50"},
                headers={
                    "Host": "dashboard.example",
                    "X-MNS-Admin-Token": admin_token,
                },
            )
            assert ok_resp.status_code == 200, (
                f"{path} with valid admin token in remote mode should succeed"
            )

        monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "0")
        for path in (
            "/api/cache-stats",
            "/api/metrics",
            "/api/csrf-token",
            "/api/system/ai-usage",
        ):
            resp = client.get(
                path,
                environ_base={"REMOTE_ADDR": "203.0.113.50"},
                headers={
                    "Host": "dashboard.example",
                    "X-MNS-Admin-Token": admin_token,
                },
            )
            assert resp.status_code == 403, f"{path} failed to enforce _is_local_request when local-only"


def test_u3_6_api_reset_stocks_preserves_dict_identity_on_success_and_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_mod
    from app_state import app_state
    from routes.stocks import views as views_mod
    from utils.storage import UserStocksPersistError

    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)

    with app_state.market.user_stocks_lock:
        orig_us = dict(app_state.market.user_us)
        orig_jp = dict(app_state.market.user_jp)
        orig_idx = dict(app_state.market.user_idx)
        us_ref = app_state.market.user_us
        jp_ref = app_state.market.user_jp
        idx_ref = app_state.market.user_idx
        us_ref["AAPL"] = {"name": "Apple"}

    try:
        with app_mod.app.test_client() as client:
            headers = _csrf_headers(client)
            with patch.object(
                views_mod,
                "save_user_stocks",
                side_effect=UserStocksPersistError("disk full"),
            ):
                fail_resp = client.post("/api/stocks/reset", json={}, headers=headers)
            assert fail_resp.status_code == 503
            assert app_state.market.user_us is us_ref
            assert app_state.market.user_jp is jp_ref
            assert app_state.market.user_idx is idx_ref
            assert "AAPL" in us_ref

            with (
                patch.object(views_mod, "save_user_stocks", return_value=None),
                patch.object(views_mod, "schedule_sync_all_stocks_now"),
            ):
                ok_resp = client.post("/api/stocks/reset", json={}, headers=headers)
            assert ok_resp.status_code == 200
            assert app_state.market.user_us is us_ref
            assert app_state.market.user_jp is jp_ref
            assert app_state.market.user_idx is idx_ref
            assert us_ref == {}
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.clear()
            app_state.market.user_us.update(orig_us)
            app_state.market.user_jp.clear()
            app_state.market.user_jp.update(orig_jp)
            app_state.market.user_idx.clear()
            app_state.market.user_idx.update(orig_idx)


def test_u3_7_analyze_chart_image_preserves_upstream_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_mod
    from error_codes import ErrorCode

    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)

    expected_cases = [
        (401, ErrorCode.API_AUTH_FAILED),
        (403, ErrorCode.FORBIDDEN),
        (429, ErrorCode.API_RATE_LIMITED),
        (503, ErrorCode.API_SERVICE_ERROR),
        (504, ErrorCode.TIMEOUT_ERROR),
    ]

    with app_mod.app.test_client() as client:
        headers = _csrf_headers(client)
        for upstream_status, expected_code in expected_cases:
            with (
                patch("routes.api_analysis.extract_api_key", return_value="sk-real-key-123456"),
                patch(
                    "routes.api_analysis.analyze_chart_image_with_mistral",
                    return_value={"error": {"status_code": upstream_status, "message": "secret-trace"}},
                ),
            ):
                ci_resp = client.post(
                    "/api/analyze-chart-image",
                    json={"image_data": _PNG_1X1_DATA_URI, "market": "jp", "symbol": "7203"},
                    headers=headers,
                )
                assert ci_resp.status_code == upstream_status
                ci_body = ci_resp.get_json()
                assert ci_body["error_code"] == int(expected_code)
                assert "secret-trace" not in json.dumps(ci_body)
