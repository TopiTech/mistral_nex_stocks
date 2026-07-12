import os
import unittest
import queue
import sqlite3
import time
from unittest.mock import MagicMock, patch
from flask import Flask

from utils.networking import _is_local_request
from utils.threading import DaemonThreadPoolExecutor
from utils.chat_history import SQLiteChatHistoryStore


class TestProductionImprovements(unittest.TestCase):
    def setUp(self):
        self.app = Flask("test_app")
        self.app.config["WTF_CSRF_ENABLED"] = False

    def test_is_local_request_host_spoofing_prod(self):
        """_is_local_request must reject localhost Host header spoofing in production."""
        with patch.dict(os.environ, {"MNS_PROD": "1", "MNS_ALLOW_REMOTE_API": "0"}):
            req = MagicMock()
            req.environ = {"RAW_REMOTE_ADDR": "127.0.0.1", "REMOTE_ADDR": "127.0.0.1"}
            req.headers = {"Host": "localhost"}
            # Host: localhost must be rejected in prod to prevent spoofing through proxies
            self.assertFalse(_is_local_request(req))

    def test_is_local_request_forwarded_for_prod(self):
        """_is_local_request must reject requests with X-Forwarded-For in production."""
        with patch.dict(os.environ, {"MNS_PROD": "1", "MNS_ALLOW_REMOTE_API": "0"}):
            req = MagicMock()
            req.environ = {"RAW_REMOTE_ADDR": "127.0.0.1", "REMOTE_ADDR": "127.0.0.1"}
            # X-Forwarded-For is set, so it's a proxy-forwarded external request
            req.headers = {"Host": "myapp.com", "X-Forwarded-For": "127.0.0.1"}
            self.assertFalse(_is_local_request(req))

    def test_is_local_request_allowed_in_dev(self):
        """_is_local_request must allow loopback access in development."""
        with patch.dict(os.environ, {"MNS_PROD": "0", "MNS_ALLOW_REMOTE_API": "0"}):
            req = MagicMock()
            req.environ = {"RAW_REMOTE_ADDR": "127.0.0.1", "REMOTE_ADDR": "127.0.0.1"}
            req.headers = {"Host": "localhost"}
            self.assertTrue(_is_local_request(req))

    def test_bounded_thread_pool_executor_full(self):
        """DaemonThreadPoolExecutor with max_queue_size must raise queue.Full when overloaded."""
        # Spawn executor with 1 worker and 1 max queue slot
        executor = DaemonThreadPoolExecutor(max_workers=1, max_queue_size=1)

        # Block the single worker
        executor.submit(lambda: time.sleep(0.5))
        # Fill the queue slot
        executor.submit(lambda: time.sleep(0.1))

        # Third submit must raise queue.Full as max_workers=1 and max_queue_size=1 (total capacity 2)
        with self.assertRaises(queue.Full):
            executor.submit(lambda: 3)

        # Clean shutdown
        executor.shutdown(wait=False, cancel_futures=True)

    def test_sqlite_locked_retry_success(self):
        """SQLiteChatHistoryStore should retry on database is locked errors and succeed."""
        store = SQLiteChatHistoryStore(max_sessions=5)

        call_count = [0]
        def mock_callback(conn, cursor):
            call_count[0] += 1
            if call_count[0] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "success"

        with patch("time.sleep") as mock_sleep:
            res = store._execute_in_transaction(mock_callback)
            self.assertEqual(res, "success")
            self.assertEqual(call_count[0], 3)
            self.assertEqual(mock_sleep.call_count, 2)

    def test_sqlite_locked_retry_failures(self):
        """SQLiteChatHistoryStore should fail if lock persists after max retries."""
        store = SQLiteChatHistoryStore(max_sessions=5)

        def mock_callback(conn, cursor):
            raise sqlite3.OperationalError("database is locked")

        with patch("time.sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                store._execute_in_transaction(mock_callback)
