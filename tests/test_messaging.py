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
    # Fill the bounded queue (maxsize=5) without consuming
    for _ in range(5):
        q.put_nowait("old")
    ann.announce("new")
    # Backpressure path drops the slow listener and injects a None sentinel
    # after evicting one buffered item; the sentinel should be present.
    items = [q.get(timeout=1) for _ in range(5)]
    assert None in items
