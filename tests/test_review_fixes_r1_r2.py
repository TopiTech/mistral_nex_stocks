import logging
from pathlib import Path
from unittest.mock import patch

from app_bg import _try_acquire_atomic_lock
from utils.stock_payload import _resolve_stocks_for_response
from utils.tradingview_mapper import _resolve_ticker_exchange_dynamically


def test_r1_stock_payload_pts_exception_logging(caplog):
    """R1: Test that exception in PTS resolution is logged at DEBUG level."""
    caplog.set_level(logging.DEBUG)
    with patch(
        "services.realtime_engine.realtime_market_engine.get_pts_snapshot",
        side_effect=RuntimeError("PTS error"),
    ):
        res = _resolve_stocks_for_response()
        assert isinstance(res, dict)
        assert any(
            "Failed to resolve PTS snapshot for response" in rec.message for rec in caplog.records
        )


def test_r1_tradingview_mapper_exception_logging(caplog):
    """R1: Test that exception in dynamic exchange lookup is logged at DEBUG level."""
    caplog.set_level(logging.DEBUG)
    with patch("utils.stock_payload.get_stock_info_cached", side_effect=ValueError("Cache err")):
        res = _resolve_ticker_exchange_dynamically("TEST_TICKER")
        assert res is None
        assert any(
            "Failed to resolve ticker exchange dynamically for TEST_TICKER" in rec.message
            for rec in caplog.records
        )


def test_r2_atomic_lock_o_excl_semantics(tmp_path):
    """R2: _try_acquire_atomic_lock uses atomic O_EXCL creation, records the
    owner PID, allows re-entrant acquire from the same PID, and refuses a
    second LIVE process (no silent shared leadership)."""
    import os

    lock_file = tmp_path / ".test_leader.lock"
    pid = 12345

    try:
        # Fresh acquire succeeds and records the PID.
        assert _try_acquire_atomic_lock(Path(lock_file), pid) is True
        assert lock_file.read_text(encoding="utf-8").strip() == str(pid)

        # Re-entrant acquire from the same pid succeeds (already our lock).
        assert _try_acquire_atomic_lock(Path(lock_file), pid) is True

        # A different pid cannot acquire while the owner is still alive: use the
        # current test process as the live owner.
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        assert _try_acquire_atomic_lock(Path(lock_file), pid) is False

        # A stale lock (dead owner pid) IS reclaimed so a crashed leader does
        # not wedge election forever.
        lock_file.write_text(str(999999999), encoding="utf-8")
        assert _try_acquire_atomic_lock(Path(lock_file), pid) is True
    finally:
        # Release the module-level lock handle created by the acquires above so
        # pytest's ResourceWarning filter stays quiet (the atexit handler would
        # otherwise only run at interpreter teardown).
        from app_bg import _release_leader_lock

        _release_leader_lock()
        lock_file.unlink(missing_ok=True)
