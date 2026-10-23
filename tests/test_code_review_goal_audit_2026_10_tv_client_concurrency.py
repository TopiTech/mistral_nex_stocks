"""Concurrency and lock-separation regression test for TradingViewWSClient.

Verifies:
- self.lock is not held during _lifecycle_lock acquisition or _safe_send.
- Concurrent add_symbol and remove_symbol operations do not deadlock.
- Duplicate additions/removals are idempotent.
"""

import threading
import unittest
from unittest.mock import MagicMock

from services.realtime.tv_client import TradingViewWSClient


class TestTradingViewWSClientLockSeparation(unittest.TestCase):
    def setUp(self):
        self.client = TradingViewWSClient()
        self.client.running = True
        self.client.connected = True
        self.client.session_id = "qs_testsession"
        self.client.ws = MagicMock()

    def test_add_symbol_does_not_hold_lock_during_send(self):
        """self.lock must be released before _safe_send is invoked."""
        lock_held_during_send = None

        def mock_safe_send(ws, msg):
            nonlocal lock_held_during_send
            # Try to acquire self.lock with timeout 0; if self.lock is NOT held,
            # acquire will succeed.
            acquired = self.client.lock.acquire(blocking=False)
            if acquired:
                lock_held_during_send = False
                self.client.lock.release()
            else:
                lock_held_during_send = True

        self.client._safe_send = mock_safe_send
        self.client.add_symbol("NVDA")

        self.assertIn("NVDA", self.client.symbols)
        self.assertFalse(lock_held_during_send, "self.lock was held during _safe_send")

    def test_remove_symbol_does_not_hold_lock_during_send(self):
        """self.lock must be released before _safe_send is invoked in remove_symbol."""
        self.client.symbols.add("AAPL")
        self.client._last_quotes["AAPL"] = {"price": 150.0}

        lock_held_during_send = None

        def mock_safe_send(ws, msg):
            nonlocal lock_held_during_send
            acquired = self.client.lock.acquire(blocking=False)
            if acquired:
                lock_held_during_send = False
                self.client.lock.release()
            else:
                lock_held_during_send = True

        self.client._safe_send = mock_safe_send
        self.client.remove_symbol("AAPL")

        self.assertNotIn("AAPL", self.client.symbols)
        self.assertNotIn("AAPL", self.client._last_quotes)
        self.assertFalse(lock_held_during_send, "self.lock was held during _safe_send")

    def test_idempotent_duplicate_add_and_remove(self):
        """Duplicate adds and removes should not send extra network messages."""
        sent_messages = []
        self.client._safe_send = lambda ws, msg: sent_messages.append(msg)

        self.client.add_symbol("MSFT")
        self.assertEqual(len(sent_messages), 1)

        # Duplicate add
        self.client.add_symbol("MSFT")
        self.assertEqual(len(sent_messages), 1, "Duplicate add sent redundant message")

        # First remove
        self.client.remove_symbol("MSFT")
        self.assertEqual(len(sent_messages), 2)

        # Duplicate remove
        self.client.remove_symbol("MSFT")
        self.assertEqual(len(sent_messages), 2, "Duplicate remove sent redundant message")

    def test_concurrent_add_and_remove_threads(self):
        """Concurrent add_symbol and remove_symbol across multiple threads must not deadlock."""
        self.client._safe_send = lambda ws, msg: None

        def worker(sym):
            for _ in range(50):
                self.client.add_symbol(sym)
                self.client.remove_symbol(sym)

        threads = [threading.Thread(target=worker, args=(f"SYM{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "Worker thread hung or deadlocked")


if __name__ == "__main__":
    unittest.main()
