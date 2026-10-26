"""Regression tests for the background sync layer (C-1, H-1).

C-1: ``_sync_generation`` was a read-modify-write counter; a lost update could
let a stale sync pass the generation check and clear a newer sync's
``is_syncing`` flag (double sync). The counter is now an atomic
``_next_sync_generation()`` and the ``is_syncing`` clear is generation-guarded.

H-1: ``first_sync_completed_at`` was advanced in a ``finally`` regardless of
outcome. It must only advance when the sync actually succeeds; a failed run
must leave it untouched while still recording ``first_sync_attempted``.
"""

import threading

import pytest

import app_bg
from app_state import app_state
from bg.sync.process import ProcessedStocks
from bg.sync.state import (
    _get_sync_generation,
    _next_sync_generation,
    _set_sync_generation,
)


@pytest.fixture(autouse=True)
def reset_sync_state():
    """Reset the sync generation and first-sync flags around every test."""
    _set_sync_generation(0)
    app_state.market.first_sync_attempted = False
    app_state.market.first_sync_completed_at = 0.0
    with app_state.market.is_syncing_lock:
        app_state.market.is_syncing = False
    yield
    _set_sync_generation(0)
    app_state.market.first_sync_attempted = False
    app_state.market.first_sync_completed_at = 0.0
    with app_state.market.is_syncing_lock:
        app_state.market.is_syncing = False


# ---------------------------------------------------------------------------
# C-1: atomic generation counter
# ---------------------------------------------------------------------------


def test_next_sync_generation_monotonic():
    assert _next_sync_generation() == 1
    assert _next_sync_generation() == 2
    assert _next_sync_generation() == 3
    assert _get_sync_generation() == 3


def test_set_and_get_generation_consistent():
    _set_sync_generation(42)
    assert _get_sync_generation() == 42


def test_next_sync_generation_unique_under_concurrency():
    """Concurrent increments must never collide (no lost update, C-1)."""
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    results: list[int] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        gen = _next_sync_generation()
        with results_lock:
            results.append(gen)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n_threads
    # Every generation is unique and forms the contiguous range 1..N.
    assert sorted(results) == list(range(1, n_threads + 1))
    assert _get_sync_generation() == n_threads


# ---------------------------------------------------------------------------
# H-1: completion is recorded only on actual success
# ---------------------------------------------------------------------------


def _run_sync_success():
    """Run sync_all_stocks_now on a mocked-success path and return it."""
    from unittest.mock import patch

    _batch = [
        {"symbol": "AAPL", "market": "us", "price": 150.0},
        {"symbol": "9984.T", "market": "jp", "price": 6000.0},
        {"symbol": "^N225", "market": "idx", "price": 38000.0},
    ]
    app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}

    with (
        patch.object(app_bg, "_is_sync_leader", True),
        patch.object(app_bg, "is_market_open", return_value=True),
        patch.object(app_bg, "_warm_payload_cache_from_disk", lambda: None),
        patch.object(app_bg, "fetch_index_data", return_value=("DJI", {})),
        patch.object(app_bg, "fetch_stocks_batch", return_value=_batch),
    ):
        app_bg.sync_all_stocks_now()


def test_successful_sync_records_completion_and_clears_is_syncing():
    _run_sync_success()
    assert app_state.market.first_sync_attempted is True
    assert app_state.market.first_sync_completed_at > 0.0
    with app_state.market.is_syncing_lock:
        assert app_state.market.is_syncing is False


def test_failed_sync_does_not_record_completion():
    """A failed sync must not advance first_sync_completed_at (H-1 invariant)."""
    from unittest.mock import patch

    with (
        patch.object(app_bg, "_is_sync_leader", True),
        patch.object(app_bg, "_warm_payload_cache_from_disk", lambda: None),
        patch.object(app_bg, "fetch_stocks_batch", side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(RuntimeError):
            app_bg.sync_all_stocks_now()

    # Attempted regardless of outcome, but completion is untouched.
    assert app_state.market.first_sync_attempted is True
    assert app_state.market.first_sync_completed_at == 0.0
    # A failed (but current-generation) run still releases the syncing flag.
    with app_state.market.is_syncing_lock:
        assert app_state.market.is_syncing is False


# ---------------------------------------------------------------------------
# C-1: double-sync prevention (generation-guarded is_syncing clear)
# ---------------------------------------------------------------------------


def test_stale_sync_does_not_clear_newer_is_syncing():
    """A superseded run must not release a newer run's is_syncing flag (C-1)."""
    from unittest.mock import patch

    _batch = [{"symbol": "AAPL", "market": "us", "price": 150.0}]
    app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}

    def bump_generation_mid_run(items, snapshot_ts_ms=None, **kwargs):
        # Simulate a newer sync taking over while this run is fetching.
        _next_sync_generation()
        return _batch

    with (
        patch.object(app_bg, "_is_sync_leader", True),
        patch.object(app_bg, "_warm_payload_cache_from_disk", lambda: None),
        patch.object(app_bg, "is_market_open", return_value=True),
        patch.object(app_bg, "fetch_index_data", return_value=("DJI", {})),
        patch.object(app_bg, "fetch_stocks_batch", side_effect=bump_generation_mid_run),
        patch.object(
            app_bg,
            "_process_fetched_stocks",
            return_value=ProcessedStocks(us=[1], jp=[], idx=[]),
        ),
        patch.object(app_bg, "_update_indices_data", lambda *a, **k: None),
    ):
        app_bg.sync_all_stocks_now()

    # The stale run was superseded (generation advanced mid-run) so it must NOT
    # clear is_syncing -- the newer generation owns the flag. Leaving it set is
    # the structural guard that prevents a double sync.
    with app_state.market.is_syncing_lock:
        assert app_state.market.is_syncing is True
    # It also must not record completion (its work was discarded).
    assert app_state.market.first_sync_completed_at == 0.0

    # Clean up the flag so it does not leak into other tests.
    with app_state.market.is_syncing_lock:
        app_state.market.is_syncing = False
