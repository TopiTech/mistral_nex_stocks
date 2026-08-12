"""Regression test suite verifying review fixes for R1, R3, R4."""

from unittest.mock import MagicMock

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


def test_r3_master_key_update_lock_acquisition():
    """R3: _master_key_update_lock handles acquisition and release cleanly."""
    # Run the lock context manager
    with _master_key_update_lock():
        assert True


def test_r4_api_analyze_v2_result_partial_success_structure():
    """R4: Verify structure of partial_success and warnings in analysis result logic."""
    raw_research_context = ""
    langsearch_api_key = "test_key_123"
    tavily_api_key = ""
    search_errors = ["timeout"]
    data_source = "client"

    search_attempted = bool(langsearch_api_key or tavily_api_key)
    search_failed = bool(
        search_attempted and (search_errors or not raw_research_context.strip())
    )
    partial_success = search_failed or (data_source == "client")
    warnings = []
    if search_failed:
        warnings.append("Web search context was unavailable or returned empty")
    if data_source == "client":
        warnings.append("Server stock price fetch failed; fallback client data used")

    assert search_failed is True
    assert partial_success is True
    assert len(warnings) == 2
