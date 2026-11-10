"""Backpressure regressions for the agents-mode SSE bridge.

The producer runs inside ``agent.run`` and hands deltas to the SSE consumer via
a bounded ``queue.Queue``.  ``queue.Queue.put`` blocks forever on a full queue
and cannot observe the consumer's abort event while parked, so a consumer that
left without draining used to strand the worker thread, its asyncio loop and
the provider HTTP stream indefinitely — the SDK's ``timeout=`` cannot fire
while the synchronous ``on_event`` callback is blocked.

These tests pin the two mechanisms that make the abort reachable:

* ``_put_with_abort`` polls the abort event instead of blocking forever;
* the consumer drains the queue before joining the worker, so a producer
  parked in ``put()`` can make progress and then see the abort.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app_state import app_state
from services import ai_service

# The agents bridge defines its abort signal as a function-local exception, so
# the only handle a test has on it is its qualified name.
_ABORT_QUALNAME = "stream_mistral_chat.<locals>._AgentConsumerAborted"

# The agents bridge uses a 512-slot queue.
_QUEUE_CAPACITY = 512


def test_put_with_abort_enqueues_when_room_available() -> None:
    q: queue.Queue[object] = queue.Queue(maxsize=1)

    assert ai_service._put_with_abort(q, "delta", threading.Event()) is True
    assert q.get_nowait() == "delta"


def test_put_with_abort_drops_item_when_consumer_aborts_full_queue() -> None:
    q: queue.Queue[object] = queue.Queue(maxsize=1)
    q.put("queued")
    abort = threading.Event()
    outcome: list[bool] = []

    def _enqueue() -> None:
        outcome.append(ai_service._put_with_abort(q, "dropped", abort))

    worker = threading.Thread(target=_enqueue, daemon=True, name="mns-test-enqueue")
    worker.start()
    timer = threading.Timer(0.05, abort.set)
    timer.start()
    # A blocking ``put`` never returns here, so the join is the assertion: the
    # polling enqueue must observe the abort and give up on its own.
    worker.join(timeout=5.0)
    timer.join(timeout=5.0)

    assert not worker.is_alive(), "_put_with_abort blocked instead of observing the abort"
    assert outcome == [False]
    assert q.qsize() == 1, "the abort path must not enqueue into a full queue"


def test_drain_queue_returns_count_and_empties() -> None:
    q: queue.Queue[int] = queue.Queue()
    for i in range(3):
        q.put(i)

    assert ai_service._drain_queue(q) == 3
    assert q.empty()
    assert ai_service._drain_queue(q) == 0


def _fake_agents_module() -> SimpleNamespace:
    class TextDelta:
        def __init__(self, text: str):
            self.text = text

    return SimpleNamespace(TextDelta=TextDelta)


@pytest.fixture()
def agents_stream_patches():
    def _apply(agent):
        return [
            patch("services.ai_service._resolve_agents_target", return_value=(True, "")),
            patch("services.ai_service.HAS_AGENTS_SDK", True),
            patch("services.ai_service.mistral_agents", _fake_agents_module()),
            patch("services.ai_service._get_mistral_client", return_value=object()),
            patch("services.ai_service._acquire_mistral_call_slot", return_value=0.0),
            patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
            patch("services.ai_service.load_sdk_agent", return_value=agent),
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        ]

    return _apply


def test_cancelled_agent_run_reports_error_not_empty_done(agents_stream_patches) -> None:
    """``asyncio.CancelledError`` is a BaseException; it must not look like success."""

    class _CancelledAgent:
        async def run(self, content, *, on_event=None, timeout=None):
            raise asyncio.CancelledError()

    reports: list[dict] = []

    def _record_report(*args, **kwargs):
        reports.append(kwargs)

    from contextlib import ExitStack

    with ExitStack() as stack:
        for patcher in agents_stream_patches(_CancelledAgent()):
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(
                app_state.market,
                "report_circuit_result",
                side_effect=_record_report,
            )
        )
        events = list(
            ai_service.stream_mistral_chat("k", [{"role": "user", "content": "x"}])
        )

    assert any(event.get("type") == "error" for event in events)
    assert not any(event.get("type") == "done" for event in events)
    assert not any(report.get("success") is True for report in reports)


def test_consumer_close_reclaims_flooding_worker(agents_stream_patches) -> None:
    """Closing the SSE generator must stop a producer parked on a full queue.

    The agent floods deltas without awaiting, so the bounded queue fills and the
    producer parks inside the enqueue.  A blocking ``queue.Queue.put`` cannot
    observe the abort event while parked; only the polling enqueue and the
    pre-join drain let it return and see the flag.  The spy announces the exact
    enqueue that must find the queue full, so the generator is closed while the
    producer is provably parked rather than while it still has room.
    """
    mods = _fake_agents_module()
    finished = threading.Event()
    parked = threading.Event()
    observed: list[BaseException] = []
    delivered = 0
    counter_lock = threading.Lock()
    real_put = ai_service._put_with_abort

    def _spy_put(work_queue, item, abort) -> bool:
        nonlocal delivered
        with counter_lock:
            attempt = delivered
        if attempt == _QUEUE_CAPACITY:
            parked.set()
        placed = real_put(work_queue, item, abort)
        if placed:
            with counter_lock:
                delivered += 1
        return placed

    class _FloodingAgent:
        async def run(self, content, *, on_event=None, timeout=None):
            try:
                for i in range(5000):
                    assert on_event is not None
                    on_event(mods.TextDelta(f"d{i} "))
                raise AssertionError(
                    "producer drained all deltas: the queue never applied backpressure"
                )
            except BaseException as exc:
                observed.append(exc)
                raise
            finally:
                finished.set()
            return SimpleNamespace(text="done")

    agent = _FloodingAgent()
    patches = [
        *agents_stream_patches(agent),
        patch("services.ai_service._put_with_abort", side_effect=_spy_put),
    ]
    for p in patches:
        p.start()
    gen = None
    try:
        gen = ai_service.stream_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "hi"}],
        )
        first = next(gen)
        assert first["type"] == "delta"
        assert parked.wait(timeout=10.0), (
            "producer never reached the full queue, so backpressure is untested"
        )
        # The producer must still be parked when the consumer leaves: the
        # polling implementation is inside its first 0.25 s wait at this point.
        assert not finished.wait(timeout=0.1), "producer finished before the close"
        gen.close()
    finally:
        if gen is not None:
            gen.close()
        for p in reversed(patches):
            p.stop()

    assert finished.wait(timeout=10.0), "producer thread body did not finish"
    assert [type(e).__qualname__ for e in observed] == [_ABORT_QUALNAME], (
        f"producer stopped for the wrong reason: {observed!r}"
    )


def test_consumer_close_reclaims_slow_worker(agents_stream_patches) -> None:
    """The cooperative abort still reaches a worker that awaits between events."""
    mods = _fake_agents_module()
    finished = threading.Event()
    observed: list[BaseException] = []

    class _SlowAgent:
        async def run(self, content, *, on_event=None, timeout=None):
            try:
                for i in range(500):
                    assert on_event is not None
                    on_event(mods.TextDelta(f"d{i} "))
                    await asyncio.sleep(0.02)
            except BaseException as exc:
                observed.append(exc)
                raise
            finally:
                finished.set()
            return SimpleNamespace(text="done")

    agent = _SlowAgent()
    patches = agents_stream_patches(agent)
    for p in patches:
        p.start()
    gen = None
    try:
        gen = ai_service.stream_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "hi"}],
        )
        assert next(gen)["type"] == "delta"
        gen.close()
    finally:
        if gen is not None:
            gen.close()
        for p in reversed(patches):
            p.stop()

    assert finished.wait(timeout=10.0), "slow producer thread body did not finish"
    assert [type(e).__qualname__ for e in observed] == [_ABORT_QUALNAME], (
        f"slow producer stopped for the wrong reason: {observed!r}"
    )
