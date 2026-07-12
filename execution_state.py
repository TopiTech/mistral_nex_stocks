"""
execution_state.py - Thread pool and background task execution management.

Extracted from app_state.py to reduce module complexity.
"""

import logging
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
