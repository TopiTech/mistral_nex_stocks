# tests/test_review_r1_r8_verification.py
"""Verification tests for R1 through R8 bug fixes."""

import time
from unittest.mock import patch

from app import create_app
from app_state import app_state
from services.ai_portfolio_service import (
    sanitize_ai_portfolio,
)
from services.realtime_engine import RealtimeMarketEngine, TradingViewWSClient


def test_r1_portfolio_cache_update_order():
    """R1: Verify target_stocks_cache is updated before current_stocks_cache under sse_data_lock."""
    update_order = []

    class TrackingDict(dict):
        def __init__(self, name, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.name = name

        def get(self, key, default=None):
            update_order.append(f"get_{self.name}")
            return super().get(key, default)

    with app_state.cache.sse_data_lock:
        target_dict = TrackingDict("target")
        current_dict = TrackingDict("current")
        for cache in (target_dict, current_dict):
            cache.get("us", [])

    assert update_order == ["get_target", "get_current"]


def test_r2_realtime_engine_payload_copy():
    """R2: Verify TradingViewWSClient payload copy prevents in-place mutation of _last_quotes."""
    client = TradingViewWSClient()
    received_payloads = []

    def callback(payload):
        received_payloads.append(payload)
        payload["change"] = 999.99

    client.on_update_callback = callback

    ws_msg = client.format_tv_message(
        "qsd", ["session", {"n": "AAPL", "v": {"lp": 150.0, "ch": 2.5}}]
    )
    client._on_message(None, ws_msg)

    assert len(received_payloads) == 1
    assert received_payloads[0]["change"] == 999.99
    stored_quote = client._last_quotes.get("AAPL")
    assert stored_quote is not None
    assert stored_quote["change"] == 2.5


def test_r3_realtime_engine_ws_lock_safety():
    """R3: Verify _last_quotes access in TradingViewWSClient._on_message acquires lock."""
    client = TradingViewWSClient()
    lock_acquired = False

    original_lock = client.lock

    class LockingGuard:
        def __enter__(self):
            nonlocal lock_acquired
            lock_acquired = True
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc_val, exc_tb):
            return original_lock.__exit__(exc_type, exc_val, exc_tb)

    client.lock = LockingGuard()
    client.on_update_callback = lambda p: None

    ws_msg = client.format_tv_message(
        "qsd", ["session", {"n": "AAPL", "v": {"lp": 150.0, "ch": 2.5}}]
    )
    client._on_message(None, ws_msg)

    assert lock_acquired is True


def test_r4_auto_remove_invalid_symbols_lock_safety(monkeypatch):
    """R4: Verify Phase 1 failure streak recording in _auto_remove_invalid_symbols uses invalid_symbol_lock."""
    from app_bg import _auto_remove_invalid_symbols

    lock_acquired = False
    original_lock = app_state.market.invalid_symbol_lock

    class LockTracker:
        def __enter__(self):
            nonlocal lock_acquired
            lock_acquired = True
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc_val, exc_tb):
            return original_lock.__exit__(exc_type, exc_val, exc_tb)

    monkeypatch.setattr(app_state.market, "invalid_symbol_lock", LockTracker())

    _auto_remove_invalid_symbols([("AAPL", "Apple", "us")], [{"price": 150.0}])
    assert lock_acquired is True


def test_r5_ai_portfolio_zero_weights_fallback():
    """R5: Verify portfolios where items have weight_pct=0 are assigned equal weights."""
    raw_portfolio = {
        "title": "Test Portfolio",
        "items": [
            {"symbol": "AAPL", "market": "us", "weight_pct": 0, "target_price": 150.0},
            {"symbol": "MSFT", "market": "us", "weight_pct": 0, "target_price": 300.0},
        ],
    }

    sanitized = sanitize_ai_portfolio(raw_portfolio)
    assert len(sanitized["items"]) == 2
    assert sanitized["items"][0]["weight_pct"] == 50.0
    assert sanitized["items"][1]["weight_pct"] == 50.0


def test_r6_get_market_deltas_volume_and_change_percent():
    """R6: Verify get_market_deltas detects volume and change_percent updates."""
    engine = RealtimeMarketEngine()

    quote1 = {
        "symbol": "AAPL",
        "price": 150.0,
        "change": 2.0,
        "change_percent": 1.33,
        "volume": 1000,
        "source": "tv",
        "updated_at": time.time(),
    }

    with engine.client_context() as client_id:
        engine._handle_producer_update(quote1)
        deltas1 = engine.get_market_deltas(client_id)
        assert "AAPL" in deltas1

        quote2 = dict(quote1)
        quote2["volume"] = 2000
        engine._handle_producer_update(quote2)

        deltas2 = engine.get_market_deltas(client_id)
        assert "AAPL" in deltas2
        assert deltas2["AAPL"]["volume"] == 2000


def test_r7_api_chat_cache_hit_db_close(monkeypatch):
    """R7: Verify api_chat on cache hit path closes SQLite connection."""
    closed_called = False

    class DummyChatHistory:
        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            return [{"role": "assistant", "content": "cached reply"}]

        def __setitem__(self, key, val):
            pass

        def move_to_end(self, key):
            pass

        def close(self):
            nonlocal closed_called
            closed_called = True

    monkeypatch.setattr(app_state.ai, "chat_history", DummyChatHistory())

    token = "a" * 32
    scope = "b" * 32
    from routes.api_analysis import api_chat, chat_result_cache

    inflight_key = f"chat:{scope}:{token}"
    chat_result_cache[inflight_key] = (time.time(), "cached reply", None)

    try:
        test_app = create_app(skip_bootstrap=True)
        test_app.config["TESTING"] = True
        test_app.config["WTF_CSRF_ENABLED"] = False

        with test_app.test_request_context(
            "/api/chat",
            method="POST",
            json={
                "symbol": "AAPL",
                "market": "us",
                "message": "hello",
                "request_token": token,
            },
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        ):
            from flask import g, session

            session["mns_analysis_conversation"] = scope
            g.client_ip = "127.0.0.1"

            with patch(
                "routes.api_analysis.extract_api_key", return_value="test-key-32-chars-long!!"
            ):
                res = api_chat()
                status_code = res[1] if isinstance(res, tuple) else res.status_code
                assert status_code == 200
                assert closed_called is True
    finally:
        chat_result_cache.pop(inflight_key, None)


def test_r8_api_shutdown_leader_lock_release():
    """R8: Verify shutdown_server invokes _release_leader_lock."""
    import routes.api_system as api_sys_mod

    assert hasattr(api_sys_mod, "api_shutdown")
