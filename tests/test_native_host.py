import http.server
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import native_host.start_backend as start_backend


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


class NativeHostStartBackendTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.pid_file = Path(self.temp_dir.name) / '.backend.pid'
        self.log_file = Path(self.temp_dir.name) / 'native_host.log'
        patcher_pid = patch.object(start_backend, 'PID_FILE', self.pid_file)
        patcher_log = patch.object(start_backend, 'LOG', self.log_file)
        self.addCleanup(patcher_pid.stop)
        self.addCleanup(patcher_log.stop)
        patcher_pid.start()
        patcher_log.start()

    def test_get_backend_port_from_env(self):
        with patch.dict('os.environ', {'MNS_BACKEND_PORT': '54321'}):
            self.assertEqual(start_backend.get_backend_port(), 54321)

    def test_is_port_in_use_detects_bound_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.listen(1)
            self.assertTrue(start_backend.is_port_in_use(port))

    def test_is_backend_healthy_once_returns_true_when_service_available(self):
        server = socketserver.TCPServer(('127.0.0.1', 0), HealthHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join)

        with patch.object(start_backend, 'get_backend_port', return_value=port):
            try:
                self.assertTrue(start_backend.is_backend_healthy_once(timeout_sec=2.0))
            finally:
                server.shutdown()
                thread.join(timeout=1)

    def test_start_spawns_backend_when_port_is_free(self):
        fake_proc = MagicMock()
        fake_proc.pid = 4242

        with patch.object(start_backend, 'is_port_in_use', return_value=False), patch.object(start_backend, 'wait_for_backend_ready', return_value=True), patch.object(start_backend.subprocess, 'Popen', return_value=fake_proc):
            result = start_backend.start(extension_id='a' * 32)
            self.assertTrue(result['ok'])
            self.assertEqual(result['pid'], fake_proc.pid)
            self.assertEqual(result['port'], start_backend.DEFAULT_BACKEND_PORT)

    def test_start_returns_configured_port(self):
        fake_proc = MagicMock()
        fake_proc.pid = 5252

        with patch.dict('os.environ', {'MNS_BACKEND_PORT': '54321'}), patch.object(start_backend, 'is_port_in_use', return_value=False), patch.object(start_backend, 'wait_for_backend_ready', return_value=True), patch.object(start_backend.subprocess, 'Popen', return_value=fake_proc):
            result = start_backend.start(extension_id='a' * 32)
            self.assertEqual(result['port'], 54321)
