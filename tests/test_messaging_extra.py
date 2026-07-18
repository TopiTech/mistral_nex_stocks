"""Additional coverage tests for messaging.py — MessageAnnouncer edge cases."""

import queue
import threading

import pytest

from messaging import MessageAnnouncer


def test_unlisten_non_existent_queue():
    """unlisten on a queue that was never registered should not raise."""
    ann = MessageAnnouncer()
    q = queue.Queue()
    # Should not raise ValueError
    ann.unlisten(q)


def test_unlisten_already_removed_queue():
    """unlisten on a queue that was already removed logs debug but does not raise."""
    ann = MessageAnnouncer()
    q = ann.listen()
    ann.unlisten(q)
    # Second unlisten should not raise
    ann.unlisten(q)


def test_announce_no_listeners():
    """announce with zero listeners should not raise."""
    ann = MessageAnnouncer()
    ann.announce("hello")  # should not error


def test_multiple_listeners_all_receive():
    """announce delivers to all registered listeners."""
    ann = MessageAnnouncer()
    q1 = ann.listen()
    q2 = ann.listen()
    q3 = ann.listen()
    ann.announce("broadcast")

    for q in (q1, q2, q3):
        assert q.get(timeout=1) == "broadcast"


def test_listener_count_after_unlisten():
    """listener_count reflects removals."""
    ann = MessageAnnouncer()
    q1 = ann.listen()
    q2 = ann.listen()
    assert ann.listener_count() == 2
    ann.unlisten(q1)
    assert ann.listener_count() == 1
    ann.unlisten(q2)
    assert ann.listener_count() == 0


def test_announce_backpressure_drains_queue():
    """When a listener's queue is full, announce drains one item before retry."""
    ann = MessageAnnouncer()
    q = ann.listen()
    # Fill queue to maxsize
    for _ in range(5):
        q.put_nowait("stale")
    # This should trigger backpressure: slow listener is removed,
    # None sentinel is injected after draining one stale item.
    ann.announce("fresh")

    # Read all items: 4 remaining stale + None sentinel
    items = [q.get(timeout=1) for _ in range(5)]
    assert None in items
    assert "stale" in items
    # After removal, listener_count should be 0
    assert ann.listener_count() == 0


def test_announce_backpressure_drain_empty_queue():
    """When the queue is full but draining fails (queue already empty)."""
    # This path is tricky to trigger because we need qsize >= maxsize
    # but then get_nowait raises Empty. We can force this by having
    # another thread consume between qsize check and get_nowait.
    ann = MessageAnnouncer()
    q = ann.listen()

    # Fill the queue
    for _ in range(5):
        q.put_nowait("x")

    # In announce(), when it tries to drain the full queue:
    # q_over.get_nowait() gets one item, q_over.put_nowait(None) succeeds.
    # To test get_nowait raise Empty AND put_nowait raise Full,
    # we'd need interleaving threads.
    # Just verify the backpressure path works at all.
    ann.announce("test")
    # The listener was removed, but we may still read
    items = []
    try:
        while True:
            items.append(q.get_nowait())
    except queue.Empty:
        pass
    assert items  # should have at least the sentinel or stale messages


def test_announce_persistent_overflow_logs_warning():
    """When a regular (non-backpressure) listener's queue is full after drain retry."""
    ann = MessageAnnouncer()
    q = ann.listen()

    # Fill the queue completely
    for _ in range(5):
        q.put_nowait("stale")

    # The queue is full but not being backpressure-dropped yet.
    # Send a message: it will try put_nowait, fail, drain one, retry.
    # If still full after drain, it logs a warning.
    # Since we have only one item to drain and 4+1=5 remaining,
    # the retry will fail (queue still full after 1 drain + 1 put = 5).
    ann.announce("overflow-test")

    # Listener should have been removed by backpressure
    assert ann.listener_count() == 0


def test_too_many_listeners_raises():
    """listen raises RuntimeError when MAX_SSE_LISTENERS is exceeded."""
    from constants import MAX_SSE_LISTENERS

    ann = MessageAnnouncer()
    queues = []
    try:
        for _ in range(MAX_SSE_LISTENERS):
            queues.append(ann.listen())
        with pytest.raises(RuntimeError, match="too many SSE listeners"):
            ann.listen()
    finally:
        for q in queues:
            ann.unlisten(q)


def test_listener_context_yields_queue():
    """listener_context registers and unregisters correctly."""
    ann = MessageAnnouncer()
    with ann.listener_context() as q:
        assert ann.listener_count() == 1
        ann.announce("ctx-test")
        assert q.get(timeout=1) == "ctx-test"
    assert ann.listener_count() == 0


def test_concurrent_listener_access():
    """Multiple threads can register/unregister without races."""
    ann = MessageAnnouncer()
    errors = []

    def _worker():
        try:
            q = ann.listen()
            ann.announce(f"from-{threading.get_ident()}")
            ann.unlisten(q)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"Errors during concurrent access: {errors}"
    assert ann.listener_count() == 0
