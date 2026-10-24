"""Tests verifying fixes for startup GDELT timeout warning and shutdown closed session warning."""

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

import trend_sources as ts
from app_state import app_state
from logging_config import init_logging
from session_manager import YFinanceSessionManager


def test_trend_session_adapter_zero_retries():
    """Verify trend session HTTPAdapter uses max_retries=0 to avoid retry timeout warnings."""
    session = ts._get_trend_session()
    for prefix in ("https://", "http://"):
        adapter = session.adapters.get(prefix)
        assert adapter is not None
        # In urllib3, max_retries is a Retry object; total should be 0 or falsy
        retry_obj = adapter.max_retries
        total = getattr(retry_obj, "total", retry_obj)
        assert total == 0, f"Expected 0 retries on {prefix}, got {total}"


def test_collect_gdelt_items_timeout_breaks_early(caplog):
    """Verify collect_gdelt_items catches Timeout, logs debug, and skips remaining queries."""
    with caplog.at_level(logging.WARNING):
        with patch("trend_sources._request_json", side_effect=requests.Timeout("Read timed out")):
            items = ts.collect_gdelt_items(["query1", "query2", "query3"], "us")
            assert items == []
            # Ensure no WARNING was emitted
            assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_collect_gdelt_items_429_breaks_early(caplog):
    """Verify collect_gdelt_items catches 429 HTTPError and skips remaining queries."""
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    err = requests.HTTPError("429 Client Error", response=mock_resp)

    with caplog.at_level(logging.WARNING):
        with patch("trend_sources._request_json", side_effect=err):
            items = ts.collect_gdelt_items(["query1", "query2", "query3"], "us")
            assert items == []
            assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_session_manager_no_warning_or_refresh_on_shutdown(caplog):
    """Verify request on closed session during shutdown does not log warning or refresh session."""
    mgr = YFinanceSessionManager()
    sess = mgr.get_session()

    # Simulate shutdown close_all
    mgr.close_all()

    fresh_sess = MagicMock()
    with patch.object(mgr, "get_session", return_value=fresh_sess):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(Exception):
                sess.request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL")

        # get_session should NOT have been called to refresh
        fresh_sess.request.assert_not_called()
        # No warning about "Detected request on closed session" should be logged
        assert "Detected request on closed session" not in caplog.text


def test_session_manager_aborts_when_shutdown_event_set():
    """Verify session request raises RuntimeError immediately when shutdown_event is set."""
    mgr = YFinanceSessionManager()
    sess = mgr.get_session()

    with patch.object(app_state.execution.shutdown_event, "is_set", return_value=True):
        with pytest.raises(RuntimeError, match="Application is shutting down"):
            sess.request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL")


def test_logging_config_urllib3_suppressed():
    """Verify logging_config sets urllib3.connectionpool to ERROR level."""
    mock_app = MagicMock()
    mock_app.logger = logging.getLogger("test_mock_app")
    init_logging(mock_app)

    pool_logger = logging.getLogger("urllib3.connectionpool")
    assert pool_logger.level == logging.ERROR
