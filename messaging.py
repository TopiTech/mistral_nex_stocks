"""
messaging.py - SSE (Server-Sent Events) listener management.

Extracted from app_state.py to reduce module complexity.
Provides backpressure-aware message broadcasting to SSE listeners and the
sliding-window event replay log used for Last-Event-ID resume.
"""

import logging
import queue
import threading
from collections import deque
from contextlib import contextmanager
from typing import Any

from constants import MAX_SSE_LISTENERS, MAX_SSE_QUEUE_SIZE, SSE_EVENT_LOG_MAX

logger = logging.getLogger("backend")


class MessageAnnouncer:
    """Manages SSE listeners with backpressure control."""

    def __init__(self):
        self.listeners: list[queue.Queue[Any]] = []
        self.lock = threading.Lock()
        # Observability counters, incremented inside ``self.lock`` (read-
        # modify-write is not atomic under the GIL) and exposed via ``stats()``:
        #   * announced_count: listener-deliveries (len(targets) per announce).
        #   * dropped_count:   slow listeners removed due to queue overflow.
        self.announced_count = 0
        self.dropped_count = 0

    def listen(self, maxsize: int | None = None):
        """Register and return a new SSE listener queue."""
        q_maxsize = maxsize if maxsize is not None else MAX_SSE_QUEUE_SIZE
        q: queue.Queue[Any] = queue.Queue(maxsize=q_maxsize)
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
                logger.debug("SSE listener already removed from list")

    def announce(self, msg):
        """Broadcast a message to all listeners with backpressure.

        Lock scope is deliberately minimal: we snapshot the listener list and
        detect overloaded queues inside the lock, then perform the (potentially
        blocking) ``put_nowait`` outside it so a slow listener can never stall
        listener registration/unregistration (``listen``/``unlisten``) or other
        broadcasters.
        """
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
            logger.info(
                "SSE backpressure: dropped %d slow listener(s) due to queue overflow",
                len(overloaded),
            )

        with self.lock:
            self.announced_count += len(targets)
            self.dropped_count += len(overloaded)

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

    def close(self) -> None:
        """Close and unregister all active listeners, signaling stream termination."""
        with self.lock:
            for q in list(self.listeners):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        pass
            self.listeners.clear()

    def stats(self):
        """Return observability counters (listeners / announced / dropped)."""
        with self.lock:
            return {
                "listeners": len(self.listeners),
                "announced": self.announced_count,
                "dropped": self.dropped_count,
            }


class SSEEventLog:
    """Sliding-window buffer of recently emitted SSE events for Last-Event-ID replay.

    Every meaningful SSE event is assigned a globally monotonic sequence id and
    recorded here. On reconnect the client presents its last-seen id and the
    stream resumes from the buffered events instead of resending a full
    snapshot - provided the gap is still covered by the buffer.

    Entries are ``(seq, mode, kind, payload)`` tuples:
      * ``mode``: the SSE stream mode that emitted the event (1 or 2).
      * ``kind``: ``"frame"`` (verbatim SSE frame, e.g. mode-1 diff / full
        snapshot) or ``"delta"`` / ``"pts_delta"`` (mode 2: the delta's
        symbol keys; current engine values are resolved at replay time).
    """

    def __init__(self, maxlen: int | None = None):
        self._entries: deque[tuple[int, int, str, Any]] = deque(
            maxlen=maxlen if maxlen is not None else SSE_EVENT_LOG_MAX
        )
        self._lock = threading.Lock()
        self._seq = 0

    def next_id(self) -> int:
        """Allocate the next globally monotonic SSE event id."""
        with self._lock:
            self._seq += 1
            return self._seq

    def record(self, seq: int, mode: int, kind: str, payload: Any) -> None:
        """Record an emitted event (bounded by the sliding window)."""
        with self._lock:
            self._entries.append((seq, mode, kind, payload))

    def replay_after(self, last_id: int, mode: int):
        """Return buffered events with ``seq > last_id`` for the given mode.

        Returns:
          * ``None`` when the buffer no longer covers the gap (older entries
            were evicted, log has no history for this mode while last_id > 0,
            or last_id exceeds the current highest sequence) - the caller
            must fall back to a full snapshot;
          * ``[]`` when the client is fully caught up (nothing to replay);
          * a list of ``(seq, kind, payload)`` tuples otherwise.
        """
        with self._lock:
            if last_id > self._seq:
                return None
            same_mode = [
                (seq, kind, payload)
                for (seq, m, kind, payload) in self._entries
                if m == mode
            ]
        if not same_mode:
            return None if last_id > 0 else []
        oldest = same_mode[0][0]
        if last_id < oldest:
            return None
        return [(seq, kind, payload) for (seq, kind, payload) in same_mode if seq > last_id]

    def clear(self) -> None:
        """Drop all buffered events (used by tests / diagnostics)."""
        with self._lock:
            self._entries.clear()


# Global SSE event replay log shared by all stream connections.
sse_event_log = SSEEventLog()
