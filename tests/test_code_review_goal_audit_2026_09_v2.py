# tests/test_code_review_goal_audit_2026_09_v2.py
"""Regression tests for code review audit fixes v2 (September 2026).

Covers:
- Screener reset table sort indicator synchronization.
- Frontend LocalStorage write exception safety across state, api, index_main, settings.
- Chrome extension launcher parity for /experimental/orbit route and grid styling.
- Backend screener default parameter handling and sorting consistency.
"""

from __future__ import annotations

import pathlib
import re
import unittest


class TestScreenerResetTableIndicatorSync(unittest.TestCase):
    """Test screener reset table sort indicator synchronization."""

    def test_screener_reset_calls_update_table_sort_indicators(self):
        """screener.js must call updateTableSortIndicators() when resetting."""
        screener_path = pathlib.Path("static/js/screener.js")
        content = screener_path.read_text(encoding="utf-8")

        start_idx = content.find('resetBtn.addEventListener("click"')
        self.assertNotEqual(start_idx, -1, "resetBtn click listener must exist in screener.js")
        end_idx = content.find("triggerFetch();", start_idx)
        self.assertNotEqual(end_idx, -1, "triggerFetch must follow resetBtn listener")
        body = content[start_idx:end_idx]

        self.assertIn("updateSortOrderBtn()", body)
        self.assertIn("updateTableSortIndicators()", body)

        # Ensure updateTableSortIndicators is called after updateSortOrderBtn
        sort_btn_pos = body.index("updateSortOrderBtn()")
        table_ind_pos = body.index("updateTableSortIndicators()")
        self.assertLess(sort_btn_pos, table_ind_pos)


class TestLocalStorageHardening(unittest.TestCase):
    """Test frontend localStorage resilience against QuotaExceeded / SecurityError."""

    def test_api_js_apply_analysis_result_catches_localstorage(self):
        """api.js applyAnalysisResult must safely wrap localStorage.setItem."""
        api_path = pathlib.Path("static/js/api.js")
        content = api_path.read_text(encoding="utf-8")

        self.assertIn("applyAnalysisResult", content)
        target = (
            "  try {\n"
            "    localStorage.setItem(`ai_prev_${stockKey}`, JSON.stringify(data));\n"
            "  } catch (_e) {"
        )
        self.assertIn(target, content)

    def test_api_js_set_sse_mode_catches_localstorage(self):
        """api.js setSseMode must safely wrap localStorage.setItem."""
        api_path = pathlib.Path("static/js/api.js")
        content = api_path.read_text(encoding="utf-8")

        match = re.search(
            r'try\s*\{\s*localStorage\.setItem\("mns_sse_mode",',
            content,
        )
        self.assertIsNotNone(
            match, "localStorage.setItem for mns_sse_mode must be protected by try/catch"
        )

    def test_state_js_save_favorites_catches_localstorage(self):
        """state.js saveFavorites must safely wrap localStorage.setItem."""
        state_path = pathlib.Path("static/js/state.js")
        content = state_path.read_text(encoding="utf-8")

        match = re.search(
            r'saveFavorites\(\)\s*\{\s*try\s*\{\s*localStorage\.setItem\("favorites",',
            content,
        )
        self.assertIsNotNone(
            match, "localStorage.setItem for favorites must be protected by try/catch"
        )

    def test_index_main_js_save_alerts_config_catches_localstorage(self):
        """index_main.js saveAlertsConfig must safely wrap localStorage.setItem."""
        index_path = pathlib.Path("static/js/index_main.js")
        content = index_path.read_text(encoding="utf-8")

        match = re.search(
            r'function saveAlertsConfig\(cfg\)\s*\{\s*try\s*\{\s*localStorage\.setItem\("userAlerts",',
            content,
        )
        self.assertIsNotNone(
            match, "localStorage.setItem for userAlerts must be protected by try/catch"
        )

    def test_settings_js_save_sort_order_catches_localstorage(self):
        """settings.js saveSortOrder must safely wrap localStorage.setItem."""
        settings_path = pathlib.Path("static/js/settings.js")
        content = settings_path.read_text(encoding="utf-8")

        match = re.search(
            r"function saveSortOrder\(market,\s*order\)\s*\{\s*try\s*\{\s*localStorage\.setItem\(",
            content,
        )
        self.assertIsNotNone(
            match, "localStorage.setItem in saveSortOrder must be protected by try/catch"
        )


class TestChromeExtensionLauncherParity(unittest.TestCase):
    """Test Chrome extension launcher parity with allowed background routes."""

    def test_open_orbit_btn_in_popup_html(self):
        """popup.html must have openOrbitBtn."""
        popup_path = pathlib.Path("chrome_extension/popup.html")
        content = popup_path.read_text(encoding="utf-8")
        self.assertIn('id="openOrbitBtn"', content)
        self.assertIn("観測所 (Orbit)", content)

    def test_open_orbit_btn_bound_in_popup_js(self):
        """popup.js must bind openOrbitBtn to /experimental/orbit."""
        popup_js_path = pathlib.Path("chrome_extension/popup.js")
        content = popup_js_path.read_text(encoding="utf-8")
        self.assertIn(
            '$("openOrbitBtn")?.addEventListener("click", () =>\n'
            '    openAppPage("/experimental/orbit"),\n'
            "  );",
            content,
        )

    def test_experimental_orbit_route_in_background_allowed_routes(self):
        """background.js must permit /experimental/orbit in ALLOWED_ROUTES."""
        bg_path = pathlib.Path("chrome_extension/background.js")
        content = bg_path.read_text(encoding="utf-8")
        self.assertIn('"/experimental/orbit"', content)

    def test_popup_css_has_span_2_rule(self):
        """popup.css must provide .span-2 rule for balanced button layout."""
        css_path = pathlib.Path("chrome_extension/popup.css")
        content = css_path.read_text(encoding="utf-8")
        self.assertIn(".actions.grid-2x2 .span-2", content)
        self.assertIn("grid-column: 1 / -1;", content)


if __name__ == "__main__":
    unittest.main()
