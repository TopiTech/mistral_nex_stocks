# tests/test_code_review_ui_accessibility_2026.py
"""Automated verification tests for UI responsiveness, accessibility, and settings enhancements."""

from __future__ import annotations

import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _read_file(rel_path: str) -> str:
    path = ROOT_DIR / rel_path
    assert path.is_file(), f"File {rel_path} must exist"
    return path.read_text(encoding="utf-8")


def test_settings_template_div_tag_parity():
    """Verify that templates/settings.html maintains strict balance of <div> and </div> tags."""
    content = _read_file("templates/settings.html")
    open_count = content.count("<div")
    close_count = content.count("</div>")
    assert open_count == close_count, (
        f"Mismatched div tags: {open_count} open vs {close_count} close"
    )


def test_settings_template_password_toggles():
    """Verify password visibility toggles and ARIA attributes in templates/settings.html."""
    content = _read_file("templates/settings.html")

    expected_inputs = [
        "mistral-api-key-input",
        "tavily-api-key-input",
        "alphavantage-api-key-input",
    ]

    for inp_id in expected_inputs:
        assert f'id="{inp_id}"' in content, f"Input {inp_id} not found in settings.html"
        assert f'data-target="{inp_id}"' in content, (
            f"Password toggle for {inp_id} not found in settings.html"
        )

    # Check toggle button attributes
    toggle_matches = list(re.finditer(r'<button[^>]*class="[^"]*password-toggle[^"]*"[^>]*>', content))
    assert len(toggle_matches) >= 3, f"Expected at least 3 password toggle buttons, found {len(toggle_matches)}"

    for match in toggle_matches:
        tag = match.group(0)
        assert 'aria-pressed="false"' in tag, f"Toggle missing aria-pressed: {tag}"
        assert 'aria-label=' in tag, f"Toggle missing aria-label: {tag}"


def test_index_template_mobile_nav_semantics():
    """Verify templates/index.html mobile bottom nav uses semantic link for settings."""
    content = _read_file("templates/index.html")

    # mobileSettingsBtn should be an <a> element pointing to /settings
    match = re.search(r'<a[^>]*id="mobileSettingsBtn"[^>]*>', content)
    assert match is not None, "mobileSettingsBtn must be an <a> element"
    tag = match.group(0)
    assert 'href="/settings"' in tag
    assert 'class="mobile-nav-item' in tag


def test_index_css_button_mobile_nav_item_reset():
    """Verify static/css/index.css resets button styles on mobile-nav-item."""
    content = _read_file("static/css/index.css")
    assert "button.mobile-nav-item" in content
    assert "background: transparent;" in content


def test_heatmap_css_responsive_media_queries():
    """Verify static/css/heatmap.css contains responsive media queries for controls."""
    content = _read_file("static/css/heatmap.css")

    assert "@media (max-width: 1024px)" in content
    assert "@media (max-width: 768px)" in content
    assert ".heatmap-controls" in content
    assert "flex-wrap: wrap;" in content
    assert "flex-direction: column;" in content


def test_settings_css_password_wrapper_styles():
    """Verify static/css/settings.css contains styling for password wrapper and toggle."""
    content = _read_file("static/css/settings.css")

    assert ".password-wrapper" in content
    assert ".password-toggle" in content
    assert ".password-toggle:hover" in content
    assert ".password-toggle.visible" in content


def test_settings_js_password_toggle_and_enter_key():
    """Verify static/js/settings.js binds Enter key submission and password visibility toggle."""
    content = _read_file("static/js/settings.js")

    assert "function togglePasswordVisibility(" in content
    assert "password-toggle" in content
    assert "isComposing" in content
    assert 'e.key === "Enter"' in content
    assert "saveAlphaBtn?.click()" in content
