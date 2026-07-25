"""Coverage tests for execution_state.py and shutdown_manager.py."""

import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from execution_state import ExecutionState
from shutdown_manager import ShutdownTokenManager


class ExecutionStateCoverageTests(unittest.TestCase):
    def setUp(self):
        self.state = ExecutionState()

    def tearDown(self):
        self.state.shutdown()

    def test_executor_stats_handles_normal_and_exception_paths(self):
        # Normal executor
        stats = self.state.executor_stats(self.state.executor)
        self.assertIn("max_queue_size", stats)
        self.assertIn("pending", stats)

        # Mock executor with internal queue
        mock_ex = MagicMock()
        mock_ex._max_queue_size = 20
        mock_queue = MagicMock()
        mock_queue.qsize.return_value = 5
        mock_ex._queue = mock_queue

        stats = self.state.executor_stats(mock_ex)
        self.assertEqual(stats["max_queue_size"], 20)
        self.assertEqual(stats["pending"], 5)

        # Mock executor throwing exception on attribute access
        faulty_ex = MagicMock()
        type(faulty_ex)._max_queue_size = property(
            fget=MagicMock(side_effect=RuntimeError("fault"))
        )
        stats = self.state.executor_stats(faulty_ex)
        self.assertEqual(stats, {"max_queue_size": 0, "pending": 0})

    def test_safe_submit_handles_all_branches(self):
        # 1. Invalid executor name
        self.assertFalse(self.state.safe_submit("non_existent_executor", lambda: None))

        # 2. Successful submission
        executed = []
        res = self.state.safe_submit("executor", lambda: executed.append(True))
        self.assertTrue(res)

        # 3. queue.Full backpressure
        with patch.object(self.state.executor, "submit", side_effect=queue.Full):
            self.assertFalse(self.state.safe_submit("executor", lambda: None))

        # 4. RuntimeError shutdown exception
        with patch.object(
            self.state.executor, "submit", side_effect=RuntimeError("cannot schedule")
        ):
            self.assertFalse(self.state.safe_submit("executor", lambda: None))


class ShutdownManagerCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.mgr = ShutdownTokenManager()
        self.mgr.token_file = self.tmp_path / ".mns_shutdown_token"
        self.mgr.used_marker = self.tmp_path / ".mns_shutdown_token.used"

    def tearDown(self):
        self.tmp.cleanup()

    def test_token_creation_consumption_and_validation(self):
        # Create token
        token1 = self.mgr.get_or_create_shutdown_token()
        self.assertTrue(token1)
        self.assertEqual(self.mgr.get_or_create_shutdown_token(), token1)

        # Validate token
        self.assertTrue(self.mgr.validate_shutdown_token(token1))
        self.assertFalse(self.mgr.validate_shutdown_token("invalid-token"))
        self.assertFalse(self.mgr.validate_shutdown_token(""))

        # Consume token
        self.assertTrue(self.mgr.consume_shutdown_token(token1))
        self.assertTrue(self.mgr.shutdown_token_used)
        self.assertFalse(self.mgr.consume_shutdown_token(token1))

    def test_rotate_shutdown_token(self):
        t1 = self.mgr.get_or_create_shutdown_token()
        self.mgr.commit_shutdown_token()
        self.mgr.rotate_shutdown_token()
        self.assertTrue(self.mgr.used_marker.exists())
        t2 = self.mgr.get_or_create_shutdown_token()
        self.assertNotEqual(t1, t2)

    def test_token_file_read_write_exceptions(self):
        # Mock file write error
        with patch.object(Path, "write_text", side_effect=OSError("Disk full")):
            t = self.mgr.get_or_create_shutdown_token()
            self.assertTrue(t)

        # Mock read error when loading from file
        mgr2 = ShutdownTokenManager()
        mgr2.token_file = self.tmp_path / ".mns_shutdown_token_bad"
        mgr2.token_file.write_text("invalid_raw", encoding="utf-8")

        with patch("json.loads", side_effect=ValueError("Invalid JSON")):
            t2 = mgr2.get_or_create_shutdown_token()
            self.assertTrue(t2)
