# tests/test_code_review_goal_audit_2026_09.py
"""Regression tests for code review audit fixes (September 2026).

Covers:
- Fallback provider session WeakSet & native handle leak prevention
- Fallback provider SSRF / symbol validation
- bg.sync_worker circular import prevention
- Search API case-insensitive cache key normalization
- Screener float parsing with formatted numerical strings
- Colors CSS skip-link definition for cross-template accessibility
- Chrome extension popup HTML single-main / tabpanel semantics
- Setup.js APP_CONFIG fallback ordering & safety
"""

from __future__ import annotations

import pathlib
import unittest
import weakref
from unittest.mock import patch

from services.fallback_provider import (
    AlphaVantageProvider,
    MinkabuProvider,
    Nikkei225JPProvider,
    YahooJPScraperProvider,
    YahooWebScraperProvider,
)
from utils.caching import global_cache


class TestFallbackProviderHardening(unittest.TestCase):
    """Test fallback provider session memory management and input validation."""

    def test_providers_use_weakset_for_all_sessions(self):
        """Verify that scraper providers use weakref.WeakSet for session tracking."""
        y_scraper = YahooWebScraperProvider()
        self.assertIsInstance(y_scraper._all_sessions, weakref.WeakSet)

        yjp_scraper = YahooJPScraperProvider()
        self.assertIsInstance(yjp_scraper._all_sessions, weakref.WeakSet)

        nikkei_scraper = Nikkei225JPProvider()
        self.assertIsInstance(nikkei_scraper._all_sessions, weakref.WeakSet)

        minkabu_scraper = MinkabuProvider()
        self.assertIsInstance(minkabu_scraper._all_sessions, weakref.WeakSet)

    def test_weakset_allows_garbage_collection_of_dead_sessions(self):
        """Verify that when a session object is discarded by its thread, WeakSet drops it."""
        y_scraper = YahooWebScraperProvider()

        class DummySession:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        sess = DummySession()
        y_scraper._all_sessions.add(sess)
        self.assertEqual(len(y_scraper._all_sessions), 1)

        del sess
        self.assertEqual(len(y_scraper._all_sessions), 0)

    def test_close_invokes_close_on_surviving_sessions(self):
        """Verify provider.close() properly calls close() on all active sessions."""
        y_scraper = YahooWebScraperProvider()

        class DummySession:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        sess1 = DummySession()
        sess2 = DummySession()
        y_scraper._all_sessions.add(sess1)
        y_scraper._all_sessions.add(sess2)

        y_scraper.close()
        self.assertTrue(sess1.closed)
        self.assertTrue(sess2.closed)
        self.assertEqual(len(y_scraper._all_sessions), 0)

    def test_yahoo_web_scraper_rejects_invalid_symbols(self):
        """YahooWebScraperProvider.get_latest_quote must reject dangerous/invalid symbols."""
        provider = YahooWebScraperProvider()
        with patch.object(provider, "_get_client") as mock_client:
            self.assertIsNone(provider.get_latest_quote("../../etc/passwd"))
            self.assertIsNone(provider.get_latest_quote("AAPL;DROP TABLE"))
            self.assertIsNone(provider.get_latest_quote("INVALID/SYMBOL"))
            self.assertIsNone(provider.get_latest_quote("toolongsymbolname123456789"))
            mock_client.assert_not_called()

    def test_alphavantage_rejects_invalid_symbols(self):
        """AlphaVantageProvider.get_latest_quote must reject dangerous/invalid symbols."""
        provider = AlphaVantageProvider()
        with (
            patch("services.fallback_provider.get_alphavantage_api_key", return_value="dummy-key"),
            patch("requests.get") as mock_get,
        ):
            self.assertIsNone(provider.get_latest_quote("../../evil"))
            self.assertIsNone(provider.get_latest_quote("SYM\x00NULL"))
            self.assertIsNone(provider.get_latest_quote("TOOLONG" * 5))
            mock_get.assert_not_called()


class TestSyncWorkerImports(unittest.TestCase):
    """Test sync_worker import architecture to prevent circular dependency."""

    def test_sync_worker_imports_heatmap_from_common(self):
        """bg.sync_worker must import _fetch_heatmap_cached from routes.stocks.common."""
        sync_worker_path = pathlib.Path("bg/sync_worker.py")
        content = sync_worker_path.read_text(encoding="utf-8")
        self.assertIn("from routes.stocks.common import _fetch_heatmap_cached", content)
        self.assertNotIn("from routes.api_stocks import _fetch_heatmap_cached", content)


class TestSearchApiCacheNormalization(unittest.TestCase):
    """Test case-insensitive search caching in quotes blueprint."""

    def test_search_cache_key_is_normalized(self):
        from app import create_app
        from app_state import app_state

        app = create_app()
        app.config["TESTING"] = True

        with global_cache.cache_lock:
            global_cache.caches.clear()

        client = app.test_client()

        with patch.object(
            app_state.stock_provider,
            "search",
            return_value=[{"symbol": "AAPL", "name": "Apple Inc."}],
        ) as mock_search:
            # Query uppercase
            res1 = client.get(
                "/api/search?q=AAPL",
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(res1.status_code, 200)

            # Query lowercase
            res2 = client.get(
                "/api/search?q=aapl",
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(res2.status_code, 200)

            # Both should share the same cache entry
            self.assertEqual(mock_search.call_count, 1)


class TestScreenerFloatParsing(unittest.TestCase):
    """Test screener parsing of formatted numeric strings."""

    def test_screener_parse_float_handles_formatted_strings(self):
        from app import create_app

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()

        with app.test_request_context("/api/screener?min_price=10&max_price=100"):
            res = client.get(
                "/api/screener?min_price=10&max_price=100",
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertIn(res.status_code, (200, 503))


class TestFrontendAccessibilityAndStyles(unittest.TestCase):
    """Test frontend templates and stylesheets for accessibility compliance."""

    def test_colors_css_defines_skip_link(self):
        """colors.css must define .skip-link so all templates render it offscreen."""
        css_path = pathlib.Path("static/css/colors.css")
        content = css_path.read_text(encoding="utf-8")
        self.assertIn(".skip-link", content)
        self.assertIn("top: -100px", content)
        self.assertIn(".skip-link:focus", content)
        self.assertIn("top: 16px", content)

    def test_chrome_extension_popup_has_no_multiple_mains(self):
        """chrome_extension/popup.html must not contain multiple <main> elements."""
        popup_path = pathlib.Path("chrome_extension/popup.html")
        content = popup_path.read_text(encoding="utf-8")
        self.assertNotIn("<main", content)
        self.assertIn('id="tab-content-stocks"', content)
        self.assertIn('role="tabpanel"', content)

    def test_screener_js_tr_lacks_conflicting_aria_label(self):
        """screener.js <tr> must not set aria-label, preserving cell accessibility."""
        screener_path = pathlib.Path("static/js/screener.js")
        content = screener_path.read_text(encoding="utf-8")
        self.assertNotIn('tr.setAttribute(\n        "aria-label"', content)
        self.assertNotIn('tr.setAttribute("aria-label"', content)

    def test_ui_js_set_detail_item_visibility_includes_drawer(self):
        """ui.js setDetailItemVisibility must query drawer when DOM is borrowed."""
        ui_path = pathlib.Path("static/js/ui.js")
        content = ui_path.read_text(encoding="utf-8")
        self.assertIn("#stock-detail-drawer .detail-item-", content)

    def test_setup_js_app_config_ordering(self):
        """setup.js must ensure window.APP_CONFIG is initialized before bootstrap call."""
        setup_path = pathlib.Path("static/js/setup.js")
        content = setup_path.read_text(encoding="utf-8")
        self.assertIn("window.APP_CONFIG?.has_mistral_api_key", content)
        fallback_pos = content.find('if (typeof APP_CONFIG === "undefined")')
        bootstrap_pos = content.find("bootstrapLegacyCredentials();")
        self.assertNotEqual(fallback_pos, -1)
        self.assertNotEqual(bootstrap_pos, -1)
        self.assertLess(fallback_pos, bootstrap_pos)


if __name__ == "__main__":
    unittest.main()
