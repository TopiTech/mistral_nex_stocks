"""Regression test suite verifying review fixes for R1, R3, R4."""

import ctypes
import json
import os
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import config.locks
from config_store import _master_key_update_lock
from services.realtime_engine import TradingViewWSClient


def test_r1_tradingview_client_add_remove_connected_guard():
    """R1: TradingViewWSClient add_symbol / remove_symbol do not call ws.send when client is disconnected."""
    client = TradingViewWSClient()
    mock_ws = MagicMock()
    client.ws = mock_ws
    client.running = True
    client.connected = False  # Client is NOT connected yet

    client.add_symbol("AAPL")
    assert "AAPL" in client.symbols
    mock_ws.send.assert_not_called()

    client.remove_symbol("AAPL")
    assert "AAPL" not in client.symbols
    mock_ws.send.assert_not_called()

    # Now mark connected = True
    client.connected = True
    client.add_symbol("NVDA")
    assert mock_ws.send.called


def test_r3_master_key_update_lock_serializes_across_threads():
    """R3: a second thread cannot enter while the master-key lock is held."""
    contender_started = threading.Event()
    contender_acquired = threading.Event()

    def contender() -> None:
        contender_started.set()
        with _master_key_update_lock():
            contender_acquired.set()

    thread = threading.Thread(target=contender, name="master-key-lock-contender", daemon=True)
    try:
        with _master_key_update_lock():
            thread.start()
            assert contender_started.wait(5.0), "contender thread did not start"
            # The contender must remain outside while this thread owns the lock.
            assert not contender_acquired.wait(0.25), (
                "second thread acquired _master_key_update_lock while it was held"
            )
    finally:
        if thread.ident is not None:
            thread.join(timeout=5.0)

    assert contender_acquired.is_set(), "lock was not released to the waiting thread"


def test_r3_master_key_update_lock_posix(monkeypatch):
    """Verify posix path delegates to config_update_lock cleanly."""
    called = []

    @contextmanager
    def _mock_lock():
        called.append(True)
        yield

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(config.locks, "config_update_lock", _mock_lock)
    with _master_key_update_lock():
        pass
    assert called == [True]


def test_r3_master_key_update_lock_no_windll(monkeypatch):
    """Verify RuntimeError when ctypes.windll is not available on Windows."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delattr(ctypes, "windll", raising=False)
    with pytest.raises(RuntimeError, match=r"Windows API \(ctypes\.windll\) is not available"):
        with _master_key_update_lock():
            pass


def test_r4_api_analyze_v2_result_partial_success_structure():
    """R4: Real /api/analyze-v2 surfaces search_failed, partial_success, and warnings."""
    from app import create_app
    from app_state import app_state
    from routes.api_analysis import (
        analyze_fetch_inflight,
        analyze_fetch_lock,
        analyze_result_cache,
        analyze_result_fingerprints,
    )

    flask_app = create_app(
        config_override={"TESTING": True, "WTF_CSRF_ENABLED": False},
        skip_bootstrap=True,
    )
    req_token = "tok-r4-partial-success-123456"
    scope = "conv-r4-partial-success"
    inflight_key = f"analyze:{scope}:{req_token}"

    def _failing_research(*args, **kwargs):
        errors_out = kwargs.get("errors_out")
        if isinstance(errors_out, list):
            errors_out.append("timeout")
        return ""

    llm_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": "HOLD",
                            "confidence": 0.5,
                            "analysis_summary": "Fallback analysis summary",
                            "reasoning": "Fallback analysis reasoning",
                            "time_horizon": "短期",
                            "risk_level": "中",
                            "catalysts": [],
                            "risks": [],
                            "sentiment": "NEUTRAL",
                        }
                    )
                }
            }
        ]
    }

    with analyze_fetch_lock:
        analyze_fetch_inflight.pop(inflight_key, None)
        analyze_result_cache.pop(inflight_key, None)
        analyze_result_fingerprints.pop(inflight_key, None)

    try:
        with (
            patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, None)),
            patch(
                "routes.api_analysis.extract_api_key",
                return_value="fake-mistral-key-1234567890123456",
            ),
            patch("routes.api_analysis.extract_langsearch_api_key", return_value="test_key_123"),
            patch("routes.api_analysis.extract_tavily_api_key", return_value=""),
            patch("routes.api_analysis.fetch_stock", return_value=None),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch(
                "routes.api_analysis.get_cached_context_with_negative_cache",
                side_effect=lambda _key, fn, *_a, **_kw: fn(),
            ),
            patch(
                "routes.api_analysis.collect_symbol_research_context",
                side_effect=_failing_research,
            ),
            patch("routes.api_analysis.call_mistral_chat", return_value=llm_payload),
        ):
            client = flask_app.test_client()
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = scope
            resp = client.post(
                "/api/analyze-v2",
                json={
                    "symbol": "AAPL",
                    "market": "us",
                    "price": 150.0,
                    "chart_data": [{"date": "2026-01-02", "close": 150.0}],
                    "request_token": req_token,
                    "conversation_scope": scope,
                },
                headers={"Origin": "http://localhost:5000"},
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["search_failed"] is True
        assert body["partial_success"] is True
        assert body["data_source"] == "client"
        assert "Web search context was unavailable or returned empty" in body["warnings"]
        assert "Server stock price fetch failed; fallback client data used" in body["warnings"]
    finally:
        with analyze_fetch_lock:
            analyze_fetch_inflight.pop(inflight_key, None)
            analyze_result_cache.pop(inflight_key, None)
            analyze_result_fingerprints.pop(inflight_key, None)
        with app_state.ai.chat_history_lock:
            app_state.ai.chat_history.close()
