"""Regression tests for 2026 frontend, UI, and template audit fixes (Tasks 04-12)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executing frontend JavaScript test scripts")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Node script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result.stdout


# ===========================================================================
# TASK-04: chrome_extension/popup.js detectorGeneration race condition fix
# ===========================================================================


def test_task04_detector_generation_counter():
    """TASK-04: loadDetectedTickers tracks generation counter to discard stale async responses."""
    source = _read("chrome_extension/popup.js")
    assert "let detectorGeneration = 0;" in source
    assert "const currentGen = ++detectorGeneration;" in source
    assert "if (currentGen !== detectorGeneration) return;" in source

    # Node.js execution verification of generation counter race condition prevention
    node_test = r"""
const assert = require("assert");

let detectorGeneration = 0;
const results = [];

async function fakeLoadDetectedTickers(requestId, delayMs, resultData) {
  const currentGen = ++detectorGeneration;
  await new Promise((resolve) => setTimeout(resolve, delayMs));
  if (currentGen !== detectorGeneration) {
    // Discard stale response
    return;
  }
  results.push({ requestId, resultData });
}

(async () => {
  // Call 1 takes 50ms, Call 2 takes 10ms -> Call 2 completes first and sets detectorGeneration to 2.
  // When Call 1 finishes later, currentGen (1) !== detectorGeneration (2), so it is discarded.
  const p1 = fakeLoadDetectedTickers(1, 50, ["TICKER_A"]);
  const p2 = fakeLoadDetectedTickers(2, 10, ["TICKER_B"]);

  await Promise.all([p1, p2]);

  assert.strictEqual(results.length, 1);
  assert.strictEqual(results[0].requestId, 2);
  assert.deepStrictEqual(results[0].resultData, ["TICKER_B"]);
  console.log("TASK-04 OK");
})();
"""
    out = _run_node(node_test)
    assert "TASK-04 OK" in out


# ===========================================================================
# TASK-05: chrome_extension/popup.js focus and scroll preservation in polling
# ===========================================================================


def test_task05_popup_focus_and_scroll_preservation():
    """TASK-05: filterAndRenderStocks remembers active focused element and scroll position."""
    source = _read("chrome_extension/popup.js")
    assert "activeElement.getAttribute(\"data-symbol\")" in source
    assert "const prevScrollTop = container.scrollTop;" in source
    assert "container.scrollTop = prevScrollTop;" in source
    assert "elementToFocus.focus({ preventScroll: true });" in source


# ===========================================================================
# TASK-06: static/css/screener.css mobile bottom padding for navbar overlay
# ===========================================================================


def test_task06_screener_mobile_padding_bottom():
    """TASK-06: body.screener-page has padding-bottom in @media (max-width: 768px)."""
    source = _read("static/css/screener.css")
    # Verify the rule exists within media query
    match = re.search(
        r"@media\s*\(\s*max-width:\s*768px\s*\)\s*\{[^}]*body\.screener-page\s*\{[^}]*padding-bottom:\s*calc\(64px\s*\+\s*env\(safe-area-inset-bottom,\s*0px\)\);",
        source,
        re.DOTALL,
    )
    assert match is not None, "body.screener-page padding-bottom rule not found under max-width: 768px"


# ===========================================================================
# TASK-07: templates/experimental_orbit.html duplicate colors.css removal
# ===========================================================================


def test_task07_orbit_no_duplicate_colors_css():
    """TASK-07: experimental_orbit.html does not duplicate colors.css from base.html."""
    orbit_source = _read("templates/experimental_orbit.html")
    assert "css/colors.css" not in orbit_source, "Duplicate colors.css found in experimental_orbit.html"

    base_source = _read("templates/base.html")
    assert "css/colors.css" in base_source, "Base colors.css missing from base.html"


# ===========================================================================
# TASK-09: static/css/heatmap.css duplicate .heatmap-tooltip removal
# ===========================================================================


def test_task09_heatmap_tooltip_duplicate_removal():
    """TASK-09: Old duplicate .heatmap-tooltip is removed, keeping sleek enhanced tooltip."""
    source = _read("static/css/heatmap.css")
    # The old duplicate had background: rgba(8, 12, 22, 0.94);
    assert "rgba(8, 12, 22, 0.94)" not in source, "Old .heatmap-tooltip background definition still exists"

    # The sleek one exists
    assert "/* --- Sleek Tooltip Enhancement --- */" in source
    assert "rgba(10, 14, 23, 0.88)" in source


# ===========================================================================
# TASK-10: static/css/settings.css duplicate .observatory-launch-wrapper removal
# ===========================================================================


def test_task10_settings_observatory_duplicate_removal():
    """TASK-10: Duplicate .observatory-launch-wrapper in settings.css is eliminated."""
    source = _read("static/css/settings.css")
    occurrences = re.findall(r"\.observatory-launch-wrapper\s*\{", source)
    assert len(occurrences) == 1, f"Expected exactly 1 occurrence of .observatory-launch-wrapper, found {len(occurrences)}"


# ===========================================================================
# TASK-11: static/js/index_main.js idempotent portfolio step buttons init
# ===========================================================================


def test_task11_portfolio_step_buttons_idempotent_init():
    """TASK-11: Portfolio modal step buttons are initialized once with flag guard."""
    source = _read("static/js/index_main.js")
    assert "_pfStepButtonsInitialized" in source
    assert "initPortfolioStepButtons" in source
    assert "if (_pfStepButtonsInitialized) return;" in source
    assert "initPortfolioStepButtons();" in source


# ===========================================================================
# TASK-12: templates/screener.html sort indicator aria-hidden="true"
# ===========================================================================


def test_task12_screener_sort_indicator_aria_hidden():
    """TASK-12: All table header sort indicator spans have aria-hidden='true'."""
    source = _read("templates/screener.html")

    # There should be 4 sort-indicator elements
    indicators_with_aria = re.findall(r'<span class="sort-indicator" aria-hidden="true"></span>', source)
    assert len(indicators_with_aria) == 4, f"Expected 4 sort-indicators with aria-hidden='true', found {len(indicators_with_aria)}"

    # Ensure no bare sort-indicator spans exist without aria-hidden
    bare_indicators = re.findall(r'<span class="sort-indicator"\s*></span>', source)
    assert len(bare_indicators) == 0, f"Found {len(bare_indicators)} bare sort-indicator spans without aria-hidden"
