"""Regression tests for the bg app_bg bridge (M-2 / M-3) and sync contract (M-4).

M-3: ``_get_app_bg_attr`` / ``_set_app_bg_attr`` must have a single owner
(``bg._app_bridge``) that every consumer module re-exports, so the app_bg mirror
behaviour cannot drift per call path.

M-2: reading an attribute must be side-effect-free -- a side-effecting default is
evaluated lazily (only when the mirror lacks the attribute), never eagerly.

M-4: ``_process_fetched_stocks`` must return a named ``ProcessedStocks`` contract
(per-market merged rows + an explicit ``is_empty``) instead of an implicit bare
3-tuple whose truthiness the caller had to interpret.
"""

from unittest.mock import patch

import pytest

import app_bg
from app_state import app_state
from bg._app_bridge import _get_app_bg_attr, _set_app_bg_attr
from bg.sync.process import ProcessedStocks, _process_fetched_stocks
from bg.sync.state import _set_sync_generation


@pytest.fixture(autouse=True)
def _reset_sync_state():
    _set_sync_generation(0)
    app_state.market.first_sync_attempted = False
    app_state.market.first_sync_completed_at = 0.0
    with app_state.market.is_syncing_lock:
        app_state.market.is_syncing = False
    yield


# --- M-3: single owner of the app_bg bridge -------------------------------


def test_app_bg_bridge_is_single_owner():
    """All consumer modules must share one _get_app_bg_attr implementation."""
    from bg import common, leader_election
    from bg.sync import state

    assert common._get_app_bg_attr is _get_app_bg_attr
    assert state._get_app_bg_attr is _get_app_bg_attr
    assert leader_election._get_app_bg_attr is _get_app_bg_attr
    assert common._set_app_bg_attr is _set_app_bg_attr


def test_set_app_bg_attr_writes_and_get_reads_mirror():
    _set_app_bg_attr("_u3_probe_attr", 123)
    try:
        assert app_bg.__dict__["_u3_probe_attr"] == 123
        assert _get_app_bg_attr("_u3_probe_attr", None) == 123
    finally:
        app_bg.__dict__.pop("_u3_probe_attr", None)


# --- M-2: reads are side-effect-free -------------------------------------


def test_get_app_bg_attr_fallback_is_a_value_not_invoked():
    """A callable fallback must be returned as a value, never auto-invoked."""

    def _must_not_run() -> None:
        raise AssertionError("fallback must not be evaluated by the getter")

    assert _get_app_bg_attr("_u2_missing_probe", _must_not_run) is _must_not_run


def test_leader_read_does_not_call_is_leader_when_mirror_has_it():
    """M-2: is_leader() must not run eagerly when the mirror already has the flag."""
    with (
        patch.object(app_bg, "_is_sync_leader", False),
        patch("bg.sync.control.is_leader") as mock_is_leader,
        patch.object(app_bg, "_warm_payload_cache_from_disk", lambda: None),
        patch.object(app_bg, "_invalidate_sse_payload_cache", lambda: None),
        patch.object(app_bg, "announce_current_market_state", lambda: None),
    ):
        app_bg.sync_all_stocks_now()
    mock_is_leader.assert_not_called()


def test_leader_read_calls_is_leader_only_when_mirror_missing():
    """The lazy default runs only when the mirror lacks the attribute."""
    app_bg.__dict__.pop("_is_sync_leader", None)
    try:
        with (
            patch("bg.sync.control.is_leader", return_value=False) as mock_is_leader,
            patch.object(app_bg, "_warm_payload_cache_from_disk", lambda: None),
            patch.object(app_bg, "_invalidate_sse_payload_cache", lambda: None),
            patch.object(app_bg, "announce_current_market_state", lambda: None),
        ):
            app_bg.sync_all_stocks_now()
        mock_is_leader.assert_called_once()
    finally:
        app_bg.__dict__["_is_sync_leader"] = True


# --- M-4: explicit ProcessedStocks contract -----------------------------


def test_processed_stocks_is_empty_boundary():
    assert ProcessedStocks().is_empty is True
    assert ProcessedStocks(us=[], jp=[], idx=[]).is_empty is True
    assert ProcessedStocks(us=[{"symbol": "AAPL"}], jp=[], idx=[]).is_empty is False
    assert ProcessedStocks(us=[], jp=[{"symbol": "7203.T"}], idx=[]).is_empty is False
    assert ProcessedStocks(us=[], jp=[], idx=[{"symbol": "^N225"}]).is_empty is False


def test_process_fetched_stocks_stale_generation_returns_empty():
    """A superseded generation discards work and reports an empty result."""
    _set_sync_generation(5)
    result = _process_fetched_stocks(
        [{"symbol": "AAPL", "market": "us", "price": 1.0}], sync_generation=1
    )
    assert isinstance(result, ProcessedStocks)
    assert result.is_empty is True
    assert result.us == [] and result.jp == [] and result.idx == []


def test_process_fetched_stocks_returns_named_rows():
    """Normal path returns per-market merged rows under named fields."""
    with app_state.cache.sse_data_lock:
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.previous_close_cache.clear()
    with patch("bg.sync.process.announce_real_market_state", lambda: None):
        result = _process_fetched_stocks(
            [
                {"symbol": "AAPL", "market": "us", "price": 190.0, "change": 2.0},
                {"symbol": "7203.T", "market": "jp", "price": 950.0, "change": 20.0},
            ],
            sync_generation=None,
        )
    assert isinstance(result, ProcessedStocks)
    assert result.is_empty is False
    assert [r["symbol"] for r in result.us] == ["AAPL"]
    assert [r["symbol"] for r in result.jp] == ["7203.T"]
    assert result.idx == []


# --- C-1 (cross-cut): SSE generation/counter increment atomicity ---------


def test_invalidate_sse_payload_cache_generation_no_lost_update():
    """C-1 cross-cut: concurrent cache invalidations must not lose updates.

    ``_invalidate_sse_payload_cache`` and the ``_sse_full_snapshot_counter``
    bump both live under ``_sse_payload_lock``; N concurrent increments must
    advance the generation by exactly N (a lock-free read-modify-write would
    lose updates and yield fewer than N).
    """
    from concurrent.futures import ThreadPoolExecutor

    from bg import common as bg_common

    def _gen() -> int:
        return int(_get_app_bg_attr("_sse_payload_generation", bg_common._sse_payload_generation))

    n = 64
    before = _gen()
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: bg_common._invalidate_sse_payload_cache(), range(n)))
    after = _gen()
    assert after == before + n
