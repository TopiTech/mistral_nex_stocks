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


class SseListenerReservation:
    """One idempotent reservation in the process-wide SSE listener budget."""

    def __init__(self, limiter: "SseListenerLimiter"):
        self._limiter = limiter
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        """Return the reserved slot exactly once."""
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._limiter._release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


class SseListenerLimiter:
    """Atomically cap SSE connections across both streaming modes.

    ``MessageAnnouncer`` owns each mode's queues independently.  The HTTP
    endpoint needs one process-wide admission budget because either mode holds
    a Gunicorn gthread worker for the full connection lifetime.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reserved = 0

    def reserve(self) -> SseListenerReservation | None:
        """Reserve an SSE slot or return ``None`` when the global cap is full."""
        with self._lock:
            if self._reserved >= MAX_SSE_LISTENERS:
                return None
            self._reserved += 1
        return SseListenerReservation(self)

    def _release(self) -> None:
        with self._lock:
            if self._reserved > 0:
                self._reserved -= 1
            else:
                logger.warning("SSE listener reservation underflow prevented")

    def listener_count(self) -> int:
        """Return reserved connections, including streams not yet iterated."""
        with self._lock:
            return self._reserved

    def reset_for_testing(self) -> None:
        """Clear reservations after an isolated test; not used at runtime."""
        with self._lock:
            self._reserved = 0


class MessageAnnouncer:
    """Manages SSE listeners with backpressure control."""

    def __init__(self):
        self.listeners: list[queue.Queue[Any]] = []
        self._closed_queues: set[int] = set()
        self.lock = threading.Lock()
        # Observability counters, incremented inside ``self.lock`` (read-
        # modify-write is not atomic under the GIL) and exposed via ``stats()``:
        #   * announced_count: listener-deliveries (actual enqueued broadcasts).
        #   * dropped_count:   slow listeners removed due to queue overflow.
        self.announced_count = 0
        self.dropped_count = 0

    def _enqueue_close_sentinel(self, q: queue.Queue[Any]) -> None:
        """Guarantee a ``None`` sentinel reaches a closed listener queue."""
        budget = max(getattr(q, "maxsize", 0), 1) + 8
        for _ in range(budget):
            try:
                q.put_nowait(None)
                return
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
        logger.warning("SSE queue overflow persists: dropping sentinel for one listener")

    def listen(self, maxsize: int | None = None, *, enforce_limit: bool = True):
        """Register and return a new SSE listener queue.

        HTTP streams reserve the process-wide ``SseListenerLimiter`` before
        reaching this method, so they pass ``enforce_limit=False`` and have one
        authoritative admission decision across both modes.  Keep the local
        guard for direct/internal users of ``MessageAnnouncer``.
        """
        q_maxsize = maxsize if maxsize is not None else MAX_SSE_QUEUE_SIZE
        q: queue.Queue[Any] = queue.Queue(maxsize=q_maxsize)
        with self.lock:
            if enforce_limit and len(self.listeners) >= MAX_SSE_LISTENERS:
                raise RuntimeError("too many SSE listeners")
            self.listeners.append(q)
        return q

    def unlisten(self, q):
        """Unregister a listener queue."""
        with self.lock:
            self._closed_queues.discard(id(q))
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
            overloaded = [
                q for q in self.listeners if q.maxsize > 0 and q.qsize() >= q.maxsize
            ]
            for q_over in overloaded:
                try:
                    self.listeners.remove(q_over)
                    self._closed_queues.add(id(q_over))
                except ValueError:
                    pass
            targets = list(self.listeners)
            if overloaded:
                self.dropped_count += len(overloaded)

        for q_over in overloaded:
            self._enqueue_close_sentinel(q_over)

        if overloaded:
            logger.info(
                "SSE backpressure: dropped %d slow listener(s) due to queue overflow",
                len(overloaded),
            )

        delivered = 0
        for q_target in targets:
            with self.lock:
                if id(q_target) in self._closed_queues:
                    continue
            try:
                q_target.put_nowait(msg)
                delivered += 1
            except queue.Full:
                removed_now = False
                with self.lock:
                    try:
                        self.listeners.remove(q_target)
                        self._closed_queues.add(id(q_target))
                        removed_now = True
                        self.dropped_count += 1
                    except ValueError:
                        pass
                if not removed_now:
                    continue
                self._enqueue_close_sentinel(q_target)
                logger.info(
                    "SSE backpressure: dropped 1 listener mid-broadcast due to queue overflow"
                )

        if delivered:
            with self.lock:
                self.announced_count += delivered

    @contextmanager
    def listener_context(self, *, enforce_limit: bool = True):
        q = self.listen(enforce_limit=enforce_limit)
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
            closing = list(self.listeners)
            for q in closing:
                self._closed_queues.add(id(q))
            self.listeners.clear()
        for q in closing:
            self._enqueue_close_sentinel(q)

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

    Entries are ``(seq, kind, payload)`` tuples held in a per-mode buffer:
      * ``mode``: the SSE stream mode that emitted the event (1 or 2). Each
        mode owns an independent sliding window, so a high-frequency mode-2
        stream cannot evict a mode-1 client's history and force it into a
        needless full-snapshot resync (and vice versa).
      * ``kind``: ``"frame"`` (verbatim SSE frame, including mode-2 quote
        updates and snapshots). ``"delta"`` / ``"pts_delta"`` remain
        supported for backwards-compatible in-process callers, but new
        emissions should record immutable frames so replay does not depend on
        mutable engine state.

    ``seq`` remains globally monotonic across modes because a client's
    ``Last-Event-ID`` cursor is a single sequence shared by both modes.
    The id is masked to 32-bit unsigned (R2) so a long-running process does
    not send ever-growing integers to the browser.
    """

    # R2: wrap the monotonic counter at 2**32. The id is sent to the browser
    # verbatim in the SSE ``id:`` field and echoed back as ``Last-Event-ID``;
    # masking keeps the wire format bounded and prevents an unbounded
    # Python int from ever surfacing in the protocol.
    _SEQ_MASK = (1 << 32) - 1

    def __init__(self, maxlen: int | None = None):
        self._maxlen = maxlen if maxlen is not None else SSE_EVENT_LOG_MAX
        self._entries: dict[int, deque[tuple[int, str, Any]]] = {}
        self._lock = threading.Lock()
        self._seq = 0
        # R2: per-mode timestamp for evicting deques that have not been
        # written to for an extended period. Long-running servers that switch
        # modes (e.g. start in mode 1, later switch to mode 2) will release
        # the idle deque instead of leaving it in the process heap forever.
        self._last_used: dict[int, float] = {}

    def _buffer_for(self, mode: int) -> deque[tuple[int, str, Any]]:
        """Return the sliding window for *mode* (created on first use).

        Caller MUST hold ``self._lock``.
        """
        buf = self._entries.get(mode)
        if buf is None:
            buf = deque(maxlen=self._maxlen)
            self._entries[mode] = buf
        return buf

    def _next_seq_locked(self) -> int:
        """Advance and return the 32-bit unsigned sequence ID in ``1.._SEQ_MASK``.

        Skipping ``0`` prevents wrap-around IDs from colliding with the
        protocol sentinel ``last_id == 0`` (no prior event). Caller MUST hold ``self._lock``.
        """
        nxt = (self._seq + 1) & self._SEQ_MASK
        if nxt == 0:
            nxt = 1
        self._seq = nxt
        return nxt

    @classmethod
    def _seq_age(cls, seq: int, head: int) -> int:
        """Return circular distance behind *head* in ``1.._SEQ_MASK``."""
        return (head - seq) % cls._SEQ_MASK

    def next_id(self) -> int:
        """Allocate the next globally monotonic SSE event id.

        R2: the returned id wraps in ``1.._SEQ_MASK`` (32-bit unsigned non-zero)
        so a long-running process stays protocol-bounded without colliding
        with sentinel ``last_id == 0``.
        """
        with self._lock:
            return self._next_seq_locked()

    def record(self, seq: int, mode: int, kind: str, payload: Any) -> None:
        """Record an emitted event (bounded by the sliding window).

        R2: also track last-used timestamp so a long-running process can
        evict deques for modes that have not received events in a while.
        """
        with self._lock:
            self._buffer_for(mode).append((seq, kind, payload))
            import time as _time

            self._last_used[mode] = _time.time()

    def emit(self, mode: int, kind: str, payload: Any) -> int:
        """Allocate the next sequence id and record the event atomically.

        Splitting allocation (``next_id``) from recording (``record``) lets two
        broadcaster threads interleave and store their ids out of order inside
        the per-mode buffer, so ``replay_after`` would hand a reconnecting
        client its frames in the wrong order (and mis-report ``oldest``).
        Emitters that always record what they allocate must use this method;
        ``next_id``/``record`` remain for callers that intentionally allocate
        without recording (e.g. simulated gaps in tests).
        """
        with self._lock:
            seq = self._next_seq_locked()
            self._buffer_for(mode).append((seq, kind, payload))
            import time as _time

            self._last_used[mode] = _time.time()
            return seq

    def contains(self, seq: int, mode: int) -> bool:
        """Return whether *seq* is an actually recorded event for *mode*."""
        with self._lock:
            return any(
                entry_seq == seq for entry_seq, _kind, _payload in self._entries.get(mode, ())
            )

    def _gc_idle_modes(
        self,
        now: float,
        ttl_sec: float,
        exclude_mode: int | str | None = None,
    ) -> None:
        """Drop deques for modes that have not been written to in *ttl_sec*.

        Caller MUST hold ``self._lock``.
        """
        if ttl_sec <= 0:
            return
        stale = [
            m
            for m, ts in self._last_used.items()
            if m != exclude_mode and now - ts > ttl_sec
        ]
        for m in stale:
            self._entries.pop(m, None)
            self._last_used.pop(m, None)

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
            import time as _time

            # R2: opportunistically evict deques for modes that have been
            # silent for 5 minutes. This prevents an idle mode-1 deque from
            # leaking forever in a server that only ever uses mode 2, while
            # never evicting the mode currently being replayed.
            self._gc_idle_modes(_time.time(), ttl_sec=300.0, exclude_mode=mode)
            head = self._seq
            same_mode = list(self._entries.get(mode, ()))
        if last_id < 0 or last_id > self._SEQ_MASK:
            return None
        if head == 0 or not same_mode:
            return None if last_id > 0 else []
        if last_id == 0:
            return None
        oldest_age = self._seq_age(same_mode[0][0], head)
        last_age = self._seq_age(last_id, head)
        if last_age > oldest_age:
            return None
        return [
            (seq, kind, payload)
            for (seq, kind, payload) in same_mode
            if self._seq_age(seq, head) < last_age
        ]

    def clear(self) -> None:
        """Drop all buffered events (used by tests / diagnostics)."""
        with self._lock:
            self._entries.clear()
            self._last_used.clear()


# Global SSE event replay log shared by all stream connections.
sse_event_log = SSEEventLog()
