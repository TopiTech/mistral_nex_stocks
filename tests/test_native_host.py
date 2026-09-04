import http.server
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from native_host import start_backend


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"app":"Mistral NeX Stocks","ready":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


class NativeHostStartBackendTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.pid_file = Path(self.temp_dir.name) / ".backend.pid"
        self.log_file = Path(self.temp_dir.name) / "native_host.log"
        # _LEGACY_PID_FILE points at the real project root by default; without
        # patching it a developer's real .backend.pid would be read — and in
        # some paths deleted — by these tests.
        self.legacy_pid_file = Path(self.temp_dir.name) / "legacy.backend.pid"
        patcher_pid = patch.object(start_backend, "PID_FILE", self.pid_file)
        patcher_legacy = patch.object(start_backend, "_LEGACY_PID_FILE", self.legacy_pid_file)
        patcher_log = patch.object(start_backend, "LOG", self.log_file)
        self.addCleanup(patcher_pid.stop)
        self.addCleanup(patcher_legacy.stop)
        self.addCleanup(patcher_log.stop)
        patcher_pid.start()
        patcher_legacy.start()
        patcher_log.start()

    def test_get_backend_port_from_env(self):
        with patch.dict("os.environ", {"MNS_BACKEND_PORT": "54321"}):
            self.assertEqual(start_backend.get_backend_port(), 54321)

    def test_get_backend_port_clamps_out_of_range_values(self):
        """Out-of-range or non-numeric ports must fall back to the default (R4)."""
        for bad_value in ("70000", "0", "-1", "65536", "abc", "1.5", ""):
            with self.subTest(bad_value=bad_value):
                with patch.dict("os.environ", {"MNS_BACKEND_PORT": bad_value}):
                    self.assertEqual(
                        start_backend.get_backend_port(), start_backend.DEFAULT_BACKEND_PORT
                    )

    def test_get_backend_port_accepts_boundary_values(self):
        for boundary in ("1", "65535"):
            with self.subTest(boundary=boundary):
                with patch.dict("os.environ", {"MNS_BACKEND_PORT": boundary}):
                    self.assertEqual(start_backend.get_backend_port(), int(boundary))

    def test_is_port_in_use_never_raises_on_out_of_range_port(self):
        """is_port_in_use must not raise OverflowError for an out-of-range port (R4)."""
        with patch.dict("os.environ", {"MNS_BACKEND_PORT": "70000"}):
            port = start_backend.get_backend_port()
        self.assertEqual(port, start_backend.DEFAULT_BACKEND_PORT)
        # The default port is a valid socket argument, so this must not raise.
        self.assertIsInstance(start_backend.is_port_in_use(port), bool)

    def test_is_port_in_use_detects_bound_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen(1)
            self.assertTrue(start_backend.is_port_in_use(port))

    def test_is_backend_healthy_once_returns_true_when_service_available(self):
        server = socketserver.TCPServer(("127.0.0.1", 0), HealthHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join)

        with patch.object(start_backend, "get_backend_port", return_value=port):
            try:
                self.assertTrue(start_backend.is_backend_healthy_once(timeout_sec=2.0))
            finally:
                server.shutdown()
                thread.join(timeout=1)
                server.server_close()

    def test_start_spawns_backend_when_port_is_free(self):
        fake_proc = MagicMock()
        fake_proc.pid = 4242

        with (
            patch.object(start_backend, "is_port_in_use", return_value=False),
            patch.object(start_backend, "wait_for_backend_ready", return_value=True),
            patch.object(start_backend.subprocess, "Popen", return_value=fake_proc),
        ):
            result = start_backend.start(extension_id="a" * 32)
            self.assertTrue(result["ok"])
            self.assertEqual(result["pid"], fake_proc.pid)
            self.assertEqual(result["port"], start_backend.DEFAULT_BACKEND_PORT)

    def test_start_returns_configured_port(self):
        fake_proc = MagicMock()
        fake_proc.pid = 5252

        with (
            patch.dict("os.environ", {"MNS_BACKEND_PORT": "54321"}),
            patch.object(start_backend, "is_port_in_use", return_value=False),
            patch.object(start_backend, "wait_for_backend_ready", return_value=True),
            patch.object(start_backend.subprocess, "Popen", return_value=fake_proc),
        ):
            result = start_backend.start(extension_id="a" * 32)
            self.assertEqual(result["port"], 54321)

    def test_start_normalizes_invalid_port_for_spawned_backend(self):
        fake_proc = MagicMock()
        fake_proc.pid = 6262

        with (
            patch.dict("os.environ", {"MNS_BACKEND_PORT": "70000"}),
            patch.object(start_backend, "is_port_in_use", return_value=False),
            patch.object(start_backend, "wait_for_backend_ready", return_value=True),
            patch.object(start_backend.subprocess, "Popen", return_value=fake_proc) as popen,
        ):
            result = start_backend.start(extension_id="a" * 32)
            spawned_env = popen.call_args.kwargs["env"]

        self.assertEqual(result["port"], start_backend.DEFAULT_BACKEND_PORT)
        self.assertEqual(spawned_env["MNS_BACKEND_PORT"], str(start_backend.DEFAULT_BACKEND_PORT))

    def test_is_running_validates_active_and_inactive_pids(self):
        # 1. Invalid PID <= 0
        self.assertFalse(start_backend.is_running(0))
        self.assertFalse(start_backend.is_running(-10))

        # 2. Active PID (using current process PID)
        import os

        current_pid = os.getpid()
        self.assertTrue(start_backend.is_running(current_pid))

        # 3. Non-existent PID
        self.assertFalse(start_backend.is_running(999999))

        # 4. Zombie state check via mocking psutil.Process
        import psutil

        mock_proc = MagicMock()
        mock_proc.status.return_value = psutil.STATUS_ZOMBIE
        mock_proc.is_running.return_value = True
        with patch("psutil.Process", return_value=mock_proc):
            self.assertFalse(start_backend.is_running(12345))

        # 5. AccessDenied state check via mocking psutil.Process
        with patch("psutil.Process", side_effect=psutil.AccessDenied):
            self.assertTrue(start_backend.is_running(12345))


class NativeHostMainLoopTestCase(unittest.TestCase):
    """Regression: main() must survive a start_backend exception (R-lock OSError)."""

    def test_main_start_backend_exception_keeps_loop_alive(self):
        """start() raising OSError (startup-lock contention) must not crash main()."""
        import native_host.native_host as nh

        sent = []
        messages = iter(
            [
                {"action": "start_backend", "extension_id": "a" * 32},
                None,  # EOF terminates the loop
            ]
        )

        with (
            patch.object(nh, "read_message", side_effect=lambda: next(messages)),
            patch.object(nh, "send_message", side_effect=lambda m: sent.append(m)),
            patch.object(nh, "_check_rate_limit", return_value=True),
            patch.object(nh, "_require_valid_extension_id", return_value="a" * 32),
            patch.object(nh, "start", side_effect=OSError("lock contention")),
        ):
            nh.main()

        # The failure was reported to the extension and the loop reached EOF
        # without propagating the exception (i.e. the process did not crash).
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("Retry", sent[0]["error"])

    def test_main_start_backend_success_sends_result(self):
        """start() success path still forwards the result dict unchanged."""
        import native_host.native_host as nh

        sent = []
        messages = iter(
            [
                {"action": "start_backend", "extension_id": "a" * 32},
                None,  # EOF terminates the loop
            ]
        )

        with (
            patch.object(nh, "read_message", side_effect=lambda: next(messages)),
            patch.object(nh, "send_message", side_effect=lambda m: sent.append(m)),
            patch.object(nh, "_check_rate_limit", return_value=True),
            patch.object(nh, "_require_valid_extension_id", return_value="a" * 32),
            patch.object(nh, "start", return_value={"ok": True, "pid": 1234, "port": 5000}),
        ):
            nh.main()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], {"ok": True, "pid": 1234, "port": 5000})
