"""Regression test suite for autonomous code review fixes (2026-08-30 v4).

Validates:
1. Finding 1: Leader election state synchronization between bg.leader_election and app_bg facade.
2. Finding 2: Sync generation state synchronization and stale fetch discard behavior.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import app_bg
import bg.leader_election as le
import bg.sync_worker as sw
from app_state import app_state


class TestHeadReviewAutonomousFixesV4(unittest.TestCase):
    """Regression tests for leader election and background sync generation fixes."""

    def tearDown(self):
        # Reset state after tests
        le._set_is_sync_leader(True)
        sw._set_sync_generation(0)

    def test_is_leader_synchronizes_state_between_bg_module_and_app_bg_facade(self):
        """Verify that _set_is_sync_leader updates both bg.leader_election and app_bg."""
        # Initial state should be leader (True)
        le._set_is_sync_leader(True)
        self.assertTrue(le.is_leader())
        self.assertTrue(app_bg._is_sync_leader)
        self.assertTrue(le._is_sync_leader)

        # Set follower (False)
        le._set_is_sync_leader(False)
        self.assertFalse(le.is_leader())
        self.assertFalse(app_bg._is_sync_leader)
        self.assertFalse(le._is_sync_leader)

        # Set leader again (True)
        le._set_is_sync_leader(True)
        self.assertTrue(le.is_leader())
        self.assertTrue(app_bg._is_sync_leader)
        self.assertTrue(le._is_sync_leader)

    def test_bg_leader_election_loop_follower_correctly_sets_is_leader_false(self):
        """Verify that when leader lock cannot be acquired, follower evaluates is_leader as False."""
        le._set_is_sync_leader(True)

        with patch("bg.leader_election._try_acquire_leader_lock", return_value=False):
            # Run one cycle of leader election loop by triggering shutdown immediately
            app_state.execution.shutdown_event.set()
            try:
                le.bg_leader_election_loop()
                self.assertFalse(le.is_leader())
                self.assertFalse(app_bg._is_sync_leader)
                self.assertFalse(le._is_sync_leader)
            finally:
                app_state.execution.shutdown_event.clear()

    def test_release_leader_lock_resets_is_leader_flag(self):
        """Verify that releasing leader lock marks the process as not a leader."""
        le._set_is_sync_leader(True)
        self.assertTrue(le.is_leader())

        le._release_leader_lock()
        self.assertFalse(le.is_leader())
        self.assertFalse(app_bg._is_sync_leader)

    def test_sync_generation_synchronizes_state_across_modules(self):
        """Verify that _set_sync_generation updates bg.sync_worker and app_bg."""
        sw._set_sync_generation(10)
        self.assertEqual(sw._sync_generation, 10)
        self.assertEqual(app_bg._sync_generation, 10)
        self.assertEqual(sw._get_app_bg_attr("_sync_generation", sw._sync_generation), 10)

    def test_process_fetched_stocks_discards_stale_generation(self):
        """Verify that _process_fetched_stocks drops results from a superseded sync generation."""
        sw._set_sync_generation(5)

        dummy_fetched = [
            {"symbol": "AAPL", "market": "us", "price": 150.0, "currency": "USD"},
        ]

        # Call with stale generation (e.g. generation 3 when active generation is 5)
        us_res, jp_res, idx_res = sw._process_fetched_stocks(dummy_fetched, sync_generation=3)
        self.assertEqual(us_res, [])
        self.assertEqual(jp_res, [])
        self.assertEqual(idx_res, [])

        # Call with current generation (5)
        us_res, jp_res, idx_res = sw._process_fetched_stocks(dummy_fetched, sync_generation=5)
        self.assertEqual(len(us_res), 1)
        self.assertEqual(us_res[0]["symbol"], "AAPL")


if __name__ == "__main__":
    unittest.main()
