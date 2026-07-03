"""
utils/threading.py - Shared threading utilities.

Provides a DaemonThreadPoolExecutor used across the application to avoid
duplication between app_state.py and trend_sources.py.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor subclass that spawns daemon threads and prevents
    blocking shutdown on interpreter exit.

    The standard ThreadPoolExecutor creates non-daemon threads, which can
    prevent the Python interpreter from exiting cleanly. This subclass
    ensures every worker thread is a daemon thread.
    """

    def _get_executor_threads(self):
        """Get worker threads belonging to this executor across Python
        versions."""
        try:
            # Python 3.9+: _threads is a set of Thread objects
            return list(self._threads)
        except AttributeError:
            pass
        # Fallback: enumerate all threads and match by prefix
        prefix = getattr(self, "_thread_name_prefix", "") or ""
        if prefix:
            return [
                t
                for t in threading.enumerate()
                if t.name and t.name.startswith(prefix)
            ]
        return []

    def submit(self, fn, /, *args, **kwargs):
        future = super().submit(fn, *args, **kwargs)
        try:
            for t in self._get_executor_threads():
                if not t.daemon:
                    t.daemon = True
        except Exception as exc:
            logger.debug("Failed to set executor thread daemon mode: %s", exc)

        def _done_callback(fut):
            try:
                exc = fut.exception()
                if exc:
                    logger.error(
                        "Background task %s failed with exception: %s",
                        fn.__name__ if hasattr(fn, "__name__") else str(fn),
                        exc,
                        exc_info=exc,
                    )
                else:
                    logger.debug(
                        "Background task %s completed successfully",
                        fn.__name__ if hasattr(fn, "__name__") else str(fn),
                    )
            except Exception as cb_exc:
                logger.error("Error in background task done callback: %s", cb_exc)

        future.add_done_callback(_done_callback)
        return future
