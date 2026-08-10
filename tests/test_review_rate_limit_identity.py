"""Tests for the rate-limit identity fix (R1) and /api/shutdown throttling (R3).

R1: behind a reverse proxy in remote mode, RAW_REMOTE_ADDR is the proxy's own
address, so every remote client shared one bucket (mutual DoS) and inherited
the loopback treatment. The proxy-supplied address must be used instead.

R3: /api/shutdown was the only API route without @rate_limit.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import route_helpers
from app import app


class RateLimitIdentityTests(unittest.TestCase):
    """R1: _rate_limit_identity picks the right client key per deployment mode."""

    def _identity(self, env: dict, overrides: dict):
        with (
            patch.dict("os.environ", env, clear=False),
            app.test_request_context(environ_overrides=overrides),
        ):
            return route_helpers._rate_limit_identity()

    def test_direct_mode_uses_raw_peer_address(self):
        """Default (no proxy): the raw socket peer wins over a spoofable value."""
        key, is_local = self._identity(
            {"MNS_ALLOW_REMOTE_API": "0", "MNS_PROXY_FIX": "0"},
            {"REMOTE_ADDR": "203.0.113.9", "RAW_REMOTE_ADDR": "198.51.100.7"},
        )
        self.assertEqual(key, "198.51.100.7")
        self.assertFalse(is_local)

    def test_direct_mode_loopback_detected(self):
        key, is_local = self._identity(
            {"MNS_ALLOW_REMOTE_API": "0", "MNS_PROXY_FIX": "0"},
            {"REMOTE_ADDR": "127.0.0.1", "RAW_REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(key, "127.0.0.1")
        self.assertTrue(is_local)

    def test_direct_mode_falls_back_to_remote_addr(self):
        """No RAW_REMOTE_ADDR (middleware not applied) -> remote_addr is used."""
        key, is_local = self._identity(
            {"MNS_ALLOW_REMOTE_API": "0", "MNS_PROXY_FIX": "0"},
            {"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(key, "127.0.0.1")
        self.assertTrue(is_local)

    def test_remote_proxy_mode_buckets_per_client_not_per_proxy(self):
        """R1 core: two clients behind one proxy must not share a bucket."""
        env = {"MNS_ALLOW_REMOTE_API": "1", "MNS_PROXY_FIX": "1"}
        key_a, local_a = self._identity(
            env, {"REMOTE_ADDR": "203.0.113.10", "RAW_REMOTE_ADDR": "127.0.0.1"}
        )
        key_b, local_b = self._identity(
            env, {"REMOTE_ADDR": "203.0.113.11", "RAW_REMOTE_ADDR": "127.0.0.1"}
        )
        self.assertEqual(key_a, "203.0.113.10")
        self.assertEqual(key_b, "203.0.113.11")
        self.assertNotEqual(key_a, key_b)
        # The proxy hop is loopback, but remote clients must not be treated local.
        self.assertFalse(local_a)
        self.assertFalse(local_b)

    def test_remote_without_proxy_keeps_raw_peer(self):
        """Remote mode with a direct listener still trusts only the raw peer."""
        key, is_local = self._identity(
            {"MNS_ALLOW_REMOTE_API": "1", "MNS_PROXY_FIX": "0"},
            {"REMOTE_ADDR": "127.0.0.1", "RAW_REMOTE_ADDR": "203.0.113.12"},
        )
        self.assertEqual(key, "203.0.113.12")
        self.assertFalse(is_local)

    def test_remote_proxy_mode_ignores_local_bypass(self):
        """MNS_DISABLE_LOCAL_RATE_LIMIT must not exempt proxied remote callers."""
        calls = []

        @route_helpers.rate_limit(max_requests=2, window_seconds=60)
        def dummy_remote_route():
            calls.append(1)
            return "ok"

        env = {
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_DISABLE_LOCAL_RATE_LIMIT": "1",
        }
        store = {}
        windows = {}
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(route_helpers, "_rate_limit_store", store),
            patch.object(route_helpers, "_rate_limit_window_by_key", windows),
        ):
            for _ in range(3):
                with app.test_request_context(
                    environ_overrides={
                        "REMOTE_ADDR": "203.0.113.20",
                        "RAW_REMOTE_ADDR": "127.0.0.1",
                    }
                ):
                    dummy_remote_route()

        # Requests were counted (not bypassed) and bucketed under the client IP.
        self.assertTrue(any(k.startswith("203.0.113.20:") for k in store))
        self.assertLess(len(calls), 3)


class ShutdownRateLimitTests(unittest.TestCase):
    """R3: /api/shutdown must carry a rate limit like every other API route."""

    def test_shutdown_route_registers_a_rate_limit_bucket(self):
        """The decorator is actually applied (it creates a per-endpoint bucket)."""
        view = app.view_functions.get("api_system.api_shutdown")
        self.assertIsNotNone(view, "api_shutdown view not registered")

        store = {}
        windows = {}
        with (
            patch.object(route_helpers, "_rate_limit_store", store),
            patch.object(route_helpers, "_rate_limit_window_by_key", windows),
            patch.dict(
                "os.environ", {"MNS_DISABLE_LOCAL_RATE_LIMIT": "0"}, clear=False
            ),
        ):
            app.test_client().post("/api/shutdown", json={})

        self.assertTrue(
            any(k.endswith(":api_system.api_shutdown") for k in store),
            f"no rate-limit bucket created for /api/shutdown: {list(store)}",
        )

    def test_shutdown_returns_429_after_limit(self):
        """Repeated unauthenticated attempts get throttled instead of looping free.

        Loopback callers get MNS_LOCAL_RATE_LIMIT_MULTIPLE (default 10x), so the
        effective budget here is 5 * 10 = 50 requests per 60s window.
        """
        store = {}
        windows = {}
        with (
            patch.object(route_helpers, "_rate_limit_store", store),
            patch.object(route_helpers, "_rate_limit_window_by_key", windows),
            patch.dict(
                "os.environ",
                {
                    "MNS_DISABLE_LOCAL_RATE_LIMIT": "0",
                    "MNS_LOCAL_RATE_LIMIT_MULTIPLE": "1",
                },
                clear=False,
            ),
        ):
            client = app.test_client()
            statuses = [
                client.post("/api/shutdown", json={}).status_code for _ in range(8)
            ]

        self.assertIn(429, statuses, f"expected throttling, got {statuses}")
        # The limit engages only after the configured budget (5) is consumed.
        self.assertEqual(statuses.index(429), 5)


if __name__ == "__main__":
    unittest.main()
