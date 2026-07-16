"""
utils/threading.py - Shared threading utilities.

Provides a DaemonThreadPoolExecutor used across the application to avoid
duplication between app_state.py and trend_sources.py.
"""

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor subclass that spawns daemon threads and prevents
    blocking shutdown on interpreter exit, with an optional bounded queue limit.
    """

    _semaphore: threading.BoundedSemaphore | None

    def __init__(
        self,
        max_workers=None,
        max_queue_size=None,
        thread_name_prefix="",
        initializer=None,
        initargs=(),
    ):
        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=initializer,
            initargs=initargs,
        )
        self._max_queue_size = max_queue_size
        if max_queue_size is not None and max_queue_size > 0:
            self._semaphore = threading.BoundedSemaphore(self._max_workers + max_queue_size)
        else:
            self._semaphore = None

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
            return [t for t in threading.enumerate() if t.name and t.name.startswith(prefix)]
        return []

    def submit(self, fn, /, *args, **kwargs):
        if self._semaphore is not None:
            acquired = self._semaphore.acquire(blocking=False)
            if not acquired:
                raise queue.Full("ThreadPoolExecutor queue is full")

        def _wrapper(*w_args, **w_kwargs):
            return fn(*w_args, **w_kwargs)

        try:
            future = super().submit(_wrapper, *args, **kwargs)
        except Exception:
            if self._semaphore is not None:
                try:
                    self._semaphore.release()
                except ValueError:
                    pass
            raise

        try:
            for t in self._get_executor_threads():
                if not t.daemon:
                    t.daemon = True
        except Exception as exc:
            logger.debug("Failed to set executor thread daemon mode: %s", exc)

        def _done_callback(fut):
            if self._semaphore is not None:
                try:
                    self._semaphore.release()
                except ValueError:
                    pass

            try:
                if fut.cancelled():
                    logger.debug("Background task was cancelled")
                    return
                exc = fut.exception()
                if exc:
                    logger.error(
                        "Background task failed with exception: %s",
                        exc,
                        exc_info=exc,
                    )
                else:
                    logger.debug("Background task completed successfully")
            except Exception as cb_exc:
                logger.error("Error in background task done callback: %s", cb_exc)

        future.add_done_callback(_done_callback)
        return future
