"""Regressions for the bounded DDGS fan-out pool.

The shared pool in ``services/search/ddgs.py`` is bounded (3 workers plus 30
queued tasks, see ``_DDGS_SEARCH_MAX_QUEUE_SIZE``). ``submit`` therefore raises
``queue.Full`` when concurrent requests saturate it, and ``RuntimeError`` once
``shutdown_ddgs_pool`` has run. A refused submission used to abort the whole
fan-out from the comprehension, discarding the results of every query that had
already been submitted.
"""

from __future__ import annotations

import queue
import threading
import time as real_time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import pytest

from services.search import ddgs as ddgs_module
from utils.threading import DaemonThreadPoolExecutor


class _RefusingPool:
    """Runs submissions synchronously and refuses the N-th one."""

    def __init__(self, refuse_at: int, error: BaseException) -> None:
        self._refuse_at = refuse_at
        self._error = error
        self.submitted: list[str] = []

    def submit(self, fn: Callable[..., Any], idx: int, query: str) -> Future[Any]:
        self.submitted.append(query)
        if len(self.submitted) == self._refuse_at:
            raise self._error
        future: Future[Any] = Future()
        future.set_result(fn(idx, query))
        return future


def _install_search_seams(monkeypatch: pytest.MonkeyPatch, pool: _RefusingPool) -> list[str]:
    seen: list[str] = []

    def _news(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        seen.append(query)
        return [
            {
                "title": f"news-{query}",
                "body": f"body-{query}",
                "url": f"https://example.test/{query}",
            }
        ]

    def _text(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(ddgs_module, "ddgs_news_search", _news)
    monkeypatch.setattr(ddgs_module, "ddgs_text_search", _text)
    monkeypatch.setattr(ddgs_module, "_get_ddgs_search_pool", lambda: pool)
    monkeypatch.setattr(ddgs_module.time, "sleep", lambda *_: None)
    return seen


@pytest.mark.parametrize(
    "error",
    [
        queue.Full("ThreadPoolExecutor queue is full"),
        RuntimeError("cannot schedule new futures after shutdown"),
    ],
    ids=["queue-full", "pool-shut-down"],
)
def test_refused_submission_keeps_results_of_accepted_queries(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    pool = _RefusingPool(refuse_at=2, error=error)
    seen = _install_search_seams(monkeypatch, pool)

    items = ddgs_module._collect_ddgs_items(
        ["alpha", "beta", "gamma"], "us-en", "d", 5, 5, limit=10, query_limit=3
    )

    assert pool.submitted == ["alpha", "beta", "gamma"], (
        "a refused query must not stop the queries that follow it"
    )
    assert seen == ["alpha", "gamma"], "the refused query must not be executed"
    assert [item["title"] for item in items] == ["news-alpha", "news-gamma"]


def test_results_stay_in_query_order_when_completion_reverses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow first query must not reorder or drop the merged results.

    The merge is keyed by query index, so the completion order of the pool is
    irrelevant. The fixture finishes ``gamma`` before ``alpha`` to prove the
    ordering comes from the submission index rather than from timing.
    """
    delays = {"alpha": 0.20, "beta": 0.05, "gamma": 0.0}
    completion: list[str] = []
    lock = threading.Lock()

    def _news(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        real_time.sleep(delays[query])
        with lock:
            completion.append(query)
        return [
            {
                "title": f"news-{query}",
                "body": f"body-{query}",
                "url": f"https://example.test/{query}",
            }
        ]

    monkeypatch.setattr(ddgs_module, "ddgs_news_search", _news)
    monkeypatch.setattr(ddgs_module, "ddgs_text_search", lambda query, **kwargs: [])
    # Neutralise the per-query jitter inside _collect_one; the deliberate delays
    # above are what create the reversed completion order.
    monkeypatch.setattr(ddgs_module.random, "uniform", lambda _a, _b: 0.0)
    pool = DaemonThreadPoolExecutor(max_workers=3)
    monkeypatch.setattr(ddgs_module, "_get_ddgs_search_pool", lambda: pool)
    try:
        items = ddgs_module._collect_ddgs_items(
            ["alpha", "beta", "gamma"], "us-en", "d", 5, 5, limit=10, query_limit=3
        )
    finally:
        pool.shutdown(wait=True)

    assert completion[0] == "gamma", f"fixture must finish gamma first, got {completion}"
    assert completion[-1] == "alpha", f"fixture must finish alpha last, got {completion}"
    assert [item["title"] for item in items] == [
        "news-alpha",
        "news-beta",
        "news-gamma",
    ], "merged results must follow query order, not completion order"
