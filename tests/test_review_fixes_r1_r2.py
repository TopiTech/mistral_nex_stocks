import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app_bg import _try_acquire_atomic_lock
from utils.stock_payload import _resolve_stocks_for_response
from utils.tradingview_mapper import _resolve_ticker_exchange_dynamically


def test_r1_stock_payload_pts_exception_logging(caplog):
    """R1: Test that exception in PTS resolution is logged at DEBUG level."""
    caplog.set_level(logging.DEBUG)
    with patch("services.realtime_engine.realtime_market_engine.get_pts_snapshot", side_effect=RuntimeError("PTS error")):
        res = _resolve_stocks_for_response()
        assert isinstance(res, dict)
        assert any("Failed to resolve PTS snapshot for response" in rec.message for rec in caplog.records)


def test_r1_tradingview_mapper_exception_logging(caplog):
    """R1: Test that exception in dynamic exchange lookup is logged at DEBUG level."""
    caplog.set_level(logging.DEBUG)
    with patch("utils.stock_payload.get_stock_info_cached", side_effect=ValueError("Cache err")):
        res = _resolve_ticker_exchange_dynamically("TEST_TICKER")
        assert res is None
        assert any("Failed to resolve ticker exchange dynamically for TEST_TICKER" in rec.message for rec in caplog.records)


def test_r2_atomic_lock_closes_existing_fd(tmp_path):
    """R2: Test that _try_acquire_atomic_lock closes an existing _LEADER_LOCK_FILE handle."""
    lock_file = tmp_path / ".test_leader.lock"
    mock_file = MagicMock()

    with patch("app_bg._LEADER_LOCK_FILE", mock_file), \
         patch("os.open", return_value=999), \
         patch("os.write"), \
         patch("os.close"), \
         patch("builtins.open", return_value=MagicMock()):
        acquired = _try_acquire_atomic_lock(Path(lock_file), 12345)
        assert acquired is True
        mock_file.close.assert_called_once()
