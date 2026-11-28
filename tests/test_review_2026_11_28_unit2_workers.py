"""Regression tests for Unit 2 runtime state, session manager, messaging, and background workers."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest


def test_session_manager_compute_wait_staggers_concurrent_threads_and_throttles_decay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1A-01 & D1A-08: Concurrent _compute_wait calls must stagger slots; decay must happen once per window."""
    import session_manager as sm

    sm.YFinanceSessionManager._reset_for_testing()
    mgr = sm.YFinanceSessionManager()
    try:
        mgr._adaptive_interval_sec = 2.0
        mgr._last_block_ts = 100.0
        mgr._last_decay_ts = 100.0
        mgr._last_request_ts = 100.0

        monkeypatch.setattr(sm.time, "time", lambda: 100.0)
        waits = [mgr._compute_wait() for _ in range(3)]
        assert waits == pytest.approx([2.0, 4.0, 6.0], abs=1e-6)

        # After an exclusion window > YFINANCE_REQ_INTERVAL_DECAY_AFTER, multiple calls at the
        # same timestamp must decay at most once (not collapse to base in one burst).
        mgr._adaptive_interval_sec = 10.0
        mgr._last_block_ts = 100.0
        mgr._last_decay_ts = 100.0
        mgr._last_request_ts = 300.0
        monkeypatch.setattr(sm.time, "time", lambda: 300.0)
        mgr._compute_wait()
        after_first = mgr._adaptive_interval_sec
        mgr._compute_wait()
        after_second = mgr._adaptive_interval_sec
        assert after_first < 10.0
        assert after_second == pytest.approx(after_first)
    finally:
        sm.YFinanceSessionManager._reset_for_testing()


def test_session_manager_reclaim_does_not_close_session_entering_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1A-03: Reclaimed sessions marked closing must not run sockets while closing, and close outside locks."""
    import session_manager as sm

    sm.YFinanceSessionManager._reset_for_testing()
    mgr = sm.YFinanceSessionManager()
    try:
        closed_events: list[str] = []

        class _FakeSess:
            def __init__(self, label: str) -> None:
                self.label = label
                self.headers = {}
                self.closed = False

            def request(self, *_args, **_kwargs):
                return MagicMock(status_code=200)

            def close(self) -> None:
                assert not mgr._lock._is_owned()  # type: ignore[attr-defined]
                self.closed = True
                closed_events.append(self.label)

        def _fake_create(_ua: str, ua_index: int = 0) -> _FakeSess:
            sess = _FakeSess(f"s{ua_index}")
            mgr._all_sessions.append((sess, mgr._session_epoch, time.time()))
            return sess

        monkeypatch.setattr(mgr, "_create_session", _fake_create)
        s0 = mgr.get_session()
        assert mgr.session_count() == 1
        mgr._reclaim_epoch_sessions(mgr._session_epoch + 1)
        assert s0.closed is True
        assert closed_events == ["s0"]
    finally:
        sm.YFinanceSessionManager._reset_for_testing()


def test_message_announcer_guarantees_sentinel_under_concurrent_flood() -> None:
    """D1A-02: A full/evicted queue must always receive the None termination sentinel."""
    from messaging import MessageAnnouncer

    ann = MessageAnnouncer()
    q = ann.listen(maxsize=1, enforce_limit=False)
    q.put_nowait("first")

    def _spam() -> None:
        for i in range(25):
            ann.announce(f"msg-{i}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_spam) for _ in range(4)]
        for fut in futs:
            fut.result(timeout=5.0)

    drained: list[object] = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert None in drained


def test_early_excepthook_sanitizes_secrets_in_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1A-07: _early_excepthook must redact credentials/query tokens in startup crash logs."""
    from app_factory import early

    monkeypatch.setenv("MNS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(early.sys, "__excepthook__", lambda *_a: None)

    try:
        raise RuntimeError(
            "boom https://user:superpass@proxy.local/v1?api_key=SECRET123&sse_ticket=TICK999"
        )
    except RuntimeError as exc:
        early._early_excepthook(type(exc), exc, exc.__traceback__)

    log_text = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "superpass" not in log_text
    assert "SECRET123" not in log_text
    assert "TICK999" not in log_text


def test_configure_secret_key_rejects_whitespace_only_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1A-13: 32 spaces in FLASK_SECRET_KEY must not pass production secret validation."""
    from flask import Flask

    from app_factory.wiring import _configure_secret_key

    monkeypatch.setenv("MNS_PROD", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", " " * 32)
    app = Flask(__name__)
    with pytest.raises(ValueError, match="FLASK_SECRET_KEY"):
        _configure_secret_key(app)


def test_execution_state_shutdown_continues_past_single_executor_error() -> None:
    """D1A-09: One failing executor.shutdown must not skip remaining executors."""
    from execution_state import ExecutionState

    state = ExecutionState()
    good = MagicMock()
    bad = MagicMock()
    bad.shutdown.side_effect = RuntimeError("pool broken")
    state.executor = bad  # type: ignore[assignment]
    state.data_executor = good  # type: ignore[assignment]
    state.news_executor = MagicMock()  # type: ignore[assignment]
    state.sync_refresh_executor = MagicMock()  # type: ignore[assignment]

    state.shutdown(wait=False)
    good.shutdown.assert_called_once()


def test_market_state_watchlist_tombstone_no_premature_eviction_and_probe_owner() -> None:
    """D1A-11 & D1A-12: Re-added/re-removed watchlist keys must not evict prematurely; stale probe cannot clear new probe."""
    from market_state import MarketDataState

    m = MarketDataState()
    m._removed_watchlist_maxsize = 3
    m.mark_watchlist_removed("AAPL", "us")
    m.mark_watchlist_removed("MSFT", "us")
    m.clear_watchlist_removed("AAPL", "us")
    m.mark_watchlist_removed("NVDA", "us")
    m.mark_watchlist_removed("AAPL", "us")
    m.mark_watchlist_removed("TSLA", "us")
    # Order should be MSFT -> NVDA -> AAPL -> TSLA; evicting oldest removes MSFT, keeping AAPL.
    assert m.is_watchlist_removed("MSFT", "us") is False
    assert m.is_watchlist_removed("AAPL", "us") is True

    # D1A-12: stale probe release with mismatched token does not clear active probe
    st = m.get_circuit_state("mistral")
    st["status"] = "HALF_OPEN"
    assert m.try_claim_circuit_probe("mistral") is True
    first_since = st["probing_since"]
    st["probing_since"] = first_since + 1000.0
    m.release_circuit_probe("mistral", probe_since=first_since)
    assert st["probing"] is True
    m.release_circuit_probe("mistral", probe_since=st["probing_since"])
    assert st["probing"] is False


def test_fetch_stocks_batch_persists_payload_to_disk_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2-01: Batch-fetched stock payloads must be persisted to payload_disk_cache."""
    from app_state import app_state
    from bg.sync import fetch as sync_fetch

    written: dict[str, dict] = {}
    monkeypatch.setattr("app_bg.fetch_stocks_batch", sync_fetch.fetch_stocks_batch)
    monkeypatch.setattr("app_bg.acquire_yfinance_slot", lambda: True)
    monkeypatch.setattr("utils.disk_cache.is_disk_cache_degraded", lambda: False)
    monkeypatch.setattr(
        app_state.stock_provider,
        "download_batch",
        lambda symbols, **_kw: pd.DataFrame(
            {"Close": [100.0, 101.0]},
            index=pd.to_datetime(["2026-11-26", "2026-11-27"]),
        ),
    )
    monkeypatch.setattr(
        "app_bg.build_stock_payload",
        lambda sym, name, mkt, _hist, **_kw: {
            "symbol": sym,
            "name": name,
            "market": mkt,
            "price": 101.0,
            "shares": 5,
        },
    )
    monkeypatch.setattr(
        app_state.payload_disk_cache,
        "set",
        lambda key, val: written.__setitem__(key, val),
    )

    res = sync_fetch.fetch_stocks_batch([("AAPL", "Apple", "us")])
    assert len(res) == 1 and res[0]["price"] == 101.0
    assert "payload_AAPL_us" in written
    assert "shares" not in written["payload_AAPL_us"]


def test_leader_election_reclaims_corrupt_lock_and_resets_mutex_on_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2-02, D2-05, D2-10a: Corrupt atomic lock must be reclaimed after stale age; fork resets mutex."""
    from bg import leader_election as le

    lock_path = tmp_path / ".mns_sync_leader.lock"
    lock_path.write_bytes(b"\xff\xfe\xfd")
    old = time.time() - le._ATOMIC_LOCK_STALE_SEC - 30.0
    os.utime(lock_path, (old, old))

    monkeypatch.setattr(le, "_LEADER_LOCK_FILE", None)
    try:
        assert le._try_acquire_atomic_lock(lock_path, os.getpid()) is True
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        le.release_leader_lock()

    old_mutex = le._LEADER_LOCK_MUTEX
    le._reset_leader_lock_in_child()
    assert le._LEADER_LOCK_MUTEX is not old_mutex


def test_sync_control_discards_stale_generation_before_auto_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2-04: Superseded sync generation must exit before mutating invalid_symbol_streak/auto-removal."""
    from bg.sync import control as sync_ctrl
    from bg.sync import process as sync_proc

    auto_rm_calls: list[object] = []
    monkeypatch.setattr(sync_ctrl, "_get_sync_execution_lock", lambda: threading.Lock())
    monkeypatch.setattr("app_bg._is_sync_leader", True)
    monkeypatch.setattr("app_bg.sync_all_stocks_now", sync_ctrl.sync_all_stocks_now)
    monkeypatch.setattr(sync_ctrl, "_next_sync_generation", lambda: 1)
    monkeypatch.setattr(sync_proc, "_get_sync_generation", lambda: 2)
    monkeypatch.setattr("app_bg._prepare_sync_items", lambda **_kw: [("BAD", "Bad", "us")])
    monkeypatch.setattr("app_bg.fetch_stocks_batch", lambda *_a, **_kw: [None])
    monkeypatch.setattr(
        "app_bg._auto_remove_invalid_symbols",
        lambda *a: auto_rm_calls.append(a),
    )

    sync_ctrl.sync_all_stocks_now(force_fetch=True)
    assert auto_rm_calls == []


def test_fluctuate_indices_avoids_zero_prev_close_explosion_and_bounds_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2-06: prev_close<=0 skips percent explosion; target_indices clamps random-walk drift."""
    from bg import sse_interpolator as interp

    monkeypatch.setattr(interp, "_get_simulate_fluctuation", lambda: True)
    monkeypatch.setattr(interp.random, "random", lambda: 0.0)
    monkeypatch.setattr(interp.random, "uniform", lambda _a, b: b)

    # 1. prev_close == 0 fluctuates price cleanly within +-1% without percent explosion
    indices_zero = {"N225": {"price": 100.0, "change": 100.0}}
    interp._fluctuate_indices(indices_zero, us_open=True, jp_open=True, target_indices=indices_zero)
    assert "percent" not in indices_zero["N225"]
    assert 99.0 <= indices_zero["N225"]["price"] <= 101.0

    # 2. Repeated fluctuation anchored to target_indices remains bounded within +-1%
    target = {"USDJPY": {"price": 150.0, "change": 0.5, "percent": 0.33}}
    live = {"USDJPY": {"price": 155.0, "change": 5.5, "percent": 3.68}}
    for _ in range(20):
        interp._fluctuate_indices(live, us_open=True, jp_open=True, target_indices=target)
    assert 150.0 * 0.99 <= live["USDJPY"]["price"] <= 150.0 * 1.01
