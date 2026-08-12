"""Unit tests for routes/api_system.py - system management endpoints."""

import json
import os
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import credential_manager
from app import app
from app_state import app_state


class ApiCredentialsTestCase(unittest.TestCase):
    """API credentials endpoint tests for uncovered error paths."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        # Local personal-use defaults: no admin token / no remote API.
        # Clear so developer machines with these env vars set do not flake.
        self._env_patcher = patch.dict(
            os.environ,
            {"MNS_ADMIN_TOKEN": "", "MNS_ALLOW_REMOTE_API": "0"},
            clear=False,
        )
        self._env_patcher.start()
        os.environ.pop("MNS_ADMIN_TOKEN", None)

    def tearDown(self):
        self._env_patcher.stop()

    def test_credentials_options(self):
        response = self.client.options("/api/credentials")
        self.assertEqual(response.status_code, 200)

    def test_credentials_delete(self):
        response = self.client.delete(
            "/api/credentials",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["ok"])

    def test_credentials_get_remote_forbidden(self):
        response = self.client.get(
            "/api/credentials",
            environ_base={"REMOTE_ADDR": "192.168.1.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_credentials_post_remote_forbidden(self):
        response = self.client.post(
            "/api/credentials",
            data=json.dumps({"mistral_api_key": "test"}),
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_credentials_post_invalid_json(self):
        response = self.client.post(
            "/api/credentials",
            data="not json",
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)

    def test_credentials_post_invalid_mistral_key_too_short(self):
        response = self.client.post(
            "/api/credentials",
            data=json.dumps({"mistral_api_key": "short"}),
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)

    def test_credentials_post_custom_prompt_too_long(self):
        response = self.client.post(
            "/api/credentials",
            data=json.dumps({"custom_ai_prompt": "x" * 5001}),
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)

    @patch("routes.api_system.save_api_credentials")
    def test_credentials_post_custom_prompt_too_long_does_not_save_keys(self, mock_save):
        """Credentials must NOT be saved when custom_ai_prompt exceeds 5000 chars."""
        response = self.client.post(
            "/api/credentials",
            data=json.dumps(
                {
                    "mistral_api_key": "a" * 40,
                    "custom_ai_prompt": "x" * 5001,
                }
            ),
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)
        mock_save.assert_not_called()

    @patch("routes.api_system.get_custom_ai_prompt", return_value="saved prompt")
    @patch("routes.api_system.get_api_credential_state", return_value={})
    @patch("routes.api_system.save_api_credentials")
    def test_credentials_and_prompt_use_one_save(self, mock_save, _mock_state, _mock_prompt):
        """A combined request must be persisted by one atomic config update."""
        response = self.client.post(
            "/api/credentials",
            data=json.dumps({"mistral_api_key": "a" * 40, "custom_ai_prompt": "prompt"}),
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 200)
        mock_save.assert_called_once_with(
            mistral_api_key="a" * 40,
            langsearch_api_key=None,
            tavily_api_key=None,
            alphavantage_api_key=None,
            custom_ai_prompt="prompt",
            update_custom_ai_prompt=True,
        )

    @patch("routes.api_system.save_api_credentials")
    def test_credentials_post_rejects_non_string_mistral_key(self, mock_save):
        """Non-string API key values must be rejected with 400, not crash (R3)."""
        for bad_value in (123, True, ["key"], {"key": "value"}):
            with self.subTest(bad_value=bad_value):
                response = self.client.post(
                    "/api/credentials",
                    data=json.dumps({"mistral_api_key": bad_value}),
                    content_type="application/json",
                    headers={"Origin": "http://localhost:5000"},
                )
                self.assertEqual(response.status_code, 400)
                data = json.loads(response.data)
                self.assertIn("mistral_api_key", data.get("details", {}).get("fields", []))
        mock_save.assert_not_called()

    @patch("routes.api_system.save_api_credentials")
    def test_credentials_post_rejects_non_string_other_keys(self, mock_save):
        """langsearch/tavily/alphavantage keys must also reject non-string values (R3)."""
        payloads = (
            {"langsearch_api_key": 123},
            {"tavily_api_key": ["abc"]},
            {"alphavantage_api_key": {"k": 1}},
            {"langsearch_api_key": False},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/credentials",
                    data=json.dumps(payload),
                    content_type="application/json",
                    headers={"Origin": "http://localhost:5000"},
                )
                self.assertEqual(response.status_code, 400)
        mock_save.assert_not_called()

    @patch("routes.api_system.clear_api_credentials", return_value=["mistral_api_key"])
    def test_credentials_delete_with_keyring_failure(self, mock_clear):
        response = self.client.delete(
            "/api/credentials",
            headers={"Origin": "http://localhost:5000"},
        )
        # Partial keyring deletion must not look like a successful logout.
        self.assertEqual(response.status_code, 500)
        data = json.loads(response.data)
        self.assertFalse(data["ok"])
        self.assertIn("failed_keys", data)
        self.assertEqual(data["failed_keys"], ["mistral_api_key"])


class CredentialPersistenceTestCase(unittest.TestCase):
    """Credential persistence must commit related settings as one config write."""

    @patch("credential_manager.crypto_utils._encode_secret")
    @patch("credential_manager.config_store.save_config")
    @patch("credential_manager.config_store.load_config")
    @patch("credential_manager.config_store.config_update_lock")
    def test_credentials_and_prompt_share_one_config_save(
        self, mock_lock, mock_load, mock_save, mock_encode
    ):
        config = {"api_credentials": {}, "custom_ai_prompt": "before"}
        mock_lock.return_value = nullcontext()
        mock_load.return_value = config
        mock_encode.return_value = {"storage": "encrypted"}

        credential_manager.save_api_credentials(
            mistral_api_key="a" * 40,
            custom_ai_prompt="after",
            update_custom_ai_prompt=True,
        )

        mock_save.assert_called_once_with(
            {
                "api_credentials": {"mistral_api_key": {"storage": "encrypted"}},
                "custom_ai_prompt": "after",
            }
        )


class CacheStatsEndpointTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_cache_stats_options(self):
        response = self.client.options("/api/cache-stats")
        self.assertEqual(response.status_code, 200)

    def test_cache_stats_remote_forbidden(self):
        response = self.client.get(
            "/api/cache-stats",
            environ_base={"REMOTE_ADDR": "192.168.1.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_cache_stats_local_returns_data(self):
        response = self.client.get(
            "/api/cache-stats",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["ok"])
        self.assertIn("cache_stats", data)
        self.assertIn("cache_sizes", data["cache_stats"])


class MetricsEndpointTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_metrics_options(self):
        response = self.client.options("/api/metrics")
        self.assertEqual(response.status_code, 200)

    def test_metrics_remote_forbidden(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_metrics_local_returns_data(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["ok"])
        self.assertIn("cache", data)
        self.assertIn("market_data", data)
        self.assertIn("sse", data)
        self.assertIn("config", data)

    def test_metrics_includes_executor_saturation(self):
        # H3/M6: the metrics endpoint must expose per-pool queue/depth so a
        # backing-up AI or market-data executor is observable.
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("executors", data)
        for pool in ("ai", "data", "news", "sync"):
            self.assertIn(pool, data["executors"])
            self.assertIn("max_queue_size", data["executors"][pool])
            self.assertIn("pending", data["executors"][pool])

    def test_metrics_includes_yfinance_state(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("yfinance", data["market_data"])
        self.assertIn("rate_limited", data["market_data"]["yfinance"])

    def test_metrics_includes_scraper_block_state(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("scraper", data["market_data"])
        self.assertIn("blocked", data["market_data"]["scraper"])
        self.assertIn("block_clears_in_sec", data["market_data"]["scraper"])

    def test_metrics_includes_sse_announcer_counters(self):
        with (
            patch.object(
                app_state.sse_announcer_mode1,
                "stats",
                return_value={"listeners": 0, "announced": 5, "dropped": 1},
            ),
            patch.object(
                app_state.sse_announcer_mode2,
                "stats",
                return_value={"listeners": 0, "announced": 3, "dropped": 0},
            ),
        ):
            response = self.client.get(
                "/api/metrics",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        data = json.loads(response.data)
        self.assertEqual(data["sse"]["mode1_announced"], 5)
        self.assertEqual(data["sse"]["mode1_dropped"], 1)
        self.assertEqual(data["sse"]["mode2_announced"], 3)
        self.assertEqual(data["sse"]["mode2_dropped"], 0)

    def test_metrics_includes_stock_counts(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("stock_counts", data["market_data"])

    def test_metrics_includes_sse_listeners(self):
        with (
            patch.object(app_state.sse_announcer_mode1, "listener_count", return_value=2),
            patch.object(app_state.sse_announcer_mode2, "listener_count", return_value=3),
            patch.object(app_state.sse_listener_limiter, "listener_count", return_value=5),
        ):
            response = self.client.get(
                "/api/metrics",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            data = json.loads(response.data)
            self.assertIn("listeners", data["sse"])
            self.assertEqual(data["sse"]["listeners"], 5)
            self.assertEqual(data["sse"]["mode1_listeners"], 2)
            self.assertEqual(data["sse"]["mode2_listeners"], 3)

    def test_metrics_includes_is_syncing(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("is_syncing", data["market_data"])

    def test_metrics_includes_realtime_engine_diagnostics(self):
        # Producer-level engine health (store counts, WS/scraper thread
        # liveness, block states) must be visible in one screen.
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("engine", data)
        engine = data["engine"]
        for key in (
            "running",
            "market_store_count",
            "pts_store_count",
            "last_update_at",
            "tv_ws_connected",
            "tv_subscribed_symbols",
            "jp_scraper_symbols",
            "tv_thread_alive",
            "jp_scraper_thread_alive",
            "pts_thread_alive",
            "scraper_blocked",
            "scraper_block_clears_in_sec",
            "yf_rate_limited",
        ):
            self.assertIn(key, engine)


class CspReportEndpointTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_csp_report_valid(self):
        response = self.client.post(
            "/api/csp-report",
            data=json.dumps(
                {
                    "document-uri": "https://example.com",
                    "violated-directive": "script-src",
                    "effective-directive": "script-src",
                    "blocked-uri": "https://evil.com/script.js",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)

    def test_csp_report_empty(self):
        response = self.client.post(
            "/api/csp-report",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)

    def test_csp_report_sanitizes_sensitive_fields(self):
        """Sensitive fields should be truncated in CSP reports."""
        response = self.client.post(
            "/api/csp-report",
            data=json.dumps(
                {
                    "document-uri": "https://example.com" + "x" * 500,
                    "blocked-uri": "https://example.com" + "y" * 500,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)

    def test_csp_report_strips_unknown_keys(self):
        """Unknown fields should be filtered out."""
        response = self.client.post(
            "/api/csp-report",
            data=json.dumps(
                {
                    "document-uri": "https://example.com",
                    "secret-token": "should-not-be-logged",
                    "api-key": "should-be-filtered",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)

    def test_csp_report_bad_json(self):
        response = self.client.post(
            "/api/csp-report",
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)

    def test_csp_report_strips_control_characters(self):
        """Control characters in URI values must be stripped to prevent log injection."""
        response = self.client.post(
            "/api/csp-report",
            data=json.dumps(
                {
                    "document-uri": "https://example.com/path\r\nINJECTED",
                    "blocked-uri": "https://evil.com\x00\x01script",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)


class ShutdownEndpointTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_shutdown_options(self):
        response = self.client.options("/api/shutdown")
        self.assertEqual(response.status_code, 200)

    def test_shutdown_remote_forbidden(self):
        response = self.client.post(
            "/api/shutdown",
            environ_base={"REMOTE_ADDR": "192.168.1.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_shutdown_invalid_json(self):
        response = self.client.post(
            "/api/shutdown",
            data="not json",
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)

    def test_shutdown_missing_confirm(self):
        response = self.client.post(
            "/api/shutdown",
            data=json.dumps({}),
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)

    def test_shutdown_missing_token(self):
        response = self.client.post(
            "/api/shutdown",
            data=json.dumps({"confirm": True}),
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_shutdown_invalid_token(self):
        response = self.client.post(
            "/api/shutdown",
            data=json.dumps({"confirm": True, "shutdown_token": "invalid"}),
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_shutdown_blocked_in_remote_api_mode(self):
        """F-4: Shutdown must be rejected when MNS_ALLOW_REMOTE_API=1."""
        with patch.dict(
            os.environ,
            {"MNS_ALLOW_REMOTE_API": "1", "MNS_ADMIN_TOKEN": "test-admin-token-0123456789abcdef"},
            clear=False,
        ):
            response = self.client.post(
                "/api/shutdown",
                data=json.dumps({"confirm": True, "shutdown_token": "any"}),
                content_type="application/json",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(response.status_code, 403)
            data = json.loads(response.data)
            self.assertIn("remote API mode", data.get("details", {}).get("reason", ""))


if __name__ == "__main__":
    unittest.main()
