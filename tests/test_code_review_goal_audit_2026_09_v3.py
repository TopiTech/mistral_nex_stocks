# tests/test_code_review_goal_audit_2026_09_v3.py
"""Regression tests for code review audit fixes v3 (September 2026).

Covers:
- R16: Session manager rate-limited backoff clamping against YFINANCE_BACKOFF_MAX.
- R17: Native host default log directory using APP_DATA_DIR without source tree pollution.
- R18: News service fanout pool shutdown hook and integration into app_state.shutdown_executors().
- R19: Networking is_allowed_trusted_origin helper with backward-compatible alias.
- R20: Stocks views strict float parser typing and origin check invocation.
- R21: AI portfolio preset pills keyboard navigation and aria-pressed synchronization.
"""

from __future__ import annotations

import inspect
import pathlib
import time
import unittest
from unittest.mock import MagicMock, patch

import config_store
import session_manager
import utils.networking
from app_state import app_state
from constants import BACKEND_PORT
from market_state import YFINANCE_BACKOFF_MAX
from services.news_service import shutdown_news_fanout_pool


class TestSessionManagerDurationClamping(unittest.TestCase):
    """Test R16: session_manager rate-limited duration clamping."""

    def setUp(self) -> None:
        session_manager.yf_session_manager.close_all()

    def tearDown(self) -> None:
        session_manager.yf_session_manager.close_all()

    def test_handle_block_clamps_duration_to_yfinance_backoff_max(self) -> None:
        """_handle_block must clamp duration to YFINANCE_BACKOFF_MAX even with excessive retry-after."""
        mgr = session_manager.YFinanceSessionManager()
        start = time.time()
        # Mock resp with an enormous Retry-After header
        resp = MagicMock()
        resp.headers = {"Retry-After": "86400"}
        mgr._handle_block(status_code=429, resp=resp)
        until = mgr.get_rate_limit_until("yfinance")
        self.assertTrue(mgr.is_rate_limited("yfinance"))
        self.assertLessEqual(until - start, YFINANCE_BACKOFF_MAX + 2)
        self.assertGreater(until - start, 0)

    def test_mark_rate_limited_clamps_negative_or_zero_duration(self) -> None:
        """mark_rate_limited must clamp non-positive durations to at least 1 second."""
        mgr = session_manager.YFinanceSessionManager()
        start = time.time()
        mgr.mark_rate_limited("yfinance", duration=-10)
        until = mgr.get_rate_limit_until("yfinance")
        self.assertLessEqual(until - start, YFINANCE_BACKOFF_MAX + 2)
        self.assertGreaterEqual(until - start, 0)

    def test_module_singleton_mark_rate_limited_clamps_duration(self) -> None:
        """yf_session_manager.mark_rate_limited clamps to YFINANCE_BACKOFF_MAX."""
        start = time.time()
        session_manager.yf_session_manager.mark_rate_limited("yfinance", duration=999999)
        until = session_manager.yf_session_manager.get_rate_limit_until("yfinance")
        self.assertTrue(session_manager.yf_session_manager.is_rate_limited("yfinance"))
        self.assertLessEqual(until - start, YFINANCE_BACKOFF_MAX + 2)
        self.assertGreater(until - start, 0)


class TestNativeHostLogDirectory(unittest.TestCase):
    """Test R17: native_host log directory default and bootstrap ordering."""

    def test_native_host_default_log_dir_matches_app_data_dir(self) -> None:
        """When MNS_NATIVE_HOST_LOG_DIR is unset, native host must default to config_store.APP_DATA_DIR."""
        import native_host.native_host as nh

        self.assertEqual(nh._default_log_dir, config_store.APP_DATA_DIR)
        self.assertNotEqual(nh._default_log_dir, pathlib.Path(nh.__file__).parent)

    def test_native_host_source_initializes_root_before_logging(self) -> None:
        """Verify native_host.py initializes ROOT and sys.path before logging setup."""
        nh_source = pathlib.Path("native_host/native_host.py").read_text(encoding="utf-8")
        root_idx = nh_source.find("ROOT = Path(__file__).resolve().parents[1]")
        sys_path_idx = nh_source.find("sys.path.insert(0, str(ROOT))")
        log_setup_idx = nh_source.find("logger = logging.getLogger(__name__)")

        self.assertNotEqual(root_idx, -1)
        self.assertNotEqual(sys_path_idx, -1)
        self.assertNotEqual(log_setup_idx, -1)
        self.assertLess(root_idx, log_setup_idx)
        self.assertLess(sys_path_idx, log_setup_idx)


class TestNewsServiceShutdown(unittest.TestCase):
    """Test R18: news service fanout thread pool shutdown."""

    def test_shutdown_news_fanout_pool_executes_cleanly(self) -> None:
        """shutdown_news_fanout_pool executes without raising exceptions."""
        shutdown_news_fanout_pool(wait=False)

    def test_app_state_shutdown_executors_invokes_news_fanout_pool_shutdown(self) -> None:
        """app_state.shutdown_executors() must invoke shutdown_news_fanout_pool."""
        with patch("services.news_service.shutdown_news_fanout_pool") as mock_news_shutdown:
            old_done = getattr(app_state, "_shutdown_executors_done", False)
            try:
                app_state._shutdown_executors_done = False
                app_state.shutdown_executors()
                mock_news_shutdown.assert_called_once_with(wait=False)
            finally:
                app_state._shutdown_executors_done = old_done


class TestTrustedOriginHelper(unittest.TestCase):
    """Test R19: is_allowed_trusted_origin helper and backward compatibility."""

    def test_alias_backward_compatibility(self) -> None:
        """_is_allowed_shutdown_origin must be an alias of is_allowed_trusted_origin."""
        self.assertIs(
            utils.networking._is_allowed_shutdown_origin,
            utils.networking.is_allowed_trusted_origin,
        )

    def test_allowed_loopback_and_extension_origins(self) -> None:
        """Loopback HTTP/HTTPS and configured extension origins are permitted."""
        valid_origins = [
            f"http://127.0.0.1:{BACKEND_PORT}",
            f"http://localhost:{BACKEND_PORT}",
            "http://127.0.0.1:5000",
            "http://localhost:5000",
        ]
        for origin in valid_origins:
            req = MagicMock()
            req.headers = {"Origin": origin}
            self.assertTrue(
                utils.networking.is_allowed_trusted_origin(req),
                f"Origin {origin} should be allowed",
            )

    def test_untrusted_remote_origin_rejected(self) -> None:
        """Remote untrusted origins or empty origins must be rejected."""
        untrusted_origins = [
            "",
            "https://evil.com",
            "http://malicious.site:5000",
            "http://192.168.1.10:5000",
        ]
        for origin in untrusted_origins:
            req = MagicMock()
            req.headers = {"Origin": origin}
            self.assertFalse(
                utils.networking.is_allowed_trusted_origin(req),
                f"Origin {origin!r} should be rejected",
            )


class TestStocksViewsTypingAndOriginValidation(unittest.TestCase):
    """Test R20: Stocks views strict float typing and origin check invocation."""

    def test_stocks_views_uses_is_allowed_trusted_origin(self) -> None:
        """api_add_stock_ext in routes/stocks/views.py must call is_allowed_trusted_origin."""
        views_src = pathlib.Path("routes/stocks/views.py").read_text(encoding="utf-8")
        self.assertIn("utils.networking.is_allowed_trusted_origin(request)", views_src)

    def test_parse_strict_float_signature_annotated(self) -> None:
        """_parse_strict_float inside api_screener view must have typed annotations."""
        import routes.stocks.views as views_mod

        src = inspect.getsource(views_mod.api_screener)
        self.assertIn(
            "def _parse_strict_float(raw: Any, field_name: str) -> float | None | tuple[Response, int]:",
            src,
        )


class TestAiPortfolioPresetBarAccessibility(unittest.TestCase):
    """Test R21: AI portfolio preset bar keyboard navigation and aria-pressed attributes."""

    def test_ai_portfolio_js_preset_bar_has_keyboard_navigation(self) -> None:
        """ai_portfolio.js setupPresetBar must handle Arrow/Home/End keys."""
        ai_pf_src = pathlib.Path("static/js/ai_portfolio.js").read_text(encoding="utf-8")

        self.assertIn('e.key === "ArrowRight" || e.key === "ArrowDown"', ai_pf_src)
        self.assertIn('e.key === "ArrowLeft" || e.key === "ArrowUp"', ai_pf_src)
        self.assertIn('e.key === "Home"', ai_pf_src)
        self.assertIn('e.key === "End"', ai_pf_src)

    def test_ai_portfolio_js_syncs_aria_pressed_in_programmatic_switches(self) -> None:
        """viewSavedAiPortfolio and deleteSavedAiPortfolio must synchronize aria-pressed."""
        ai_pf_src = pathlib.Path("static/js/ai_portfolio.js").read_text(encoding="utf-8")

        self.assertIn('pill.setAttribute("aria-pressed", String(isActive));', ai_pf_src)


if __name__ == "__main__":
    unittest.main()

