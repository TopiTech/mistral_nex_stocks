"""
Comprehensive regression test suite verifying fixes from the project-wide code review:
1. OpenAI circuit breaker HALF_OPEN single-probe claiming and release lifecycle.
2. OpenAI cache key discrimination across base_url, API key scopes, and Pydantic response formats.
3. OpenAI credential verification error normalization adhering to unified error envelope contract.
4. Heatmap payload & route unified success envelope (ok: True, success: True).
5. AI portfolio rebalance unified success envelope (ok: True, success: True, message).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pydantic import BaseModel

from app import create_app
from app_state import app_state
from error_codes import ErrorCode
from services.market_data_service import build_heatmap_payload
from services.openai_service import (
    _build_openai_cache_key,
    call_openai_chat,
    stream_openai_chat,
)


class DummySchemaA(BaseModel):
    summary: str


class DummySchemaB(BaseModel):
    summary: str


def test_openai_cache_key_isolation_base_url_and_credentials():
    """Verify _build_openai_cache_key differentiates base_url and credential_scope."""
    messages = [{"role": "user", "content": "analyze"}]
    key_default = _build_openai_cache_key(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
        response_format=None,
        tools=None,
    )

    key_alt_url = _build_openai_cache_key(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
        response_format=None,
        tools=None,
        base_url="https://api.groq.com/openai/v1",
    )
    assert key_default != key_alt_url

    key_user_a = _build_openai_cache_key(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
        response_format=None,
        tools=None,
        credential_scope="user_a_key_digest",
    )
    key_user_b = _build_openai_cache_key(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
        response_format=None,
        tools=None,
        credential_scope="user_b_key_digest",
    )
    assert key_user_a != key_user_b


def test_openai_cache_key_pydantic_schema_discrimination():
    """Verify different Pydantic models yield distinct cache keys."""
    messages = [{"role": "user", "content": "evaluate"}]
    key_a = _build_openai_cache_key(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
        response_format=DummySchemaA,
        tools=None,
    )
    key_b = _build_openai_cache_key(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
        response_format=DummySchemaB,
        tools=None,
    )
    assert key_a != key_b


def test_openai_circuit_breaker_half_open_probe_claim_and_release():
    """Verify HALF_OPEN circuit allows only one probe and rejects concurrent calls."""
    service = "openai_compatible"
    with app_state.market.circuit_lock:
        app_state.market.circuit_states[service] = {
            "failures": 0,
            "last_failure_time": 0.0,
            "status": "HALF_OPEN",
            "open_time": 0.0,
            "cooldown_sec": 60,
            "probing": False,
        }

    dummy_response = MagicMock()
    dummy_response.status_code = 200
    dummy_response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 10},
    }

    mock_client = MagicMock()
    mock_client.post.return_value = dummy_response

    # In HALF_OPEN, claim probe slot manually
    claimed = app_state.market.try_claim_circuit_probe(service)
    assert claimed is True
    assert app_state.market.circuit_states[service]["probing"] is True

    # A concurrent call should now be rejected with 503 because probe is in flight
    res = call_openai_chat(
        api_key="sk-test-mock-key-12345",
        messages=[{"role": "user", "content": "hello"}],
        use_cache=False,
    )
    assert "error" in res
    assert res["error"]["status_code"] == 503
    assert "サーキット回復プローブ実行中" in res["error"]["message"]

    # Release probe slot
    app_state.market.release_circuit_probe(service)
    assert app_state.market.circuit_states[service]["probing"] is False

    # Now call_openai_chat can execute and claim probe
    with patch("services.openai_service._get_openai_client", return_value=mock_client):
        res_success = call_openai_chat(
            api_key="sk-test-mock-key-12345",
            messages=[{"role": "user", "content": "hello"}],
            use_cache=False,
        )
        assert "choices" in res_success
        # Circuit should now be closed and probing cleared
        assert app_state.market.circuit_states[service]["status"] == "CLOSED"
        assert app_state.market.circuit_states[service]["probing"] is False

    with app_state.market.circuit_lock:
        app_state.market.circuit_states[service] = {
            "failures": 0,
            "last_failure_time": 0.0,
            "status": "CLOSED",
            "open_time": 0.0,
            "cooldown_sec": 60,
            "probing": False,
        }


def test_openai_circuit_breaker_stream_half_open_probe():
    """Verify stream_openai_chat respects HALF_OPEN circuit probe claiming."""
    service = "openai_compatible"
    with app_state.market.circuit_lock:
        app_state.market.circuit_states[service] = {
            "failures": 0,
            "last_failure_time": 0.0,
            "status": "HALF_OPEN",
            "open_time": 0.0,
            "cooldown_sec": 60,
            "probing": True,  # Already claimed by another thread
        }

    events = list(
        stream_openai_chat(
            api_key="sk-test-stream-key-12345",
            messages=[{"role": "user", "content": "stream"}],
        )
    )
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["status_code"] == 503
    assert "サーキット回復プローブ実行中" in events[0]["message"]

    with app_state.market.circuit_lock:
        app_state.market.circuit_states[service] = {
            "failures": 0,
            "last_failure_time": 0.0,
            "status": "CLOSED",
            "open_time": 0.0,
            "cooldown_sec": 60,
            "probing": False,
        }


def test_openai_credentials_verify_normalized_error_envelope():
    """Verify POST /api/credentials/verify returns unified error envelope when OpenAI verification fails."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    mock_fail_dict = {
        "ok": False,
        "valid": False,
        "error": "OpenAI互換APIの認証に失敗しました。APIキーを確認してください。",
        "latency_ms": 42,
    }

    with app.test_client() as client:
        with patch("services.openai_service.verify_openai_connection", return_value=mock_fail_dict):
            res = client.post(
                "/api/credentials/verify",
                json={
                    "ai_provider": "openai_compatible",
                    "openai_api_key": "invalid-key-test",
                    "openai_base_url": "https://api.openai.com/v1",
                },
                headers={"Origin": "http://localhost:5000"},
            )
            assert res.status_code == 400
            data = res.get_json()
            assert data["ok"] is False
            assert data["valid"] is False
            assert data["error_flag"] is True
            assert data["code"] == "INVALID_API_KEY"
            assert data["error_code"] == ErrorCode.INVALID_API_KEY.value
            assert "error" in data
            assert data["latency_ms"] == 42


def test_build_heatmap_payload_unified_envelope():
    """Verify build_heatmap_payload includes ok and success in the returned payload."""
    mock_batch = [
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "price": 150.0,
            "market_cap": 2500000000000,
            "change_percent": 1.5,
            "sector": "Technology",
        }
    ]
    payload = build_heatmap_payload("us", ["AAPL"], fetch_batch_fn=lambda items, lightweight: mock_batch)
    assert payload["ok"] is True
    assert payload["success"] is True
    assert len(payload["stocks"]) == 1
    assert payload["stocks"][0]["symbol"] == "AAPL"


def test_api_heatmap_unified_success_envelope():
    """Verify GET /api/heatmap produces ok: True and success: True under all response modes."""
    app = create_app()
    app.config["TESTING"] = True

    with app.test_client() as client:
        # 1. Fetching response mode
        with patch(
            "routes.stocks.views._get_cached_value_if_present", return_value=(False, None)
        ), patch(
            "routes.stocks.views.app_state.payload_disk_cache.get", return_value=None
        ):
            res = client.get("/api/heatmap?market=us")
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            assert data["success"] is True
            assert "stocks" in data

        # 2. In-memory cached response mode
        mock_cached = {"stocks": [{"symbol": "MSFT", "price": 400.0, "market_cap": 3000000000000}]}
        with patch(
            "routes.stocks.views._get_cached_value_if_present",
            return_value=(True, mock_cached),
        ):
            res2 = client.get("/api/heatmap?market=us")
            assert res2.status_code == 200
            data2 = res2.get_json()
            assert data2["ok"] is True
            assert data2["success"] is True
            assert len(data2["stocks"]) == 1


def test_ai_portfolio_rebalance_unified_success_envelope():
    """Verify POST /api/ai-portfolio/rebalance returns ok: True and success: True with Japanese message."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    fake_portfolio = {
        "theme": "ai",
        "stocks": [{"symbol": "NVDA", "weight_pct": 50.0}, {"symbol": "MSFT", "weight_pct": 50.0}],
    }

    with app.test_client() as client:
        with patch("routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme", return_value=fake_portfolio):
            res = client.post(
                "/api/ai-portfolio/rebalance",
                json={"theme": "ai"},
                headers={"Origin": "http://localhost:5000"},
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            assert data["success"] is True
            assert data["message"] == "リバランスが完了しました"
            assert data["portfolio"]["theme"] == "ai"
