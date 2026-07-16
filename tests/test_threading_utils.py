"""Tests for utils/threading.py — DaemonThreadPoolExecutor behavior."""

from utils.threading import DaemonThreadPoolExecutor


def test_submit_runs_task_and_success_callback():
    captured = []

    def work(value):
        captured.append(value)
        return value + 1

    ex = DaemonThreadPoolExecutor(max_workers=2)
    try:
        fut = ex.submit(work, 41)
        assert fut.result(timeout=3) == 42
        assert captured == [41]
    finally:
        ex.shutdown(wait=True)


def test_submit_exception_is_captured_and_logged():
    def boom(_):
        raise RuntimeError("fail-task")

    ex = DaemonThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(boom, 1)
        # Exception must be captured by the future, not propagated here.
        assert fut.exception(timeout=3) is not None
    finally:
        ex.shutdown(wait=True)
