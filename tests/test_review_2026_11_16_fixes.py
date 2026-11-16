"""Regression tests for the 2026-11-16 full-project review.

Two root causes are covered:

* Leader election revoked leadership based on a mismatched owner-PID marker
  even when the process held no inherited lock handle, so any reset that
  cleared the handle without clearing the marker silently demoted a healthy
  master on the next ``is_leader()`` call.
* Elapsed-time budgets in shutdown/waits used the wall clock, so an NTP or
  manual clock adjustment could stretch a wait by hours or end it early.
"""

from __future__ import annotations

import os
import types

import app_bg
import bg.leader_election as le
import execution_state
from app_state import app_state
from native_host import start_backend
from utils import stock_payload


class _FakeClock:
    """Clock stub: monotonic is fixed; reading the wall clock is a failure."""

    @staticmethod
    def monotonic() -> float:
        return 500.0

    @staticmethod
    def time() -> float:
        raise AssertionError("elapsed-time budget must not read the wall clock")

    @staticmethod
    def sleep(_seconds: float) -> None:
        return None


def test_is_leader_keeps_leadership_for_stale_pid_without_handle():
    le._set_leader_lock_file(None)
    le._set_is_sync_leader(True)
    try:
        # A partial reset cleared the handle but left the owner PID behind.
        le._LEADER_LOCK_PID = 424242
        assert le.is_leader() is True
        # The stale marker is discarded instead of being treated as a fork.
        assert le._LEADER_LOCK_PID is None
    finally:
        le._set_leader_lock_file(None)
        le._set_is_sync_leader(True)


def test_is_leader_detaches_foreign_pid_with_inherited_handle(monkeypatch):
    class _Handle:
        def close(self) -> None:
            raise AssertionError("an inherited handle must not be closed")

    le._set_leader_lock_file(_Handle())
    le._set_is_sync_leader(True)
    fake_os = types.SimpleNamespace(getpid=lambda: os.getpid() + 1)
    monkeypatch.setattr(le, "os", fake_os)
    try:
        assert le.is_leader() is False
        assert le._get_leader_lock_file() is None
    finally:
        le._set_leader_lock_file(None)
        le._set_is_sync_leader(True)


def test_shutdown_join_budget_uses_monotonic_clock(monkeypatch):
    class _FakeThread:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            self.timeouts.append(timeout)

    state = execution_state.ExecutionState()
    thread = _FakeThread()
    state.register_background_thread(thread)
    monkeypatch.setattr(execution_state, "time", _FakeClock)

    state.shutdown(wait=False, max_join_timeout=3.0)

    assert len(thread.timeouts) == 1
    assert thread.timeouts[0] is not None
    assert 0 < thread.timeouts[0] <= 3.0


def test_initial_snapshot_wait_budget_uses_monotonic_clock(monkeypatch):
    monkeypatch.setattr(stock_payload, "time", _FakeClock)
    monkeypatch.setattr(stock_payload, "_has_ready_stocks_snapshot", lambda: False)
    monkeypatch.setattr(app_state.market, "first_sync_attempted", False)
    monkeypatch.setattr(app_bg, "schedule_sync_all_stocks_now", lambda: None)

    assert stock_payload._wait_for_initial_market_snapshot("stocks", timeout_sec=0.0) is False


def test_backend_ready_budget_uses_monotonic_clock(monkeypatch):
    def _unexpected_request(*_args, **_kwargs):
        raise AssertionError("health check must not run with a zero budget")

    monkeypatch.setattr(start_backend, "time", _FakeClock)
    monkeypatch.setattr(start_backend, "get_backend_port", lambda: 65535)
    monkeypatch.setattr(start_backend, "requests", types.SimpleNamespace(get=_unexpected_request))

    assert start_backend.wait_for_backend_ready(0.0) is False
