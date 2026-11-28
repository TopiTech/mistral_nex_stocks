"""Unit 4 comprehensive review fixes regression tests (2026-11-28)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app as app_mod
from routes.stocks import ai_portfolio as ap_mod
from routes.system import credentials as creds_mod
from routes.system import guards as guards_mod


def test_system_read_routes_reverse_proxy_remote_mode_allows_remote_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_token = "super-secret-admin-token-32chars!!"
    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    monkeypatch.setenv("MNS_PROXY_FIX", "1")
    monkeypatch.setenv("MNS_ADMIN_TOKEN", admin_token)

    with app_mod.app.test_client() as client:
        for path in ("/api/cache-stats", "/api/metrics", "/api/csrf-token", "/api/system/ai-usage"):
            ok_res = client.get(
                path,
                environ_base={"REMOTE_ADDR": "203.0.113.50"},
                headers={
                    "Host": "dashboard.example",
                    "Origin": "https://dashboard.example",
                    "X-MNS-Admin-Token": admin_token,
                },
            )
            assert ok_res.status_code == 200, f"{path} failed in proxy remote mode: {ok_res.data}"

            # Production mode rejects spoofed loopback Host even behind proxy
            monkeypatch.setenv("MNS_PROD", "1")
            try:
                spoof_res = client.get(
                    path,
                    environ_base={"REMOTE_ADDR": "203.0.113.50"},
                    headers={
                        "Host": "127.0.0.1",
                        "Origin": "https://dashboard.example",
                        "X-MNS-Admin-Token": admin_token,
                    },
                )
                assert spoof_res.status_code == 403
            finally:
                monkeypatch.delenv("MNS_PROD", raising=False)


def test_ai_portfolio_generate_failed_attempt_consumes_request_token() -> None:
    tok = "apf_fail_consume_token_123456"
    with ap_mod.ai_portfolio_fetch_lock:
        ap_mod.ai_portfolio_token_records.pop(tok, None)
        ap_mod.ai_portfolio_result_cache.clear()
        ap_mod.ai_portfolio_fetch_inflight.clear()

    try:
        with app_mod.app.test_client() as client:
            csrf_res = client.get("/api/csrf-token", environ_base={"REMOTE_ADDR": "127.0.0.1"})
            csrf_token = csrf_res.get_json()["csrf_token"]
            with (
                patch.object(ap_mod, "require_trusted_or_admin", return_value=(True, None)),
                patch.object(ap_mod, "extract_api_key", return_value=""),
                patch.object(
                    ap_mod,
                    "generate_ai_portfolio_by_theme",
                    side_effect=RuntimeError("upstream failure"),
                ),
                patch("route_helpers._submit_in_app_context", lambda _ex, fn: fn()),
            ):
                res = client.post(
                    "/api/ai-portfolio/generate",
                    json={"theme": "fail-token-theme", "request_token": tok},
                    headers={"X-CSRFToken": csrf_token},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )
                assert res.status_code in (500, 502)
                with ap_mod.ai_portfolio_fetch_lock:
                    assert tok in ap_mod.ai_portfolio_token_records
                    assert ap_mod.ai_portfolio_token_records[tok][1] is True
    finally:
        with ap_mod.ai_portfolio_fetch_lock:
            ap_mod.ai_portfolio_token_records.pop(tok, None)
            ap_mod.ai_portfolio_result_cache.clear()
            ap_mod.ai_portfolio_fetch_inflight.clear()


def test_api_search_sanitizes_cached_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    from routes.stocks import quotes as quotes_mod

    monkeypatch.setattr(quotes_mod, "require_trusted_or_admin", lambda *_a, **_k: (True, None))
    monkeypatch.setattr(
        quotes_mod,
        "get_cached",
        lambda *_a, **_k: {
            "error": 'Traceback (most recent call last): https://internal.secret/api?key=123'
        },
    )

    with app_mod.app.test_client() as client:
        res = client.get("/api/search?q=AAPL", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert res.status_code == 502
    reason = res.get_json()["details"]["reason"]
    assert "Traceback" not in reason
    assert "internal.secret" not in reason


def test_api_health_filters_credential_state_with_public_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.api_system

    fake_state = {k: False for k in guards_mod.PUBLIC_SAFE_CREDENTIAL_KEYS}
    fake_state["internal_secret_dump"] = "DO_NOT_EXPOSE"
    monkeypatch.setattr(routes.api_system, "get_api_credential_state", lambda: dict(fake_state))
    monkeypatch.setattr(routes.api_system, "_is_local_request", lambda _req: True)
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)
    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)

    assert "has_mistral_api_key" in guards_mod.PUBLIC_SAFE_CREDENTIAL_KEYS
    with app_mod.app.test_client() as client:
        res = client.get("/api/health", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert res.status_code == 200
    body = res.get_json()
    assert "internal_secret_dump" not in body
    assert "has_mistral_api_key" in body
    _ = creds_mod


def test_ai_portfolio_rebalance_request_normalizes_blank_theme_to_tech() -> None:
    from schemas.ai_portfolio import AIPortfolioRebalanceRequest

    req_empty = AIPortfolioRebalanceRequest.model_validate({"theme": ""})
    req_spaces = AIPortfolioRebalanceRequest.model_validate({"theme": "   "})
    assert req_empty.theme == "tech"
    assert req_spaces.theme == "tech"
