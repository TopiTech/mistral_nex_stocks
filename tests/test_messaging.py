"""Tests for messaging.py — SSE MessageAnnouncer backpressure and delivery."""

import queue

from messaging import MessageAnnouncer


def test_listen_registers_listener():
    ann = MessageAnnouncer()
    q = ann.listen()
    assert isinstance(q, queue.Queue)
    assert ann.listener_count() == 1


def test_unlisten_removes_listener():
    ann = MessageAnnouncer()
    q = ann.listen()
    ann.unlisten(q)
    assert ann.listener_count() == 0


def test_announce_delivers_to_listener():
    ann = MessageAnnouncer()
    q = ann.listen()
    ann.announce("payload")
    assert q.get(timeout=1) == "payload"


def test_listener_context_adds_and_removes():
    ann = MessageAnnouncer()
    with ann.listener_context() as q:
        assert ann.listener_count() == 1
        ann.announce("data")
        assert q.get(timeout=1) == "data"
    assert ann.listener_count() == 0


def test_announce_backpressure_drops_full_listener():
    ann = MessageAnnouncer()
    q = ann.listen()
    # Fill the bounded queue (maxsize=q.maxsize) without consuming
    for _ in range(q.maxsize):
        q.put_nowait("old")
    ann.announce("new")
    # Backpressure path drops the slow listener and injects a None sentinel
    # after evicting one buffered item; the sentinel should be present.
    items = [q.get(timeout=1) for _ in range(q.maxsize)]
    assert None in items


def test_announce_tracks_observability_counters():
    ann = MessageAnnouncer()
    q1 = ann.listen()
    q2 = ann.listen()
    ann.announce("m")
    assert q1.get(timeout=1) == "m"
    assert q2.get(timeout=1) == "m"

    stats = ann.stats()
    assert stats["listeners"] == 2
    assert stats["announced"] == 2
    assert stats["dropped"] == 0


def test_announce_counts_dropped_listeners():
    """Backpressure drops must increment the dropped counter, not announced."""
    ann = MessageAnnouncer()
    q = ann.listen()
    for _ in range(q.maxsize):
        q.put_nowait("old")
    ann.announce("new")

    stats = ann.stats()
    assert stats["dropped"] >= 1
    # The overloaded listener is removed before broadcast, so no message is
    # announced to any surviving target.
    assert stats["announced"] == 0
    assert stats["listeners"] == 0
