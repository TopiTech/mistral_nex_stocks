"""
Regression tests for code-review findings R3, R7, R17.

R3:  get_cached_context_with_negative_cache must NOT write the negative cache
     when get_cached returns the CACHE_FETCHING sentinel (a concurrent fetch is
     still running and the waiter timed out). Poisoning it would suppress the
     soon-to-be-successful result for the whole negative TTL.
R7:  fetch_stocks_batch must survive a saturated data_executor (queue.Full on
     submit) by skipping the fallback instead of aborting the whole sync.
R17: executor_stats must read the real ThreadPoolExecutor work queue
     (``_work_queue``), not the nonexistent ``_queue`` attribute, so
     /api/metrics queue depth reflects reality.
"""

import concurrent.futures
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from app_state import app_state
from utils.caching import CACHE_FETCHING, _has_cached_key, get_cached_context_with_negative_cache


class NegativeCacheFetchingSentinelTestCase(unittest.TestCase):
    """R3: CACHE_FETCHING must not poison the negative cache."""

    def tearDown(self):
        from utils.caching import global_cache

        with global_cache.cache_lock:
            global_cache.caches.clear()
        with global_cache.fetch_events_lock:
            global_cache.fetch_events.clear()

    def test_fetching_sentinel_does_not_write_negative_cache(self):
        """A timed-out waiter seeing CACHE_FETCHING must not set the negative key."""
        mock_fetch = MagicMock(return_value="market context")
        with patch("utils.caching.get_cached", return_value=CACHE_FETCHING):
            result = get_cached_context_with_negative_cache(
                "research_key_fetching", mock_fetch, success_ttl=600, negative_ttl=90
            )
        self.assertEqual(result, "")
        self.assertFalse(
            _has_cached_key("research_key_fetching__negative", 90),
            "negative cache must not be written while a fetch is in flight",
        )

    def test_later_successful_fetch_is_not_suppressed(self):
        """After the in-flight fetch completes, the next call gets the value."""
        mock_fetch = MagicMock(return_value="market context")
        with patch("utils.caching.get_cached", side_effect=["market context"]):
            result = get_cached_context_with_negative_cache(
                "research_key_later", mock_fetch, success_ttl=600, negative_ttl=90
            )
        self.assertEqual(result, "market context")

    def test_genuine_failure_still_writes_negative_cache(self):
        """A real fetch failure (empty result) still writes the negative entry."""
        with patch("utils.caching.get_cached", return_value=""):
            result = get_cached_context_with_negative_cache(
                "research_key_fail", lambda: "", success_ttl=600, negative_ttl=90
            )
        self.assertEqual(result, "")
        self.assertTrue(_has_cached_key("research_key_fail__negative", 90))


class FetchStocksBatchQueueFullTestCase(unittest.TestCase):
    """R7: a queue.Full on the fallback submission must skip, not abort."""

    def setUp(self):
        # conftest replaces app_bg.fetch_stocks_batch with a stub (returns [])
        # to keep the suite offline. Reload the real module for the duration of
        # this test class and restore the stub afterwards.
        import importlib

        import app_bg as real_app_bg

        if hasattr(real_app_bg, "_release_leader_lock"):
            real_app_bg._release_leader_lock()

        self._saved_stub = real_app_bg.fetch_stocks_batch
        real_app_bg = importlib.reload(real_app_bg)
        self._real_fetch_stocks_batch = real_app_bg.fetch_stocks_batch

    def tearDown(self):
        import app_bg as real_app_bg

        if hasattr(real_app_bg, "_release_leader_lock"):
            real_app_bg._release_leader_lock()

        real_app_bg.fetch_stocks_batch = self._saved_stub

    def _call_real(self, items):
        return self._real_fetch_stocks_batch(items)

    def _non_empty_foreign_batch(self):
        """A non-empty batch DataFrame that contains NO data for our symbol,
        forcing the per-symbol fallback path."""
        index = pd.to_datetime(["2026-05-21"])
        arrays = [["OTHER"], ["Close"]]
        df = pd.DataFrame([[1.0]], columns=pd.MultiIndex.from_arrays(arrays), index=index)
        return df

    def _patch_degraded_false(self):
        """Pin the disk-cache degraded flag to False for the fallback tests.

        ``fetch_stocks_batch`` short-circuits with ``[None] * len(items)`` while
        the module-level degraded flag is set (within 10s of a disk-cache lock
        timeout anywhere in the suite). These tests exercise the queue.Full /
        per-symbol fallback path specifically, so they must be immune to that
        unrelated global state.
        """
        return patch("utils.disk_cache.is_disk_cache_degraded", return_value=False)

    def test_queue_full_skips_fallback_without_raising(self):
        with (
            self._patch_degraded_false(),
            patch.object(
                app_state.stock_provider,
                "download_batch",
                return_value=self._non_empty_foreign_batch(),
            ),
            patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
            patch.object(
                app_state.execution.data_executor,
                "submit",
                side_effect=__import__("queue").Full("queue is full"),
            ),
        ):
            results = self._call_real([("AAPL", "Apple", "us")])

        # The sync continues: the fallback for AAPL is skipped (None), no
        # exception propagates to abort the whole sync cycle.
        self.assertEqual(results, [None])

    def test_successful_fallback_still_returns_payload(self):
        """A working fallback path is unchanged (returns the fetched payload)."""
        payload = {"symbol": "AAPL", "price": 100.0, "market": "us"}

        def fake_submit(fn, symbol, name, market, snapshot_ts_ms=None):
            # data_executor.submit returns a Future; the batch code waits on the
            # returned object and reads ``fut.result()``.
            fut = concurrent.futures.Future()
            fut.set_result(payload)
            return fut

        with (
            self._patch_degraded_false(),
            patch.object(
                app_state.stock_provider,
                "download_batch",
                return_value=self._non_empty_foreign_batch(),
            ),
            patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
            patch.object(app_state.execution.data_executor, "submit", side_effect=fake_submit),
        ):
            results = self._call_real([("AAPL", "Apple", "us")])

        self.assertEqual(results, [payload])


class ExecutorStatsPendingTestCase(unittest.TestCase):
    """R17: executor_stats must reflect the real work-queue depth."""

    def test_pending_reflects_queued_tasks(self):
        from utils.threading import DaemonThreadPoolExecutor

        ex = DaemonThreadPoolExecutor(max_workers=1, max_queue_size=5)
        gate = threading.Event()
        try:
            ex.submit(gate.wait)  # occupies the only worker
            ex.submit(lambda: None)  # queued behind it
            time.sleep(0.2)
            stats = app_state.execution.executor_stats(ex)
            self.assertGreaterEqual(stats["pending"], 1)
            self.assertGreaterEqual(stats["max_queue_size"], 5)
        finally:
            gate.set()
            ex.shutdown(wait=True)

    def test_stats_never_raise_on_broken_executor(self):
        stats = app_state.execution.executor_stats(object())  # type: ignore[arg-type]
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["max_queue_size"], 0)


if __name__ == "__main__":
    unittest.main()
