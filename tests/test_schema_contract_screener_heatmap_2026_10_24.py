"""Tests for screener & heatmap schema contracts and unified response envelopes."""

from unittest.mock import patch

import pytest
from flask import Flask, request

from error_codes import ErrorCode
from route_helpers import _parse_heatmap_request, _parse_screener_request
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_stocks_bp
from routes.api_system import api_system_bp


@pytest.fixture
def test_app():
    """Create Flask application for testing contracts."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    app.register_blueprint(api_stocks_bp)
    app.register_blueprint(api_analysis_bp)
    app.register_blueprint(api_system_bp)
    return app


# =====================================================================
# Unit Tests: ScreenerQueryRequest & HeatmapQueryRequest via route helpers
# =====================================================================


def test_screener_request_parser_defaults(test_app):
    """Test screener request parser with default and empty values."""
    with test_app.test_request_context("/api/stocks/screener"):
        req, err = _parse_screener_request(request.args)
        assert err is None
        assert req is not None
        assert req.market == "all"
        assert req.limit == 150
        assert req.sort_by == "market_cap"
        assert req.sort_order == "desc"
        assert req.q == ""
        assert req.min_price is None


def test_screener_request_parser_valid_query_and_bounds(test_app):
    """Test screener request parser with valid query and numeric bounds."""
    with test_app.test_request_context(
        "/api/stocks/screener?market=US&q=Apple&limit=50&sort_by=price&sort_order=asc"
        "&min_price=100&max_price=300&min_change=-5.5&max_change=10.0"
        "&min_market_cap=1000000&max_market_cap=50000000"
        "&min_pe=10&max_pe=30"
    ):
        req, err = _parse_screener_request(request.args)
        assert err is None
        assert req is not None
        assert req.market == "us"
        assert req.q == "Apple"
        assert req.limit == 50
        assert req.sort_by == "price"
        assert req.sort_order == "asc"
        assert req.min_price == 100.0
        assert req.max_price == 300.0
        assert req.min_change == -5.5
        assert req.max_change == 10.0
        assert req.min_market_cap == 1000000.0
        assert req.max_market_cap == 50000000.0
        assert req.min_pe == 10.0
        assert req.max_pe == 30.0


def test_screener_request_query_length_limit(test_app):
    """Test query over 200 characters is rejected with 400."""
    long_query = "A" * 250
    with test_app.test_request_context(f"/api/stocks/screener?q={long_query}"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "200文字以内" in data["details"]["reason"]


def test_screener_request_invalid_market(test_app):
    """Test invalid market rejects with 400 and invalid_market error."""
    with test_app.test_request_context("/api/stocks/screener?market=invalid"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "all/us/jp" in data["details"]["reason"]


def test_screener_request_unsupported_market_idx(test_app):
    """Test unsupported market 'idx' rejects with 400."""
    with test_app.test_request_context("/api/stocks/screener?market=idx"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "all/us/jp" in data["details"]["reason"]


def test_screener_request_invalid_sort(test_app):
    """Test invalid sort_by or sort_order rejects with 400."""
    with test_app.test_request_context("/api/stocks/screener?sort_by=hacked"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "sort_by" in data["details"]["reason"]

    with test_app.test_request_context("/api/stocks/screener?sort_order=sideways"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "sort_order" in data["details"]["reason"]


def test_screener_request_inverted_bounds(test_app):
    """Test inverted bounds (min > max) rejects with 400 and appropriate reason."""
    with test_app.test_request_context("/api/stocks/screener?min_price=200&max_price=100"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "min_price は max_price 以下" in data["details"]["reason"]

    with test_app.test_request_context("/api/stocks/screener?min_change=10&max_change=-5"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "min_change は max_change 以下" in data["details"]["reason"]

    with test_app.test_request_context(
        "/api/stocks/screener?min_market_cap=5000&max_market_cap=1000"
    ):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "min_market_cap は max_market_cap 以下" in data["details"]["reason"]

    with test_app.test_request_context("/api/stocks/screener?min_pe=50&max_pe=10"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "min_pe は max_pe 以下" in data["details"]["reason"]


def test_screener_request_non_numeric_and_negative_prices(test_app):
    """Test non-numeric values and negative prices reject cleanly."""
    with test_app.test_request_context("/api/stocks/screener?min_price=not-a-number"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "数値で指定" in data["details"]["reason"]

    with test_app.test_request_context("/api/stocks/screener?min_price=-10"):
        req, err = _parse_screener_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "0以上の数値" in data["details"]["reason"]


def test_heatmap_request_parser_valid_and_defaults(test_app):
    """Test heatmap request parser default is 'us' and normalizes uppercase."""
    with test_app.test_request_context("/api/stocks/heatmap"):
        req, err = _parse_heatmap_request(request.args)
        assert err is None
        assert req is not None
        assert req.market == "us"

    with test_app.test_request_context("/api/stocks/heatmap?market=JP"):
        req, err = _parse_heatmap_request(request.args)
        assert err is None
        assert req is not None
        assert req.market == "jp"


def test_heatmap_request_parser_invalid_market(test_app):
    """Test heatmap request parser rejects invalid and unsupported markets."""
    with test_app.test_request_context("/api/stocks/heatmap?market=crypto"):
        req, err = _parse_heatmap_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert data["error_code"] == int(ErrorCode.INVALID_MARKET)

    with test_app.test_request_context("/api/stocks/heatmap?market=idx"):
        req, err = _parse_heatmap_request(request.args)
        assert req is None
        assert err is not None
        resp, code = err
        assert code == 400
        data = resp.get_json()
        assert "us/jp" in data["details"]["reason"]


# =====================================================================
# Unified Response Envelope Verification (ok: True AND success: True)
# =====================================================================


def test_portfolio_snapshot_unified_envelope(test_app):
    """Test /api/stocks/portfolio/snapshot returns both ok: True and success: True."""
    client = test_app.test_client()
    with patch("routes.stocks.portfolio.require_trusted_or_admin", return_value=(True, None)):
        res = client.post("/api/stocks/portfolio/snapshot")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert "stocks" in data


def test_ai_portfolios_unified_envelope(test_app):
    """Test /api/ai-portfolio returns both ok: True and success: True."""
    client = test_app.test_client()
    with patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)):
        res = client.get("/api/ai-portfolio")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert "presets" in data
        assert "saved" in data


def test_health_and_observe_unified_envelope(test_app):
    """Test /api/health, /api/cache-stats, and /api/metrics unified envelope."""
    client = test_app.test_client()
    with (
        patch("routes.api_system._is_local_request", return_value=True),
        patch("routes.api_system._require_admin_token_if_remote", return_value=(True, None)),
    ):
        res = client.get("/api/health")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert data["app"] == "Mistral NeX Stocks"

        res = client.get("/api/cache-stats")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert "cache_stats" in data

        res = client.get("/api/metrics")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert "cache" in data
        assert "market_data" in data
        assert "executors" in data


def test_csrf_token_unified_envelope(test_app):
    """Test /api/csrf-token returns unified envelope."""
    client = test_app.test_client()
    with patch("routes.api_system._require_admin_token_if_remote", return_value=(True, None)):
        res = client.get("/api/csrf-token")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert "csrf_token" in data


def test_sse_ticket_unified_envelope(test_app):
    """Test /api/stocks/stream/ticket returns unified envelope."""
    client = test_app.test_client()
    with (
        patch("routes.stocks.stream.require_trusted_or_admin", return_value=(True, None)),
        patch("routes.stocks.stream.create_sse_ticket", return_value="dummy-ticket-123"),
    ):
        res = client.post("/api/stocks/stream/ticket")
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["success"] is True
        assert data["ticket"] == "dummy-ticket-123"


def test_sse_stream_auth_error_unified_envelope(test_app):
    """Test /api/stocks/stream 403 error has unified envelope and backward-compatible fields."""
    client = test_app.test_client()
    with patch("utils.networking.require_sse_auth", return_value=(False, "unauthorized")):
        res = client.get("/api/stocks/stream")
        assert res.status_code == 403
        data = res.get_json()
        assert data["ok"] is False
        assert data["error_flag"] is True
        assert data["error"] == "unauthorized"
        assert data["message"] == "unauthorized"
