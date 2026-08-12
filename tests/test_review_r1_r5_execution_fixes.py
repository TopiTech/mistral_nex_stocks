"""
tests/test_review_r1_r5_execution_fixes.py - Regression tests for review fixes [R1] to [R5].
"""

import logging
from unittest.mock import MagicMock, patch

from app_state import app_state
from native_host.native_host import _is_caller_authorized_browser
from services.ai_portfolio_service import generate_ai_portfolio_by_theme
from services.realtime_engine import TradingViewWSClient
from utils.stock_payload import _build_portfolio_metrics, get_current_usdjpy_rate


def test_tradingview_ws_client_stop_socket_cleanup():
    """[R2] Verify that TradingViewWSClient.stop properly closes low-level socket."""
    client = TradingViewWSClient(symbols=["AAPL"])
    mock_ws = MagicMock()
    mock_sock = MagicMock()
    mock_ws.sock = mock_sock

    with client._lifecycle_lock:
        client.ws = mock_ws
        client.running = True

    client.stop()

    assert client.running is False
    assert client.ws is None
    mock_ws.close.assert_called_once()
    mock_sock.close.assert_called_once()


def test_get_current_usdjpy_rate_fallback_chain():
    """[R3] Verify get_current_usdjpy_rate multi-tier fallback mechanism."""
    # 1. When in-memory cache has valid USDJPY
    with app_state.cache.sse_data_lock:
        app_state.market.current_indices_cache["USDJPY"] = {"price": "155.50"}

    rate, is_est = get_current_usdjpy_rate(default_rate=150.0)
    assert rate == 155.50
    assert is_est is False

    # 2. When in-memory cache is missing, fallback to last_usdjpy_rate
    with app_state.cache.sse_data_lock:
        app_state.market.current_indices_cache.pop("USDJPY", None)
    app_state.market.last_usdjpy_rate = 152.30

    rate, is_est = get_current_usdjpy_rate(default_rate=150.0)
    assert rate == 152.30
    assert is_est is False

    # 3. When last_usdjpy_rate is 0/None, fallback to disk cache
    app_state.market.last_usdjpy_rate = 0.0
    with patch.object(
        app_state.stock_disk_cache,
        "get",
        return_value={"regularMarketPrice": 148.80},
    ):
        rate, is_est = get_current_usdjpy_rate(default_rate=150.0)
        assert rate == 148.80
        assert is_est is False

    # 4. When everything is missing, fallback to default_rate
    with patch.object(app_state.stock_disk_cache, "get", return_value=None):
        rate, is_est = get_current_usdjpy_rate(default_rate=150.0)
        assert rate == 150.0
        assert is_est is True


def test_build_portfolio_metrics_uses_resolved_fx():
    """[R3] Verify _build_portfolio_metrics uses dynamically resolved FX rate."""
    with app_state.cache.sse_data_lock:
        app_state.market.current_indices_cache["USDJPY"] = {"price": "160.00"}

    # 10 shares of USD stock at $100 (avg $90), no custom avg_fx_rate
    val_jpy, pl_jpy = _build_portfolio_metrics(
        shares=10.0,
        avg_price=90.0,
        avg_fx_rate=None,
        currency="USD",
        current_price=100.0,
    )
    # val_jpy = 10 * 100 * 160 = 160,000
    # cost_jpy = 10 * 90 * 160 = 144,000
    # pl_jpy = 160,000 - 144,000 = 16,000
    assert val_jpy == 160000.0
    assert pl_jpy == 16000.0


def test_native_host_ancestor_process_fallback_logging(caplog):
    """[R4] Verify ancestor process lookup fallback logs at INFO level."""
    with patch(
        "native_host.native_host._get_ancestor_process_names",
        return_value=[],
    ):
        with caplog.at_level(logging.INFO):
            authorized = _is_caller_authorized_browser()
            assert authorized is True
            assert any(
                "Ancestor process tree lookup returned empty; allowing caller by fallback"
                in record.message
                for record in caplog.records
                if record.levelname == "INFO"
            )


def test_ai_portfolio_finish_reason_truncation_detection(caplog):
    """[R5] Verify that LLM response truncation (finish_reason=length) is detected and logged."""
    truncated_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"title": "Truncated Portfolio", "items": [{"symbol": "NVDA"',
                },
                "finish_reason": "length",
            }
        ]
    }

    with patch(
        "services.ai_portfolio_service.call_mistral_chat",
        return_value=truncated_response,
    ), patch(
        "services.ai_portfolio_service.get_mistral_api_key",
        return_value="test_mistral_key",
    ), patch(
        "services.ai_portfolio_service.collect_symbol_research_context",
        return_value="",
    ):
        with caplog.at_level(logging.WARNING):
            portfolio = generate_ai_portfolio_by_theme(
                "test-truncation-theme", force_rebalance=True
            )
            assert portfolio is not None
            assert "items" in portfolio
            assert any(
                "AI portfolio response truncated by max_tokens limit (finish_reason=length)"
                in record.message
                for record in caplog.records
            )
