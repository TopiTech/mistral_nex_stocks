"""Tests for stale-sync recovery (review finding R1).

A sync that exceeds ``SYNC_STALE_TIMEOUT_SEC`` must not wedge the app forever:
``_recover_stale_sync_state_if_needed`` clears the stuck ``is_syncing`` flag so
scheduling and the UI recover, and ``sync_all_stocks_now`` attempts a *bounded*
lock takeover instead of silently skipping every subsequent sync.
"""

import importlib.util
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app_bg
from app_state import app_state


class SyncStaleRecoveryTests(unittest.TestCase):
    """Tests for stale-sync detection, recovery, and bounded lock takeover."""

    def tearDown(self):
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False
        app_bg._sync_start_time = 0.0
        super().tearDown()

    def _make_stale(self, seconds_old=None):
        """Mark a sync as in-progress and beyond the stale threshold."""
        if seconds_old is None:
            seconds_old = app_bg.SYNC_STALE_TIMEOUT_SEC + 10
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        app_bg._sync_start_time = time.time() - seconds_old

    # ------------------------------------------------------------------
    # _recover_stale_sync_state_if_needed
    # ------------------------------------------------------------------

    def test_recover_stale_sync_state_clears_stale_flag(self):
        self._make_stale()
        recovered = app_bg._recover_stale_sync_state_if_needed()
        self.assertTrue(recovered)
        with app_state.market.is_syncing_lock:
            self.assertFalse(app_state.market.is_syncing)
        self.assertEqual(app_bg._sync_start_time, 0.0)

    def test_recover_stale_sync_state_noop_when_sync_is_recent(self):
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        app_bg._sync_start_time = time.time()  # just started
        recovered = app_bg._recover_stale_sync_state_if_needed()
        self.assertFalse(recovered)
        with app_state.market.is_syncing_lock:
            self.assertTrue(app_state.market.is_syncing)

    def test_recover_stale_sync_state_noop_when_not_syncing(self):
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False
        self.assertFalse(app_bg._recover_stale_sync_state_if_needed())

    # ------------------------------------------------------------------
    # sync_all_stocks_now bounded takeover
    # ------------------------------------------------------------------

    def test_sync_takeover_proceeds_after_stale_sync(self):
        """A stale sync is superseded: the next caller clears the flag and runs."""

        class FakeLock:
            def __init__(self):
                self.acquire_calls = []

            def acquire(self, blocking=True, timeout=-1):
                self.acquire_calls.append((blocking, timeout))
                # The non-blocking probe (blocking=False) fails because a
                # previous run holds the lock; the bounded takeover
                # (blocking=True, timeout=...) succeeds immediately here.
                return blocking

            def release(self):
                pass

        fake_lock = FakeLock()
        self._make_stale()
        fetch_calls = []

        def mock_fetch(items, snapshot_ts_ms=None, **kwargs):
            fetch_calls.append(len(items))
            return []

        with (
            patch.object(app_bg, "_sync_execution_lock", fake_lock),
            patch("app_bg.fetch_stocks_batch", side_effect=mock_fetch),
        ):
            app_bg.sync_all_stocks_now()

        # The takeover ran the sync body (fetch was reached) after a bounded
        # wait. The main batch fetch is the first call; the heatmap pre-warm
        # tasks submitted to the (synchronous, in tests) data executor account
        # for the additional calls.
        self.assertGreaterEqual(len(fetch_calls), 1)
        self.assertGreater(fetch_calls[0], 0)
        self.assertGreaterEqual(len(fake_lock.acquire_calls), 2)
        with app_state.market.is_syncing_lock:
            self.assertFalse(app_state.market.is_syncing)

    def test_sync_takeover_times_out_without_running(self):
        """If the wedged run never releases the lock, the caller gives up within
        the bounded wait instead of blocking, and the stale flag stays cleared."""
        fetch_calls = []

        class FakeLock:
            def acquire(self, blocking=True, timeout=-1):
                return False

            def release(self):
                pass

        self._make_stale()
        with (
            patch.object(app_bg, "_sync_execution_lock", FakeLock()),
            patch(
                "app_bg.fetch_stocks_batch",
                side_effect=lambda items, snapshot_ts_ms=None, **kwargs: (
                    fetch_calls.append(1) or []
                ),
            ),
        ):
            app_bg.sync_all_stocks_now()

        self.assertEqual(fetch_calls, [])
        # The stale state was still cleared so scheduling and the UI can recover.
        with app_state.market.is_syncing_lock:
            self.assertFalse(app_state.market.is_syncing)

    # ------------------------------------------------------------------
    # schedule_sync_all_stocks_now recovery (via a fresh module import:
    # conftest stubs the module-level schedule function for the suite)
    # ------------------------------------------------------------------

    def _fresh_app_bg(self):
        spec = importlib.util.spec_from_file_location(
            "app_bg_fresh", Path(app_bg.__file__).resolve()
        )
        fresh = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(fresh)
        return fresh

    def test_schedule_sync_recovers_stale_state(self):
        fresh = self._fresh_app_bg()
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        fresh._sync_start_time = time.time() - (fresh.SYNC_STALE_TIMEOUT_SEC + 10)

        with patch.object(fresh, "_run_scheduled_sync_job", lambda: None):
            result = fresh.schedule_sync_all_stocks_now()

        self.assertTrue(result)
        with app_state.market.is_syncing_lock:
            self.assertFalse(app_state.market.is_syncing)

    def test_schedule_sync_still_skips_when_sync_is_recent(self):
        fresh = self._fresh_app_bg()
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        fresh._sync_start_time = time.time()  # just started

        with patch.object(fresh, "_run_scheduled_sync_job", lambda: None):
            result = fresh.schedule_sync_all_stocks_now()

        self.assertFalse(result)
        with app_state.market.sync_schedule_lock:
            self.assertTrue(app_state.market.sync_pending)
        with app_state.market.is_syncing_lock:
            self.assertTrue(app_state.market.is_syncing)


if __name__ == "__main__":
    unittest.main()
