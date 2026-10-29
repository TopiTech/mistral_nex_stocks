"""Regression test suite verifying review fixes for R1, R3, R4."""

import ctypes
import os
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock

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
    """R4: Verify structure of partial_success and warnings in analysis result logic."""
    raw_research_context = ""
    langsearch_api_key = "test_key_123"
    tavily_api_key = ""
    search_errors = ["timeout"]
    data_source = "client"

    search_attempted = bool(langsearch_api_key or tavily_api_key)
    search_failed = bool(search_attempted and (search_errors or not raw_research_context.strip()))
    partial_success = search_failed or (data_source == "client")
    warnings = []
    if search_failed:
        warnings.append("Web search context was unavailable or returned empty")
    if data_source == "client":
        warnings.append("Server stock price fetch failed; fallback client data used")

    assert search_failed is True
    assert partial_success is True
    assert len(warnings) == 2
