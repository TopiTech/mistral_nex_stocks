"""Unit 2 comprehensive review fixes regression tests (2026-11-28)."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest


def test_extract_batch_history_does_not_leak_flat_frame_across_multi_symbol() -> None:
    from bg.sync.fetch import extract_batch_history

    flat = pd.DataFrame(
        {"Open": [100.0], "High": [105.0], "Low": [99.0], "Close": [102.0], "Volume": [1000]},
        index=pd.to_datetime(["2026-11-27"]),
    )

    # Multi-symbol batch (single_symbol=False) must NOT assign flat single-ticker frame to every symbol.
    res_multi = extract_batch_history(flat, "MSFT", single_symbol=False)
    assert res_multi.empty

    # Single-symbol batch still extracts the flat frame cleanly.
    res_single = extract_batch_history(flat, "AAPL", single_symbol=True)
    assert len(res_single) == 1
    assert float(res_single["Close"].iloc[0]) == 102.0


def test_fetch_index_data_normalizes_trailing_nan_and_lowercase_and_resets_streak() -> None:
    from app_state import app_state
    from bg.sync import fetch as sync_fetch

    # Trailing holiday NaN row after two valid lowercase rows
    hist = pd.DataFrame(
        {
            "open": [39000.0, 39500.0, float("nan")],
            "high": [39200.0, 39800.0, float("nan")],
            "low": [38900.0, 39400.0, float("nan")],
            "close": [39100.0, 39700.0, float("nan")],
            "volume": [100, 200, 0],
        },
        index=pd.to_datetime(["2026-11-25", "2026-11-26", "2026-11-27"]),
    )

    reset_calls: list[bool] = []
    with (
        patch.object(sync_fetch._shim(), "acquire_yfinance_slot", return_value=True),
        patch.object(app_state.stock_provider, "get_history", return_value=hist),
        patch.object(
            sync_fetch,
            "_reset_yfinance_streak_on_success",
            side_effect=lambda: reset_calls.append(True),
        ),
    ):
        res = sync_fetch.fetch_index_data("N225", "^N225")

    assert res is not None
    key, payload = res
    assert key == "N225"
    assert payload["price"] == 39700.0
    assert payload["change"] == 600.0
    assert reset_calls == [True]


def test_start_background_worker_concurrent_spawn_is_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app_state import app_state
    from bg.sync import control as sync_control

    app_state.execution.shutdown_event.clear()
    orig_threads = app_state.execution.get_background_threads()
    with app_state.execution._threads_lock:
        app_state.execution.background_threads.clear()

    created_names: list[str] = []
    barrier = threading.Barrier(2)

    class _FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None) -> None:
            self.target = target
            self.args = args
            self.name = name or ""
            self.daemon = daemon
            self._alive = False
            created_names.append(self.name)

        def start(self) -> None:
            self._alive = True

        def is_alive(self) -> bool:
            return self._alive

    orig_thread_cls = sync_control.threading.Thread
    monkeypatch.setattr(sync_control.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sync_control, "_get_app_bg_attr", lambda _name, default=None: default)
    monkeypatch.setattr("utils.storage.load_user_stocks", lambda force=False: None)
    monkeypatch.setattr(
        "services.realtime_engine.realtime_market_engine",
        SimpleNamespace(register_symbols=lambda *_a, **_k: None, start=lambda: None),
    )

    try:
        errors: list[BaseException] = []

        def _invoke() -> None:
            try:
                barrier.wait(timeout=3.0)
                sync_control.start_background_worker()
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        t1 = orig_thread_cls(target=_invoke)
        t2 = orig_thread_cls(target=_invoke)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert not errors
        threads = app_state.execution.get_background_threads()
        assert len(threads) == 5
        assert sorted(created_names) == sorted(
            ["Yahoo", "LeaderElection", "SessionReap", "Interpolate", "Watchdog"]
        )
    finally:
        with app_state.execution._threads_lock:
            app_state.execution.background_threads[:] = orig_threads


def test_sse_interpolator_rejects_negative_prev_close_and_drifted_index_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bg import sse_interpolator as interp

    monkeypatch.setattr(interp, "_get_simulate_fluctuation", lambda: True)
    monkeypatch.setattr(interp.random, "random", lambda: 0.0)
    monkeypatch.setattr(interp.random, "uniform", lambda _a, _b: 0.0001)

    # 1) Stock with invalid negative inferred previous_close (change > price) must NOT invert sign
    targets = [
        {
            "symbol": "TEST",
            "price": 10.0,
            "change": 20.0,  # target_price - target_change = -10.0 (invalid!)
            "change_percent": 5.0,
        },
        {
            "symbol": "AAPL",
            "price": 100.0,
            "change": 2.0,
            "previous_close": 98.5,  # explicit previous_close must be preferred
            "change_percent": 1.52,
        },
    ]
    res = interp._interpolate_and_fluctuate_market(targets, [], is_open=True, market="us")
    by_sym = {item["symbol"]: item for item in res}
    assert by_sym["TEST"]["change"] == 20.0
    assert by_sym["TEST"]["change_percent"] == 5.0
    # AAPL uses explicit previous_close=98.5: published_price - 98.5
    assert by_sym["AAPL"]["change"] == round(by_sym["AAPL"]["price"] - 98.5, 4)

    # 2) _fluctuate_indices resets drifted change when adopting target price without target change,
    #    and refuses negative prev_close.
    indices = {"DJI": {"price": 200.0, "change": 250.0, "percent": 1.0}}
    target_indices = {"DJI": {"price": 200.0}}  # change omitted in target
    interp._fluctuate_indices(indices, us_open=True, jp_open=False, target_indices=target_indices)
    # prev_close = 200.0 - 0.0 = 200.0 (not 200.0 - 250.0 = -50.0!)
    assert indices["DJI"]["change"] == round(indices["DJI"]["price"] - 200.0, 2)
    assert indices["DJI"]["percent"] == round((indices["DJI"]["change"] / 200.0) * 100, 2)

    neg_indices = {"DJI": {"price": 10.0, "change": 20.0, "percent": 5.0}}
    neg_targets = {"DJI": {"price": 10.0, "change": 20.0}}
    interp._fluctuate_indices(neg_indices, us_open=True, jp_open=False, target_indices=neg_targets)
    assert neg_indices["DJI"]["percent"] == 5.0


def test_sync_control_restores_sync_pending_when_reschedule_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app_state import app_state
    from bg.sync import control as sync_control

    app_state.execution.shutdown_event.clear()
    with app_state.market.sync_schedule_lock:
        app_state.market.sync_scheduled = False
        app_state.market.sync_pending = True
    with app_state.market.is_syncing_lock:
        app_state.market.is_syncing = False

    monkeypatch.setattr(sync_control, "sync_all_stocks_now", lambda force_fetch=False: None)
    monkeypatch.setattr(sync_control, "schedule_sync_all_stocks_now", lambda force=False: False)

    sync_control._run_scheduled_sync_job()
    with app_state.market.sync_schedule_lock:
        assert app_state.market.sync_pending is True
        app_state.market.sync_pending = False


def test_leader_election_shutdown_release_race_does_not_leave_is_leader_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app_state import app_state
    from bg import leader_election as le

    le._release_leader_lock()
    orig_shutdown = app_state.execution.shutdown_event.is_set()
    try:
        app_state.execution.shutdown_event.set()  # exit loop after initial attempt

        def _acquire_then_concurrent_release() -> bool:
            le._release_leader_lock()
            return True

        with (
            patch.object(le, "_try_acquire_leader_lock", side_effect=_acquire_then_concurrent_release),
            patch.object(le, "_get_app_bg_attr", side_effect=lambda _n, default=None: default),
        ):
            le.bg_leader_election_loop()
            assert le.is_leader() is False
    finally:
        le._release_leader_lock()
        if not orig_shutdown:
            app_state.execution.shutdown_event.clear()
