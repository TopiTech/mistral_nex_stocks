# tests/test_session_manager.py
"""Tests for YFinanceSessionManager session-pool bounding & reclamation.

These guard against the long-running leak where rotated/idle yfinance sessions
were appended to _all_sessions and never reclaimed, exhausting FDs/memory.
"""

import threading
import unittest

from session_manager import YFinanceSessionManager


class TestYFinanceSessionManager(unittest.TestCase):
    """Session pool bounding, epoch sweep, and idle reclamation."""

    def setUp(self):
        # Fresh singleton so rate-limit/exclusion state does not leak across tests.
        YFinanceSessionManager._reset_for_testing()
        self.mgr = YFinanceSessionManager()
        # Disable the 15s request cap so created sessions are not auto-closed by timeout.
        self.mgr._request_min_interval_sec = 0.0
        self.mgr._adaptive_interval_sec = 0.0

    def _make_session(self):
        """Create a session via the manager's public entry point and return it."""
        return self.mgr.get_session()

    def test_get_session_interface_unchanged(self):
        """get_session() returns a session object (callers don't change)."""
        sess = self.mgr.get_session()
        self.assertIsNotNone(sess)
        # Same thread/UA index reuses the same session object.
        self.assertIs(self.mgr.get_session(), sess)

    def test_pool_cap_enforced(self):
        """Sessions beyond the cap are reclaimed (oldest idle first)."""
        # Force a tiny cap for the test.
        import constants

        original = constants.YFINANCE_SESSION_POOL_MAX
        constants.YFINANCE_SESSION_POOL_MAX = 4
        try:
            # Rotate UA many times; each rotation creates a new session.
            for _ in range(20):
                self.mgr.mark_rate_limited("yfinance", duration=1)
            # Allow the reaper-style enforcement to run.
            self.mgr._enforce_pool_cap()
            self.assertLessEqual(self.mgr.session_count(), 4)
        finally:
            constants.YFINANCE_SESSION_POOL_MAX = original

    def test_epoch_sweep_removes_prior_epoch_sessions(self):
        """A UA rotation sweeps all prior-epoch sessions (burnt crumb identity).

        We create sessions on helper threads (each thread holds its own
        thread-local session) so that rotating UA epochs actually leaves
        multiple prior-epoch sessions in _all_sessions to be swept.
        """
        created = []
        lock = threading.Lock()

        def make_session():
            s = self.mgr.get_session()
            with lock:
                created.append(s)

        threads = [threading.Thread(target=make_session) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        before = self.mgr.session_count()
        self.assertGreaterEqual(before, 3)

        # Mark one session in-flight so it is spared by the sweep.
        with self.mgr._active_sessions_lock:
            self.mgr._active_sessions.add(id(created[-1]))
        try:
            # Rotate: sweeps all prior-epoch sessions except the in-flight one.
            self.mgr.mark_rate_limited("yfinance", duration=1)
            live_ids = [id(e[0]) for e in self.mgr._all_sessions]
            for s in created[:-1]:
                self.assertNotIn(id(s), live_ids)
            self.assertIn(id(created[-1]), live_ids)
        finally:
            with self.mgr._active_sessions_lock:
                self.mgr._active_sessions.discard(id(created[-1]))

    def test_idle_reclamation(self):
        """Sessions idle beyond TTL are reclaimed; recent ones kept."""
        import session_manager as sm

        original_ttl = sm.YFINANCE_SESSION_IDLE_TTL_SEC
        sm.YFINANCE_SESSION_IDLE_TTL_SEC = 0  # anything idle is reclaimable
        try:
            # Create a session (fresh ts). The return value is unused; the call
            # has the side effect of registering a session in _all_sessions.
            self.mgr.get_session()
            # Make it appear idle by back-dating its creation timestamp.
            with self.mgr._lock:
                self.mgr._all_sessions = [
                    (e[0], e[1], e[2] - 1000.0) for e in self.mgr._all_sessions
                ]
            # No in-flight sessions -> all idle reclaimed.
            self.mgr._reclaim_idle_and_cap()
            self.assertEqual(self.mgr.session_count(), 0)
        finally:
            sm.YFINANCE_SESSION_IDLE_TTL_SEC = original_ttl

    def test_inflight_session_spawned(self):
        """A session used inside a request is tracked and not reclaimed mid-flight."""
        sess = self.mgr.get_session()
        # Simulate an in-flight request on this session.
        with self.mgr._active_sessions_lock:
            self.mgr._active_sessions.add(id(sess))
        try:
            # Even if idle beyond TTL, in-flight sessions are spared.
            import session_manager as sm

            original_ttl = sm.YFINANCE_SESSION_IDLE_TTL_SEC
            sm.YFINANCE_SESSION_IDLE_TTL_SEC = 0
            try:
                with self.mgr._lock:
                    self.mgr._all_sessions = [
                        (e[0], e[1], e[2] - 1000.0) for e in self.mgr._all_sessions
                    ]
                self.mgr._reclaim_idle_and_cap()
                self.assertIn(id(sess), [id(e[0]) for e in self.mgr._all_sessions])
            finally:
                sm.YFINANCE_SESSION_IDLE_TTL_SEC = original_ttl
        finally:
            with self.mgr._active_sessions_lock:
                self.mgr._active_sessions.discard(id(sess))

    def test_singleton_instance_shared(self):
        """The module-level singleton shares state via the class lock.

        NOTE: setUp() calls _reset_for_testing() which clears the singleton, so
        we verify the singleton contract directly: two YFinanceSessionManager()
        calls within the same (non-reset) instance return the same object.
        """
        mgr_a = YFinanceSessionManager()
        mgr_b = YFinanceSessionManager()
        self.assertIs(mgr_a, mgr_b)

    def test_thread_local_cache_invalidated_after_idle_reclaim(self):
        """get_session() must not return a session already reclaimed by idle reaper.

        This guards the exact bug pattern reported: after ~10 minutes of idle
        time, the background reaper removes idle sessions from the global pool,
        but a thread can still hold a stale reference in its local cache. The
        fix requires get_session() to verify the local session is still in the
        global pool before reusing it.
        """
        import session_manager as sm

        original_ttl = sm.YFINANCE_SESSION_IDLE_TTL_SEC
        sm.YFINANCE_SESSION_IDLE_TTL_SEC = 0  # reclaim immediately
        try:
            # First call registers a session in pool + thread-local cache.
            first = self.mgr.get_session()
            self.assertIsNotNone(first)

            # Force the session to look very idle so the reaper drops it.
            with self.mgr._lock:
                self.mgr._all_sessions = [
                    (e[0], e[1], e[2] - 1000.0) for e in self.mgr._all_sessions
                ]
            self.mgr._reclaim_idle_and_cap()
            self.assertEqual(self.mgr.session_count(), 0)

            # The same thread still has the old session cached locally.
            # get_session() must create a fresh one instead of reusing it.
            second = self.mgr.get_session()
            self.assertIsNotNone(second)
            self.assertIsNot(first, second)
        finally:
            sm.YFINANCE_SESSION_IDLE_TTL_SEC = original_ttl

    def test_session_timestamp_updated_on_request(self):
        """A session's timestamp is updated when a request is made, preventing premature idle reclamation."""
        from unittest.mock import patch, MagicMock
        from session_manager import CURL_CFFI_AVAILABLE

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        patch_path = (
            "curl_cffi.requests.Session.request"
            if CURL_CFFI_AVAILABLE
            else "requests.Session.request"
        )

        with patch(patch_path, return_value=mock_resp):
            sess = self.mgr.get_session()
            self.assertIsNotNone(sess)

            # Get initial timestamp from _all_sessions
            with self.mgr._lock:
                initial_ts = next(e[2] for e in self.mgr._all_sessions if e[0] is sess)

            # Back-date the initial creation timestamp to make it look old
            with self.mgr._lock:
                self.mgr._all_sessions = [
                    (e[0], e[1], e[2] - 50.0) if e[0] is sess else e for e in self.mgr._all_sessions
                ]

            # Execute a request via the session
            sess.request("GET", "https://example.com")

            # Verify the timestamp was updated (and is now greater than the back-dated one)
            with self.mgr._lock:
                updated_ts = next(e[2] for e in self.mgr._all_sessions if e[0] is sess)
            self.assertGreater(updated_ts, initial_ts - 50.0)



if __name__ == "__main__":
    unittest.main()
