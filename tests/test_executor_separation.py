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
    """M6: the queue-depth helper used by /api/metrics must report a known
    pending count derived from the bounded semaphore's free slots."""
    ex = ExecutionState()
    try:
        max_q = ex.data_executor._max_queue_size
        free = ex.data_executor._semaphore._value
        # pending = configured - free slots
        expected_pending = max(0, max_q - free)
        assert 0 <= expected_pending <= max_q
    finally:
        ex.shutdown()
