"""
test_review_r1_sse_ticket_binding.py - Regression tests for review findings R1/R3/R5.

R1: SSE tickets must be bound to a Flask session, never to the peer address
    (which is shared by every client on the same host).
R3: MNS_DISABLE_LOCAL_RATE_LIMIT must apply a high-but-finite ceiling rather
    than bypassing rate limiting entirely.
R5: A malformed extension manifest must be reported as a degraded status
    instead of being silently swallowed.
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from utils.networking import (
    SseTicketSessionUnavailable,
    _session_id_for_sse,
    consume_sse_ticket,
    create_sse_ticket,
)


def _make_app():
    app = create_app(skip_bootstrap=True)
    app.secret_key = "test-secret-key-for-sse-ticket-binding"
    return app


class TestR1SseTicketSessionBinding(unittest.TestCase):
    """R1: tickets are session-bound and fail closed without a session."""

    def test_ticket_roundtrip_succeeds_within_same_session(self):
        """正常系: same browser session issues and redeems its own ticket."""
        app = _make_app()
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            from flask import request as flask_request

            ticket = create_sse_ticket(flask_request)
            self.assertTrue(consume_sse_ticket(flask_request, ticket))

    def test_ticket_is_single_use(self):
        """境界値: a redeemed ticket cannot be replayed."""
        app = _make_app()
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            from flask import request as flask_request

            ticket = create_sse_ticket(flask_request)
            self.assertTrue(consume_sse_ticket(flask_request, ticket))
            self.assertFalse(consume_sse_ticket(flask_request, ticket))

    def test_empty_ticket_is_rejected(self):
        """境界値: empty/None ticket never validates."""
        app = _make_app()
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            from flask import request as flask_request

            self.assertFalse(consume_sse_ticket(flask_request, ""))
            self.assertFalse(consume_sse_ticket(flask_request, None))

    def test_ticket_cannot_be_redeemed_from_a_different_session_same_ip(self):
        """R1 acceptance: same peer address + different session cookie must fail.

        This is the shared-host attack the finding described: two local users
        behind the identical REMOTE_ADDR must not be able to steal each other's
        tickets.
        """
        app = _make_app()
        same_ip = {"REMOTE_ADDR": "127.0.0.1"}

        with app.test_request_context("/", environ_base=same_ip):
            from flask import request as victim_request

            ticket = create_sse_ticket(victim_request)

        # A separate request context = a separate (empty) session cookie,
        # while the peer address is byte-for-byte identical.
        with app.test_request_context("/", environ_base=same_ip):
            from flask import request as attacker_request

            self.assertFalse(
                consume_sse_ticket(attacker_request, ticket),
                "Ticket was redeemable from a different session on the same host",
            )

    def test_create_ticket_without_session_raises(self):
        """異常系: no Flask session -> fail closed instead of binding to the IP."""
        dummy_req = MagicMock()
        dummy_req.environ = {"REMOTE_ADDR": "127.0.0.1"}

        with self.assertRaises(SseTicketSessionUnavailable):
            create_sse_ticket(dummy_req)

    def test_consume_without_session_is_rejected(self):
        """異常系: a sessionless request can never redeem a ticket."""
        app = _make_app()
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            from flask import request as flask_request

            ticket = create_sse_ticket(flask_request)

        dummy_req = MagicMock()
        dummy_req.environ = {"REMOTE_ADDR": "127.0.0.1"}
        self.assertFalse(consume_sse_ticket(dummy_req, ticket))

    def test_session_identity_reports_not_session_backed_outside_context(self):
        """The fallback identity is explicitly flagged as not session-backed."""
        dummy_req = MagicMock()
        dummy_req.environ = {"REMOTE_ADDR": "127.0.0.1"}

        identity, session_backed = _session_id_for_sse(dummy_req)
        self.assertFalse(session_backed)
        self.assertTrue(identity.startswith("addr:"))

    def test_session_identity_is_stable_within_a_session(self):
        """The per-session id is generated once and reused."""
        app = _make_app()
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            from flask import request as flask_request

            first, ok1 = _session_id_for_sse(flask_request)
            second, ok2 = _session_id_for_sse(flask_request)

            self.assertTrue(ok1)
            self.assertTrue(ok2)
            self.assertEqual(first, second)
            self.assertTrue(first.startswith("sid:"))


class TestR1SseTicketEndpoint(unittest.TestCase):
    """R1: the ticket endpoint returns 403 (not 500) when no session exists."""

    def test_ticket_endpoint_returns_403_without_session(self):
        app = _make_app()
        app.config["TESTING"] = True
        # CSRF runs before the view; disable it so the 403 under test is the
        # ticket/session decision rather than a missing CSRF token (400).
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()

        with patch(
            "routes.api_stocks.create_sse_ticket",
            side_effect=SseTicketSessionUnavailable("no session"),
        ):
            resp = client.post(
                "/api/stocks/stream/ticket",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={"Origin": "http://127.0.0.1:5000"},
            )

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.get_json()["ok"])


class TestR3LocalRateLimitCeiling(unittest.TestCase):
    """R3: the local bypass flag now applies a finite ceiling."""

    def test_disable_flag_still_enforces_a_finite_ceiling(self):
        """異常系: a runaway local loop eventually receives a 429."""
        import route_helpers

        app = _make_app()
        app.config["TESTING"] = True

        @app.route("/__r3_probe")
        @route_helpers.rate_limit(max_requests=1, window_seconds=60)
        def _probe():
            return {"ok": True}

        client = app.test_client()
        env = {
            "MNS_DISABLE_LOCAL_RATE_LIMIT": "1",
            "MNS_LOCAL_RATE_LIMIT_MULTIPLE": "1",
            "MNS_LOCAL_RATE_LIMIT_CEILING": "3",
        }

        with patch.dict(os.environ, env):
            with patch.dict(route_helpers._rate_limit_store, {}, clear=True):
                statuses = [
                    client.get("/__r3_probe", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code
                    for _ in range(5)
                ]

        self.assertEqual(statuses[:3], [200, 200, 200], "ceiling applied too early")
        self.assertIn(429, statuses, "local bypass never enforced a finite ceiling")


class TestR5ManifestStatusOnMalformedFile(unittest.TestCase):
    """R5: a malformed manifest is reported, not silently swallowed."""

    def test_malformed_manifest_marks_status_not_ok(self):
        from app_state import app_state
        from utils.networking import _load_allowed_extension_origins

        with app_state._extension_origins_cache_lock:
            app_state._extension_origins_cache_ts = 0.0

        with patch("pathlib.Path.exists", return_value=True):
            with patch("json.load", side_effect=json.JSONDecodeError("bad", "doc", 0)):
                origins = _load_allowed_extension_origins()

        self.assertIsInstance(origins, set)
        with app_state._extension_origins_cache_lock:
            self.assertFalse(app_state._extension_manifest_status["ok"])
            self.assertIn("manifest_load_error", app_state._extension_manifest_status["error"])
            app_state._extension_origins_cache_ts = 0.0


if __name__ == "__main__":
    unittest.main()
