"""Regression tests for executor separation (H3) and metrics exposure (M6)."""

from execution_state import ExecutionState


def test_execution_state_has_data_executor():
    """H3: a dedicated market-data executor must exist separately from the
    AI-bound `executor` so an AI call surge cannot starve price/history work."""
    ex = ExecutionState()
    try:
        assert ex.data_executor is not None
        # Must be an independent pool, not the same object as the AI executor.
        assert ex.data_executor is not ex.executor
        assert ex.news_executor is not None
        assert ex.sync_refresh_executor is not None
    finally:
        ex.shutdown()


def test_metrics_executor_stats_reports_depth():
    """M6: executor_stats must report a non-zero pending count when tasks are
    queued (reads the real ThreadPoolExecutor ``_work_queue``; the previous
    version never called the helper and only checked local arithmetic)."""
    import threading
    import time

    ex = ExecutionState()
    gate = threading.Event()
    try:
        # sync_refresh_executor has a single worker, so a second task stays
        # queued while the first blocks on the gate.
        ex.sync_refresh_executor.submit(gate.wait)
        ex.sync_refresh_executor.submit(lambda: None)
        time.sleep(0.2)
        stats = ex.executor_stats(ex.sync_refresh_executor)
        assert stats["max_queue_size"] == ex.sync_refresh_executor._max_queue_size
        assert stats["pending"] >= 1, f"pending should reflect the queued task, got {stats}"
    finally:
        gate.set()
        ex.shutdown()
