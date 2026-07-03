"""Extra targeted coverage tests to reach 70% threshold."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class LoggingConfigTestCase(unittest.TestCase):
    """Test logging_config.py uncovered lines."""

    @patch("logging_config.logging.getLogger")
    @patch("logging_config.RotatingFileHandler")
    def test_init_logging(self, mock_handler, mock_get_logger):
        """Test init_logging function - should create rotating handlers."""
        from logging_config import init_logging
        mock_app = MagicMock()
        init_logging(mock_app)
        # Verify handlers were created and applied
        self.assertTrue(mock_handler.called)
        self.assertTrue(mock_get_logger.called)

    @patch.dict(os.environ, {"LOG_FORMAT": "text"})
    @patch("logging_config.logging.getLogger")
    @patch("logging_config.RotatingFileHandler")
    def test_init_logging_text_format(self, mock_handler, mock_get_logger):
        """Test init_logging with text log format."""
        from logging_config import init_logging
        mock_app = MagicMock()
        init_logging(mock_app)
        self.assertTrue(mock_handler.called)

    def test_log_level_export(self):
        """Test LOG_LEVEL constant is exported as int."""
        import logging_config
        self.assertIsInstance(logging_config.LOG_LEVEL, int)

    def test_detailed_api_log_paths(self):
        """Test DETAILED_API_LOG_PATHS is exported."""
        import logging_config
        self.assertIsInstance(logging_config.DETAILED_API_LOG_PATHS, set)
        self.assertIn("/api/chat", logging_config.DETAILED_API_LOG_PATHS)




class ConstantsTestCase(unittest.TestCase):
    """Test constants.py uncovered lines (_get_backend_port)."""

    def test_backend_port_default(self):
        """Test backend port default value."""
        import constants as c
        self.assertIsInstance(c.BACKEND_PORT, int)
        self.assertTrue(1 <= c.BACKEND_PORT <= 65535)

    def test_backend_port_from_env(self):
        """Test backend port from env var."""
        with patch.dict(os.environ, {"MNS_BACKEND_PORT": "9090"}):
            import importlib
            import constants
            importlib.reload(constants)
            self.assertEqual(constants.BACKEND_PORT, 9090)

    def test_backend_port_invalid_env_fallback(self):
        """Test invalid backend port falls back to default."""
        with patch.dict(os.environ, {"MNS_BACKEND_PORT": "invalid"}):
            import importlib
            import constants
            importlib.reload(constants)
            self.assertEqual(constants.BACKEND_PORT, 5000)

    def test_backend_port_out_of_range(self):
        """Test out-of-range port falls back to default."""
        with patch.dict(os.environ, {"MNS_BACKEND_PORT": "99999"}):
            import importlib
            import constants
            importlib.reload(constants)
            self.assertEqual(constants.BACKEND_PORT, 5000)

    def test_all_constants_accessible(self):
        """Test all major constants are accessible."""
        import constants as c
        self.assertIsInstance(c.MISTRAL_API_TIMEOUT_SEC, float)
        self.assertIsInstance(c.LANGSEARCH_API_KEY_MIN_LENGTH, int)
        self.assertIsInstance(c.TAVILY_API_KEY_MIN_LENGTH, int)
        self.assertIsInstance(c.YFINANCE_TIMEOUT_BATCH, int)
        self.assertIsInstance(c.CACHE_DURATION, int)
        self.assertIsInstance(c.MAX_JSON_SIZE, int)
        self.assertGreater(len(c.POPULAR_US), 0)
        self.assertGreater(len(c.POPULAR_JP), 0)
        self.assertIsInstance(c.SSE_HEARTBEAT_INTERVAL, int)


class ErrorHandlersTestCase(unittest.TestCase):
    """Test error_handlers.py uncovered lines by triggering actual handlers."""

    def _make_app(self):
        from flask import Flask, abort
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["SERVER_NAME"] = "localhost"
        app.config["WTF_CSRF_ENABLED"] = False

        # Simple routes to trigger specific errors
        @app.route("/trigger-400")
        def trigger_400():
            abort(400)

        @app.route("/trigger-403")
        def trigger_403():
            abort(403)

        @app.route("/trigger-404")
        def trigger_404():
            abort(404)

        @app.route("/trigger-413")
        def trigger_413():
            abort(413)

        @app.route("/trigger-429")
        def trigger_429():
            abort(429)

        @app.route("/trigger-500")
        def trigger_500():
            abort(500)

        return app

    def test_all_error_handlers_return_json(self):
        """Test all error handler endpoints return expected JSON structure."""
        from error_handlers import register_error_handlers
        app = self._make_app()
        register_error_handlers(app)

        with app.test_client() as client:
            # 400 Bad Request
            resp = client.get("/trigger-400")
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data.get("ok", True))
            self.assertIn("Bad Request", data.get("error", ""))

            # 403 Forbidden
            resp = client.get("/trigger-403")
            self.assertEqual(resp.status_code, 403)
            data = resp.get_json()
            self.assertFalse(data.get("ok", True))
            self.assertIn("Forbidden", data.get("error", ""))

            # 404 Not Found
            resp = client.get("/trigger-404")
            self.assertEqual(resp.status_code, 404)
            data = resp.get_json()
            self.assertFalse(data.get("ok", True))
            self.assertIn("Not Found", data.get("error", ""))

            # 413 Payload Too Large
            resp = client.get("/trigger-413")
            self.assertEqual(resp.status_code, 413)
            data = resp.get_json()
            self.assertFalse(data.get("ok", True))
            self.assertIn("Payload Too Large", data.get("error", ""))

            # 429 Too Many Requests
            resp = client.get("/trigger-429")
            self.assertEqual(resp.status_code, 429)
            data = resp.get_json()
            self.assertFalse(data.get("ok", True))
            self.assertIn("Too Many Requests", data.get("error", ""))

            # 500 Internal Server Error
            resp = client.get("/trigger-500")
            self.assertEqual(resp.status_code, 500)
            data = resp.get_json()
            self.assertFalse(data.get("ok", True))
            self.assertIn("Internal Server Error", data.get("error", ""))


class RouteHelpersExtraTestCase(unittest.TestCase):
    """Test route_helpers.py uncovered functions."""

    def test_rate_limit_env_name(self):
        """Test _rate_limit_env_name with correct two args."""
        from route_helpers import _rate_limit_env_name
        result = _rate_limit_env_name("test_endpoint", "MAX")
        self.assertEqual(result, "MNS_RATE_LIMIT_TEST_ENDPOINT_MAX")

        result2 = _rate_limit_env_name("chat-api", "WINDOW")
        self.assertEqual(result2, "MNS_RATE_LIMIT_CHAT_API_WINDOW")

    def test_rate_limit_env_name_empty_endpoint(self):
        """Test _rate_limit_env_name with empty endpoint (falls back to default)."""
        from route_helpers import _rate_limit_env_name
        result = _rate_limit_env_name("", "MAX")
        # Empty string is falsy, so "default" is used
        self.assertEqual(result, "MNS_RATE_LIMIT_DEFAULT_MAX")

    def test_rate_limit_env_name_none_endpoint(self):
        """Test _rate_limit_env_name with None endpoint (falls back to default)."""
        from route_helpers import _rate_limit_env_name
        result = _rate_limit_env_name(None, "MAX")
        self.assertEqual(result, "MNS_RATE_LIMIT_DEFAULT_MAX")

    def test_as_text(self):
        """Test _as_text helper."""
        from route_helpers import _as_text
        self.assertEqual(_as_text("hello"), "hello")
        self.assertEqual(_as_text(None), "")
        self.assertEqual(_as_text(123), "123")
        self.assertEqual(_as_text(0), "0")

    def test_seconds_until(self):
        """Test _seconds_until helper."""
        import time
        from route_helpers import _seconds_until
        # Past timestamp should return 0
        self.assertEqual(_seconds_until(0), 0.0)
        # Future timestamp should be positive
        future = time.time() + 60
        result = _seconds_until(future)
        self.assertGreater(result, 0)
        self.assertLessEqual(result, 60.01)

    def test_extract_text_from_mistral_content_string(self):
        """Test _extract_text_from_mistral_content with string input."""
        from route_helpers import _extract_text_from_mistral_content
        self.assertEqual(_extract_text_from_mistral_content("hello"), "hello")
        self.assertEqual(_extract_text_from_mistral_content("  spaced  "), "spaced")
        self.assertEqual(_extract_text_from_mistral_content(""), "")

    def test_extract_text_from_mistral_content_list(self):
        """Test _extract_text_from_mistral_content with list of content dicts."""
        from route_helpers import _extract_text_from_mistral_content
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"},
        ]
        result = _extract_text_from_mistral_content(content)
        self.assertIn("Hello", result)
        self.assertIn("World", result)

    def test_extract_text_from_mistral_content_empty(self):
        """Test _extract_text_from_mistral_content with empty inputs."""
        from route_helpers import _extract_text_from_mistral_content
        self.assertEqual(_extract_text_from_mistral_content([]), "")
        self.assertEqual(_extract_text_from_mistral_content(None), "")

    @patch("route_helpers.clear_cache_prefix")
    def test_invalidate_stock_caches(self, mock_clear):
        """Test invalidate_stock_caches calls clear_cache_prefix."""
        from route_helpers import invalidate_stock_caches
        invalidate_stock_caches("AAPL")
        self.assertEqual(mock_clear.call_count, 3)

    def test_stock_display_name(self):
        """Test _stock_display_name with various inputs."""
        from route_helpers import _stock_display_name
        # Unknown symbol should return itself
        name = _stock_display_name("ZZZZ", "us")
        self.assertEqual(name, "ZZZZ")

    def test_cleanup_history_circuit_state(self):
        """Test cleanup_history_circuit_state with empty state."""
        from route_helpers import cleanup_history_circuit_state
        # Should not raise
        cleanup_history_circuit_state(now_ts=1000)
        cleanup_history_circuit_state(now_ts=1000, stale_after_sec=10)

    @patch("route_helpers._get_stock_container")
    def test_stock_display_name_from_container(self, mock_get):
        """Test _stock_display_name with symbol in container."""
        mock_get.return_value = {"AAPL": "Apple Inc."}
        from route_helpers import _stock_display_name
        name = _stock_display_name("AAPL", "us")
        self.assertEqual(name, "Apple Inc.")


class AppHelpersExtraTestCase(unittest.TestCase):
    """Test app_helpers.py additional uncovered functions."""

    def test_normalize_text_values(self):
        from app_helpers import normalize_text
        self.assertEqual(normalize_text(None), "")
        self.assertEqual(normalize_text("  hello  "), "hello")
        self.assertEqual(normalize_text(42), "42")
        self.assertEqual(normalize_text(""), "")

    def test_get_default_symbols(self):
        from app_helpers import get_default_symbols
        symbols = get_default_symbols()
        self.assertIn("us", symbols)
        self.assertIn("jp", symbols)
        self.assertIn("idx", symbols)
        self.assertTrue(len(symbols["us"]) > 0)

    def test_normalize_symbol(self):
        from app_helpers import normalize_symbol
        self.assertEqual(normalize_symbol(None), "")
        self.assertEqual(normalize_symbol("  aapl  "), "AAPL")
        self.assertEqual(normalize_symbol(123), "123")
        self.assertEqual(normalize_symbol("brk.a"), "BRK.A")

    def test_is_valid_symbol_edge_cases(self):
        from app_helpers import is_valid_symbol
        self.assertFalse(is_valid_symbol(None))
        self.assertFalse(is_valid_symbol(""))
        self.assertFalse(is_valid_symbol("A" * 20))
        self.assertFalse(is_valid_symbol("../etc"))
        self.assertFalse(is_valid_symbol("sym\nbol"))
        self.assertTrue(is_valid_symbol("AAPL"))
        self.assertTrue(is_valid_symbol("BRK.A"))


class TrendSourcesExtraTestCase(unittest.TestCase):
    """Additional trend_sources tests for uncovered utility functions."""

    def test_collect_google_trends_keyword_no_pytrends(self):
        """When TrendReq is None, should return empty list."""
        with patch("trend_sources.TrendReq", None):
            from trend_sources import collect_google_trends_keyword_items
            result = collect_google_trends_keyword_items("test", "us", limit=3)
            self.assertEqual(result, [])

    @patch("trend_sources._trend_queries_for_keyword", return_value=[])
    def test_collect_google_trends_keyword_empty(self, mock_trends):
        from trend_sources import collect_google_trends_keyword_items
        result = collect_google_trends_keyword_items("test", "us", limit=3)
        self.assertEqual(result, [])

    @patch("trend_sources.collect_rss_items")
    def test_collect_yahoo_news_rss_items_with_empty(self, mock_rss):
        """collect_yahoo_news_rss_items with empty results."""
        mock_rss.return_value = []
        from trend_sources import collect_yahoo_news_rss_items
        result = collect_yahoo_news_rss_items("us", count=8)
        self.assertEqual(result, [])

    @patch("trend_sources.collect_rss_items")
    def test_collect_yahoo_news_rss_items_jp(self, mock_rss):
        """collect_yahoo_news_rss_items for JP market."""
        mock_rss.return_value = []
        from trend_sources import collect_yahoo_news_rss_items
        result = collect_yahoo_news_rss_items("jp", count=4)
        self.assertEqual(result, [])


class ConstantsHelperExtraTestCase(unittest.TestCase):
    """Test additional constants helper coverage."""

    def test_cache_duration_default(self):
        """Test CACHE_DURATION is a positive int."""
        import constants
        self.assertGreater(constants.CACHE_DURATION, 0)

    def test_sse_intervals(self):
        """Test SSE interval constants."""
        import constants
        self.assertGreater(constants.SSE_HEARTBEAT_INTERVAL, 0)
        self.assertGreater(constants.SSE_MARKET_CLOSED_SLEEP, 0)



class StorageUtilsTestCase(unittest.TestCase):
    """Test utils/storage.py uncovered lines."""

    @patch("utils.storage.os.path.exists", return_value=False)
    def test_load_user_stocks_no_file(self, mock_exists):
        """When file doesn't exist, load_user_stocks should return early."""
        from utils.storage import load_user_stocks
        result = load_user_stocks(force=False)
        self.assertIsNone(result)

    @patch("utils.storage.USER_STOCKS_FILE", "/tmp/nonexistent_test_file.json")
    @patch("utils.storage.os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=MagicMock)
    @patch("utils.storage.json.load", return_value={"us": {}, "jp": {}, "idx": {}})
    @patch("utils.storage.os.stat")
    def test_load_user_stocks_no_scheme(self, mock_stat, mock_json, mock_open, mock_exists):
        """Load user stocks without encryption scheme (direct dict)."""
        mock_stat.return_value.st_mtime_ns = 100
        from utils.storage import load_user_stocks
        from app_state import app_state
        app_state.market.last_modified_ns = 0
        load_user_stocks(force=True)
        # Should have loaded data without error
        self.assertIsNotNone(app_state)

    @patch("utils.storage.USER_STOCKS_FILE", "/tmp/test_save_stocks.json")
    @patch("utils.storage.json.dump")
    @patch("utils.storage.json.dumps", return_value='{"us": {}}')
    @patch("utils.storage.protect_data", return_value={"scheme": "test", "value": "encrypted"})
    @patch("utils.storage.os.replace")
    @patch("utils.storage.os.stat")
    @patch("utils.storage.platform.system", return_value="Windows")
    def test_save_user_stocks(
        self, mock_platform, mock_stat, mock_replace, mock_protect,
        mock_dumps, mock_dump
    ):
        """Test save_user_stocks function."""
        mock_stat.return_value.st_mtime_ns = 200
        from utils.storage import save_user_stocks
        from app_state import app_state
        app_state.market.user_us = {}
        app_state.market.user_jp = {}
        app_state.market.user_idx = {}
        app_state.market.last_usdjpy_rate = 150.0
        # Should not raise
        save_user_stocks()
        self.assertTrue(mock_protect.called)


class ThreadingUtilsTestCase(unittest.TestCase):
    """Test utils/threading.py uncovered lines."""

    def test_daemon_thread_pool_executor_submit(self):
        """Test DaemonThreadPoolExecutor can submit and complete tasks."""
        from utils.threading import DaemonThreadPoolExecutor
        results = []

        def dummy_task(x):
            results.append(x)
            return x

        with DaemonThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(dummy_task, 42)
            result = future.result(timeout=5)
            self.assertEqual(result, 42)
            self.assertIn(42, results)

    def test_daemon_thread_pool_executor_with_exception(self):
        """Test DaemonThreadPoolExecutor handles exceptions gracefully."""
        from utils.threading import DaemonThreadPoolExecutor

        def failing_task():
            raise ValueError("Test error")

        with DaemonThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(failing_task)
            with self.assertRaises(ValueError):
                future.result(timeout=5)


class RouteHelpersMoreTestCase(unittest.TestCase):
    """Additional route_helpers.py coverage tests."""

    @patch("route_helpers.clear_cache_prefix")
    @patch("route_helpers.app_state")
    def test_remove_stock_from_caches(self, mock_state, mock_clear):
        """Test remove_stock_from_caches."""
        from route_helpers import remove_stock_from_caches
        mock_state.cache.sse_data_lock = MagicMock()
        mock_state.market.current_stocks_cache = {"us": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}
        mock_state.market.target_stocks_cache = {"us": [{"symbol": "AAPL"}]}
        remove_stock_from_caches("AAPL", "us")
        # Verify AAPL was removed
        self.assertEqual(len(mock_state.market.current_stocks_cache["us"]), 1)
        self.assertEqual(mock_state.market.current_stocks_cache["us"][0]["symbol"], "MSFT")
        self.assertEqual(len(mock_state.market.target_stocks_cache["us"]), 0)

    def test_extract_text_from_mistral_with_object_chunks(self):
        """Test _extract_text_from_mistral_content with object chunks (hasattr)."""
        from route_helpers import _extract_text_from_mistral_content
        # Create mock objects with .type and .text attributes
        mock_chunk = MagicMock()
        mock_chunk.type = "text"
        mock_chunk.text = "Hello from object"
        result = _extract_text_from_mistral_content([mock_chunk])
        self.assertIn("Hello from object", result)

    def test_stock_display_name_with_dict_value(self):
        """Test _stock_display_name with dict container value."""
        from route_helpers import _stock_display_name
        with patch("route_helpers._get_stock_container") as mock_get:
            mock_get.return_value = {"AAPL": {"name": "  Apple Inc.  "}}
            name = _stock_display_name("AAPL", "us")
            self.assertEqual(name, "Apple Inc.")

    def test_stock_display_name_with_default(self):
        """Test _stock_display_name with default stock names fallback."""
        with patch("route_helpers._get_stock_container", return_value=None):
            with patch("route_helpers._default_stock_names") as mock_default:
                mock_default.return_value = {"AAPL": "Apple Inc. (default)"}
                from route_helpers import _stock_display_name
                name = _stock_display_name("AAPL", "us")
                self.assertEqual(name, "Apple Inc. (default)")


if __name__ == "__main__":
    unittest.main()
