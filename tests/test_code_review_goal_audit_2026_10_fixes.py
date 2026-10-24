"""Regression tests for code review audit fixes.

Verifies:
1. session_manager raises RuntimeError on closed session during shutdown.
2. api_news parses valid and malformed requests properly without asserts.
3. api_stock_details and api_stock_history handle parsing results without asserts.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from werkzeug.datastructures import MultiDict

from error_codes import ErrorCode
from route_helpers import _parse_news_request
from session_manager import YFinanceSessionManager


def test_session_manager_shutdown_request_runtime_error(caplog):
    """Verify request on closed session during shutdown raises RuntimeError and no warning."""
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
        assert "Detected request on closed session" not in caplog.text


def test_api_news_and_quotes_no_asserts(client):
    """Verify route handlers function cleanly without assertion errors on invalid inputs."""
    # Invalid symbol for stock-details
    resp = client.get("/api/stock-details?symbol=INVALID%%20SYMBOL&market=us")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error_code"] == ErrorCode.INVALID_SYMBOL.value

    # Invalid symbol for stock-history
    resp_hist = client.get("/api/stock-history?symbol=INVALID%%20SYMBOL&market=us")
    assert resp_hist.status_code == 400
    data_hist = resp_hist.get_json()
    assert data_hist["error_code"] == ErrorCode.INVALID_SYMBOL.value


def test_parse_news_request_guard_and_no_asserts():
    """Verify _parse_news_request returns tuple properly and is handled without asserts."""
    app = Flask(__name__)
    with app.test_request_context(
        "/api/news", method="POST", data='{"force":', content_type="application/json"
    ):
        req = MagicMock(args=MultiDict())
        force, err = _parse_news_request(req)
        assert force is None
        assert err is not None
        resp, status = err
        assert status == 400
        assert resp.get_json()["error_code"] == ErrorCode.MALFORMED_INPUT.value
