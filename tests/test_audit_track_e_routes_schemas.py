"""Regression tests for Track E Wave 2 fixes across routes, schemas, and helpers."""

from __future__ import annotations

import inspect
import json
import logging
import socket
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import requests
from flask import g

from app import app
from app_state import app_state
from credential_manager import PREVIEW_API_KEY
from error_codes import ErrorCode
from route_helpers import _is_polling_token_inflight
from routes.api_analysis import _chat_error_response, _stream_chat_response
from routes.stocks.ai_portfolio import _ai_portfolio_error_response
from services.ai_service import ProviderAPIError

_LOCAL_HEADERS = {"Origin": "http://localhost:5000"}
_PNG_1X1_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(name="api_client")
def _make_api_client():
    """Return a Flask test client with CSRF disabled for route tests."""
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# SEC-001: api_credentials_verify key isolation and outbound URL checks
# ---------------------------------------------------------------------------


def test_verify_custom_base_url_does_not_leak_stored_openai_key(api_client):
    """Caller-supplied custom base URL without explicit key must not receive stored key."""
    captured: list[tuple[str | None, str | None]] = []

    def _fake_verify(base_url: str | None = None, api_key: str | None = None) -> dict[str, object]:
        captured.append((base_url, api_key))
        return {
            "ok": False,
            "error": "APIキーが未設定または認証に失敗しました",
            "latency_ms": 5,
        }

    with (
        patch("credential_manager.get_openai_api_key", return_value="sk-secret-stored-9999"),
        patch("credential_manager.get_openai_base_url", return_value="https://api.openai.com/v1"),
        patch("services.openai_service.verify_openai_connection", side_effect=_fake_verify),
        patch(
            "utils.networking.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        resp = api_client.post(
            "/api/credentials/verify",
            json={
                "ai_provider": "openai_compatible",
                "openai_base_url": "https://attacker.example/v1",
                "openai_api_key": "",
            },
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 400
    assert captured == [("https://attacker.example/v1", "")]


def test_verify_normalized_matching_base_url_uses_stored_key(api_client):
    """Canonical-equivalent base URLs (scheme/host case, port, slash) still use stored key."""
    captured: list[tuple[str | None, str | None]] = []

    def _fake_verify(base_url: str | None = None, api_key: str | None = None) -> dict[str, object]:
        captured.append((base_url, api_key))
        return {"ok": True, "valid": True, "provider": "openai_compatible", "latency_ms": 11}

    stored_url = "https://api.groq.com/openai/v1/"
    with (
        patch("credential_manager.get_openai_api_key", return_value="sk-groq-stored-key-1234"),
        patch("credential_manager.get_openai_base_url", return_value=stored_url),
        patch("services.openai_service.verify_openai_connection", side_effect=_fake_verify),
    ):
        resp = api_client.post(
            "/api/credentials/verify",
            json={
                "ai_provider": "openai_compatible",
                "openai_base_url": "HTTPS://API.GROQ.COM:443/openai/v1",
                "openai_api_key": "",
            },
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 200
    assert captured == [("HTTPS://API.GROQ.COM:443/openai/v1", "sk-groq-stored-key-1234")]


def test_verify_http_port_difference_blocks_stored_key_fallback(api_client):
    """Same host on a non-default port must not reuse the stored key for another port."""
    captured: list[tuple[str | None, str | None]] = []

    def _fake_verify(base_url: str | None = None, api_key: str | None = None) -> dict[str, object]:
        captured.append((base_url, api_key))
        return {"ok": True, "valid": True, "provider": "openai_compatible", "latency_ms": 7}

    with (
        patch("credential_manager.get_openai_api_key", return_value="sk-stored-localhost-key"),
        patch("credential_manager.get_openai_base_url", return_value="http://localhost:11434/v1"),
        patch("services.openai_service.verify_openai_connection", side_effect=_fake_verify),
    ):
        resp = api_client.post(
            "/api/credentials/verify",
            json={
                "ai_provider": "openai_compatible",
                "openai_base_url": "http://localhost:80/v1",
            },
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 200
    assert captured == [("http://localhost:80/v1", "")]


def test_verify_never_forwards_preview_api_key(api_client):
    """Sentinel PREVIEW_API_KEY must never be transmitted externally."""
    captured: list[tuple[str | None, str | None]] = []

    def _fake_verify(base_url: str | None = None, api_key: str | None = None) -> dict[str, object]:
        captured.append((base_url, api_key))
        return {"ok": True, "valid": True, "provider": "openai_compatible", "latency_ms": 9}

    with (
        patch("credential_manager.get_openai_api_key", return_value=PREVIEW_API_KEY),
        patch("credential_manager.get_openai_base_url", return_value="https://api.openai.com/v1"),
        patch("services.openai_service.verify_openai_connection", side_effect=_fake_verify),
    ):
        resp = api_client.post(
            "/api/credentials/verify",
            json={"ai_provider": "openai_compatible"},
            headers=_LOCAL_HEADERS,
        )
        resp_explicit = api_client.post(
            "/api/credentials/verify",
            json={
                "ai_provider": "openai_compatible",
                "openai_api_key": PREVIEW_API_KEY,
            },
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 200
    assert resp_explicit.status_code == 200
    assert captured == [
        ("https://api.openai.com/v1", ""),
        ("https://api.openai.com/v1", ""),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"ai_provider": "openai_compatible", "openai_base_url": "http://169.254.169.254/latest"},
        {"ai_provider": "openai_compatible", "openai_base_url": "http://[fe80::1]/v1"},
        {"ai_provider": "openai_compatible", "openai_base_url": "http://user:pass@example.com/v1"},
        {"ai_provider": "openai_compatible", "openai_base_url": "http://"},
        {"ai_provider": "openai_compatible", "openai_base_url": "ftp://example.com/v1"},
        {"ai_provider": "openai_compatible", "openai_base_url": 0},
        {"ai_provider": "openai_compatible", "openai_api_key": 0},
    ],
)
def test_verify_rejects_ssrf_userinfo_malformed_and_falsy_nonstring_inputs(api_client, payload):
    """Blocked/malformed URLs or non-string fields must fail with 400 before outbound call."""
    with patch("services.openai_service.verify_openai_connection") as mock_verify:
        resp = api_client.post(
            "/api/credentials/verify",
            json=payload,
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 400
    mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# SEC-002: CredentialsSaveRequest + credentials save SSRF/userinfo rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/v1",
        "http://[fd00:ec2::254]/latest",
        "https://[::ffff:169.254.169.254]/v1",
        "http://user:pass@example.com/v1",
        "http://user@example.com/v1",
        "http://",
        "https://:8080/v1",
        "https://example.com:99999/v1",
        "https://example.com/v1\r\nX-Injected: 1",
    ],
)
def test_credentials_save_blocks_ssrf_userinfo_and_malformed_urls(api_client, bad_url):
    """CredentialsSaveRequest rejects SSRF, userinfo, missing host, and invalid URLs."""
    with patch("routes.api_system.save_api_credentials") as mock_save:
        resp = api_client.post(
            "/api/credentials",
            json={"openai_base_url": bad_url},
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error_code"] == ErrorCode.INVALID_INPUT.value
    assert data["details"]["fields"] == ["openai_base_url"]
    mock_save.assert_not_called()


def test_credentials_save_route_defensive_guard_blocks_ssrf_if_schema_bypassed(api_client):
    """Route handler validates openai_base_url defensively even if schema parser is bypassed."""
    bypass_parsed = SimpleNamespace(
        mistral_api_key=None,
        openai_api_key=None,
        langsearch_api_key=None,
        tavily_api_key=None,
        alphavantage_api_key=None,
        custom_ai_prompt=None,
        mistral_api_mode=None,
        mistral_agent_id=None,
        preview_mode=None,
        mistral_model=None,
        ai_provider=None,
        openai_base_url="http://169.254.169.254/latest",
        openai_model=None,
        openai_embeddings_model=None,
    )
    with (
        patch(
            "routes.system.credentials._parse_credentials_save_request",
            return_value=(bypass_parsed, None),
        ),
        patch("routes.api_system.save_api_credentials") as mock_save,
    ):
        resp = api_client.post(
            "/api/credentials",
            json={"openai_base_url": "http://169.254.169.254/latest"},
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error_code"] == ErrorCode.INVALID_INPUT.value
    assert data["details"]["fields"] == ["openai_base_url"]
    mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# API-001: api_news must shallow-copy cached bundle instead of mutating in place
# ---------------------------------------------------------------------------


def test_api_news_does_not_mutate_cached_result_dict(api_client):
    """Returning /api/news response must not inject disclaimer into the shared cached dict."""
    cached_obj: dict[str, object] = {
        "retrieve_status": "ok",
        "us": {"content": "テスト米国市場ニュース"},
        "news": [{"title": "テスト記事"}],
    }
    captured_cached: list[dict[str, object]] = []

    def _fake_get_news(**_kwargs: object) -> dict[str, object]:
        return cached_obj

    def _fake_set_cached(key: str, value: object, **_kwargs: object) -> None:
        if isinstance(value, dict) and key.startswith("news_bundle_latest_"):
            captured_cached.append(value)

    with (
        patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, "ok")),
        patch(
            "routes.api_analysis.extract_api_key",
            return_value="sk-valid-test-mistral-key-123456",
        ),
        patch("routes.api_analysis.extract_langsearch_api_key", return_value=None),
        patch("routes.api_analysis.extract_tavily_api_key", return_value=None),
        patch(
            "routes.api_analysis.news_service.get_synchronized_market_news",
            side_effect=_fake_get_news,
        ),
        patch("utils.caching._get_cached_value", return_value=None),
        patch("utils.caching._set_cached_value", side_effect=_fake_set_cached),
    ):
        resp = api_client.post(
            "/api/news",
            json={"force": True},
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "disclaimer" in data
    assert len(captured_cached) == 1
    assert "disclaimer" not in cached_obj
    assert "disclaimer" not in captured_cached[0]


# ---------------------------------------------------------------------------
# API-004: Word-boundary 401 detection & upstream error/log sanitization
# ---------------------------------------------------------------------------


def test_chat_error_response_word_boundary_401_and_sanitization(caplog):
    """Substrings like '40123' must not classify as HTTP 401; URLs/tokens are sanitized."""
    caplog.set_level(logging.WARNING)
    with app.test_request_context("/api/chat"):
        g.request_id = "req-wb-401"
        resp_500, status_500 = _chat_error_response(
            Exception("upstream request id 40123 failed at port 4010\r\nforged"),
            g,
        )
        assert status_500 == 500
        body_500 = resp_500.get_json()
        assert body_500["error_code"] == "ERR_CHAT_500"
        assert "40123" not in body_500["reply"]

        resp_auth_clean, status_auth_clean = _chat_error_response(Exception("API認証エラー"), g)
        assert status_auth_clean == 401
        assert "API認証エラー" in resp_auth_clean.get_json()["reply"]

        secret_token = "sk-abcdef1234567890abcdef1234"
        resp_auth_dirty, status_auth_dirty = _chat_error_response(
            Exception(
                "API認証エラー: "
                f"https://internal.secret.local/v1?access_token=secret123 {secret_token}"
            ),
            g,
        )
        assert status_auth_dirty == 401
        dirty_json = json.dumps(resp_auth_dirty.get_json(), ensure_ascii=False)
        assert "API認証エラー" in dirty_json
        assert "internal.secret.local" not in dirty_json
        assert "secret123" not in dirty_json
        assert "sk-abcdef" not in dirty_json

    log_output = caplog.text
    assert "\r" not in log_output
    assert "access_token=secret123" not in log_output
    assert "sk-abcdef1234567890" not in log_output
    assert "?[REDACTED]" in log_output


def test_stream_chat_response_sanitizes_sse_errors_and_logs(caplog):
    """SSE stream error branch must not echo raw upstream errors or query strings/CRLF."""
    caplog.set_level(logging.WARNING)

    def _fake_stream(*_args: object, **_kwargs: object):
        yield {
            "type": "error",
            "status_code": 400,
            "message": (
                "upstream failed https://secret.upstream/v1?sig=abc123\n"
                'Traceback (most recent call last): File "/srv/key.py"'
            ),
        }

    with app.test_request_context("/api/chat", method="POST"):
        g.request_id = "req-stream-001"
        with patch("routes.api_analysis.stream_mistral_chat", side_effect=_fake_stream):
            response = _stream_chat_response(
                api_key="test-key",
                messages_snapshot=[{"role": "user", "content": "hi"}],
                operation_token="op-tok-stream-001",
                chat_key="scope",
                inflight_key="k",
                result_holder=None,
            )
            chunks = response.get_data(as_text=True)

    assert "secret.upstream" not in chunks
    assert "sig=abc123" not in chunks
    assert "Traceback" not in chunks
    assert "リクエスト内容またはモデル設定が無効です" in chunks
    assert "sig=abc123" not in caplog.text
    assert "?[REDACTED]" in caplog.text


def test_analyze_chart_image_sanitizes_upstream_error_response_and_logs(api_client, caplog):
    """/api/analyze-chart-image returns fixed client reason and sanitizes error logs."""
    caplog.set_level(logging.ERROR)
    with (
        patch("routes.api_analysis.extract_api_key", return_value="sk-chart-test-key-123456"),
        patch(
            "routes.api_analysis.analyze_chart_image_with_mistral",
            return_value={
                "error": {
                    "message": (
                        "https://vision.internal/v1?token=supersecret\r\n"
                        'Traceback (most recent call last): File "/srv/vision.py"'
                    )
                }
            },
        ),
    ):
        resp = api_client.post(
            "/api/analyze-chart-image",
            json={"image_data": _PNG_1X1_DATA_URI, "market": "jp", "symbol": "7203"},
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["details"]["reason"] == "画像分析に失敗しました"
    serialized = json.dumps(body, ensure_ascii=False)
    assert "vision.internal" not in serialized
    assert "supersecret" not in serialized
    assert "Traceback" not in serialized
    assert "supersecret" not in caplog.text
    assert "?[REDACTED]" in caplog.text


def test_ai_portfolio_error_response_allowlists_details_and_sanitizes_messages(caplog):
    """_ai_portfolio_error_response must drop unexpected details keys and raw URL/trace errors."""
    caplog.set_level(logging.ERROR)
    with app.test_request_context("/api/ai-portfolio/generate"):
        g.request_id = "req-aip-001"
        err = ProviderAPIError(
            (
                "Upstream https://internal.ai/v1?key=secret\n"
                'Traceback (most recent call last): File "/x.py"'
            ),
            status_code=502,
            details={
                "retry_after": 15,
                "provider_error": {
                    "status_code": 503,
                    "message": "api_key=sk-secret1234567890abcd",
                },
                "secret_headers": {"Authorization": "Bearer supersecret"},
                "internal_url": "https://secret.backend/admin",
            },
        )
        resp, status = _ai_portfolio_error_response(err, g)
        assert status == 502
        data = resp.get_json()
        details = data["details"]
        assert details["retry_after"] == 15
        assert details["provider_error"]["status_code"] == 503
        assert "secret_headers" not in details
        assert "internal_url" not in details
        serialized = json.dumps(data, ensure_ascii=False)
        assert "sk-secret" not in serialized
        assert "internal.ai" not in serialized
        assert "Traceback" not in serialized

        req = httpx.Request("POST", "https://internal-host:8443/v1?auth=topsecret")
        http_err = httpx.HTTPStatusError(
            "401 Unauthorized https://internal-host:8443/v1?auth=topsecret",
            request=req,
            response=httpx.Response(401, request=req),
        )
        http_resp, http_status = _ai_portfolio_error_response(http_err, g)
        assert http_status == 401
        http_details = http_resp.get_json()["details"]
        assert "401" in http_details["reason"]
        assert "internal-host" not in json.dumps(http_resp.get_json(), ensure_ascii=False)

        net_resp, net_status = _ai_portfolio_error_response(
            requests.ConnectionError("Failed connecting to https://internal-db:5432/secret"),
            g,
        )
        assert net_status == 503
        assert "internal-db" not in json.dumps(net_resp.get_json(), ensure_ascii=False)

    assert "auth=topsecret" not in caplog.text
    assert "key=secret" not in caplog.text


# ---------------------------------------------------------------------------
# BE-001: api_copy_ai_portfolio_to_my uses MarketState USD/JPY lock helpers
# ---------------------------------------------------------------------------


def test_copy_ai_portfolio_uses_market_fx_lock_helpers(api_client):
    """US stock copy must read via get_usdjpy_rate_snapshot and write via update_usdjpy_rate."""
    snapshot_calls: list[tuple[float, float]] = []
    update_calls: list[tuple[float, float | None]] = []

    orig_snapshot = app_state.market.get_usdjpy_rate_snapshot
    orig_update = app_state.market.update_usdjpy_rate

    def _spy_snapshot() -> tuple[float, float]:
        res = orig_snapshot()
        snapshot_calls.append(res)
        return res

    def _spy_update(rate: float, ts: float | None = None) -> tuple[float, float]:
        update_calls.append((rate, ts))
        return orig_update(rate, ts=ts)

    isolated_us_stocks: dict[str, dict[str, object]] = {}
    with (
        patch.object(app_state.market, "last_usdjpy_rate", 140.0),
        patch.object(app_state.market, "last_usdjpy_rate_ts", 0.0),
        patch.object(app_state.market, "get_usdjpy_rate_snapshot", side_effect=_spy_snapshot),
        patch.object(app_state.market, "update_usdjpy_rate", side_effect=_spy_update),
        patch("routes.stocks.ai_portfolio.get_current_usdjpy_rate", return_value=(152.5, False)),
        patch(
            "routes.stocks.ai_portfolio._get_stock_container",
            return_value=isolated_us_stocks,
        ),
        patch("routes.stocks.ai_portfolio._watchlist_has_capacity", return_value=True),
        patch("routes.stocks.ai_portfolio.save_user_stocks"),
        patch("routes.stocks.ai_portfolio._stock_display_name", return_value="Apple Inc."),
        patch("routes.stocks.ai_portfolio.invalidate_stock_caches"),
        patch("routes.stocks.ai_portfolio.schedule_sync_all_stocks_now"),
    ):
        resp = api_client.post(
            "/api/ai-portfolio/copy-to-my",
            json={
                "items": [
                    {
                        "symbol": "AAPL",
                        "market": "us",
                        "weight_pct": 100.0,
                        "target_price": 200.0,
                    }
                ]
            },
            headers=_LOCAL_HEADERS,
        )

    assert resp.status_code == 200
    assert len(snapshot_calls) == 1
    assert len(update_calls) == 1
    assert update_calls[0][0] == pytest.approx(152.5)
    assert isolated_us_stocks["AAPL"]["avg_fx_rate"] == pytest.approx(152.5)


# ---------------------------------------------------------------------------
# RT-001: _is_polling_token_inflight dead news_fetch_inflight check removed
# ---------------------------------------------------------------------------


def test_is_polling_token_inflight_preserves_contract_without_dead_news_check():
    """Dead news_fetch_inflight lookup is gone while untracked-token default stays True."""
    src = inspect.getsource(_is_polling_token_inflight)
    assert "news_fetch_inflight" not in src
    with app.test_request_context("/api/ai-portfolio/generate", method="POST"):
        assert _is_polling_token_inflight("untracked-token-xyz") is True
