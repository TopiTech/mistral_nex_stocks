# tests/test_code_review_goal_audit_2026_10_fixes.py
"""Regression test suite for 2026 code review audit fixes (R44, R45, R46).

Covers:
- R44: DDGS search thread pool lifecycle management, dynamic recreation, and app_state shutdown
- R45: Screener active chip dismissal WAI-ARIA roving tabindex synchronization
- R46: Initial static WAI-ARIA roving tabindex in server-rendered templates (screener, heatmap, index, orbit)
"""

from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from app_state import app_state
from services.search.ddgs import (
    _get_ddgs_search_pool,
    shutdown_ddgs_pool,
)


class TestDDGSSearchPoolLifecycle:
    """Tests for R44: DDGS search pool lifecycle management."""

    def test_ddgs_search_pool_lifecycle_and_recreation(self):
        """Verify _get_ddgs_search_pool returns live pool, can be shut down, and recreates cleanly."""
        pool1 = _get_ddgs_search_pool()
        assert pool1 is not None
        assert pool1._max_workers == 3

        # Submit a small task to verify pool works
        future = pool1.submit(lambda x: x * 2, 21)
        assert future.result(timeout=2.0) == 42

        # Shut down pool
        shutdown_ddgs_pool(wait=True)

        # After shutdown, _get_ddgs_search_pool should recreate a new live pool
        pool2 = _get_ddgs_search_pool()
        assert pool2 is not None
        assert pool2 is not pool1

        future2 = pool2.submit(lambda x: x + 1, 99)
        assert future2.result(timeout=2.0) == 100

        # Clean teardown
        shutdown_ddgs_pool(wait=False)

    def test_app_state_shutdown_executors_invokes_ddgs_shutdown(self):
        """Verify app_state.shutdown_executors calls shutdown_ddgs_pool."""
        with patch("services.search.ddgs.shutdown_ddgs_pool") as mock_shutdown:
            app_state.shutdown_executors()
            mock_shutdown.assert_called_once_with(wait=False)


class TestScreenerChipTabindexSync:
    """Tests for R45: Screener active chip dismissal WAI-ARIA roving tabindex synchronization."""

    def test_screener_js_chip_dismissal_updates_tabindex(self):
        """Verify screener.js updates tabindex when dismissing market and preset filter chips."""
        screener_js_path = Path("static/js/screener.js")
        assert screener_js_path.exists()
        content = screener_js_path.read_text(encoding="utf-8")

        # In renderActiveChips:
        # Market toggle chip dismissal must update tabindex
        assert 'b.setAttribute("tabindex", isActive ? "0" : "-1");' in content

        # Check specifically inside chipEl click event listener
        chip_click_idx = content.find('chipEl.addEventListener("click"')
        assert chip_click_idx != -1
        chip_handler_slice = content[chip_click_idx : chip_click_idx + 1200]

        assert 'c.key === "market"' in chip_handler_slice
        assert 'b.setAttribute("tabindex", isActive ? "0" : "-1");' in chip_handler_slice
        assert 'c.key === "preset"' in chip_handler_slice


class TestTemplateStaticWaiAriaTabindex:
    """Tests for R46: Initial static WAI-ARIA roving tabindex on server-rendered templates."""

    def test_screener_template_tabindex(self):
        """Verify templates/screener.html has valid roving tabindex attributes."""
        html_path = Path("templates/screener.html")
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

        # Market toggle
        market_group = soup.find(id="screenerMarketToggle")
        assert market_group is not None
        market_btns = market_group.find_all("button")
        assert len(market_btns) == 3
        active_market = [b for b in market_btns if b.get("tabindex") == "0"]
        inactive_market = [b for b in market_btns if b.get("tabindex") == "-1"]
        assert len(active_market) == 1
        assert active_market[0].get("data-market") == "all"
        assert len(inactive_market) == 2

        # Preset toggle
        preset_group = soup.find(id="screenerChangePreset")
        assert preset_group is not None
        preset_btns = preset_group.find_all("button")
        assert len(preset_btns) == 4
        active_preset = [b for b in preset_btns if b.get("tabindex") == "0"]
        inactive_preset = [b for b in preset_btns if b.get("tabindex") == "-1"]
        assert len(active_preset) == 1
        assert active_preset[0].get("data-preset") == "all"
        assert len(inactive_preset) == 3

    def test_heatmap_template_tabindex(self):
        """Verify templates/heatmap.html has valid roving tabindex attributes."""
        html_path = Path("templates/heatmap.html")
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

        # Market toggle
        assert soup.find(id="toggle-us").get("tabindex") == "0"
        assert soup.find(id="toggle-jp").get("tabindex") == "-1"

        # View toggle
        assert soup.find(id="view-2d").get("tabindex") == "0"
        assert soup.find(id="view-3d").get("tabindex") == "-1"

        # Size toggle
        assert soup.find(id="size-market-cap").get("tabindex") == "0"
        assert soup.find(id="size-volume").get("tabindex") == "-1"

        # Cam btn group
        cam_group = soup.find(class_="cam-btn-group")
        assert cam_group is not None
        cam_btns = cam_group.find_all("button")
        assert len(cam_btns) == 3
        active_cam = [b for b in cam_btns if b.get("tabindex") == "0"]
        inactive_cam = [b for b in cam_btns if b.get("tabindex") == "-1"]
        assert len(active_cam) == 1
        assert active_cam[0].get("id") == "cam-reset"
        assert len(inactive_cam) == 2

    def test_index_template_tabindex(self):
        """Verify templates/index.html has valid roving tabindex attributes."""
        html_path = Path("templates/index.html")
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

        # SSE mode selector
        sse_group = soup.find(id="sseModeSelector")
        assert sse_group is not None
        sse_btns = sse_group.find_all("button")
        assert len(sse_btns) == 3
        active_sse = [b for b in sse_btns if b.get("tabindex") == "0"]
        inactive_sse = [b for b in sse_btns if b.get("tabindex") == "-1"]
        assert len(active_sse) == 1
        assert active_sse[0].get("data-mode") == "2"
        assert len(inactive_sse) == 2

        # AI Portfolio preset bar
        ai_pf_group = soup.find(class_="ai-pf-preset-bar")
        assert ai_pf_group is not None
        ai_pf_btns = ai_pf_group.find_all("button")
        assert len(ai_pf_btns) == 4
        active_ai = [b for b in ai_pf_btns if b.get("tabindex") == "0"]
        inactive_ai = [b for b in ai_pf_btns if b.get("tabindex") == "-1"]
        assert len(active_ai) == 1
        assert active_ai[0].get("data-preset") == "tech"
        assert len(inactive_ai) == 3

    def test_orbit_template_tabindex(self):
        """Verify templates/experimental_orbit.html has valid roving tabindex attributes."""
        html_path = Path("templates/experimental_orbit.html")
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

        # Market selector
        assert soup.find(id="market-toggle-us").get("tabindex") == "0"
        assert soup.find(id="market-toggle-jp").get("tabindex") == "-1"
        assert soup.find(id="market-toggle-all").get("tabindex") == "-1"

        # Timeline granularity
        gran_group = soup.find(class_="timeline-granularity-group")
        assert gran_group is not None
        gran_btns = gran_group.find_all("button")
        assert len(gran_btns) == 5
        active_gran = [b for b in gran_btns if b.get("tabindex") == "0"]
        inactive_gran = [b for b in gran_btns if b.get("tabindex") == "-1"]
        assert len(active_gran) == 1
        assert active_gran[0].get("data-granularity") == "3mo"
        assert len(inactive_gran) == 4
