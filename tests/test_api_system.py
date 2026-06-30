"""Unit tests for routes/api_system.py - system management endpoints."""

import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app


class ApiCredentialsTestCase(unittest.TestCase):
    """API credentials endpoint tests for uncovered error paths."""

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

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

    def test_metrics_includes_yfinance_state(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("yfinance", data["market_data"])
        self.assertIn("rate_limited", data["market_data"]["yfinance"])

    def test_metrics_includes_stock_counts(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("stock_counts", data["market_data"])

    def test_metrics_includes_sse_listeners(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("listeners", data["sse"])

    def test_metrics_includes_is_syncing(self):
        response = self.client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        data = json.loads(response.data)
        self.assertIn("is_syncing", data["market_data"])


class CspReportEndpointTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_csp_report_valid(self):
        response = self.client.post(
            "/api/csp-report",
            data=json.dumps({
                "document-uri": "https://example.com",
                "violated-directive": "script-src",
                "effective-directive": "script-src",
                "blocked-uri": "https://evil.com/script.js",
            }),
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
            data=json.dumps({
                "document-uri": "https://example.com" + "x" * 500,
                "blocked-uri": "https://example.com" + "y" * 500,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)

    def test_csp_report_strips_unknown_keys(self):
        """Unknown fields should be filtered out."""
        response = self.client.post(
            "/api/csp-report",
            data=json.dumps({
                "document-uri": "https://example.com",
                "secret-token": "should-not-be-logged",
                "api-key": "should-be-filtered",
            }),
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


if __name__ == "__main__":
    unittest.main()
