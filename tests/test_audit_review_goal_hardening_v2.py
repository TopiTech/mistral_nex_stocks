"""
Regression test suite for autonomous review, hardening & accessibility v2 (2026).
Covers:
1. routes/api_analysis.py: api_news timestamp parsing resilience against boolean and corrupted cache values.
2. routes/api_analysis.py: api_analyze_v2 price validation rejecting both standard and NumPy booleans.
3. templates/index.html & experimental_orbit.html: Dialog containers have tabindex="-1" for programmatic focus.
4. templates/index.html: Drawer tabs have roving tabindex (0 on active tab, -1 on inactive tab).
5. static/js/ui.js: selectChartTab and selectAiTab dynamically update tabindex for roving tabindex.
"""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app import create_app
from error_codes import ErrorCode


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MNS_DATA_DIR", "tests_runtime_data")
    monkeypatch.setenv("MNS_DISABLE_LOCAL_RATE_LIMIT", "1")
    app = create_app(config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True)
    with app.test_client() as c:
        yield c


@pytest.mark.parametrize(
    "corrupted_ts",
    [True, False, np.bool_(True), "invalid_ts", None, float("nan"), float("inf"), float("-inf")],
)
def test_api_news_resilience_to_malformed_ts(client, corrupted_ts):
    """Verify /api/news safely parses corrupted or boolean timestamp cache values without 500 error."""
    mock_bundle = {
        "items": [{"title": "Market Rally", "source": "Bloomberg", "url": "https://example.com/1"}],
        "cached_at": 1700000000.0,
    }

    def mock_get_cached_value(key, duration=86400, default=None):
        if "_ts" in key:
            return corrupted_ts
        return mock_bundle

    with patch("routes.api_analysis.extract_api_key", return_value="fake-api-key-123456789012345678901234"), \
         patch("routes.api_analysis._submit_in_app_context"), \
         patch("utils.caching._get_cached_value", side_effect=mock_get_cached_value):
        resp = client.post(
            "/api/news",
            json={"strategy": "standard"},
            headers={"Origin": "http://localhost:5000", "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "items" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["title"] == "Market Rally"


def test_api_analyze_v2_rejects_booleans(client):
    """Verify /api/analyze-v2 strictly rejects boolean values for price."""
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    with patch("routes.api_analysis.extract_api_key", return_value="fake-api-key-123456789012345678901234"):
        # Python bool True
        resp = client.post(
            "/api/analyze-v2",
            json={"symbol": "AAPL", "market": "us", "price": True, "request_token": "a" * 32},
            headers=headers,
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data.get("error_code") == ErrorCode.INVALID_INPUT.value
        assert "price must be a finite number" in data.get("details", {}).get("reason", "")

        # Python bool False
        resp_false = client.post(
            "/api/analyze-v2",
            json={"symbol": "AAPL", "market": "us", "price": False, "request_token": "a" * 32},
            headers=headers,
        )
        assert resp_false.status_code == 400
        assert resp_false.get_json()["error_code"] == ErrorCode.INVALID_INPUT.value

    # Direct request handler test with NumPy bool scalar in payload dict
    from routes.api_analysis import api_analyze_v2
    app = create_app(config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True)
    with patch("routes.api_analysis.extract_api_key", return_value="fake-api-key-123456789012345678901234"), \
         patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, "")), \
         patch(
             "routes.api_analysis._parse_json_request",
             return_value={"symbol": "AAPL", "market": "us", "price": np.bool_(True), "request_token": "a" * 32},
         ):
        with app.test_request_context(
            "/api/analyze-v2",
            method="POST",
            headers=headers,
        ):
            raw_resp = api_analyze_v2()
            if isinstance(raw_resp, tuple):
                data = raw_resp[0].get_json()
            else:
                data = raw_resp.get_json()
            assert data["ok"] is False
            assert data.get("error_code") == ErrorCode.INVALID_INPUT.value
            assert "price must be a finite number" in data.get("details", {}).get("reason", "")


def test_templates_dialog_tabindex_and_roving_tabindex():
    """Verify HTML dialogs have tabindex="-1" and drawer tabs implement roving tabindex."""
    project_root = Path(__file__).resolve().parent.parent

    # 1. templates/index.html dialogs & tabs
    index_html = (project_root / "templates" / "index.html").read_text(encoding="utf-8")

    dialog_ids = [
        "portfolioModal",
        "alertModal",
        "stock-detail-drawer",
        "ai-drawer",
        "chart-fullscreen-modal",
    ]
    for d_id in dialog_ids:
        assert f'id="{d_id}"' in index_html, f"Missing dialog {d_id} in templates/index.html"
        start_idx = index_html.find(f'id="{d_id}"')
        tag_start = index_html.rfind("<", 0, start_idx)
        tag_end = index_html.find(">", start_idx) + 1
        element_tag = index_html[tag_start:tag_end]
        assert 'tabindex="-1"' in element_tag, f"Dialog {d_id} missing tabindex='-1' in tag: {element_tag}"

    # Drawer tabs in index.html
    assert 'id="drawerTabChartBtn"' in index_html
    chart_start = index_html.find('id="drawerTabChartBtn"')
    chart_tag = index_html[index_html.rfind("<", 0, chart_start): index_html.find(">", chart_start) + 1]
    assert 'tabindex="0"' in chart_tag, "drawerTabChartBtn missing tabindex='0'"

    assert 'id="drawerTabAiBtn"' in index_html
    ai_start = index_html.find('id="drawerTabAiBtn"')
    ai_tag = index_html[index_html.rfind("<", 0, ai_start): index_html.find(">", ai_start) + 1]
    assert 'tabindex="-1"' in ai_tag, "drawerTabAiBtn missing tabindex='-1'"

    # 2. templates/experimental_orbit.html dialogs
    orbit_html = (project_root / "templates" / "experimental_orbit.html").read_text(encoding="utf-8")
    orbit_dialog_ids = ["ai-dive-overlay", "shortcuts-help-modal", "orbit-search-modal"]
    for d_id in orbit_dialog_ids:
        assert f'id="{d_id}"' in orbit_html, f"Missing dialog {d_id} in experimental_orbit.html"
        start_idx = orbit_html.find(f'id="{d_id}"')
        tag_start = orbit_html.rfind("<", 0, start_idx)
        tag_end = orbit_html.find(">", start_idx) + 1
        element_tag = orbit_html[tag_start:tag_end]
        assert 'tabindex="-1"' in element_tag, f"Dialog {d_id} in experimental_orbit.html missing tabindex='-1'"


def test_ui_js_roving_tabindex_logic():
    """Verify static/js/ui.js selectChartTab and selectAiTab dynamically toggle tabindex 0 and -1."""
    project_root = Path(__file__).resolve().parent.parent
    ui_js = (project_root / "static" / "js" / "ui.js").read_text(encoding="utf-8")

    # selectChartTab must set chartTabBtn tabindex to "0" and aiTabBtn to "-1"
    assert 'chartTabBtn?.setAttribute("tabindex", "0");' in ui_js
    assert 'aiTabBtn?.setAttribute("tabindex", "-1");' in ui_js

    # selectAiTab must set aiTabBtn tabindex to "0" and chartTabBtn to "-1"
    assert 'aiTabBtn?.setAttribute("tabindex", "0");' in ui_js
    assert 'chartTabBtn?.setAttribute("tabindex", "-1");' in ui_js
