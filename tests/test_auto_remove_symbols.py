"""Regression tests for H3: silent auto-removal of user stocks.

These guard against a temporary Yahoo/network outage silently deleting a
user-added symbol. Only a *genuinely* invalid symbol (delisted / not found,
signalled via the ("__INVALID_SYMBOL__", symbol) marker returned by
fetch_stocks_batch) should advance the removal streak.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable when pytest runs from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app_bg
from app_state import app_state
from tests import reset_app_state_internals


@pytest.fixture(autouse=True)
def _reset():
    reset_app_state_internals()
    yield
    reset_app_state_internals()


def _add_user_symbol(symbol: str, market: str, name: str = "TestCo") -> None:
    container = app_bg._get_stock_container(market)
    container[symbol] = {"name": name, "shares": 0, "avg_price": 0, "avg_fx_rate": 1.0}


def _invalid_marker(symbol: str):
    return (app_bg._BATCH_INVALID_MARKER, symbol)


def test_transient_failure_does_not_delete_user_symbol():
    """A repeated transient failure (None) must NOT auto-remove a user stock."""
    _add_user_symbol("USERA", "us")
    threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD

    items = [("USERA", "TestCo", "us")]
    # Simulate `threshold` consecutive transient failures (None result).
    for _ in range(threshold + 1):
        app_bg._auto_remove_invalid_symbols(items, [None])

    container = app_bg._get_stock_container("us")
    assert "USERA" in container, "transient failure must not delete user stock"


def test_invalid_symbol_is_removed_after_threshold():
    """A genuinely invalid symbol marker removes the user stock after threshold."""
    _add_user_symbol("DELIST", "us")
    threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD

    items = [("DELIST", "GoneCo", "us")]
    # One short of threshold: must still be present.
    for _ in range(threshold - 1):
        app_bg._auto_remove_invalid_symbols(items, [_invalid_marker("DELIST")])
    container = app_bg._get_stock_container("us")
    assert "DELIST" in container

    # Cross the threshold: now it should be removed.
    app_bg._auto_remove_invalid_symbols(items, [_invalid_marker("DELIST")])
    container = app_bg._get_stock_container("us")
    assert "DELIST" not in container


@patch("app_bg.remove_stock_from_caches")
@patch("app_bg.invalidate_stock_caches")
@patch("app_bg.save_user_stocks", side_effect=OSError("disk unavailable"))
def test_auto_removal_restores_symbol_when_persistence_fails(
    mock_save, mock_invalidate, mock_remove_cache
):
    """A failed save must not make an auto-removal visible or durable later."""
    _add_user_symbol("DELIST", "us")
    threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD
    app_state.market.invalid_symbol_streak["DELIST"] = threshold - 1

    app_bg._auto_remove_invalid_symbols([("DELIST", "GoneCo", "us")], [_invalid_marker("DELIST")])

    assert "DELIST" in app_bg._get_stock_container("us")
    assert app_state.market.invalid_symbol_streak["DELIST"] == threshold
    mock_save.assert_called_once_with()
    mock_invalidate.assert_not_called()
    mock_remove_cache.assert_not_called()


def test_invalid_symbol_helper_detects_yfinance_missing():
    from services.stock_provider import _is_yfinance_invalid_symbol_error

    try:
        from yfinance.exceptions import YFTickerMissingError

        exc = YFTickerMissingError("ZZZZZZ", "Symbol does not exist")
        assert _is_yfinance_invalid_symbol_error(exc) is True
    except ImportError:
        pytest.skip("yfinance.exceptions.YFTickerMissingError not available")

    # A generic transient error must NOT be classified as invalid.
    assert _is_yfinance_invalid_symbol_error(RuntimeError("Connection reset")) is False
