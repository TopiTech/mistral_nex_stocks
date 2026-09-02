"""tests/test_sse_mode_ui_consistency.py - Static consistency tests for the 3-stage SSE mode UI.

Verifies the code-review findings R1/R2/R3/R6 are reflected in the shipped
frontend artifacts (templates + JS) without requiring a JS runtime:

- R1: complementary/simulated prices are disclosed in the UI and the mode-2
      label no longer claims the SSE stream itself is TradingView realtime data.
- R2: ``state.isStreaming`` is derived from the SSE mode (``mns_sse_mode``),
      not the legacy ``isStreamingEnabled`` key, so SSE error fallback polling
      still triggers after an upgrade from the old toggle.
- R3: the removed ``streamToggleBtn`` / ``setStreamingIndicatorText`` dead code
      is gone from api.js.
- R6: the ticker tape container is activated only after widget data arrives,
      so no empty 48px band is shown while the stream is connecting.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SseModeUiConsistencyTest(unittest.TestCase):
    """Static assertions over templates/index.html and static/js/api.js/state.js."""

    def setUp(self):
        self.index_html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.api_js = (ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
        self.state_js = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")

    # ---- R1: disclosure + accurate label ----
    def test_mode2_label_no_longer_claims_realtime_sse(self):
        self.assertIn("🚀 TV連携SSE", self.index_html)
        self.assertNotIn("🚀 TV実データSSE", self.index_html)

    def test_market_data_note_discloses_simulated_prices(self):
        self.assertIn('id="marketDataNote"', self.index_html)
        self.assertIn('class="market-data-note"', self.index_html)
        self.assertIn("リアルタイム配信中", self.index_html)

    def test_market_data_note_updated_per_mode(self):
        selector_region = self.api_js[
            self.api_js.index("function updateSseModeSelectorUI") : self.api_js.index(
                "function setSseMode"
            )
        ]
        self.assertIn('document.getElementById("marketDataNote")', selector_region)
        self.assertIn("mode === 2", selector_region)
        self.assertIn("60秒ポーリング", selector_region)
        self.assertIn("TradingView WS", selector_region)

    def test_mode2_toast_updated(self):
        self.assertNotIn("TradingView実データSSE（超高速配信）", self.api_js)

    # ---- R2: streaming state derived from SSE mode ----
    def test_connect_sse_syncs_is_streaming_with_mode(self):
        self.assertIn("state.isStreaming = currentMode !== 0;", self.api_js)

    def test_state_initializes_streaming_from_sse_mode_key(self):
        self.assertIn('localStorage.getItem("mns_sse_mode")', self.state_js)
        self.assertNotIn(
            'localStorage.getItem("isStreamingEnabled") !== "false"',
            self.state_js,
        )

    def test_state_setter_no_longer_writes_legacy_key(self):
        # The isStreaming setter must not resurrect the legacy key: the SSE mode
        # is the single source of truth for streaming state (R2).
        self.assertNotIn(
            'localStorage.setItem("isStreamingEnabled"',
            self.state_js,
        )

    # ---- R3: dead code removed ----
    def test_streaming_indicator_dead_code_removed(self):
        self.assertNotIn("setStreamingIndicatorText", self.api_js)
        self.assertNotIn("streamToggleBtn", self.api_js)

    # ---- R6: ticker tape activation deferred to data arrival ----
    def test_ticker_tape_activated_after_widget_init(self):
        selector_ui = self.api_js[
            self.api_js.index("function updateSseModeSelectorUI") : self.api_js.index(
                "function setSseMode"
            )
        ]
        self.assertNotIn('classList.add("active")', selector_ui)

        process_region = self.api_js[
            self.api_js.index("const processSseData") : self.api_js.index("const handleSseError")
        ]
        self.assertIn("initTickerTape", process_region)
        self.assertIn('tapeContainer.classList.add("active")', process_region)

    def test_indices_bar_hidden_in_mode2(self):
        selector_ui = self.api_js[
            self.api_js.index("function updateSseModeSelectorUI") : self.api_js.index(
                "function setSseMode"
            )
        ]
        self.assertIn('document.querySelector(".indices-bar-wrapper")', selector_ui)
        self.assertIn('indicesWrapper.style.display = mode === 2 ? "none" : ""', selector_ui)

    def test_tradingview_defaults_to_dark_theme(self):
        tv_manager_js = (ROOT / "static" / "js" / "tradingview_manager.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('!document.body.classList.contains("light-mode")', tv_manager_js)

    def test_sr_only_defined_in_stylesheets(self):
        colors_css = (ROOT / "static" / "css" / "colors.css").read_text(encoding="utf-8")
        index_css = (ROOT / "static" / "css" / "index.css").read_text(encoding="utf-8")
        self.assertIn(".sr-only", colors_css)
        self.assertIn("clip: rect(0, 0, 0, 0)", colors_css)
        self.assertIn(".sr-only", index_css)
        self.assertIn("clip: rect(0, 0, 0, 0)", index_css)


if __name__ == "__main__":
    unittest.main()

