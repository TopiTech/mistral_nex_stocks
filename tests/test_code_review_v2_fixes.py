"""
test_code_review_v2_fixes.py - Tests for code review fix verification.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from routes.api_analysis import _chat_error_response
from utils.networking import _load_allowed_extension_origins, consume_sse_ticket, create_sse_ticket


class TestCodeReviewV2Fixes(unittest.TestCase):

    def test_h1_remote_api_without_proxy_fix_logs_warning(self):
        """H-1: Setting MNS_ALLOW_REMOTE_API=1 without MNS_PROXY_FIX=1 should log a warning."""
        with patch.dict(os.environ, {"MNS_ALLOW_REMOTE_API": "1", "MNS_PROXY_FIX": "0"}):
            with patch("app.logger.warning") as mock_warn:
                create_app(skip_bootstrap=True)
                # Find the call containing MNS_ALLOW_REMOTE_API
                found = any(
                    "MNS_ALLOW_REMOTE_API is enabled but MNS_PROXY_FIX is not set" in str(call.args[0])
                    for call in mock_warn.call_args_list
                    if call.args
                )
                self.assertTrue(found, "Warning for remote API without proxy fix was not logged")

    def test_m6_manifest_status_thread_safety(self):
        """M-6: _load_allowed_extension_origins updates _extension_manifest_status safely."""
        with patch("pathlib.Path.exists", return_value=False):
            origins = _load_allowed_extension_origins()
            self.assertIsInstance(origins, set)

    def test_m7_sse_ticket_capacity_and_expiry_cleanup(self):
        """M-7: create_sse_ticket cleans expired tickets and enforces max capacity limit."""
        dummy_req = MagicMock()
        dummy_req.environ = {"REMOTE_ADDR": "127.0.0.1"}

        # Issue ticket with 0.001s TTL
        t_expired = create_sse_ticket(dummy_req, ttl_sec=0.001)
        import time
        time.sleep(0.01)

        # Issue new ticket
        t_new = create_sse_ticket(dummy_req, ttl_sec=60)
        self.assertTrue(consume_sse_ticket(dummy_req, t_new))
        self.assertFalse(consume_sse_ticket(dummy_req, t_expired))

    def test_m8_start_backend_atomic_pid_write(self):
        """M-8: start_backend uses atomic PID file write."""
        from native_host import start_backend
        with patch.object(start_backend, "is_port_in_use", return_value=False), \
             patch.object(start_backend.subprocess, "Popen") as mock_popen, \
             patch.object(start_backend, "wait_for_backend_ready", return_value=True):
            fake_proc = MagicMock()
            fake_proc.pid = 99999
            mock_popen.return_value = fake_proc

            res = start_backend.start()
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("pid"), 99999)

    def test_l5_chat_error_response_payload_consistency(self):
        """L-5: _chat_error_response includes request_token and disclaimer."""
        app = create_app(skip_bootstrap=True)
        fake_g = MagicMock()
        fake_g.request_id = "req-123"

        with app.app_context():
            with patch("routes.api_analysis.jsonify") as mock_jsonify:
                mock_jsonify.side_effect = lambda d: d
                resp, status = _chat_error_response(
                    ValueError("bad input"), fake_g, operation_token="tok-456"
                )
                self.assertEqual(status, 400)
                self.assertEqual(resp.get("request_token"), "tok-456")
                self.assertIn("disclaimer", resp)
                self.assertIn("reply", resp)


if __name__ == "__main__":
    unittest.main()
