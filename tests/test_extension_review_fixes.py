"""Static regression guards for extension review findings R1/R4-R7."""

import json
from html.parser import HTMLParser
from pathlib import Path

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
    assert 'chrome.scripting.executeScript' in popup
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
