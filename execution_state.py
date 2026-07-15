"""
execution_state.py - Thread pool and background task execution management.

Extracted from app_state.py to reduce module complexity.
"""

import logging
import queue
import threading
from utils.threading import DaemonThreadPoolExecutor

logger = logging.getLogger(__name__)


class ExecutionState:
    """Manages thread pools and background task execution."""

    def __init__(self):
        # AI-bound work (Mistral chat/analyze/news). Kept separate so that
        # heavy AI calls cannot starve real-time market-data work below.
        self.executor = DaemonThreadPoolExecutor(max_workers=5, max_queue_size=50)
        # Real-time market data work (history fetches, heatmap builds, yfinance
        # per-symbol fallbacks). H3: split out from `self.executor` so an AI
        # call surge cannot fill the shared queue and cause 503s on price/history.
        self.data_executor = DaemonThreadPoolExecutor(max_workers=4, max_queue_size=30)
        self.news_executor = DaemonThreadPoolExecutor(max_workers=4, max_queue_size=10)
        self.sync_refresh_executor = DaemonThreadPoolExecutor(max_workers=1, max_queue_size=5)
        self.shutdown_event = threading.Event()
        self.background_threads: list[threading.Thread] = []

    def shutdown(self):
        """Shut down all executors without blocking."""
        self.shutdown_event.set()
        for ex in [self.executor, self.data_executor, self.news_executor, self.sync_refresh_executor]:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)

        for t in self.background_threads:
            try:
                if t.is_alive():
                    t.join(timeout=2.0)
            except Exception:
                logger.debug("Background thread join failed (expected during shutdown)")

    def executor_stats(self, ex) -> dict:
        """Return queue saturation stats without reaching into private attrs.

        Avoids coupling to DaemonThreadPoolExecutor._semaphore._value (which is
        fragile and triggers linter/type warnings). Returns free slots and the
        configured max queue size; pending = max - free.
        """
        try:
            sem = getattr(ex, "_semaphore", None)
            free = sem._value if sem is not None else 0
            max_queue = getattr(ex, "_max_queue_size", 0) or 0
            pending = max(0, max_queue - free) if max_queue else 0
            return {"max_queue_size": max_queue, "pending": pending}
        except Exception:
            return {"max_queue_size": 0, "pending": 0}

    def safe_submit(self, executor_name: str, fn, *args, **kwargs) -> bool:
        """Submit a task to a named executor with safe error handling.

        Catches ``queue.Full`` (backpressure) and ``RuntimeError`` (shutdown)
        and returns False instead of propagating the exception to the caller.
        Logs a warning on failure.

        Args:
            executor_name: Attribute name of the executor (e.g. "executor",
                          "data_executor", "news_executor", "sync_refresh_executor").
            fn: Callable to execute.
            *args, **kwargs: Arguments passed to fn.

        Returns:
            True if the task was successfully submitted, False on backpressure
            or shutdown.
        """
        ex = getattr(self, executor_name, None)
        if ex is None:
            logger.warning("safe_submit: executor %r not found", executor_name)
            return False
        try:
            ex.submit(fn, *args, **kwargs)
            return True
        except queue.Full:
            logger.warning(
                "safe_submit: executor %r queue is full, task dropped",
                executor_name,
            )
            return False
        except RuntimeError as exc:
            logger.warning(
                "safe_submit: executor %r rejected task: %s",
                executor_name,
                exc,
            )
            return False
