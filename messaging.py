"""
messaging.py - SSE (Server-Sent Events) listener management.

Extracted from app_state.py to reduce module complexity.
Provides backpressure-aware message broadcasting to SSE listeners.
"""

import logging
import queue
import threading
from contextlib import contextmanager
from typing import Any

from constants import MAX_SSE_LISTENERS

logger = logging.getLogger("backend")


class MessageAnnouncer:
    """Manages SSE listeners with backpressure control."""

    def __init__(self):
        self.listeners: list[queue.Queue[Any]] = []
        self.lock = threading.Lock()

    def listen(self):
        """Register and return a new SSE listener queue."""
        q: queue.Queue[Any] = queue.Queue(maxsize=5)
        with self.lock:
            if len(self.listeners) >= MAX_SSE_LISTENERS:
                raise RuntimeError("too many SSE listeners")
            self.listeners.append(q)
        return q

    def unlisten(self, q):
        """Unregister a listener queue."""
        with self.lock:
            try:
                self.listeners.remove(q)
            except ValueError:
                pass

    def announce(self, msg):
        """Broadcast a message to all listeners with backpressure."""
        with self.lock:
            overloaded = [q for q in self.listeners if q.qsize() >= q.maxsize]
            for q_over in overloaded:
                try:
                    self.listeners.remove(q_over)
                    try:
                        q_over.put_nowait(None)
                    except queue.Full:
                        try:
                            q_over.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            q_over.put_nowait(None)
                        except queue.Full:
                            pass
                except ValueError:
                    pass
            targets = list(self.listeners)

        if overloaded:
            logger.warning(
                "SSE backpressure: dropped %d slow listener(s) due to queue overflow",
                len(overloaded),
            )

        for q_target in targets:
            try:
                q_target.put_nowait(msg)
            except queue.Full:
                try:
                    q_target.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q_target.put_nowait(msg)
                except queue.Full:
                    logger.warning(
                        "SSE queue overflow persists: dropping latest message for one listener"
                    )

    @contextmanager
    def listener_context(self):
        q = self.listen()
        try:
            yield q
        finally:
            self.unlisten(q)

    def listener_count(self):
        """Return current number of listeners."""
        with self.lock:
            return len(self.listeners)
