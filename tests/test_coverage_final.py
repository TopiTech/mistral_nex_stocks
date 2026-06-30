"""Final batch of focused coverage tests — pure, no side effects on app_state."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class RouteHelpersCleanupTestCase(unittest.TestCase):
    """Test route_helpers.py _cleanup_rate_limit_store function."""

    def setUp(self):
        import route_helpers as rh
        # Save and reset state
        self._saved_store = rh._rate_limit_store.copy()
        self._saved_window = rh._rate_limit_window_by_key.copy()
        rh._rate_limit_store.clear()
        rh._rate_limit_window_by_key.clear()

    def tearDown(self):
        import route_helpers as rh
        rh._rate_limit_store.clear()
        rh._rate_limit_store.update(self._saved_store)
        rh._rate_limit_window_by_key.clear()
        rh._rate_limit_window_by_key.update(self._saved_window)

    def test_cleanup_rate_limit_store_empty(self):
        """Cleanup with empty store should not raise."""
        from route_helpers import _cleanup_rate_limit_store
        _cleanup_rate_limit_store()

    def test_cleanup_rate_limit_store_with_expired(self):
        """Cleanup should remove expired entries."""
        import time
        from route_helpers import _cleanup_rate_limit_store, _rate_limit_store, _rate_limit_window_by_key
        old_time = time.time() - 1000
        _rate_limit_store["test_key"] = [old_time]
        _rate_limit_window_by_key["test_key"] = 300
        _cleanup_rate_limit_store()
        self.assertNotIn("test_key", _rate_limit_store)
        self.assertNotIn("test_key", _rate_limit_window_by_key)

    def test_cleanup_rate_limit_store_with_valid(self):
        """Cleanup should keep valid entries."""
        import time
        from route_helpers import _cleanup_rate_limit_store, _rate_limit_store, _rate_limit_window_by_key
        now = time.time()
        _rate_limit_store["valid_key"] = [now]
        _rate_limit_window_by_key["valid_key"] = 300
        _cleanup_rate_limit_store()
        self.assertIn("valid_key", _rate_limit_store)

    def test_cleanup_rate_limit_store_exceeds_max(self):
        """Cleanup should trim oldest entries when store exceeds max."""
        import time
        from route_helpers import _cleanup_rate_limit_store, _rate_limit_store, _rate_limit_window_by_key
        now = time.time()
        with patch("route_helpers._RATE_LIMIT_MAX_ENTRIES", 2):
            _rate_limit_store["old_key"] = [now - 10]
            _rate_limit_window_by_key["old_key"] = 300
            _rate_limit_store["new_key"] = [now]
            _rate_limit_window_by_key["new_key"] = 300
            _rate_limit_store["newest_key"] = [now + 1]
            _rate_limit_window_by_key["newest_key"] = 300
            _cleanup_rate_limit_store()
            self.assertNotIn("old_key", _rate_limit_store)


class RouteHelpersRateLimitTestCase(unittest.TestCase):
    """Test route_helpers.py rate limit resolve functions."""

    @patch.dict(os.environ, {}, clear=True)
    def test_resolve_rate_limit_defaults(self):
        """Test resolve_rate_limit with default values."""
        from route_helpers import _resolve_rate_limit
        max_r, window = _resolve_rate_limit("test", 60, 300)
        self.assertEqual(max_r, 60)
        self.assertEqual(window, 300)

    @patch.dict(os.environ, {
        "MNS_RATE_LIMIT_DEFAULT_MAX": "100",
        "MNS_RATE_LIMIT_DEFAULT_WINDOW": "500",
    })
    def test_resolve_rate_limit_env_overrides(self):
        """Test resolve_rate_limit with env overrides."""
        from route_helpers import _resolve_rate_limit
        max_r, window = _resolve_rate_limit("test", 60, 300)
        self.assertEqual(max_r, 100)
        self.assertEqual(window, 500)

    @patch.dict(os.environ, {
        "MNS_RATE_LIMIT_TEST_ENDPOINT_MAX": "200",
        "MNS_RATE_LIMIT_TEST_ENDPOINT_WINDOW": "600",
    })
    def test_resolve_rate_limit_endpoint_specific(self):
        """Test resolve_rate_limit with endpoint-specific env vars."""
        from route_helpers import _resolve_rate_limit
        max_r, window = _resolve_rate_limit("test_endpoint", 60, 300)
        self.assertEqual(max_r, 200)
        self.assertEqual(window, 600)


class ThreadingMoreTestCase(unittest.TestCase):
    """Additional threading tests — no app_state dependencies."""

    def test_daemon_executor_submit_and_get(self):
        """Test submit task and get result."""
        from utils.threading import DaemonThreadPoolExecutor
        with DaemonThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(lambda: 42)
            result = future.result(timeout=5)
            self.assertEqual(result, 42)

    def test_daemon_executor_multi_submit(self):
        """Test multiple submits work correctly."""
        from utils.threading import DaemonThreadPoolExecutor
        results = []
        with DaemonThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(lambda x=i: results.append(x)) for i in range(5)]
            for f in futures:
                f.result(timeout=5)
        self.assertEqual(len(results), 5)


class StoragePureTestCase(unittest.TestCase):
    """Test storage.py without importing app_state at module level."""

    @patch("utils.storage.USER_STOCKS_FILE", "/tmp/test_no_exist.json")
    @patch("utils.storage.os.path.exists", return_value=False)
    def test_load_no_file_returns_none(self, mock_exists):
        """When file doesn't exist, load returns None."""
        from utils.storage import load_user_stocks
        result = load_user_stocks(force=False)
        self.assertIsNone(result)

    @patch("utils.storage.USER_STOCKS_FILE", "/tmp/test_save.json")
    @patch("utils.storage.json.dump")
    @patch("utils.storage.json.dumps", return_value='{"us": {}}')
    @patch("utils.storage.protect_data", return_value={"scheme": "test", "value": "encrypted"})
    @patch("utils.storage.os.replace")
    @patch("utils.storage.os.stat")
    @patch("utils.storage.platform.system", return_value="Windows")
    def test_save_user_stocks_windows(
        self, mock_platform, mock_stat, mock_replace, mock_protect,
        mock_dumps, mock_dump
    ):
        """Test save_user_stocks on Windows (no chmod)."""
        from utils.storage import save_user_stocks
        from app_state import app_state
        app_state.user_us = {}
        app_state.user_jp = {}
        app_state.user_idx = {}
        app_state.last_usdjpy_rate = 150.0
        save_user_stocks()
        self.assertTrue(mock_protect.called)
        self.assertTrue(mock_replace.called)

    @patch("utils.storage.USER_STOCKS_FILE", "/tmp/test_save2.json")
    @patch("utils.storage.json.dump")
    @patch("utils.storage.json.dumps", return_value='{"us": {}}')
    @patch("utils.storage.protect_data", return_value={"scheme": "test", "value": "encrypted"})
    @patch("utils.storage.os.replace")
    @patch("utils.storage.os.stat")
    @patch("utils.storage.platform.system", return_value="linux")
    @patch("utils.storage.os.chmod")
    def test_save_user_stocks_linux(
        self, mock_chmod, mock_platform, mock_stat, mock_replace,
        mock_protect, mock_dumps, mock_dump
    ):
        """Test save_user_stocks on Linux (chmod called)."""
        from utils.storage import save_user_stocks
        from app_state import app_state
        app_state.user_us = {}
        app_state.user_jp = {}
        app_state.user_idx = {}
        app_state.last_usdjpy_rate = 150.0
        save_user_stocks()
        self.assertTrue(mock_protect.called)
        self.assertTrue(mock_chmod.called)


class SecurityConfigTestCase(unittest.TestCase):
    """Test security_config.py uncovered lines."""

    def test_init_security(self):
        """Test init_security function creates CSRFProtect and configures app."""
        from flask import Flask
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SERVER_NAME"] = "localhost"

        from security_config import init_security
        result = init_security(app)
        self.assertIsNotNone(result)
        self.assertTrue(app.config.get("SESSION_COOKIE_HTTPONLY"))


class AppHelpersMoreTestCase(unittest.TestCase):
    """More app_helpers.py tests — pure functions only."""

    def test_normalize_text_edge_cases(self):
        from app_helpers import normalize_text
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text("   "), "")
        self.assertEqual(normalize_text(True), "True")


if __name__ == "__main__":
    unittest.main()
