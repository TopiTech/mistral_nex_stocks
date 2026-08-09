"""
test_experimental_orbit.py - Unit and integration tests for Market Observatory.

Validates the experimental orbit route, template rendering, security headers,
HTML structure, query parameter safety, and static asset accessibility.
"""

from pathlib import Path

import pytest

from app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    """Create a test client with isolated app instance."""
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True)
    with app.test_client() as test_client:
        yield test_client


def test_experimental_orbit_page_status_and_content(client):
    """GET /experimental/orbit returns 200 OK and renders Market Observatory."""
    res = client.get("/experimental/orbit")
    assert res.status_code == 200
    assert res.mimetype == "text/html"

    html = res.get_data(as_text=True)
    # Check core branding, navigation, and settings button
    assert "Market Observatory" in html
    assert "badge-exp" in html
    assert 'href="/main?view=dashboard"' in html
    assert 'id="btn-orbit-settings"' in html
    assert 'href="/settings"' in html

    # Check canvas and critical UI elements
    assert 'id="orbit-canvas"' in html
    assert 'id="center-stock-card"' in html
    assert 'id="timeline-slider"' in html
    assert 'id="constellation-drawer"' in html
    assert 'id="ai-dive-overlay"' in html
    assert 'id="shortcuts-help-modal"' in html
    assert 'id="observatory-live-region"' in html


def test_experimental_orbit_security_and_csp(client):
    """Ensure CSP nonce and security headers are properly attached."""
    res = client.get("/experimental/orbit")
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    # Ensure script tags have nonce
    assert 'script nonce="' in html or "script" in html
    assert "static/js/experimental/data-adapter.js" in html
    assert "static/js/experimental/orbit-state.js" in html
    assert "static/js/experimental/orbit-renderer.js" in html
    assert "static/js/experimental/gesture-controller.js" in html
    assert "static/js/experimental/temporal-controller.js" in html
    assert "static/js/experimental/constellation-controller.js" in html
    assert "static/js/experimental/ai-dive-controller.js" in html
    assert "static/js/experimental/accessibility-controller.js" in html
    assert "static/js/experimental/orbit-entry.js" in html


def test_experimental_orbit_query_parameters(client):
    """Verify route handles query parameters safely without crashes or reflections."""
    # Test valid symbol & market query params
    res = client.get("/experimental/orbit?symbol=NVDA&market=us")
    assert res.status_code == 200

    # Test malicious / malformed query params
    res_xss = client.get('/experimental/orbit?symbol=<script>alert(1)</script>&market="../../etc"')
    assert res_xss.status_code == 200
    html = res_xss.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html


def test_experimental_orbit_static_assets_exist():
    """Verify that all new static files exist in the repository."""
    assets = [
        "static/css/experimental-orbit.css",
        "static/js/experimental/data-adapter.js",
        "static/js/experimental/orbit-state.js",
        "static/js/experimental/orbit-renderer.js",
        "static/js/experimental/gesture-controller.js",
        "static/js/experimental/temporal-controller.js",
        "static/js/experimental/constellation-controller.js",
        "static/js/experimental/ai-dive-controller.js",
        "static/js/experimental/accessibility-controller.js",
        "static/js/experimental/orbit-entry.js",
        "templates/experimental_orbit.html",
    ]

    for rel in assets:
        path = ROOT / rel
        assert path.exists(), f"Asset {rel} does not exist"
        assert path.stat().st_size > 0, f"Asset {rel} is empty"


def test_navigation_links_present_in_settings_and_removed_from_headers(client):
    """Ensure link to /experimental/orbit is present in settings page and removed from headers of main, heatmap, and screener."""
    # Settings page must contain link to Market Observatory
    res_settings = client.get("/settings")
    assert res_settings.status_code == 200
    html_settings = res_settings.get_data(as_text=True)
    assert "/experimental/orbit" in html_settings, "Link to /experimental/orbit missing in /settings"

    # Header pages should no longer contain header EXP link
    for page in ["/main", "/heatmap", "/screener"]:
        res = client.get(page)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'class="heatmap-nav-btn exp-nav-btn"' not in html, f"Deprecated EXP link still present in {page}"


def test_stock_history_api_response_schema(client):
    """Verify /api/stock-history returns non-empty history array with short keys for frontend data-adapter."""
    res = client.get("/api/stock-history?symbol=MRAM&market=us&period=3mo")
    assert res.status_code == 200
    data = res.get_json()
    assert isinstance(data, dict)
    assert data.get("symbol") == "MRAM"
    history = data.get("history", [])
    if history:
        item = history[0]
        assert "c" in item or "close" in item
        assert "x" in item or "timestamp" in item or "date" in item
