"""Tests for 2026-09 code review improvements:
- Accessibility ARIA attributes on settings status indicators
- Deep link market parameters in screener.js and heatmap.js
- Fast wake-up & error signaling on background job queue exhaustion
"""

from __future__ import annotations

import pathlib
import queue
import re
from unittest.mock import patch

import pytest

from app import app
from error_codes import ErrorCode


@pytest.fixture
def test_client():
    """Test client with CSRF disabled."""
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with app.test_client() as c:
            yield c
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_settings_html_status_aria_attributes():
    """Verify settings.html status messages have role='status' and aria-live='polite'."""
    settings_path = pathlib.Path("templates/settings.html")
    assert settings_path.is_file(), "templates/settings.html must exist"
    content = settings_path.read_text(encoding="utf-8")

    # Check model-save-status
    model_match = re.search(r'<span[^>]*id=["\']model-save-status["\'][^>]*>', content)
    assert model_match is not None, "model-save-status element not found"
    tag = model_match.group(0)
    assert 'role="status"' in tag
    assert 'aria-live="polite"' in tag

    # Check prompt-save-status
    prompt_match = re.search(r'<span[^>]*id=["\']prompt-save-status["\'][^>]*>', content)
    assert prompt_match is not None, "prompt-save-status element not found"
    tag = prompt_match.group(0)
    assert 'role="status"' in tag
    assert 'aria-live="polite"' in tag

    # Check alpha-save-status
    alpha_match = re.search(r'<span[^>]*id=["\']alpha-save-status["\'][^>]*>', content)
    assert alpha_match is not None, "alpha-save-status element not found"
    tag = alpha_match.group(0)
    assert 'role="status"' in tag
    assert 'aria-live="polite"' in tag


def test_screener_js_deep_link_has_market():
    """Verify screener.js passes market parameter on row click navigation."""
    screener_path = pathlib.Path("static/js/screener.js")
    assert screener_path.is_file()
    content = screener_path.read_text(encoding="utf-8")

    assert "/main?q=" in content
    assert "stock.market" in content
    assert "&market=" in content


def test_heatmap_js_deep_link_has_market():
    """Verify heatmap.js 2D and 3D navigation pass market parameter."""
    heatmap_path = pathlib.Path("static/js/heatmap.js")
    assert heatmap_path.is_file()
    content = heatmap_path.read_text(encoding="utf-8")

    assert "/main?q=" in content
    assert "state.currentMarket" in content
    assert "&market=" in content


def test_ai_portfolio_generate_queue_full_signals_done(test_client):
    """Verify queue.Full in api_generate_ai_portfolio signals done event immediately."""
    with (
        patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.stocks.ai_portfolio.extract_api_key", return_value="dummy_key"),
        patch(
            "routes.api_stocks._submit_in_app_context",
            side_effect=queue.Full("Queue is full"),
        ),
        patch(
            "route_helpers._submit_in_app_context",
            side_effect=queue.Full("Queue is full"),
        ),
    ):
        resp = test_client.post(
            "/api/ai-portfolio/generate",
            json={"theme": "tech_test_qfull"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error_code"] == ErrorCode.TOO_MANY_REQUESTS


def test_ai_portfolio_rebalance_queue_full_signals_done(test_client):
    """Verify queue.Full in api_rebalance_ai_portfolio signals done event immediately."""
    with (
        patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.stocks.ai_portfolio.extract_api_key", return_value="dummy_key"),
        patch(
            "routes.api_stocks._submit_in_app_context",
            side_effect=queue.Full("Queue is full"),
        ),
        patch(
            "route_helpers._submit_in_app_context",
            side_effect=queue.Full("Queue is full"),
        ),
    ):
        resp = test_client.post(
            "/api/ai-portfolio/rebalance",
            json={"theme": "tech_test_rebal_qfull"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error_code"] == ErrorCode.TOO_MANY_REQUESTS


def test_chat_job_queue_full_signals_done(test_client):
    """Verify queue.Full in chat job scheduling unblocks in-flight event."""
    with (
        patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.api_analysis.extract_api_key", return_value="dummy_key"),
        patch(
            "routes.api_analysis._submit_in_app_context",
            side_effect=queue.Full("Queue full"),
        ),
    ):
        resp = test_client.post(
            "/api/chat",
            json={
                "message": "hello",
                "request_token": "a" * 32,
                "market": "us",
                "symbol": "AAPL",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error_code"] == ErrorCode.TOO_MANY_REQUESTS


def test_analyze_job_queue_full_signals_done(test_client):
    """Verify queue.Full in stock analysis scheduling unblocks in-flight event."""
    with (
        patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.api_analysis.extract_api_key", return_value="dummy_key"),
        patch(
            "routes.api_analysis._submit_in_app_context",
            side_effect=queue.Full("Queue full"),
        ),
    ):
        resp = test_client.post(
            "/api/analyze-v2",
            json={
                "market": "us",
                "symbol": "AAPL",
                "request_token": "b" * 32,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error_code"] == ErrorCode.TOO_MANY_REQUESTS


def test_news_job_queue_full_signals_done(test_client):
    """Verify queue.Full in news scheduling unblocks in-flight event."""
    with (
        patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.api_analysis.extract_api_key", return_value="dummy_key"),
        patch(
            "routes.api_analysis._submit_in_app_context",
            side_effect=queue.Full("Queue full"),
        ),
    ):
        resp = test_client.post(
            "/api/news?force=true",
            json={"force": True},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error_code"] == ErrorCode.TOO_MANY_REQUESTS
