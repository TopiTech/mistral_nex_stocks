"""K-03 remainder: stop() must drain an in-flight PTS fetch and report status.

Without the fix, stop() joins only 5s: a worker blocked in a fallback HTTP
chain stays alive, start() then raises RuntimeError, and restart() defers —
realtime updates stop until the stale thread happens to die.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from services.realtime.engine import RealtimeMarketEngine


def _start_blocked_engine(engine, entered, release, payload=None, handle_mock=None):
    """Start the real PTS loop with a fetch fake blocked on ``release``."""
    engine._fetch_pts_with_fallback_calls = []

    def fake_fetch(symbol, use_primary=True):
        entered.set()
        engine._fetch_pts_with_fallback_calls.append(symbol)
        assert release.wait(timeout=15.0), "fetch release never arrived"
        return payload

    patches = [
        patch.object(engine.yahoojp_scraper, "_is_startup_ready", return_value=True),
        patch("services.realtime.engine._is_scraper_blocked", return_value=False),
        patch("services.realtime.engine._is_yf_rate_limited", return_value=False),
        patch("services.realtime.engine.is_pts_session", return_value=True),
        patch("services.realtime.engine._dedupe_pts_symbols", return_value=["7203.T"]),
        patch.object(engine, "_pts_cached_payload", return_value=None),
        patch.object(engine, "_fetch_pts_with_fallback", side_effect=fake_fetch),
        patch.object(engine.tv_client, "start"),
        patch.object(engine.yahoojp_scraper, "start"),
    ]
    if handle_mock is not None:
        patches.append(patch.object(engine, "_handle_pts_update", handle_mock))
    for p in patches:
        p.start()
    engine._k03_patches = patches
    engine.start()
    assert entered.wait(timeout=10.0), "PTS worker never entered fetch"
    return patches


def _stop_patches(engine):
    for p in getattr(engine, "_k03_patches", []):
        p.stop()
    engine._k03_patches = []


def test_stop_waits_for_inflight_fetch_and_reports_true():
    engine = RealtimeMarketEngine()
    entered = threading.Event()
    release = threading.Event()
    _start_blocked_engine(engine, entered, release)
    try:
        threading.Timer(0.5, release.set).start()
        t0 = time.monotonic()
        stopped = engine.stop()
        elapsed = time.monotonic() - t0
        assert stopped is True, "stop() must report clean shutdown explicitly"
        assert elapsed >= 0.4, f"stop() returned in {elapsed:.2f}s without draining fetch"
        thread = engine.pts_thread
        assert thread is None or not thread.is_alive()
        # Restart path must work immediately (no RuntimeError wedge).
        engine.start()
    finally:
        release.set()
        try:
            engine.stop()
        finally:
            _stop_patches(engine)


def test_restart_recovers_while_fetch_inflight():
    """Watchdog crash recovery: restart() during a blocked fetch must produce
    a live new-epoch worker instead of deferring forever."""
    engine = RealtimeMarketEngine()
    entered = threading.Event()
    release = threading.Event()
    _start_blocked_engine(engine, entered, release)
    epoch_before = engine._pts_epoch
    try:
        threading.Timer(0.5, release.set).start()
        engine.restart()
        # stop() bumps the epoch once, start() bumps it again.
        assert engine._pts_epoch == epoch_before + 2
        assert engine.pts_thread is not None and engine.pts_thread.is_alive()
    finally:
        release.set()
        try:
            engine.stop()
        finally:
            _stop_patches(engine)


def test_stop_reports_false_when_fetch_outlives_drain_bound():
    from unittest.mock import MagicMock

    engine = RealtimeMarketEngine()
    entered = threading.Event()
    release = threading.Event()
    handle_mock = MagicMock()
    _start_blocked_engine(engine, entered, release, payload={"symbol": "7203.T"},
                          handle_mock=handle_mock)
    try:
        with patch("services.realtime.engine.PTS_FETCH_DRAIN_TIMEOUT_SEC", 0.05):
            stopped = engine.stop()
        assert stopped is False, "stop() must explicitly report the surviving thread"
        assert engine.pts_thread is not None and engine.pts_thread.is_alive()
        # The orphaned fetch must not publish once released (epoch gate).
        release.set()
        engine.pts_thread.join(timeout=10.0)
        assert not engine.pts_thread.is_alive()
        handle_mock.assert_not_called()
        # Recovery works once the stale thread is gone.
        engine.start()
    finally:
        release.set()
        try:
            engine.stop()
        finally:
            _stop_patches(engine)
