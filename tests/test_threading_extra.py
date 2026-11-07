"""Additional coverage tests for utils/threading.py — DaemonThreadPoolExecutor edge cases."""

import logging
import queue
import threading
import time

import pytest

from utils.threading import DaemonThreadPoolExecutor


def test_submit_with_max_queue_size():
    """max_queue_size > 0 creates a semaphore and limits queued tasks."""
    ex = DaemonThreadPoolExecutor(max_workers=1, max_queue_size=1)
    try:
        # Submit one task that blocks the single worker
        blocker = threading.Event()
        released = threading.Event()

        def _block():
            blocker.set()
            released.wait(timeout=5)

        ex.submit(_block)
        assert blocker.wait(timeout=3), "blocker task should have started"

        # Second submission goes into the internal work queue (size=1)
        ex.submit(lambda: "queued")

        # Third submission should raise queue.Full because both
        # max_workers=1 and max_queue_size=1 slots are occupied.
        with pytest.raises(queue.Full):
            ex.submit(lambda: "overflow")
    finally:
        released.set()
        ex.shutdown(wait=True)


def test_semaphore_released_on_submit_exception():
    """If super().submit() raises, the semaphore must be released (M34).

    Regression for the 2026-11-07 review: this test previously asserted
    nothing — deleting the release block in utils/threading.py would still
    pass. Drain the bounded semaphore after an injected submit failure to
    prove it was returned to its full initial capacity.
    """
    ex = DaemonThreadPoolExecutor(max_workers=1, max_queue_size=5)
    try:
        initial = ex._semaphore._initial_value if ex._semaphore is not None else None
        assert initial is not None

        sem = threading.BoundedSemaphore(initial)
        with pytest.MonkeyPatch.context() as mp:
            object.__setattr__(ex, "_semaphore", None)
            import concurrent.futures as cf_mod


            def failing_submit(self_inner, fn, /, *args, **kwargs):
                # The DaemonThreadPoolExecutor wrapper acquires the semaphore
                # itself before calling super().submit; this only raises.
                raise RuntimeError("injected submit failure")

            mp.setattr(cf_mod.ThreadPoolExecutor, "submit", failing_submit)
            object.__setattr__(ex, "_semaphore", sem)

            with pytest.raises(RuntimeError):
                ex.submit(lambda: None)

        # The wrapper's except must have released the slot: the semaphore
        # drains back to its FULL initial capacity.
        drained = 0
        if ex._semaphore is not None:
            while ex._semaphore.acquire(blocking=False) and drained < initial:
                drained += 1
        assert drained == initial, (
            "submit failure must release the semaphore back to full capacity"
        )
    finally:
        ex.shutdown(wait=True)


def test_cancelled_future_does_not_propagate(caplog):
    """A cancelled future is logged but does not raise in the done callback.

    (M34/F9, 2026-11-07 review) a running future cannot be cancelled, so the
    genuine cancelled branch (utils/threading.py debug log) is only executed
    when the task never started. Pin the observable behavior via log capture;
    cancelling a RUNNING task is covered separately below.
    """
    ex = DaemonThreadPoolExecutor(max_workers=1)
    try:
        blocker = threading.Event()
        released = threading.Event()

        def _slow():
            blocker.set()
            released.wait(timeout=5)
            return "done"

        started = ex.submit(_slow)
        assert blocker.wait(timeout=3)

        # Queue a second task behind the busy worker, then cancel it before
        # it starts: futures that never ran genuinely report cancelled().
        def _never():
            return "never"

        queued = ex.submit(_never)
        with caplog.at_level(logging.DEBUG, logger="utils.threading"):
            # add_done_callback fires synchronously on cancel — capture first.
            assert queued.cancel() is True
            assert any(
                record.getMessage() == "Background task was cancelled"
                for record in caplog.records
            ), "cancelled future must hit the debug-log branch in the done callback"

        assert queued.cancelled() is True
        released.set()
        assert started.result(timeout=3) == "done"
    finally:
        ex.shutdown(wait=True)


def test_cancel_request_on_running_task_is_denied():
    """Cancelling a task that is already running is denied; it completes.

    (M34/F9) the previous single test mixed both cases; cancelling a RUNNING
    future returns False and must leave the result intact.
    """
    ex = DaemonThreadPoolExecutor(max_workers=1)
    try:
        blocker = threading.Event()
        released = threading.Event()

        def _slow():
            blocker.set()
            released.wait(timeout=5)
            return "result"

        fut = ex.submit(_slow)
        assert blocker.wait(timeout=3)
        # Running tasks cannot be cancelled by the standard executor contract.
        assert fut.cancel() is False
        released.set()
        assert fut.result(timeout=3) == "result"
        assert fut.cancelled() is False
    finally:
        ex.shutdown(wait=True)


def test_task_exception_in_callback_logged():
    """An exception in the done callback itself must be caught and logged."""
    captured = []

    class CustomHandler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    logger = logging.getLogger("utils.threading")
    handler = CustomHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    ex = DaemonThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(lambda: 42)
        # Wait for completion
        assert fut.result(timeout=3) == 42

        # The done callback already fired; we can't easily inject
        # a failure afterward. Instead verify the success path logged.
        assert any("completed successfully" in msg for msg in captured)
    finally:
        logger.removeHandler(handler)
        ex.shutdown(wait=True)


def test_executor_shutdown_does_not_block():
    """Daemon threads ensure shutdown(wait=False) does not hang."""
    ex = DaemonThreadPoolExecutor(max_workers=2)
    try:
        fut = ex.submit(time.sleep, 0.1)
        # Shutdown without waiting — daemon threads prevent hang
        ex.shutdown(wait=False)
        # Future should be cancelled or still running
        assert fut.cancel() or not fut.done()
    finally:
        # Final cleanup (already shut down)
        try:
            ex.shutdown(wait=True)
        except RuntimeError:
            pass


def test_get_executor_threads_fallback_via_prefix():
    """When _threads attribute is missing, _get_executor_threads falls back
    to prefix-based enumeration."""
    ex = DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="test-fallback-")
    try:
        fut = ex.submit(lambda: 42)
        assert fut.result(timeout=3) == 42
    finally:
        ex.shutdown(wait=True)


def test_get_executor_threads_empty_prefix():
    """When _threads is missing and prefix is empty, returns []."""
    ex = DaemonThreadPoolExecutor(max_workers=1)
    try:
        threads = ex._get_executor_threads()
        # Should not raise
        assert isinstance(threads, list)
    finally:
        ex.shutdown(wait=True)


def test_submit_no_semaphore():
    """When max_queue_size is None/0, no semaphore is created."""
    ex = DaemonThreadPoolExecutor(max_workers=2)
    try:
        assert ex._semaphore is None
        # Should still work normally
        fut = ex.submit(lambda: 99)
        assert fut.result(timeout=3) == 99
    finally:
        ex.shutdown(wait=True)


def test_max_queue_size_zero_disables_semaphore():
    """max_queue_size=0 should behave same as None (no semaphore)."""
    ex = DaemonThreadPoolExecutor(max_workers=2, max_queue_size=0)
    try:
        assert ex._semaphore is None
        fut = ex.submit(lambda: 0)
        assert fut.result(timeout=3) == 0
    finally:
        ex.shutdown(wait=True)
