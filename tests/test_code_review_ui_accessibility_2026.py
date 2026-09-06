# tests/test_code_review_ui_accessibility_2026.py
"""Automated verification tests for UI responsiveness, accessibility, and settings enhancements."""

from __future__ import annotations

import re
import shutil
import subprocess
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


def test_index_js_mobile_settings_anchor_modifier_click_guard():
    """mobileSettingsBtn click handler must not hijack modifier/auxiliary clicks.

    mobileSettingsBtn was converted from <button> to <a href="/settings">.
    If the JS handler unconditionally calls preventDefault(), auxiliary
    navigation such as Ctrl/Cmd+click (open in new tab) would also navigate
    the current tab. The handler must bail out for modifier keys, non-primary
    buttons, and already-handled events, and only then prevent the default.
    """
    content = _read_file("static/js/index_main.js")
    match = re.search(
        r'getElementById\("mobileSettingsBtn"\)[\s\S]*?addEventListener\("click",'
        r"\s*\(event\)\s*=>\s*\{([\s\S]*?)\}\);",
        content,
    )
    assert match is not None, "mobileSettingsBtn click handler taking (event) not found"
    body = match.group(1)

    for guard in (
        "event.defaultPrevented",
        "event.button !== 0",
        "event.metaKey",
        "event.ctrlKey",
        "event.shiftKey",
        "event.altKey",
    ):
        assert guard in body, f"mobileSettingsBtn handler missing guard: {guard}"

    # preventDefault must come after the guard so plain left clicks still
    # navigate via JS, while modified clicks keep native anchor behavior.
    guard_idx = body.find("event.defaultPrevented")
    prevent_idx = body.find("event.preventDefault()")
    assert prevent_idx != -1, "handler must call event.preventDefault() for plain clicks"
    assert guard_idx < prevent_idx


def test_settings_js_password_toggle_and_enter_key():
    """Verify static/js/settings.js binds Enter key submission and password visibility toggle."""
    content = _read_file("static/js/settings.js")

    assert "function togglePasswordVisibility(" in content
    assert "password-toggle" in content
    assert "isComposing" in content
    assert 'e.key === "Enter"' in content
    assert "saveAlphaBtn?.click()" in content


def test_dashboard_search_uses_an_accessible_listbox_and_ignores_stale_responses():
    """Arrow-key search selection must be exposed and latest-query wins."""
    template = _read_file("templates/index.html")
    main_source = _read_file("static/js/index_main.js")
    api_source = _read_file("static/js/api.js")

    assert 'role="combobox"' in template
    assert 'aria-controls="search-results-list"' in template
    assert 'role="listbox"' in template
    assert 'aria-labelledby="search-results-title"' in template
    assert 'row.setAttribute("role", "option")' in api_source
    assert 'row.setAttribute("aria-selected", "false")' in api_source
    assert "row.tabIndex = -1" in api_source
    assert 'row.id = `search-result-option-${index}`' in api_source
    assert 'searchInput.setAttribute("aria-activedescendant", activeItem.id)' in main_source
    assert 'item.setAttribute("aria-selected", String(isSelected))' in main_source
    assert 'searchInput.addEventListener("searchresultschange", clearHighlightedResult)' in main_source
    assert "const controller = new AbortController();" in api_source
    assert "if (activeSearchController !== controller) return;" in api_source


def test_setup_page_has_a_single_top_level_heading():
    """The onboarding page needs a navigable page topic for screen readers."""
    content = _read_file("templates/setup.html")

    assert content.count("<h1") == 1
    assert '<h1 class="logo">' in content
    assert "<h2>🚀 利用できる機能</h2>" in content


def test_dashboard_search_arrow_navigation_updates_aria_state_at_runtime():
    """The visual selection and the combobox active option must stay in sync."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend runtime regression test")

    source = _read_file("static/js/index_main.js")
    start = source.index("function initSearchEvents()")
    end = source.index("/** Initialize tab switching events */", start)
    search_events_source = source[start:end]
    script = f"""
const vm = require("vm");
const source = {search_events_source!r};

function makeItem(id) {{
  const classes = new Set();
  return {{
    id,
    attributes: {{}},
    classList: {{
      toggle(name, active) {{ active ? classes.add(name) : classes.delete(name); }},
      remove(name) {{ classes.delete(name); }},
      contains(name) {{ return classes.has(name); }},
    }},
    setAttribute(name, value) {{ this.attributes[name] = String(value); }},
    scrollIntoView() {{ this.scrolled = true; }},
  }};
}}

const items = [makeItem("search-result-option-0"), makeItem("search-result-option-1")];
const listeners = {{}};
const input = {{
  attributes: {{}},
  addEventListener(name, handler) {{ listeners[name] = handler; }},
  setAttribute(name, value) {{ this.attributes[name] = String(value); }},
  removeAttribute(name) {{ delete this.attributes[name]; }},
}};
const document = {{
  getElementById(id) {{
    if (id === "searchInput") return input;
    if (id === "search-results-list") return {{ querySelectorAll: () => items }};
    return null;
  }},
}};
const context = {{ document, console }};
vm.runInNewContext(`${{source}}\nglobalThis.initSearchEvents = initSearchEvents;`, context);
context.initSearchEvents();

function key(key) {{
  return {{ key, keyCode: 0, isComposing: false, preventDefault() {{ this.prevented = true; }} }};
}}

listeners.keydown(key("ArrowDown"));
if (input.attributes["aria-activedescendant"] !== "search-result-option-0") throw new Error("first option was not made active");
if (items[0].attributes["aria-selected"] !== "true" || !items[0].classList.contains("highlighted")) throw new Error("first option state was not announced");

listeners.keydown(key("ArrowDown"));
if (input.attributes["aria-activedescendant"] !== "search-result-option-1") throw new Error("second option was not made active");
if (items[0].attributes["aria-selected"] !== "false" || items[1].attributes["aria-selected"] !== "true") throw new Error("selection state did not move");

listeners.input();
if (input.attributes["aria-activedescendant"] !== undefined) throw new Error("stale active descendant was not cleared");
if (items.some((item) => item.attributes["aria-selected"] !== "false" || item.classList.contains("highlighted"))) throw new Error("stale option state was not cleared");
console.log("search keyboard accessibility checks passed");
"""
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("search keyboard accessibility checks passed")
