# tests/test_code_review_goal_audit_2026_10_fixes.py
"""Regression test suite for 2026 code review audit fixes (R44, R45, R46, R47, R48).

Covers:
- R44: DDGS search thread pool lifecycle management, dynamic recreation, and app_state shutdown
- R45: Screener active chip dismissal WAI-ARIA roving tabindex synchronization
- R46: Initial static WAI-ARIA roving tabindex in server-rendered templates (screener, heatmap, index, orbit)
- R47: app_bg stale sync recovery atomic timestamp synchronization and scheduling failure alerts
- R48: AI portfolio error response handling without flask.g shadowing and clean exception mapping
"""

from __future__ import annotations

import logging
import queue
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from bs4 import BeautifulSoup
from flask import Flask, g

import app_bg
import bg.sync.state as sync_state
from app_state import app_state
from bg.sync.control import schedule_sync_all_stocks_now as _real_schedule_sync_all_stocks_now
from routes.stocks.ai_portfolio import _ai_portfolio_error_response
from services.ai_portfolio_service import PortfolioStorageError
from services.ai_service import ProviderAPIError
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
        fake_state = MagicMock()
        fake_state._shutdown_executors_done = False
        fake_state.ai.mistral_clients_lock.acquire.return_value = False

        with (
            patch("services.search.ddgs.shutdown_ddgs_pool") as mock_shutdown,
            patch("services.news_service.shutdown_news_fanout_pool"),
            patch("services.realtime_engine.realtime_market_engine.stop"),
            patch("app_state.yf_session_manager.close_all"),
        ):
            type(app_state).shutdown_executors(fake_state)

        mock_shutdown.assert_called_once_with(wait=False)
        fake_state.execution.shutdown.assert_called_once_with()


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


class TestR47StaleSyncRecoveryAndScheduling:
    def setup_method(self) -> None:
        self._orig_schedule = getattr(app_bg, "schedule_sync_all_stocks_now", None)
        app_bg.schedule_sync_all_stocks_now = _real_schedule_sync_all_stocks_now
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
            app_state.market.sync_pending = False
            app_state.market.sync_forced = False
        app_bg._sync_start_time = 0.0
        sync_state._sync_start_time = 0.0

    def teardown_method(self) -> None:
        if self._orig_schedule is not None:
            app_bg.schedule_sync_all_stocks_now = self._orig_schedule
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
            app_state.market.sync_pending = False
            app_state.market.sync_forced = False
        app_bg._sync_start_time = 0.0
        sync_state._sync_start_time = 0.0

    def test_recover_stale_sync_state_updates_both_mirrors_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Stale recovery must reset app_bg._sync_start_time AND sync_state._sync_start_time,
        reset is_syncing, and emit a critical alert log."""
        stale_time = time.time() - (app_bg.SYNC_STALE_TIMEOUT_SEC + 30.0)
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        app_bg._sync_start_time = stale_time
        sync_state._sync_start_time = stale_time

        with caplog.at_level(logging.CRITICAL):
            recovered = app_bg._recover_stale_sync_state_if_needed()

        assert recovered is True
        with app_state.market.is_syncing_lock:
            assert app_state.market.is_syncing is False
        assert app_bg._sync_start_time == 0.0
        assert sync_state._sync_start_time == 0.0
        assert any("Sync is stale" in record.message for record in caplog.records)

    def test_recover_stale_sync_state_noop_when_not_stale(self) -> None:
        """Active sync that has not exceeded timeout should not be cancelled."""
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        recent_time = time.time() - 5.0
        app_bg._sync_start_time = recent_time
        sync_state._sync_start_time = recent_time

        recovered = app_bg._recover_stale_sync_state_if_needed()
        assert recovered is False
        with app_state.market.is_syncing_lock:
            assert app_state.market.is_syncing is True

    def test_schedule_sync_all_stocks_now_queue_full_logging(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When executor queue is full, schedule_sync_all_stocks_now resets scheduled flag
        and emits warning log."""
        mock_executor = MagicMock()
        mock_executor.submit.side_effect = queue.Full("Queue limit reached")

        with patch.object(app_state.execution, "sync_refresh_executor", mock_executor):
            with caplog.at_level(logging.WARNING):
                res = app_bg.schedule_sync_all_stocks_now()

        assert res is False
        with app_state.market.sync_schedule_lock:
            assert app_state.market.sync_scheduled is False
        assert any("Failed to schedule stock sync (queue full)" in r.getMessage() for r in caplog.records)

    def test_schedule_sync_all_stocks_now_runtime_error_logging(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When executor raises RuntimeError, schedule_sync_all_stocks_now resets scheduled flag
        and emits warning log."""
        mock_executor = MagicMock()
        mock_executor.submit.side_effect = RuntimeError("Executor shutdown")

        with patch.object(app_state.execution, "sync_refresh_executor", mock_executor):
            with caplog.at_level(logging.WARNING):
                res = app_bg.schedule_sync_all_stocks_now()

        assert res is False
        with app_state.market.sync_schedule_lock:
            assert app_state.market.sync_scheduled is False
        assert any("Failed to schedule stock sync: Executor shutdown" in r.getMessage() for r in caplog.records)


class TestR48AIPortfolioErrorResponse:
    def test_error_response_with_explicit_flask_g(self) -> None:
        app = Flask(__name__)
        with app.app_context():
            mock_g = MagicMock()
            mock_g.request_id = "custom-req-123"

            prov_err = ProviderAPIError("Quota exceeded", status_code=429, details={"retry_after": 30})
            resp, status = _ai_portfolio_error_response(prov_err, flask_g=mock_g)
            assert status == 429
            data = resp.get_json()
            assert data["ok"] is False
            assert data["code"] == "1203"
            assert data["details"]["reason"] == "Quota exceeded"

    def test_error_response_with_positional_mock_g(self) -> None:
        app = Flask(__name__)
        with app.app_context():
            mock_g = MagicMock()
            mock_g.request_id = "pos-req-456"

            timeout_err = requests.Timeout("Gateway timed out")
            resp, status = _ai_portfolio_error_response(timeout_err, mock_g)
            assert status == 504
            data = resp.get_json()
            assert data["ok"] is False
            assert data["code"] == "1203"
            assert "タイムアウト" in data["details"]["reason"]

    def test_error_response_fallback_to_context_g(self) -> None:
        app = Flask(__name__)
        with app.app_context():
            g.request_id = "context-g-789"

            storage_err = PortfolioStorageError("Disk full", status_code=503)
            resp, status = _ai_portfolio_error_response(storage_err)
            assert status == 503
            data = resp.get_json()
            assert data["ok"] is False
            assert data["code"] == "1203"
            assert "完了しませんでした" in data["details"]["reason"]

    def test_error_response_unexpected_error(self) -> None:
        app = Flask(__name__)
        with app.app_context():
            unexpected_err = ValueError("Invalid matrix computation")
            resp, status = _ai_portfolio_error_response(unexpected_err)
            assert status == 500
            data = resp.get_json()
            assert data["ok"] is False
            assert data["code"] == "1304"
