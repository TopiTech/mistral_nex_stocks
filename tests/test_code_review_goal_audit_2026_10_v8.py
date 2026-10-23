"""Unit tests for comprehensive code review and goal remediation (2026-10 v8).

Covers:
1. Rejection of negative prices in /api/analyze-v2 matching domain and Pydantic constraints.
2. Prevention of AB-BA deadlock in /api/stocks/add_ext by ensuring user_stocks_lock is released before save_user_stocks.
3. Atomic holdings initialization and update in ensure_stock_placeholder_in_caches with canonical target-then-current cache ordering.
4. Prompt injection prevention in analyze_chart_image_with_openai via _sanitize_prompt_text.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest

from app import create_app
from app_state import app_state
from error_codes import ErrorCode
from route_helpers import ensure_stock_placeholder_in_caches
from services.openai_service import _sanitize_prompt_text, analyze_chart_image_with_openai


@pytest.fixture
def test_app():
    app = create_app(
        config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True
    )
    return app


@pytest.fixture
def client(test_app):
    with test_app.test_client() as c:
        yield c


def test_api_analyze_v2_rejects_negative_price(client):
    """Verify /api/analyze-v2 strictly rejects negative price values."""
    headers = {
        "Origin": "http://localhost:5000",
        "Content-Type": "application/json",
    }

    with patch(
        "routes.api_analysis.extract_api_key",
        return_value="fake-api-key-123456789012345678901234",
    ):
        # Negative price -10.0
        resp = client.post(
            "/api/analyze-v2",
            json={"symbol": "AAPL", "market": "us", "price": -10.0, "request_token": "a" * 32},
            headers=headers,
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data.get("error_code") == ErrorCode.INVALID_INPUT.value
        assert "price must be a finite non-negative number" in data.get("details", {}).get(
            "reason", ""
        )

        # Negative price -0.001
        resp_small = client.post(
            "/api/analyze-v2",
            json={"symbol": "AAPL", "market": "us", "price": -0.001, "request_token": "a" * 32},
            headers=headers,
        )
        assert resp_small.status_code == 400
        assert resp_small.get_json()["error_code"] == ErrorCode.INVALID_INPUT.value

        # Non-negative price 0.0 (valid in schema, should pass price validation)
        with (
            patch("schemas.analysis.is_valid_symbol", return_value=True),
            patch("routes.api_analysis.fetch_stock", return_value={"price": 0.0, "chart_data": []}),
            patch("routes.api_analysis.collect_symbol_research_context", return_value=""),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch(
                "routes.api_analysis.call_mistral_chat",
                return_value={
                    "recommendation": "HOLD",
                    "reasoning": "Test reasoning",
                    "time_horizon": "短期",
                    "risk_level": "中",
                    "catalysts": [],
                    "risks": [],
                    "sentiment": "NEUTRAL",
                },
            ),
        ):
            resp_zero = client.post(
                "/api/analyze-v2",
                json={"symbol": "AAPL", "market": "us", "price": 0.0, "request_token": "a" * 32},
                headers=headers,
            )
            # Should not be rejected for price (status can be 200 or polling)
            assert resp_zero.status_code != 400 or (
                "price" not in resp_zero.get_json().get("details", {}).get("reason", "")
            )


def test_api_add_stock_ext_releases_user_stocks_lock_before_save(test_app):
    """Verify api_add_stock_ext releases user_stocks_lock before calling save_user_stocks."""
    lock_was_held_during_save = None

    def mock_save():
        nonlocal lock_was_held_during_save
        lock = app_state.market.user_stocks_lock
        lock_was_held_during_save = bool(
            getattr(lock, "_is_owned", getattr(lock, "locked", lambda: False))()
        )

    try:
        with (
            patch("utils.env_helpers._is_remote_api_enabled", return_value=False),
            patch("utils.networking._is_local_request", return_value=True),
            patch("utils.networking._is_loopback_ip", return_value=True),
            patch("utils.networking.is_allowed_trusted_origin", return_value=True),
            patch(
                "credential_manager.get_or_create_extension_api_token",
                return_value="test-token-ext-12345",
            ),
            patch("utils.storage.save_user_stocks", side_effect=mock_save),
            patch("routes.stocks.views.save_user_stocks", side_effect=mock_save),
            patch("routes.stocks.views.invalidate_stock_caches"),
            patch("routes.stocks.views.ensure_stock_placeholder_in_caches"),
            patch("routes.stocks.common._announce_watchlist_state"),
            patch("routes.stocks.common._sync_realtime_symbol"),
            patch("app_bg.schedule_sync_all_stocks_now"),
        ):
            client = test_app.test_client()
            res = client.post(
                "/api/stocks/add_ext",
                headers={
                    "X-MNS-Extension-Request": "true",
                    "Authorization": "Bearer test-token-ext-12345",
                    "Origin": "http://127.0.0.1:5000",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                json={"symbol": "DEADLOCK_TEST", "market": "us", "name": "Deadlock Test"},
            )
            assert res.status_code == 200
            assert res.get_json()["ok"] is True
            # Crucial check: user_stocks_lock must NOT be held during save_user_stocks
            assert lock_was_held_during_save is False
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.pop("DEADLOCK_TEST", None)


def test_ensure_stock_placeholder_in_caches_atomic_holdings():
    """Verify ensure_stock_placeholder_in_caches correctly initializes and updates holdings."""
    with app_state.cache.sse_data_lock:
        orig_target = copy.deepcopy(app_state.market.target_stocks_cache)
        orig_current = copy.deepcopy(app_state.market.current_stocks_cache)
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}

    try:
        # 1. Add new entry with initial holdings
        ensure_stock_placeholder_in_caches(
            "TEST_HOLDING",
            "Test Holding Corp",
            "us",
            shares=15.5,
            avg_price=120.0,
            avg_fx_rate=152.3,
        )

        with app_state.cache.sse_data_lock:
            for cache in (
                app_state.market.target_stocks_cache,
                app_state.market.current_stocks_cache,
            ):
                items = cache["us"]
                assert len(items) == 1
                entry = items[0]
                assert entry["symbol"] == "TEST_HOLDING"
                assert entry["shares"] == 15.5
                assert entry["avg_price"] == 120.0
                assert entry["avg_fx_rate"] == 152.3

        # 2. Update existing entry in-place
        ensure_stock_placeholder_in_caches(
            "TEST_HOLDING",
            "Test Holding Corp",
            "us",
            shares=30.0,
            avg_price=125.0,
            avg_fx_rate=None,
        )

        with app_state.cache.sse_data_lock:
            for cache in (
                app_state.market.target_stocks_cache,
                app_state.market.current_stocks_cache,
            ):
                items = cache["us"]
                assert len(items) == 1
                entry = items[0]
                assert entry["shares"] == 30.0
                assert entry["avg_price"] == 125.0
                assert "avg_fx_rate" not in entry

        # 3. JP market must never retain avg_fx_rate
        ensure_stock_placeholder_in_caches(
            "7203.T",
            "Toyota Motor",
            "jp",
            shares=100.0,
            avg_price=2500.0,
            avg_fx_rate=150.0,  # should be ignored for JP
        )

        with app_state.cache.sse_data_lock:
            for cache in (
                app_state.market.target_stocks_cache,
                app_state.market.current_stocks_cache,
            ):
                items = cache["jp"]
                assert len(items) == 1
                entry = items[0]
                assert entry["symbol"] == "7203.T"
                assert entry["shares"] == 100.0
                assert entry["avg_price"] == 2500.0
                assert "avg_fx_rate" not in entry

    finally:
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache = orig_target
            app_state.market.current_stocks_cache = orig_current


def test_ensure_stock_placeholder_cache_order():
    """Verify target_stocks_cache is updated before current_stocks_cache."""
    update_order = []

    class OrderTrackingDict(dict):
        def __init__(self, name: str, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.name = name

        def __getitem__(self, key: Any) -> Any:
            update_order.append(f"access_{self.name}")
            return super().__getitem__(key)

    with app_state.cache.sse_data_lock:
        orig_target = app_state.market.target_stocks_cache
        orig_current = app_state.market.current_stocks_cache
        target_mock = OrderTrackingDict("target", {"us": []})
        current_mock = OrderTrackingDict("current", {"us": []})
        app_state.market.target_stocks_cache = target_mock
        app_state.market.current_stocks_cache = current_mock

    try:
        ensure_stock_placeholder_in_caches("ORDER_TEST", "Order Test", "us")
        assert update_order == ["access_target", "access_current"]
    finally:
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache = orig_target
            app_state.market.current_stocks_cache = orig_current


def test_openai_service_prompt_sanitization():
    """Verify _sanitize_prompt_text strips control characters and XML metacharacters."""
    dirty_text = "<script>alert('test')</script>\x00\x08\r\nHello & World"
    clean = _sanitize_prompt_text(dirty_text, max_len=100)
    assert "<" not in clean
    assert ">" not in clean
    assert "\x00" not in clean
    assert "\x08" not in clean
    assert "script alert('test') /script" in clean
    assert "Hello & World" in clean


def test_analyze_chart_image_with_openai_sanitizes_inputs():
    """Verify analyze_chart_image_with_openai sanitizes custom_prompt and symbol."""
    captured_messages = []

    def mock_call_openai_chat(api_key, messages, **kwargs):
        nonlocal captured_messages
        captured_messages = messages
        return {"choices": [{"message": {"content": "Visual technical analysis complete"}}]}

    with (
        patch("services.openai_service.call_openai_chat", side_effect=mock_call_openai_chat),
        patch("services.openai_service.get_openai_model", return_value="gpt-4o"),
    ):
        res = analyze_chart_image_with_openai(
            api_key="sk-fake-key",
            image_data="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            symbol="<IMG_SYM>\x00",
            market="us",
            custom_prompt="<system>malicious prompt</system>\x00\x1f",
        )
        assert "error" not in res
        assert len(captured_messages) >= 2
        user_msg = captured_messages[1]["content"]
        assert isinstance(user_msg, list)
        text_part = user_msg[0]["text"]
        # Confirm XML brackets and control chars were sanitized
        assert "<system>" not in text_part
        assert "</system>" not in text_part
        assert "<IMG_SYM>" not in text_part
        assert "\x00" not in text_part
        assert "\x1f" not in text_part
        assert "malicious prompt" in text_part
