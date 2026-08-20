import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from app_state import app_state
from native_host.native_host import _is_caller_authorized_browser
from services.ai_service import _extract_mistral_wait_seconds
from services.realtime_engine import RealtimeMarketEngine, YahooJPRealtimeScraper
from services.stock_service import _history_with_timeout
from trend_sources import collect_symbol_research_items


class TestHeadReviewGoalFixes20260820:
    def test_r1_stock_service_semaphore_not_released_on_acquire_timeout(self):
        """R1: Verify yfinance_history_semaphore is not released when acquire times out."""
        sem = threading.Semaphore(1)
        sem.acquire()  # Drain the single slot so next acquire times out

        with patch.object(app_state.market, "yfinance_history_semaphore", sem):
            with patch("services.stock_service.HISTORY_SEMAPHORE_TIMEOUT", 0.01):
                with pytest.raises(TimeoutError, match="Timed out waiting for history semaphore"):
                    _history_with_timeout("1d", "auto", "AAPL", market="us")

        # Verify the semaphore was NOT released: acquiring with blocking=False must fail
        assert sem.acquire(blocking=False) is False
        sem.release()  # Clean up the slot we acquired initially

    def test_r2_realtime_engine_pts_alias_current_check(self):
        """R2: Verify YahooJPRealtimeScraper._is_symbol_current matches both bare and .T aliases."""
        scraper = YahooJPRealtimeScraper()
        scraper.symbols.add("7203.T")

        # Should match both exact and bare code alias
        assert scraper._is_symbol_current("7203.T") is True
        assert scraper._is_symbol_current("7203") is True
        assert scraper._is_symbol_current("6758") is False

        scraper.symbols.clear()
        scraper.symbols.add("6758")
        assert scraper._is_symbol_current("6758") is True
        assert scraper._is_symbol_current("6758.T") is True

    def test_r2_realtime_engine_pts_store_alias_purge_on_unregister(self):
        """R2: Verify RealtimeMarketEngine.unregister_symbol purges all PTS aliases from stores and queues."""
        engine = RealtimeMarketEngine()
        with engine.store_lock:
            engine.pts_store["7203.T"] = {"symbol": "7203.T", "price": 2500.0}
            engine.previous_pts_store["7203.T"] = {"symbol": "7203.T", "price": 2490.0}
            engine._dirty_pts_symbols.add("7203.T")
            engine._client_pts_states["client1"] = {"7203.T": {"symbol": "7203.T", "price": 2500.0}}
            engine._client_pts_pending["client1"] = {"7203.T"}

        # Unregister via bare code "7203"
        engine.unregister_symbol("7203", "jp")

        with engine.store_lock:
            assert "7203.T" not in engine.pts_store
            assert "7203" not in engine.pts_store
            assert "7203.T" not in engine.previous_pts_store
            assert "7203.T" not in engine._dirty_pts_symbols
            assert "7203.T" not in engine._client_pts_states.get("client1", {})
            assert "7203.T" not in engine._client_pts_pending.get("client1", set())

    def test_r3_ai_service_extract_mistral_wait_seconds_relative_and_epoch(self):
        """R3: Verify relative seconds in X-RateLimit-Reset headers are parsed correctly."""
        # Relative duration (e.g. 30 seconds)
        headers_rel = {"X-RateLimit-Reset": "30"}
        wait_rel = _extract_mistral_wait_seconds(headers_rel)
        assert 29.0 <= wait_rel <= 30.0

        # Epoch timestamp in the future (e.g. now + 60s)
        future_epoch = time.time() + 60.0
        headers_epoch = {"x-ratelimit-reset": str(future_epoch)}
        wait_epoch = _extract_mistral_wait_seconds(headers_epoch)
        assert 58.0 <= wait_epoch <= 61.0

        # Retry-After string
        headers_retry = {"Retry-After": "15"}
        assert _extract_mistral_wait_seconds(headers_retry) == 15.0

    def test_r4_api_health_options_preflight(self):
        """R4: Verify OPTIONS /api/health succeeds with 200 without admin token in remote mode."""
        from routes.api_system import api_system_bp

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(api_system_bp)

        with app.test_client() as client:
            with patch("routes.api_system._require_admin_token_if_remote") as mock_admin:
                mock_admin.return_value = (False, ("Forbidden", 403))
                # OPTIONS should bypass admin token check and return 200
                res = client.options("/api/health")
                assert res.status_code == 200
                data = res.get_json()
                assert data.get("ok") is True

    def test_r5_chrome_extension_manifest_host_permissions_validity(self):
        """R5: Verify manifest.json host_permissions use valid match patterns without port wildcards."""
        manifest_path = Path("chrome_extension/manifest.json")
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        host_permissions = manifest.get("host_permissions", [])

        for pattern in host_permissions:
            assert ":*" not in pattern, f"Pattern contains invalid port wildcard: {pattern}"
            assert pattern in ("http://127.0.0.1/*", "http://localhost/*")

    def test_r6_chrome_extension_background_alarm_check(self):
        """R6: Verify background.js checks chrome.alarms.get before calling chrome.alarms.create."""
        bg_js = Path("chrome_extension/background.js").read_text(encoding="utf-8")
        assert 'chrome.alarms.get("badgeUpdate"' in bg_js
        assert 'chrome.alarms.onAlarm.addListener' in bg_js

    def test_r7_native_host_linux_browser_ancestry(self):
        """R7: Verify Linux Chrome and Chromium binary names are accepted as authorized callers."""
        assert _is_caller_authorized_browser(["google-chrome"]) is True
        assert _is_caller_authorized_browser(["google-chrome-stable"]) is True
        assert _is_caller_authorized_browser(["chromium"]) is True
        assert _is_caller_authorized_browser(["chromium-browser"]) is True
        assert _is_caller_authorized_browser(["brave"]) is True
        assert _is_caller_authorized_browser(["unknown_process"]) is False

    def test_r8_start_backend_pid_source_unlinking(self):
        """R8: Verify start_backend unlinks pid_source on occupied unhealthy port."""
        from native_host.start_backend import start

        fake_pid_source = MagicMock()
        with patch("native_host.start_backend.PID_FILE") as mock_pid_file:
            mock_pid_file.exists.return_value = False
            with patch("native_host.start_backend._LEGACY_PID_FILE", fake_pid_source):
                fake_pid_source.read_text.return_value = "12345"
                with patch("native_host.start_backend.is_running", return_value=True):
                    with patch("native_host.start_backend.is_backend_healthy_once", return_value=False):
                        with patch("native_host.start_backend.is_port_in_use", return_value=True):
                            res = start()
                            assert res["ok"] is False
                            fake_pid_source.unlink.assert_called_with(missing_ok=True)

    def test_r9_trend_sources_collect_symbol_research_items_shutdown_resilience(self):
        """R9: Verify collect_symbol_research_items handles executor submission failures gracefully."""
        with patch("trend_sources._EXECUTOR.submit", side_effect=RuntimeError("cannot schedule new futures after shutdown")):
            items = collect_symbol_research_items("AAPL", "Apple Inc.", "us")
            assert isinstance(items, list)
            assert len(items) == 0

    def test_r10_realtime_engine_duplicate_registration_is_idempotent(self):
        """Repeated response-side registration must not invalidate or requeue a symbol."""

        class RecordingExecutor:
            def __init__(self):
                self.submitted = []

            def submit(self, fn):
                self.submitted.append(fn)
                return object()

        engine = RealtimeMarketEngine()
        executor = RecordingExecutor()
        engine._bg_executor = executor

        engine.register_symbols([], ["7203.T"])
        registration_token = engine._registration_tokens[("jp", "7203.T")]
        scraper_token = engine.yahoojp_scraper._symbol_tokens["7203.T"]

        engine.register_symbol("7203.T", "jp")

        assert len(executor.submitted) == 1
        assert engine._registration_tokens[("jp", "7203.T")] is registration_token
        assert engine.yahoojp_scraper._symbol_tokens["7203.T"] is scraper_token
