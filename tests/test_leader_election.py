import os
import time
import unittest
import tempfile
import shutil
import copy
from pathlib import Path
from unittest.mock import patch, MagicMock

import app_bg
from app import app, app_state


class TestLeaderElectionAndCacheSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.lock_path = Path(self.temp_dir) / ".mns_sync_leader.lock"
        
        # Backup global states
        self.original_leader_file = app_bg._LEADER_LOCK_FILE
        app_bg._LEADER_LOCK_FILE = None
        self.original_sync_leader = app_bg._is_sync_leader

    def tearDown(self):
        # Close any open lock file descriptor created during tests
        if app_bg._LEADER_LOCK_FILE is not None:
            try:
                app_bg._LEADER_LOCK_FILE.close()
            except Exception:
                pass
            app_bg._LEADER_LOCK_FILE = None
            
        shutil.rmtree(self.temp_dir)
        app_bg._is_sync_leader = self.original_sync_leader

    def test_try_acquire_atomic_lock_success(self):
        pid = 12345
        acquired = app_bg._try_acquire_atomic_lock(self.lock_path, pid)
        self.assertTrue(acquired)
        self.assertTrue(self.lock_path.exists())
        with open(self.lock_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), str(pid))

    def test_try_acquire_atomic_lock_stale_pid(self):
        # Write a stale PID to the lock file
        stale_pid = 999999
        self.lock_path.write_text(str(stale_pid), encoding="utf-8")

        # Attempt to acquire lock with a new PID
        new_pid = 54321
        # Mock os.kill to raise OSError for stale_pid (meaning process not running)
        original_kill = os.kill

        def mock_kill(target_pid, sig):
            if target_pid == stale_pid:
                raise OSError(3, "No such process")
            return original_kill(target_pid, sig)

        with patch("os.kill", side_effect=mock_kill):
            acquired = app_bg._try_acquire_atomic_lock(self.lock_path, new_pid)
            self.assertTrue(acquired)
            with open(self.lock_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), str(new_pid))

    def test_try_acquire_atomic_lock_empty_file(self):
        # Create an empty lock file (0 bytes)
        self.lock_path.write_text("", encoding="utf-8")

        # Attempt to acquire lock
        pid = 54321
        acquired = app_bg._try_acquire_atomic_lock(self.lock_path, pid)
        self.assertTrue(acquired)
        with open(self.lock_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), str(pid))

    def test_try_acquire_atomic_lock_corrupted_content(self):
        # Create a lock file with invalid non-integer content
        self.lock_path.write_text("corrupted_pid_here", encoding="utf-8")

        # Attempt to acquire lock
        pid = 54321
        acquired = app_bg._try_acquire_atomic_lock(self.lock_path, pid)
        self.assertTrue(acquired)
        with open(self.lock_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), str(pid))

    def test_try_acquire_leader_lock_truncation_prevention_and_sharing(self):
        pid_a = 10001
        pid_b = 10002

        # Mock Path inside app_bg to resolve lock_path in our temp_dir
        with patch("app_bg.Path") as mock_path:
            mock_base = MagicMock()
            mock_base.resolve.return_value.parent = Path(self.temp_dir)
            mock_base.__truediv__.return_value = self.lock_path
            mock_path.return_value = mock_base

            # Process A attempts to acquire lock
            with patch("os.getpid", return_value=pid_a):
                acquired_a = app_bg._try_acquire_leader_lock()
                self.assertTrue(acquired_a)

            # Process B attempts to acquire lock (simulating another process/thread)
            # In a different process, _LEADER_LOCK_FILE is None on startup.
            # We backup Process A's file handle and set it to None for Process B.
            file_handle_a = app_bg._LEADER_LOCK_FILE
            app_bg._LEADER_LOCK_FILE = None

            with patch("os.getpid", return_value=pid_b):
                acquired_b = app_bg._try_acquire_leader_lock()
                self.assertFalse(acquired_b)

            # Close Process A's file handle to release lock
            if file_handle_a is not None:
                file_handle_a.close()

            # Now that the lock is released, we can safely read the lock file
            with open(self.lock_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), str(pid_a))

    def test_warm_payload_cache_from_disk_updates_current_stocks_cache(self):
        with app.app_context():
            # Clear caches first
            with app_state.cache.sse_data_lock:
                app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
                app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}

            # Set up target stock symbol and mock payload
            symbol = "TESTCOLLECT"
            market = "us"
            key = f"payload_{symbol}_{market}"
            payload = {
                "symbol": symbol,
                "name": "Test Collector Inc",
                "price": 123.45,
                "change": 1.23,
                "market": market,
                "snapshot_ts_ms": int(time.time() * 1000),
            }

            # Save mock payload to disk cache
            app_state.payload_disk_cache.set(key, payload)

            # Mock load_user_stocks and set up in-memory user stocks
            with patch("app_bg.load_user_stocks"):
                with app_state.market.user_stocks_lock:
                    original_user_us = app_state.market.user_us
                    app_state.market.user_us = {symbol: "Test Collector Inc"}

                try:
                    # 1. First warm-up: current cache is empty, so it should be populated
                    app_bg._warm_payload_cache_from_disk()

                    with app_state.cache.sse_data_lock:
                        self.assertEqual(len(app_state.market.current_stocks_cache["us"]), 1)
                        self.assertEqual(app_state.market.current_stocks_cache["us"][0]["symbol"], symbol)
                        self.assertEqual(app_state.market.current_stocks_cache["us"][0]["price"], 123.45)

                    # 2. Modify disk cache with a new price (simulating master update)
                    updated_payload = copy.deepcopy(payload)
                    updated_payload["price"] = 999.99
                    app_state.payload_disk_cache.set(key, updated_payload)

                    # Clear last loaded mtimes to force reload
                    app_bg._last_loaded_mtimes.clear()

                    # 3. Second warm-up: current cache is cleared to simulate the
                    # empty-seed path. _warm_payload_cache_from_disk must seed
                    # current_stocks_cache from the freshly warmed target when current
                    # is empty. It must NOT clobber a non-empty current cache (that is
                    # owned by the interpolation loop).
                    with app_state.cache.sse_data_lock:
                        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}

                    app_bg._warm_payload_cache_from_disk()

                    with app_state.cache.sse_data_lock:
                        self.assertEqual(len(app_state.market.current_stocks_cache["us"]), 1)
                        self.assertEqual(app_state.market.current_stocks_cache["us"][0]["symbol"], symbol)
                        # Verify it seeded the updated price from disk target
                        self.assertEqual(app_state.market.current_stocks_cache["us"][0]["price"], 999.99)
                finally:
                    # Restore original user stocks
                    with app_state.market.user_stocks_lock:
                        app_state.market.user_us = original_user_us
                    # Clean up disk cache
                    app_state.payload_disk_cache.delete(key)
                    app_bg._last_loaded_mtimes.clear()


if __name__ == "__main__":
    unittest.main()
