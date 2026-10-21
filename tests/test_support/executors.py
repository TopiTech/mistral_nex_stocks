"""Synchronous executor mocks for test isolation."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any


class SynchronousExecutor:
    """A synchronous executor drop-in replacement for concurrent.futures.ThreadPoolExecutor.

    Executes jobs immediately in the calling thread to eliminate async timing
    races and prevent background thread leaks during test teardown.
    """

    def submit(self, fn, *args, **kwargs) -> Future[Any]:
        f: Future[Any] = Future()
        try:
            res = fn(*args, **kwargs)
            f.set_result(res)
        except Exception as e:
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        if not iterables:
            return []
        return [fn(*args) for args in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass
