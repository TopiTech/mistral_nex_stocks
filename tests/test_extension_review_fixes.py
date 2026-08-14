"""Static regression guards for extension review findings R1/R4-R7."""

import json
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app_state import app_state

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class _ElementAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.attributes_by_id[element_id] = attributes


def test_r1_detector_is_explicitly_injected_without_all_site_content_script():
    manifest = json.loads(_read("chrome_extension/manifest.json"))
    assert "activeTab" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]
    assert "content_scripts" not in manifest
    assert "http://*/*" not in manifest.get("host_permissions", [])
    assert "https://*/*" not in manifest.get("host_permissions", [])
    popup = _read("chrome_extension/popup.js")
    assert "chrome.scripting.executeScript" in popup
    assert 'files: ["content.js"]' in popup


def test_r4_native_host_uses_installer_generated_launcher_path():
    template = json.loads(_read("native_host/com.mistral_nex_stocks.host.json.template"))
    assert template["path"] == "__LAUNCHER_PATH__"
    assert "__PYTHON_EXE__" in _read("native_host/host_launcher.cmd.template")
    installer = _read("native_host/install_host_windows.ps1")
    assert "com.mistral_nex_stocks.host.json.template" in installer
    assert "Replace('__LAUNCHER_PATH__'" in installer


def test_r5_failed_refresh_marks_existing_stock_data_stale():
    popup = _read("chrome_extension/popup.js")
    assert 'stockContainer.classList.add("stale")' in popup
    assert "表示中のデータは古い可能性があります" in popup
    assert ".stock-list-container.stale" in _read("chrome_extension/popup.css")


def test_r6_stock_rows_have_keyboard_activation_and_focus_style():
    popup = _read("chrome_extension/popup.js")
    css = _read("chrome_extension/popup.css")
    assert 'setAttribute("role", "button")' in popup
    assert 'setAttribute("tabindex", "0")' in popup
    assert 'event.key === "Enter"' in popup
    assert 'event.key === " "' in popup
    assert ".stock-item:focus-visible" in css


def test_stock_details_deep_link_preserves_symbol():
    popup = _read("chrome_extension/popup.js")
    assert "encodeURIComponent(symbol)" in popup
    assert "/main?q=" in popup


def test_popup_tabs_support_aria_keyboard_navigation():
    popup = _read("chrome_extension/popup.js")
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert f'event.key === "{key}"' in popup
    assert "b.tabIndex = -1" in popup


def test_popup_main_launcher_targets_dashboard_route():
    popup = _read("chrome_extension/popup.js")

    assert '$("openMainBtn")?.addEventListener("click", () => openAppPage("/main"));' in popup


def test_r7_tabs_and_panels_are_connected_with_hidden_semantics():
    html = _read("chrome_extension/popup.html")
    popup = _read("chrome_extension/popup.js")
    parser = _ElementAttributeParser()
    parser.feed(html)
    assert 'role="tablist"' in html
    assert 'aria-controls="tab-content-stocks"' in html
    assert 'aria-labelledby="tab-stocks"' in html
    for panel_id in ("tab-content-detector", "tab-content-system"):
        attributes = parser.attributes_by_id[panel_id]
        assert attributes["aria-hidden"] == "true"
        assert "hidden" in attributes
    assert 'c.setAttribute("aria-hidden", "true")' in popup
    assert 'contentEl.setAttribute("aria-hidden", "false")' in popup


def test_add_ext_does_not_duplicate_legacy_numeric_jp_symbol():
    """R1: normalized extension input must respect legacy JP ticker aliases."""
    app = create_app(skip_bootstrap=True)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app_state.market.user_jp = {"1234": "Legacy Tokyo Stock"}

    try:
        with app.test_client() as client, patch(
            "routes.api_stocks.get_or_create_extension_api_token",
            return_value="extension-test-token",
        ), patch("routes.api_stocks.save_user_stocks"), patch(
            "routes.api_stocks.schedule_sync_all_stocks_now"
        ), patch("utils.networking._is_allowed_shutdown_origin", return_value=True):
            response = client.post(
                "/api/stocks/add_ext",
                json={"symbol": "1234", "market": "jp", "name": "Canonical Tokyo Stock"},
                headers={
                    "Authorization": "Bearer extension-test-token",
                    "X-MNS-Extension-Request": "true",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        assert response.status_code == 200
        assert response.get_json()["message"] == "1234.T already exists in jp"
        assert app_state.market.user_jp == {"1234": "Legacy Tokyo Stock"}
    finally:
        app_state.market.user_jp = {}
