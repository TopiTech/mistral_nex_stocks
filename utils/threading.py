"""
utils/threading.py - Shared threading utilities.

Provides a DaemonThreadPoolExecutor used across the application to avoid
duplication between app_state.py and trend_sources.py.
"""

import logging
import queue
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker
from typing import Any, cast

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
        """Return this executor's workers for diagnostics/backward compatibility."""
        try:
            return list(self._threads)
        except AttributeError:
            prefix = getattr(self, "_thread_name_prefix", "") or ""
            if prefix:
                return [
                    thread for thread in threading.enumerate() if thread.name.startswith(prefix)
                ]
            return []

    def _adjust_thread_count(self):
        """Start daemon workers before they run any submitted task.

        ``ThreadPoolExecutor`` creates non-daemon workers inside
        ``super().submit()``. Changing ``thread.daemon`` afterwards always
        raises RuntimeError, so the previous best-effort approach could leave
        process shutdown blocked by a stuck network call.
        """
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, work_queue=self._work_queue):
            cast(Any, work_queue).put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            executor_ref = weakref.ref(self, weakref_cb)

            import inspect

            try:
                param_count = len(inspect.signature(_worker).parameters)
            except Exception:
                param_count = 3 if hasattr(self, "_create_worker_context") else 4

            if param_count == 3:
                ctx = (
                    self._create_worker_context()
                    if hasattr(self, "_create_worker_context")
                    else None
                )
                worker_args: tuple[Any, ...] = (executor_ref, ctx, self._work_queue)
            else:
                init = getattr(self, "_initializer", None)
                initargs = getattr(self, "_initargs", ())
                worker_args = (executor_ref, self._work_queue, init, initargs)

            worker = threading.Thread(
                name=thread_name,
                target=_worker,
                args=worker_args,
                daemon=True,
            )
            worker.start()
            cast(Any, self._threads).add(worker)
            # Do not register daemon workers in concurrent.futures' private
            # interpreter-exit join registry. Explicit shutdown still sends
            # their sentinel; leaving the registry entry would make CPython
            # join an active worker at exit and defeat daemon semantics.

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
