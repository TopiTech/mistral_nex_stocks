"""tests/test_sse_replay.py - SSE Last-Event-ID replay, cursor seeding, keepalive, and worker guards.

Covers the review fixes:
  * ``SSEEventLog`` sliding-window replay buffer (Last-Event-ID resume).
  * ``RealtimeMarketEngine.register_client`` seeding (no duplicate full-store
    dump right after the SSE initial snapshot).
  * mode-1 no-change ticks announcing a comment keepalive instead of a
    repeated (diff/full) payload.
  * PTS / Yahoo JP worker-generation (epoch) guards against duplicate loops
    after stop()→start() (engine restart).
"""

import json
import re
import time
from unittest.mock import patch

from app_state import app_state
from messaging import SSEEventLog, sse_event_log
from services.realtime_engine import RealtimeMarketEngine

# ---------------------------------------------------------------------------
# SSEEventLog (replay buffer)
# ---------------------------------------------------------------------------


def test_sse_event_log_next_id_monotonic():
    log = SSEEventLog(maxlen=10)
    ids = [log.next_id() for _ in range(5)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5


def test_sse_event_log_replay_after_semantics():
    log = SSEEventLog(maxlen=10)
    s1 = log.next_id()
    log.record(s1, 1, "frame", "data: a\n\n")
    s2 = log.next_id()
    log.record(s2, 1, "frame", "data: b\n\n")
    s3 = log.next_id()
    log.record(s3, 2, "delta", ("AAPL",))
    s4 = log.next_id()
    log.record(s4, 2, "delta", ("MSFT",))

    # Mode 1: replay everything after s1 → only s2.
    assert log.replay_after(s1, 1) == [(s2, "frame", "data: b\n\n")]
    # Fully caught up (no mode-1 entry after s3) → [].
    assert log.replay_after(s3, 1) == []
    # Gap: last_id older than the oldest buffered mode-1 entry → None.
    assert log.replay_after(0, 1) is None
    # Mode filtering: mode-2 entries are only visible to mode-2 clients; the
    # gap check is per-mode (s2 < s3 is a mode-2 gap because the client's own
    # mode-2 history starts at s3).
    assert log.replay_after(s3, 2) == [(s4, "delta", ("MSFT",))]
    assert log.replay_after(s2, 2) is None
    # A mode with no history when last_id > 0 is treated as a gap (full snapshot required)
    assert log.replay_after(1, 3) is None
    # A mode with no history when last_id == 0 yields []
    assert log.replay_after(0, 3) == []
    # last_id beyond current seq yields None (gap)
    assert log.replay_after(999, 1) is None

    # Empty log behavior:
    empty_log = SSEEventLog(maxlen=10)
    assert empty_log.replay_after(0, 1) == []
    assert empty_log.replay_after(10, 1) is None


def test_sse_event_log_bounded_window():
    log = SSEEventLog(maxlen=2)
    s1 = log.next_id()
    log.record(s1, 1, "frame", "a")
    s2 = log.next_id()
    log.record(s2, 1, "frame", "b")
    s3 = log.next_id()
    log.record(s3, 1, "frame", "c")
    # Oldest entry was evicted → s1 is a gap now.
    assert log.replay_after(s1, 1) is None
    assert log.replay_after(s2, 1) == [(s3, "frame", "c")]


def test_sse_event_log_contains_distinguishes_unrecorded_ids():
    log = SSEEventLog(maxlen=10)
    recorded = log.next_id()
    log.record(recorded, 2, "frame", "event: heartbeat\ndata: {}\n\n")
    unrecorded = log.next_id()
    assert log.contains(recorded, 2) is True
    assert log.contains(unrecorded, 2) is False


# ---------------------------------------------------------------------------
# Cursor seeding (register_client)
# ---------------------------------------------------------------------------


def _payload(symbol, price, change=0.0):
    return {
        "symbol": symbol,
        "price": price,
        "change": change,
        "change_percent": 0.0,
        "volume": 0,
        "source": "tradingview",
        "updated_at": time.time(),
    }


def _pts_payload(symbol, price):
    return {
        "symbol": symbol,
        "price": price,
        "change": -9.6,
        "change_percent": -0.32,
        "volume": 600,
        "source": "yahoojp_pts",
        "pts": True,
        "pts_trading": True,
        "pts_time": "17:03",
        "updated_at": time.time(),
    }


def test_register_client_seeds_cursor_with_current_snapshot():
    """A registered cursor is seeded with the current store, so the first poll
    does not re-dump the full store (the SSE initial snapshot carried it)."""
    engine = RealtimeMarketEngine()
    engine._handle_producer_update(_payload("AAPL", 220.0, 1.0))
    cid = engine.register_client()

    # Seeded: no duplicate full-store delivery on the first poll.
    assert engine.get_market_deltas(cid) == {}
    # Updates after registration are still delivered per-client.
    engine._handle_producer_update(_payload("AAPL", 221.0, 2.0))
    deltas = engine.get_market_deltas(cid)
    assert deltas["AAPL"]["price"] == 221.0
    assert engine.get_market_deltas(cid) == {}

    engine.unregister_client(cid)


def test_register_client_seeds_pts_cursor():
    engine = RealtimeMarketEngine()
    engine._handle_pts_update(_pts_payload("7203.T", 2973.9))
    cid = engine.register_client()

    assert engine.get_pts_deltas(cid) == {}
    engine._handle_pts_update(_pts_payload("7203.T", 2970.0))
    assert engine.get_pts_deltas(cid)["7203.T"]["price"] == 2970.0

    engine.unregister_client(cid)


def test_register_client_on_empty_engine_first_scan_empty():
    """An empty engine still yields an empty first scan (no behavior change)."""
    engine = RealtimeMarketEngine()
    cid = engine.register_client()
    assert engine.get_market_deltas(cid) == {}
    engine.unregister_client(cid)


# ---------------------------------------------------------------------------
# Mode-1 keepalive on no-change ticks
# ---------------------------------------------------------------------------


def test_announce_emits_keepalive_when_unchanged():
    """No-change ticks must announce a comment keepalive, not a repeated
    (diff/full) payload (mode 1)."""
    import app_bg

    with (
        patch.object(app_state.sse_announcer, "announce") as mock_announce,
        patch("app_bg.is_market_open", return_value=False),
    ):
        app_bg._sse_full_snapshot_counter = 5  # force a full snapshot first call
        app_bg._invalidate_sse_payload_cache()
        app_bg._original_announce_current_market_state()
        # Frames are announced as ``(seq, frame)`` tuples (app_bg._announce_frame).
        first = mock_announce.call_args[0][0][1]
        assert first.startswith("data: ")

        mock_announce.reset_mock()
        # Nothing changed → the next tick is a comment keepalive (plain string).
        app_bg._original_announce_current_market_state()
        second = mock_announce.call_args[0][0]
        assert second == ": keepalive\n\n"


# ---------------------------------------------------------------------------
# Worker-generation (epoch) guards
# ---------------------------------------------------------------------------


def test_pts_epoch_bumped_by_start_and_stop():
    """start()/stop() must bump the PTS epoch so a lingering worker loop from a
    previous cycle terminates at its next check (no duplicate PTS polling)."""
    engine = RealtimeMarketEngine()
    e0 = engine._pts_epoch
    # Avoid spawning real producer threads / network connections in tests.
    with (
        patch.object(engine.tv_client, "start"),
        patch.object(engine.yahoojp_scraper, "start"),
        patch.object(engine.tv_client, "stop"),
        patch.object(engine.yahoojp_scraper, "stop"),
    ):
        engine.start()
        try:
            assert engine._pts_epoch == e0 + 1
            assert engine.pts_thread is not None and engine.pts_thread.is_alive()
        finally:
            engine.stop()
    assert engine._pts_epoch == e0 + 2


def test_yahoojp_epoch_bumped_by_start_and_stop():
    """Yahoo JP scraper start()/stop() bump its worker generation too."""
    from services.realtime_engine import YahooJPRealtimeScraper

    scraper = YahooJPRealtimeScraper()
    with patch.object(scraper, "_worker_loop") as mock_loop:
        scraper.start()
        try:
            assert scraper._epoch == 1
            assert mock_loop.called  # a worker did start
            # Already running → start() is a no-op and must not bump again.
            scraper.start()
            assert scraper._epoch == 1
        finally:
            scraper.stop()
        assert scraper._epoch == 2


# ---------------------------------------------------------------------------
# Replay frame reconstruction
# ---------------------------------------------------------------------------


def test_replay_frame_for_entry_resolves_mode2_deltas():
    from routes.api_stocks import _replay_frame_for_entry
    from services.realtime_engine import realtime_market_engine

    engine = realtime_market_engine
    with engine.store_lock:
        engine.market_store["ZZTEST"] = {
            "symbol": "ZZTEST",
            "price": 12.5,
            "change": 0.1,
            "change_percent": 0.8,
            "volume": 10,
            "source": "tradingview",
            "updated_at": time.time(),
        }
    try:
        frame = _replay_frame_for_entry(42, "delta", ("ZZTEST",), 2)
        assert frame is not None
        assert frame.startswith("id: 42\n")
        assert "event: realtime_update" in frame
        assert "ZZTEST" in frame
        payload = json.loads(frame.split("data: ", 1)[1])
        assert payload["deltas"]["ZZTEST"]["price"] == 12.5

        # Verbatim frame entries replay in any mode.
        assert _replay_frame_for_entry(43, "frame", "data: x\n\n", 1) == "id: 43\ndata: x\n\n"

        # Mode-1 clients never receive mode-2 delta entries.
        assert _replay_frame_for_entry(44, "delta", ("ZZTEST",), 1) is None
        # Symbols no longer in the store resolve to nothing.
        assert _replay_frame_for_entry(45, "delta", ("NOPE",), 2) is None
    finally:
        with engine.store_lock:
            engine.market_store.pop("ZZTEST", None)


def test_mode2_live_initial_frame_is_recorded_for_resume(client):
    """Initial snapshot ids must be replay-log entries, not cursor holes."""
    sse_event_log.clear()
    try:
        response = client.get(
            "/api/stocks/stream?mode=2",
            headers={"Origin": "http://localhost:5000"},
        )
        first = next(response.response).decode("utf-8")
        first_id = _extract_frame_id(first)
        assert sse_event_log.contains(first_id, 2)
        response.response.close()
    finally:
        sse_event_log.clear()


def test_mode2_reconnect_from_unrecorded_cursor_forces_initial_snapshot(client):
    """A legacy/unrecorded cursor must not be treated as caught up."""
    sse_event_log.clear()
    try:
        seq = sse_event_log.next_id()
        # Deliberately do not record seq: this models an id emitted by the old
        # initial/heartbeat/periodic-snapshot paths.
        response = client.get(
            f"/api/stocks/stream?mode=2&last_event_id={seq}",
            headers={"Origin": "http://localhost:5000"},
        )
        first = next(response.response).decode("utf-8")
        assert "initial_snapshot" in first
        response.response.close()
    finally:
        sse_event_log.clear()


def test_mode2_live_delta_id_is_recorded_as_immutable_frame(client):
    """Mode-2 live deltas must replay their exact emitted payload."""
    from services.realtime_engine import realtime_market_engine

    sse_event_log.clear()
    with realtime_market_engine.store_lock:
        saved_market_store = dict(realtime_market_engine.market_store)
        saved_pts_store = dict(realtime_market_engine.pts_store)
        realtime_market_engine.market_store.clear()
        realtime_market_engine.pts_store.clear()
    response = None
    try:
        response = client.get(
            "/api/stocks/stream?mode=2",
            headers={"Origin": "http://localhost:5000"},
        )
        generator = response.response
        first = next(generator).decode("utf-8")
        first_id = _extract_frame_id(first)
        realtime_market_engine._handle_producer_update(
            {
                "symbol": "SSETEST",
                "price": 123.4,
                "change": 1.2,
                "change_percent": 0.98,
                "volume": 10,
                "source": "test",
                "updated_at": time.time(),
            }
        )
        delta = next(generator).decode("utf-8")
        delta_id = _extract_frame_id(delta)
        assert delta_id > first_id
        assert sse_event_log.contains(delta_id, 2)
        entry = next(
            entry for entry in sse_event_log.replay_after(first_id, 2) if entry[0] == delta_id
        )
        assert entry[1] == "frame"
        assert "SSETEST" in entry[2]
    finally:
        if response is not None:
            response.response.close()
        with realtime_market_engine.store_lock:
            realtime_market_engine.market_store.clear()
            realtime_market_engine.market_store.update(saved_market_store)
            realtime_market_engine.pts_store.clear()
            realtime_market_engine.pts_store.update(saved_pts_store)
        sse_event_log.clear()


# ---------------------------------------------------------------------------
# Stream-level replay
# ---------------------------------------------------------------------------


def test_stream_replays_buffered_events(client):
    """A reconnect with a covered last_event_id resumes from the event log
    instead of resending a full initial snapshot."""
    sse_event_log.clear()
    try:
        seq1 = sse_event_log.next_id()
        sse_event_log.record(seq1, 1, "frame", 'data: {"stream_event":"diff","n":1}\n\n')
        seq2 = sse_event_log.next_id()
        sse_event_log.record(seq2, 1, "frame", 'data: {"stream_event":"diff","n":2}\n\n')

        response = client.get(
            f"/api/stocks/stream?mode=1&last_event_id={seq1}",
            headers={"Origin": "http://localhost:5000"},
        )
        assert response.status_code == 200
        gen = response.response
        try:
            first = next(gen).decode("utf-8")
            assert "initial_snapshot" not in first
            assert f"id: {seq2}" in first
            assert '"n":2' in first
        finally:
            gen.close()
    finally:
        sse_event_log.clear()


def _extract_frame_id(chunk: str) -> int:
    """Return the ``id:`` value from an SSE frame chunk."""
    m = re.search(r"(?m)^id: (\d+)$", chunk)
    assert m, f"no id: line in chunk: {chunk[:200]!r}"
    return int(m.group(1))


def test_stream_live_ids_align_with_replay_log(client):
    """Live frames must carry globally monotonic ids that are recorded in the
    replay log, so a reconnect with the last received id resumes via replay
    (round-trip: live path -> disconnect -> reconnect -> replay)."""
    sse_event_log.clear()
    try:
        # -- First connection: no last_event_id -> initial snapshot, then a live
        #    diff frame. Both must carry ids from the shared global log.
        response = client.get(
            "/api/stocks/stream?mode=1",
            headers={"Origin": "http://localhost:5000"},
        )
        assert response.status_code == 200
        gen = response.response
        try:
            first = next(gen).decode("utf-8")
            assert "initial_snapshot" in first
            snap_id = _extract_frame_id(first)
            assert snap_id > 0

            # Feed a diff into this connection's announcer queue.
            listeners = list(app_state.sse_announcer_mode1.listeners)
            target_q = listeners[-1]
            target_q.put_nowait('data: {"stream_event":"diff","n":1}\n\n')
            diff_chunk = next(gen).decode("utf-8")
            diff_id = _extract_frame_id(diff_chunk)
            # The live diff took the next global id (no per-connection counter).
            assert diff_id == snap_id + 1
            # Fully caught up after the diff: nothing newer is pending.
            assert sse_event_log.replay_after(diff_id, 1) == []
        finally:
            gen.close()

        # -- Reconnect with the live-produced id: a buffered event after it is
        #    replayed instead of a fresh full snapshot. This discriminates
        #    whether the live diff was actually recorded: were ``diff_id`` not
        #    in the log, the reconnect would hit the gap path and fall back to
        #    a full initial snapshot.
        seq3 = sse_event_log.next_id()
        sse_event_log.record(seq3, 1, "frame", 'data: {"stream_event":"diff","n":3}\n\n')
        response = client.get(
            f"/api/stocks/stream?mode=1&last_event_id={diff_id}",
            headers={"Origin": "http://localhost:5000"},
        )
        assert response.status_code == 200
        gen = response.response
        try:
            resumed = next(gen).decode("utf-8")
            assert "initial_snapshot" not in resumed
            assert f"id: {seq3}" in resumed
            assert '"n":3' in resumed
        finally:
            gen.close()
    finally:
        sse_event_log.clear()


def test_stream_falls_back_to_initial_snapshot_on_gap(client):
    """When the buffer no longer covers the gap, the client gets a full
    initial snapshot for recovery."""
    sse_event_log.clear()
    try:
        seq = sse_event_log.next_id()
        sse_event_log.record(seq, 1, "frame", 'data: {"stream_event":"diff"}\n\n')

        response = client.get(
            f"/api/stocks/stream?mode=1&last_event_id={max(seq - 1, 0)}",
            headers={"Origin": "http://localhost:5000"},
        )
        assert response.status_code == 200
        gen = response.response
        try:
            first = next(gen).decode("utf-8")
            assert "initial_snapshot" in first
        finally:
            gen.close()
    finally:
        sse_event_log.clear()


def test_stream_mode2_falls_back_to_initial_snapshot_when_replay_frames_empty(client):
    """In Mode 2, if buffered deltas cannot resolve to any valid symbols (e.g. deleted),
    a full snapshot is emitted so the client does not stall."""
    from services.realtime_engine import realtime_market_engine

    sse_event_log.clear()
    try:
        seq1 = sse_event_log.next_id()
        # Record delta for non-existent / purged symbol
        sse_event_log.record(seq1, 2, "delta", ("PURGED_SYMBOL_XYZ",))
        with realtime_market_engine.store_lock:
            realtime_market_engine.market_store.pop("PURGED_SYMBOL_XYZ", None)

        response = client.get(
            f"/api/stocks/stream?mode=2&last_event_id={seq1 - 1}",
            headers={"Origin": "http://localhost:5000"},
        )
        assert response.status_code == 200
        gen = response.response
        try:
            first = next(gen).decode("utf-8")
            assert "initial_snapshot" in first
        finally:
            gen.close()
    finally:
        sse_event_log.clear()


def test_sse_event_log_per_mode_window_isolation():
    """R5: a busy mode-2 stream must not evict mode-1 replay history.

    Both modes previously shared one bounded deque, so mode-2 traffic could
    push mode-1 entries out and force needless full-snapshot resyncs.
    """
    log = SSEEventLog(maxlen=5)
    s1 = log.next_id()
    log.record(s1, 1, "frame", "data: m1\n\n")

    # Flood mode 2 well past maxlen.
    for _ in range(20):
        log.record(log.next_id(), 2, "delta", ("AAPL",))

    # Mode 1 is still covered (caught up, not a gap).
    assert log.replay_after(s1, 1) == []

    s_last = log.next_id()
    log.record(s_last, 1, "frame", "data: m1b\n\n")
    assert log.replay_after(s1, 1) == [(s_last, "frame", "data: m1b\n\n")]

    # Sequence ids stay globally monotonic across modes (shared Last-Event-ID).
    assert s_last > s1

    # Mode 2 still honours its own eviction window.
    assert log.replay_after(s1, 2) is None


def test_sse_event_log_clear_resets_all_modes():
    log = SSEEventLog(maxlen=5)
    log.record(log.next_id(), 1, "frame", "data: a\n\n")
    log.record(log.next_id(), 2, "delta", ("AAPL",))
    log.clear()
    assert log.replay_after(0, 1) == []
    assert log.replay_after(0, 2) == []
