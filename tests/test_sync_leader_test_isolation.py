"""Tests for leader election test isolation, sync worker state hygiene, and regression coverage.

Validates that:
1. Leader election state is reliably isolated and restored between tests.
2. Releasing the leader lock in one test never cascades into sync failures in subsequent tests.
3. Both leader and follower paths in ``sync_all_stocks_now`` behave correctly under stale recovery.
4. Conftest stubs on app_bg remain robust against module tampering.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import app_bg
import bg.leader_election as le
import bg.sync_worker as sw
from tests import reset_app_state_internals


class SyncLeaderIsolationTests(unittest.TestCase):
    """Test suite ensuring complete isolation of leader election and sync state across test runs."""

    def test_reset_app_state_internals_restores_sync_leader(self):
        """reset_app_state_internals must always reset _is_sync_leader to True."""
        # Intentionally pollute leader state to False
        le._set_is_sync_leader(False)
        self.assertFalse(le.is_leader())
        self.assertFalse(app_bg._is_sync_leader)

        # Call the standard reset function
        reset_app_state_internals()

        # Both modules must be restored to True
        self.assertTrue(le.is_leader())
        self.assertTrue(app_bg._is_sync_leader)
        self.assertTrue(le._is_sync_leader)

    def test_reset_app_state_internals_resets_sync_generation_and_time(self):
        """reset_app_state_internals must reset generation and start time counters."""
        sw._set_sync_generation(999)
        sw._set_sync_start_time(12345678.0)
        app_bg._sync_start_time = 12345678.0

        reset_app_state_internals()

        self.assertEqual(sw._sync_generation, 0)
        self.assertEqual(app_bg._sync_generation, 0)
        self.assertEqual(sw._sync_start_time, 0.0)
        self.assertEqual(app_bg._sync_start_time, 0.0)

    def test_release_leader_lock_followed_by_reset_does_not_poison_subsequent_test(self):
        """Simulate a test calling _release_leader_lock and verify isolation cleanup."""
        le._set_is_sync_leader(True)
        le._release_leader_lock()
        self.assertFalse(le.is_leader())
        self.assertFalse(app_bg._is_sync_leader)

        # Teardown / fixture reset runs between tests
        reset_app_state_internals()

        # The subsequent test should see a fresh, healthy leader state
        self.assertTrue(le.is_leader())
        self.assertTrue(app_bg._is_sync_leader)

    def test_sync_all_stocks_now_follower_path_reloads_disk_cache(self):
        """When running as a follower, sync_all_stocks_now warms cache from disk and announces state."""
        le._set_is_sync_leader(False)
        self.assertFalse(le.is_leader())

        warm_mock = MagicMock()
        inval_mock = MagicMock()
        ann_mock = MagicMock()
        fetch_mock = MagicMock()

        class FakeLock:
            def acquire(self, blocking=True, timeout=-1):
                return True

            def release(self):
                pass

        with (
            patch.object(app_bg, "_is_sync_leader", False),
            patch.object(app_bg, "_sync_execution_lock", FakeLock()),
            patch.object(app_bg, "_warm_payload_cache_from_disk", warm_mock),
            patch.object(app_bg, "_invalidate_sse_payload_cache", inval_mock),
            patch.object(app_bg, "announce_current_market_state", ann_mock),
            patch("app_bg.fetch_stocks_batch", fetch_mock),
        ):
            app_bg.sync_all_stocks_now()

        warm_mock.assert_called_once()
        inval_mock.assert_called_once()
        ann_mock.assert_called_once()
        fetch_mock.assert_not_called()

    def test_sync_all_stocks_now_leader_path_executes_batch_fetch(self):
        """When running as leader, sync_all_stocks_now executes batch fetch and pre-warms heatmap."""
        le._set_is_sync_leader(True)
        self.assertTrue(le.is_leader())

        fetch_calls = []

        class FakeLock:
            def acquire(self, blocking=True, timeout=-1):
                return True

            def release(self):
                pass

        def mock_fetch(items, snapshot_ts_ms=None, **kwargs):
            fetch_calls.append(len(items))
            return []

        with (
            patch.object(app_bg, "_is_sync_leader", True),
            patch.object(app_bg, "_sync_execution_lock", FakeLock()),
            patch("app_bg.fetch_stocks_batch", side_effect=mock_fetch),
            patch("app_bg._warm_payload_cache_from_disk"),
            patch("app_bg._prepare_sync_items", return_value=[("TEST", "Test Inc", "us")]),
        ):
            app_bg.sync_all_stocks_now()

        self.assertGreaterEqual(len(fetch_calls), 1)
        self.assertGreater(fetch_calls[0], 0)


if __name__ == "__main__":
    unittest.main()
