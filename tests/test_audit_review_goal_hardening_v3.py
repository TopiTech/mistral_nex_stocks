"""Tests for code review audit hardening fixes (Oct 2026).

Covers:
1. Envelope consistency for AI portfolio generate/rebalance fetching responses (ok: True, success: True, fetching: True).
2. Envelope consistency for _chat_error_response (ok: False, success: False, error_flag: True).
3. Frontend lifecycle and unload safety in ui.js, chart.js, and setup.js.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from routes.api_analysis import _chat_error_response


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_ai_portfolio_generate_fetching_envelope_consistency(client, app):
    """POST /api/ai-portfolio/generate returning fetching: True must have ok: True and success: True."""
    with (
        patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, "")),
        patch("route_helpers._submit_in_app_context"),
        patch("threading.Event.wait", return_value=False),
    ):
        resp = client.post(
            "/api/ai-portfolio/generate",
            json={"theme": "growth"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data.get("ok") is True
        assert data.get("success") is True
        assert data.get("fetching") is True
        assert data.get("step") == "generating"
        assert "elapsed_sec" in data


def test_ai_portfolio_rebalance_fetching_envelope_consistency(client, app):
    """POST /api/ai-portfolio/rebalance returning fetching: True must have ok: True and success: True."""
    with (
        patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, "")),
        patch("route_helpers._submit_in_app_context"),
        patch("threading.Event.wait", return_value=False),
    ):
        resp = client.post(
            "/api/ai-portfolio/rebalance",
            json={"theme": "tech"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data.get("ok") is True
        assert data.get("success") is True
        assert data.get("fetching") is True
        assert data.get("step") == "rebalancing"
        assert "elapsed_sec" in data


def test_chat_error_response_envelope_consistency(app):
    """_chat_error_response must include ok: False, success: False, error_flag: True across error categories."""
    with app.test_request_context():
        mock_g = MagicMock()
        mock_g.request_id = "req-test-123"

        test_cases = [
            (ValueError("Bad input"), 400),
            (Exception("401 Unauthorized token"), 401),
            (ConnectionError("Connection reset"), 503),
            (TimeoutError("Request timed out"), 500),
            (RuntimeError("Unexpected error"), 500),
            (Exception(""), 500),
        ]

        for exc, expected_code in test_cases:
            resp, status = _chat_error_response(exc, mock_g, operation_token="tok-456")
            assert status == expected_code
            data = resp.get_json()
            assert isinstance(data, dict)
            assert data.get("ok") is False, f"Failed for {exc}: data['ok'] is {data.get('ok')}"
            assert data.get("success") is False, f"Failed for {exc}: data['success'] is {data.get('success')}"
            assert data.get("error_flag") is True, f"Failed for {exc}: data['error_flag'] is {data.get('error_flag')}"
            assert "disclaimer" in data
            assert data.get("request_token") == "tok-456"
            assert data.get("code") == expected_code
            assert "reply" in data
            assert "error" in data

        # Without operation_token
        resp_no_tok, status_no_tok = _chat_error_response(ValueError("No tok"), mock_g, operation_token=None)
        assert status_no_tok == 400
        data_no_tok = resp_no_tok.get_json()
        assert data_no_tok.get("ok") is False
        assert data_no_tok.get("success") is False
        assert data_no_tok.get("error_flag") is True
        assert "request_token" not in data_no_tok


def test_frontend_lifecycle_safety_ui_js():
    """Verify static/js/ui.js attaches beforeunload to window (not document) and includes safety guards."""
    ui_js_path = Path("static/js/ui.js")
    assert ui_js_path.exists()
    content = ui_js_path.read_text(encoding="utf-8")

    # Must NOT have document.addEventListener("beforeunload"
    assert 'document.addEventListener(\n  "beforeunload"' not in content
    assert 'document.addEventListener("beforeunload"' not in content

    # Must have window.addEventListener with proper typeof window check
    assert 'typeof window !== "undefined"' in content
    assert 'typeof window.addEventListener === "function"' in content
    assert 'window.addEventListener(\n    "beforeunload"' in content or 'window.addEventListener("beforeunload"' in content
    assert "cleanupIntersectionObserver" in content


def test_frontend_lifecycle_safety_chart_js():
    """Verify static/js/chart.js has guarded beforeunload, cancels timers, flushes prefs, and has no duplicates."""
    chart_js_path = Path("static/js/chart.js")
    assert chart_js_path.exists()
    content = chart_js_path.read_text(encoding="utf-8")

    # Count beforeunload registrations
    beforeunload_matches = [
        line for line in content.splitlines()
        if 'addEventListener("beforeunload"' in line or "addEventListener('beforeunload'" in line
    ]
    # Exactly one unified registration
    assert len(beforeunload_matches) == 1, f"Expected 1 beforeunload listener, found: {beforeunload_matches}"

    # Guarded with typeof window
    assert 'typeof window !== "undefined"' in content
    assert "historyPrefetchTimer" in content
    assert "historyPrefetchJobTimers" in content
    assert "_flushChartPrefsToStorage()" in content
    assert "_flushStockColors()" in content


def test_frontend_lifecycle_safety_setup_js():
    """Verify static/js/setup.js uses setupAbortController and guarded beforeunload cleanup."""
    setup_js_path = Path("static/js/setup.js")
    assert setup_js_path.exists()
    content = setup_js_path.read_text(encoding="utf-8")

    assert "setupAbortController" in content
    assert "getOrCreateSetupAbortController" in content
    assert 'typeof window !== "undefined"' in content
    assert 'typeof window.addEventListener === "function"' in content
    assert 'window.addEventListener("beforeunload"' in content
    assert "setupAbortController.abort()" in content
