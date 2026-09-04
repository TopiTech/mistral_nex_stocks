"""Comprehensive regression tests guarding the fixes and improvements
identified in the 2026 code review audit.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from app import _early_excepthook, create_app
from routes.api_system import _build_safe_credentials_response

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _read_normalized(rel: str) -> str:
    return " ".join(_read(rel).split())


# ===========================================================================
# 1. System Resilience & _early_excepthook
# ===========================================================================
def test_early_excepthook_robustness():
    """_early_excepthook must handle non-type exception types gracefully."""
    mock_orig_hook = MagicMock()
    with patch("sys.__excepthook__", mock_orig_hook):
        _early_excepthook(None, None, None)
    mock_orig_hook.assert_called_once_with(None, None, None)


def test_early_excepthook_posix_directory_fallback(tmp_path: Path):
    """On POSIX systems, _early_excepthook must resolve XDG_DATA_HOME."""
    with (
        patch.dict(
            "os.environ",
            {"MNS_DATA_DIR": "", "MNS_APP_DATA_DIR": "", "XDG_DATA_HOME": str(tmp_path)},
            clear=False,
        ),
        patch("app._is_windows_runtime", return_value=False),
    ):
        mock_orig = MagicMock()
        with patch("sys.__excepthook__", mock_orig):
            try:
                raise RuntimeError("POSIX early error test")
            except RuntimeError:
                import sys

                exc_type, exc_val, exc_tb = sys.exc_info()
                _early_excepthook(exc_type, exc_val, exc_tb)

        posix_target = tmp_path / "mistral_nex_stocks"
        assert (posix_target / "backend.log").exists()
        assert (posix_target / "error.log").exists()
        log_txt = (posix_target / "backend.log").read_text(encoding="utf-8")
        assert "POSIX early error test" in log_txt


# ===========================================================================
# 2. Credential Security & Uniform Response Filtering
# ===========================================================================
def test_safe_credentials_response_filters_internal_fields():
    """_build_safe_credentials_response must strictly filter to allowed keys."""
    fake_state = {
        "has_mistral_api_key": True,
        "has_langsearch_api_key": False,
        "has_tavily_api_key": True,
        "has_alphavantage_api_key": False,
        "mistral_model": "mistral-small-2603",
        "is_ai_technical_lines_eligible": False,
        "credentials_ephemeral": False,
        "credentials_ephemeral_keys": [],
        "credentials_ephemeral_warning": None,
        "mistral_api_key_min_length": 32,
        "langsearch_api_key_min_length": 20,
        "tavily_api_key_min_length": 5,
        "sensitive_raw_key": "MUST_NOT_LEAK",
        "internal_worker_secret": "TOP_SECRET",
    }
    with (
        patch("routes.api_system.get_api_credential_state", return_value=fake_state),
        patch("routes.api_system.get_custom_ai_prompt", return_value="custom-prompt"),
    ):
        resp = _build_safe_credentials_response()
        assert "sensitive_raw_key" not in resp
        assert "internal_worker_secret" not in resp
        assert resp["has_mistral_api_key"] is True
        assert resp["custom_ai_prompt"] == "custom-prompt"


def test_credentials_delete_uses_safe_credentials_filtering():
    """DELETE /api/credentials must strictly use safe credentials filtering."""
    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with (
            patch("routes.api_system.clear_api_credentials", return_value=[]),
            patch(
                "routes.api_system.get_api_credential_state",
                return_value={
                    "has_mistral_api_key": False,
                    "has_langsearch_api_key": False,
                    "has_tavily_api_key": False,
                    "has_alphavantage_api_key": False,
                    "mistral_model": "mistral-small-2603",
                    "is_ai_technical_lines_eligible": False,
                    "credentials_ephemeral": False,
                    "credentials_ephemeral_keys": [],
                    "credentials_ephemeral_warning": None,
                    "mistral_api_key_min_length": 32,
                    "langsearch_api_key_min_length": 20,
                    "tavily_api_key_min_length": 5,
                    "leak_on_delete": "SHOULD_BE_BLOCKED",
                },
            ),
        ):
            res = client.delete("/api/credentials", headers={"Origin": "http://localhost:5000"})
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            assert "leak_on_delete" not in data


# ===========================================================================
# 3. Screener Keyboard Navigation & Accessibility Isolation
# ===========================================================================
def test_screener_tr_keydown_does_not_hijack_inner_buttons():
    """Screener row keydown must guard e.target !== tr so add button events don't trigger navigation."""
    js = _read("static/js/screener.js")
    assert "if (e.target !== tr) return;" in js
    assert 'data?.details?.reason === "既に追加済み"' in js
    assert "updateSortOrderBtn();" in js
    assert (
        'aria-label",\n        sortOrder === "desc"' in js
        or 'aria-label", sortOrder === "desc"' in js
        or 'sortOrder === "desc"' in js
    )


def test_screener_template_has_accessible_filter_groups():
    """templates/screener.html must declare role=group and aria-label for filters."""
    html = _read_normalized("templates/screener.html")
    assert 'id="screenerMarketToggle" role="group" aria-label="市場選択"' in html
    assert 'id="screenerChangePreset" role="group" aria-label="騰落率プリセット"' in html
    assert 'id="screenerSortOrderBtn"' in html
    assert 'aria-label="降順（クリックで昇順に切り替え）"' in html


# ===========================================================================
# 4. Dashboard Tabs & Portfolio Mode Accessibility
# ===========================================================================
def test_index_tabs_and_portfolio_subnav_a11y_attributes():
    """templates/index.html must have proper aria-label, roving tabindex, and panel linkages."""
    html = _read_normalized("templates/index.html")
    assert 'class="tabs" role="tablist" aria-label="市場・ポートフォリオ切り替え"' in html
    assert 'id="tab-jp"' in html and 'tabindex="-1"' in html
    assert 'class="pf-mode-subnav" role="tablist" aria-label="ポートフォリオ表示種別"' in html
    assert 'id="pf-mode-my"' in html
    assert 'aria-controls="my-portfolio-view"' in html
    assert 'id="pf-mode-ai"' in html
    assert 'aria-controls="ai-portfolio-view"' in html
    assert 'id="my-portfolio-view" role="tabpanel" aria-labelledby="pf-mode-my"' in html
    assert 'id="ai-portfolio-view"' in html
    assert 'role="tabpanel"' in html
    assert 'aria-labelledby="pf-mode-ai"' in html


def test_ai_portfolio_js_subnav_keyboard_and_hidden_attributes():
    """static/js/ai_portfolio.js must manage tabindex, hidden attributes, and arrow keys."""
    js = _read("static/js/ai_portfolio.js")
    assert 'myTab.setAttribute("tabindex", "0");' in js
    assert 'aiTab.setAttribute("tabindex", "-1");' in js
    assert 'myView.removeAttribute("hidden");' in js
    assert 'aiView.setAttribute("hidden", "");' in js
    assert 'e.key === "ArrowRight" || e.key === "ArrowLeft"' in js
    assert (
        'pill.setAttribute(\n        "aria-pressed",' in js
        or 'pill.setAttribute("aria-pressed"' in js
    )


# ===========================================================================
# 5. Settings Accessibility & API Key Update Fields
# ===========================================================================
def test_settings_template_has_labels_and_credential_inputs():
    """templates/settings.html must include accessible labels and key update fields."""
    html = _read_normalized("templates/settings.html")
    assert '<label for="custom-prompt-input"' in html
    assert '<label for="alphavantage-api-key-input"' in html
    assert '<label for="mistral-api-key-input"' in html
    assert '<label for="tavily-api-key-input"' in html
    assert 'id="mistral-api-key-input"' in html
    assert 'id="tavily-api-key-input"' in html


def test_settings_js_saves_updated_credentials():
    """static/js/settings.js must support saving mistral and tavily keys."""
    js = _read("static/js/settings.js")
    assert 'mistralInput = document.getElementById("mistral-api-key-input");' in js
    assert 'tavilyInput = document.getElementById("tavily-api-key-input");' in js
    assert "payload.mistral_api_key = mistralInput.value.trim();" in js
    assert "payload.tavily_api_key = tavilyInput.value.trim();" in js


# ===========================================================================
# 6. UI Drawer Null DetailPanel Cleanup
# ===========================================================================
def test_ui_drawer_clears_content_when_detail_panel_is_missing():
    """static/js/ui.js openStockDetailDrawer must clean chartContent and aiContent on null detailPanel."""
    js = _read("static/js/ui.js")
    start = js.index("function openStockDetailDrawer(")
    end = js.index("function closeStockDetailDrawer(", start)
    drawer_fn = js[start:end]
    assert "chartContent.replaceChildren();" in drawer_fn
    assert "aiContent.replaceChildren();" in drawer_fn
    assert "drawer-empty-msg" in drawer_fn


# ===========================================================================
# 7. Documentation Layout
# ===========================================================================
def test_readme_license_section_cleanliness():
    """README.md License section must not contain displaced AI portfolio subsection."""
    readme = _read("README.md")
    license_start = readme.index("## ライセンス / License")
    license_text = readme[license_start:]
    assert "### AIポートフォリオの対象市場" not in license_text
    assert "MIT License" in license_text
    assert "AIポートフォリオの対象市場" in readme[:license_start]
