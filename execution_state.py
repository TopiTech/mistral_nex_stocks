"""
execution_state.py - Thread pool and background task execution management.

Extracted from app_state.py to reduce module complexity.
"""

import threading
from utils.threading import DaemonThreadPoolExecutor


class ExecutionState:
    """Manages thread pools and background task execution."""

    def __init__(self):
        self.executor = DaemonThreadPoolExecutor(max_workers=5)
        self.news_executor = DaemonThreadPoolExecutor(max_workers=4)
        self.sync_refresh_executor = DaemonThreadPoolExecutor(max_workers=1)
        self.shutdown_event = threading.Event()
        self.background_threads: list[threading.Thread] = []

    def shutdown(self):
        """Shut down all executors without blocking."""
        self.shutdown_event.set()
        for ex in [self.executor, self.news_executor, self.sync_refresh_executor]:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)

        for t in self.background_threads:
            try:
                if t.is_alive():
                    t.join(timeout=2.0)
            except Exception:
                pass
