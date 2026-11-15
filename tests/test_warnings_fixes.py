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
            with pytest.raises(RuntimeError, match="Application is shutting down"):
                sess.request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL")

        # get_session should NOT have been called to refresh
        fresh_sess.request.assert_not_called()
        # No warning about "Detected request on closed session" should be logged
        assert "Detected request on closed session" not in caplog.text


def test_session_manager_aborts_when_shutdown_event_set():
    """Verify session request raises RuntimeError immediately when shutdown_event is set."""
    mgr = YFinanceSessionManager()
    try:
        sess = mgr.get_session()

        with patch.object(app_state.execution.shutdown_event, "is_set", return_value=True):
            with pytest.raises(RuntimeError, match="Application is shutting down"):
                sess.request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL")
    finally:
        mgr.close_all()


def test_logging_config_urllib3_suppressed(tmp_path, monkeypatch):
    """Verify logging_config sets urllib3.connectionpool to ERROR level."""
    monkeypatch.setenv("MNS_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    pool_logger = logging.getLogger("urllib3.connectionpool")
    wz_logger = logging.getLogger("werkzeug")
    yf_names = ["yfinance"] + [
        name for name in logging.Logger.manager.loggerDict if name.startswith("yfinance")
    ]
    saved_root = (root.level, root.handlers[:])
    saved_pool_level = pool_logger.level
    saved_wz_filters = wz_logger.filters[:]
    saved_yf = {
        name: (logging.getLogger(name).level, logging.getLogger(name).filters[:])
        for name in yf_names
    }
    mock_logger = logging.getLogger("test_mock_app")
    saved_mock_handlers = mock_logger.handlers[:]
    try:
        mock_app = MagicMock()
        mock_app.logger = mock_logger
        init_logging(mock_app)

        assert pool_logger.level == logging.ERROR
    finally:
        pool_logger.setLevel(saved_pool_level)
        wz_logger.filters[:] = saved_wz_filters
        for name, (lvl, flts) in saved_yf.items():
            lg = logging.getLogger(name)
            lg.setLevel(lvl)
            lg.filters[:] = flts
        added_handlers = [h for h in root.handlers if h not in saved_root[1]]
        root.handlers[:] = saved_root[1]
        root.setLevel(saved_root[0])
        for h in [h for h in mock_logger.handlers if h not in saved_mock_handlers]:
            mock_logger.removeHandler(h)
        mock_logger.filters.clear()
        for h in added_handlers:
            try:
                h.close()
            except Exception:
                pass
