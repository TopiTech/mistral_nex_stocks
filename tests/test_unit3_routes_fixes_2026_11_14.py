"""Unit 3 regression tests for routes, schemas, and API boundary fixes (2026-11-14)."""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from flask import g

import routes.api_analysis as ra
import routes.stocks.ai_portfolio as aip_mod
import routes.stocks.stream as stream_mod
import routes.stocks.views as views_mod
from app import app
from app_state import app_state
from constants import MAX_USER_WATCHLIST_ITEMS
from error_codes import ErrorCode
from route_helpers import _parse_news_request
from services.ai_service import ProviderAPIError
from utils.networking import _SSE_TICKETS, _SSE_TICKETS_LOCK


@pytest.fixture(autouse=True)
def _disable_csrf_for_unit3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable CSRF token requirement on test_client POST calls in Unit 3 tests."""
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", False)


def test_chat_non_stream_fallback_recovers_after_aborted_sse_stream(monkeypatch: Any) -> None:
    """[U3-02] stream=False with the same token runs fallback after _ChatStreamAbortedError."""
    token = "u3_fallback_tok_123456"
    scope = "scope-u3-fallback"
    inflight_key = f"chat:{scope}:{token}"
    with ra.chat_fetch_lock:
        ra.chat_result_cache[inflight_key] = (
            time.time(),
            None,
            ra._ChatStreamAbortedError("Chat stream closed before completion"),
        )
        ra.chat_fetch_inflight.pop(inflight_key, None)

    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test-1234567890")
    with (
        patch.object(
            ra,
            "_call_mistral_chat_with_retry",
            return_value="Fallback reply OK",
        ),
        app.test_client() as client,
    ):
        resp = client.post(
            "/api/chat",
            json={
                "market": "us",
                "symbol": "AAPL",
                "message": "質問です",
                "request_token": token,
                "conversation_scope": scope,
                "stream": False,
            },
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:5000",
            },
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body is not None
    assert body["ok"] is True
    assert body["reply"] == "Fallback reply OK"


def test_chat_background_job_exception_rolls_back_user_message(monkeypatch: Any) -> None:
    """[U3-02] Background job exception in api_chat rolls back the appended user message."""
    token = "u3_rollback_tok_123456"
    scope = "scope-u3-rollback"
    chat_key = f"{scope}:us:AAPL"
    rollback_calls: list[str] = []

    def _spy_rollback(key: str, msg: str) -> None:
        rollback_calls.append(f"{key}:{msg}")

    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test-1234567890")
    with ra.chat_fetch_lock:
        ra.chat_result_cache.pop(f"chat:{scope}:{token}", None)
        ra.chat_fetch_inflight.pop(f"chat:{scope}:{token}", None)

    with (
        patch.object(ra, "_rollback_chat_user_message", side_effect=_spy_rollback),
        patch.object(
            ra,
            "_call_mistral_chat_with_retry",
            side_effect=ProviderAPIError("rate limited", status_code=429, details={"retry_after": 7.2}),
        ),
        app.test_client() as client,
    ):
        resp = client.post(
            "/api/chat",
            json={
                "market": "us",
                "symbol": "AAPL",
                "message": "失敗時のロールバック確認",
                "request_token": token,
                "conversation_scope": scope,
                "stream": False,
            },
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:5000",
            },
        )

    assert resp.status_code == 429
    assert rollback_calls == [f"{chat_key}:失敗時のロールバック確認"]


def test_chat_error_response_429_propagates_retry_after_header_and_details() -> None:
    """[U3-02] 429 responses propagate positive retry_after in details and Retry-After header."""
    with app.test_request_context("/api/chat"):
        g.request_id = "req-u3-429-details"
        resp, status = ra._chat_error_response(
            ProviderAPIError("Too many requests", status_code=429, details={"retry_after": 12.2}),
            g,
            operation_token="tok-429-001",
        )
        assert status == 429
        assert resp.headers.get("Retry-After") == "13"
        data = resp.get_json()
        assert data is not None
        assert data["details"]["retry_after"] == 12.2

    class _ProviderAPIErrorWithResponse(ProviderAPIError):
        def __init__(self, message: str, *, status_code: int, response: Any) -> None:
            super().__init__(message, status_code=status_code)
            self.response = response

    http_resp = MagicMock()
    http_resp.headers = {"Retry-After": "9"}
    exc_with_header = _ProviderAPIErrorWithResponse(
        "Too many requests", status_code=429, response=http_resp
    )
    with app.test_request_context("/api/chat"):
        g.request_id = "req-u3-429-header"
        resp2, status2 = ra._chat_error_response(exc_with_header, g)
        assert status2 == 429
        assert resp2.headers.get("Retry-After") == "9"
        data2 = resp2.get_json()
        assert data2 is not None
        assert data2["details"]["retry_after"] == 9.0


def test_parse_news_request_maps_invalid_request_token_to_invalid_input() -> None:
    """[U3-03] _parse_news_request preserves MALFORMED_INPUT (400) for invalid request_token."""
    with app.test_request_context(
        "/api/news",
        method="POST",
        json={"force": True, "request_token": "short"},
    ):
        from flask import request

        parsed, err = _parse_news_request(request)
        assert parsed is None
        assert err is not None
        resp, status = err
        assert status == 400
        body = resp.get_json()
        assert body is not None
        assert body["error_code"] == ErrorCode.MALFORMED_INPUT.value


def test_copy_ai_portfolio_excludes_defaults_from_capacity_and_preserves_custom_name() -> None:
    """[U3-03] copy-to-my skips defaults in capacity count and keeps sanitized custom names."""
    existing_custom = {
        f"SYM{i}": {"name": f"Sym {i}", "shares": 1.0, "avg_price": 100.0}
        for i in range(MAX_USER_WATCHLIST_ITEMS)
    }
    saved_user_us = dict(app_state.market.user_us)
    saved_user_jp = dict(app_state.market.user_jp)
    saved_user_idx = dict(app_state.market.user_idx)
    try:
        with app_state.market.user_stocks_lock:
            # Leave one slot open below capacity when a default stock is copied alongside a new one
            app_state.market.user_us = dict(
                list(existing_custom.items())[: MAX_USER_WATCHLIST_ITEMS - 1]
            )
            app_state.market.user_jp = {}
            app_state.market.user_idx = {}

        with (
            patch.object(aip_mod, "save_user_stocks"),
            patch.object(aip_mod, "invalidate_stock_caches"),
            patch.object(aip_mod, "ensure_stock_placeholder_in_caches"),
            patch.object(aip_mod._common, "_sync_realtime_symbol"),
            patch.object(aip_mod._common, "_announce_watchlist_state"),
            patch.object(aip_mod, "schedule_sync_all_stocks_now"),
            app.test_client() as client,
        ):
            resp = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {
                            "symbol": "AAPL",
                            "market": "us",
                            "weight_pct": 50.0,
                            "target_price": 200.0,
                            "name": "Custom Apple Override Should Ignore",
                        },
                        {
                            "symbol": "PLTR",
                            "market": "us",
                            "weight_pct": 50.0,
                            "target_price": 50.0,
                            "name": "<b>Palantir</b> Custom\nName",
                        },
                    ]
                },
                headers={"Origin": "http://127.0.0.1:5000"},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body is not None
            assert body["ok"] is True
            assert body["added_count"] == 2
            assert app_state.market.user_us["PLTR"]["name"] == "Palantir CustomName"
            assert app_state.market.user_us["AAPL"]["name"] != "Custom Apple Override Should Ignore"
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = saved_user_us
            app_state.market.user_jp = saved_user_jp
            app_state.market.user_idx = saved_user_idx


def test_sse_ticket_preserved_on_mode_zero_and_429_still_rejects_unauthenticated(
    monkeypatch: Any,
) -> None:
    """[U3-04] mode=0 and 429 preserve single-use SSE ticket; unauthenticated stays 403."""
    admin_tok = "a" * 32
    monkeypatch.setenv("MNS_ADMIN_TOKEN", admin_tok)
    with _SSE_TICKETS_LOCK:
        _SSE_TICKETS.clear()

    with app.test_client() as client:
        # Unauthenticated request fails 403 even on mode=0
        unauth = client.get("/api/stocks/stream?mode=0")
        assert unauth.status_code == 403

        # Issue a single-use session-bound ticket
        t_resp = client.post(
            "/api/stocks/stream/ticket",
            headers={"X-MNS-Admin-Token": admin_tok},
        )
        assert t_resp.status_code == 200
        ticket = t_resp.get_json()["ticket"]

        # mode=0 must succeed AND keep ticket alive
        m0_resp = client.get(f"/api/stocks/stream?mode=0&sse_ticket={ticket}")
        assert m0_resp.status_code == 200

        # 429 capacity rejection must reject AND keep ticket alive
        with patch.object(app_state.sse_listener_limiter, "reserve", return_value=None):
            cap_resp = client.get(f"/api/stocks/stream?mode=1&sse_ticket={ticket}")
            assert cap_resp.status_code == 429

        # Now the same ticket must succeed when opening an actual stream, then be burned
        with (
            patch.object(stream_mod, "resolve_stocks_for_response", return_value={"us": [], "jp": [], "idx": []}),
            patch.object(stream_mod, "resolve_indices_for_response", return_value={}),
        ):
            ok_stream = client.get(f"/api/stocks/stream?mode=1&sse_ticket={ticket}")
            assert ok_stream.status_code == 200
            first_chunk = next(ok_stream.response).decode("utf-8")
            assert "initial_snapshot" in first_chunk
            ok_stream.close()

        # Replay of the consumed ticket must be rejected (403)
        replay = client.get(f"/api/stocks/stream?mode=1&sse_ticket={ticket}")
        assert replay.status_code == 403


def test_sse_initial_snapshot_holds_sse_data_lock_across_generation_and_resolve(
    monkeypatch: Any,
) -> None:
    """[U3-04] Initial snapshot reads generation and stock/index payloads atomically under sse_data_lock."""
    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    stream_mod._initial_snapshot_cache.clear()
    lock_held_from_other_thread: list[bool] = []

    def _probe_resolve_stocks(*args: Any, **kwargs: Any) -> dict[str, list[Any]]:
        acquired_by_other: list[bool] = []

        def _try_acquire() -> None:
            got = app_state.cache.sse_data_lock.acquire(blocking=False)
            acquired_by_other.append(got)
            if got:
                app_state.cache.sse_data_lock.release()

        t = threading.Thread(target=_try_acquire)
        t.start()
        t.join(timeout=2.0)
        lock_held_from_other_thread.append(acquired_by_other == [False])
        return {"us": [{"symbol": "AAPL", "name": "Apple", "price": 150.0}], "jp": [], "idx": []}

    with (
        patch.object(stream_mod, "resolve_stocks_for_response", side_effect=_probe_resolve_stocks),
        patch.object(stream_mod, "resolve_indices_for_response", return_value={}),
        app.test_client() as client,
    ):
        resp = client.get("/api/stocks/stream?mode=1")
        assert resp.status_code == 200
        chunk = next(resp.response).decode("utf-8")
        resp.close()

    assert lock_held_from_other_thread == [True]
    data_line = next(line for line in chunk.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line[6:])
    assert payload["stream_event"] == "initial_snapshot"


def test_reset_stocks_invalidates_removed_custom_symbol_caches(monkeypatch: Any) -> None:
    """[U3-04] api_reset_stocks calls invalidate_stock_caches for removed custom symbols."""
    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    saved_us = dict(app_state.market.user_us)
    saved_jp = dict(app_state.market.user_jp)
    saved_idx = dict(app_state.market.user_idx)
    invalidated: list[str] = []
    try:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = {"CUSTOMUS": {"name": "Custom"}, "AAPL": {"name": "Apple"}}
            app_state.market.user_jp = {"9999.T": {"name": "CustomJP"}}
            app_state.market.user_idx = {}

        with (
            patch.object(views_mod, "save_user_stocks"),
            patch.object(views_mod, "invalidate_stock_caches", side_effect=invalidated.append),
            patch.object(views_mod._common, "_sync_realtime_symbol"),
            patch.object(views_mod._common, "_announce_watchlist_state"),
            patch.object(views_mod, "schedule_sync_all_stocks_now"),
            app.test_client() as client,
        ):
            resp = client.post(
                "/api/stocks/reset",
                headers={"Origin": "http://127.0.0.1:5000"},
            )
            assert resp.status_code == 200

        assert "CUSTOMUS" in invalidated
        assert "9999.T" in invalidated
        assert "AAPL" not in invalidated
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = saved_us
            app_state.market.user_jp = saved_jp
            app_state.market.user_idx = saved_idx
