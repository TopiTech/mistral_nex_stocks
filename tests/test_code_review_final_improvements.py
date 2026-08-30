# tests/test_code_review_final_improvements.py
"""Unit tests verifying code review fixes and improvements across services, utils, bg, and native host."""

import os
from unittest.mock import MagicMock, patch

import pandas as pd

import app_bg
from app_state import app_state
from bg.sync_worker import fetch_stocks_batch
from native_host import native_host
from route_helpers import invalidate_stock_caches
from services.market_data_service import build_screener_enrichment
from services.realtime.engine import RealtimeMarketEngine
from utils.caching import _get_cached_value, _set_cached_value
from utils.disk_cache import StockDiskCache
from utils.storage import _write_user_stocks_with_lock


def test_realtime_engine_wait_for_updates_captures_late_signal():
    """Verify wait_for_updates returns True if an event was set during/after wait timeout."""
    engine = RealtimeMarketEngine()
    cid = engine.register_client()
    evt = engine._client_events[cid]

    # Pre-set the event
    evt.set()
    # Should consume and return True
    assert engine.wait_for_updates(cid, timeout=0.01) is True
    # Now it should be cleared
    assert not evt.is_set()

    # Simulate race: mock wait returning False, but event is set right before lock acquisition
    with patch.object(evt, "wait", return_value=False):
        evt.set()
        assert engine.wait_for_updates(cid, timeout=0.01) is True
        assert not evt.is_set()

    # When genuinely not set, should return False
    assert engine.wait_for_updates(cid, timeout=0.01) is False

    engine.unregister_client(cid)


def test_screener_enrichment_fault_isolation():
    """Verify a single symbol failure in info_fetch does not abort other missing items."""
    missing_items = [
        ("AAPL", "Apple Inc.", "us"),
        ("BROKEN", "Broken Co.", "us"),
        ("MSFT", "Microsoft Corp.", "us"),
    ]

    def mock_batch_fetch(items, lightweight=True, period="5d"):
        return []

    def mock_info_fetch(sym, cache_only=False):
        if sym == "BROKEN":
            raise RuntimeError("Corrupt upstream data for BROKEN")
        return {"symbol": sym, "shortName": f"{sym} Name", "price": 100.0}

    rows = build_screener_enrichment(
        missing_items,
        full_fetch_symbol=None,
        fetch_batch_fn=mock_batch_fetch,
        get_info_fn=mock_info_fetch,
    )

    assert "AAPL" in rows
    assert rows["AAPL"]["symbol"] == "AAPL"
    assert "MSFT" in rows
    assert rows["MSFT"]["symbol"] == "MSFT"
    assert "BROKEN" not in rows


def test_disk_cache_evict_safe_against_concurrent_deletion(tmp_path):
    """Verify _evict_if_needed gracefully handles files deleted between glob and stat."""
    cache = StockDiskCache(tmp_path, max_entries=2)
    f1 = tmp_path / "a.json"
    f2 = tmp_path / "b.json"
    f3 = tmp_path / "c.json"
    f1.write_text("{}", encoding="utf-8")
    f2.write_text("{}", encoding="utf-8")
    f3.write_text("{}", encoding="utf-8")

    # Mock one file's stat throwing FileNotFoundError (as if concurrently deleted)
    orig_stat = tmp_path.joinpath("b.json").stat

    def mock_stat(self):
        if self.name == "b.json":
            raise FileNotFoundError("Simulated concurrent removal")
        return orig_stat()

    with patch("pathlib.Path.stat", mock_stat):
        cache._evict_if_needed()

    # Eviction should have completed without raising an exception
    remaining = list(tmp_path.glob("*.json"))
    assert len(remaining) <= 3


def test_disk_cache_set_retries_on_transient_permission_error(tmp_path):
    """Verify StockDiskCache.set retries os.replace on transient PermissionError."""
    cache = StockDiskCache(tmp_path)
    replace_calls = 0
    orig_replace = os.replace

    def mock_replace(src, dst):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls < 3:
            raise PermissionError("Simulated WinError 32 file lock")
        orig_replace(src, dst)

    with patch("os.replace", mock_replace), patch("os.name", "nt"):
        cache.set("test_key", {"data": 123})

    assert replace_calls == 3
    cached = cache.get("test_key")
    assert cached == {"data": 123}


def test_storage_write_retries_on_transient_permission_error(tmp_path):
    """Verify _write_user_stocks_with_lock retries os.replace on transient PermissionError."""
    target_file = tmp_path / "user_stocks.json"
    tmp_file = tmp_path / "user_stocks.tmp"
    lock_file = tmp_path / "user_stocks.lock"

    replace_calls = 0
    orig_replace = os.replace

    def mock_replace(src, dst):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls < 3:
            raise PermissionError("Simulated WinError 32 file lock")
        orig_replace(src, dst)

    with patch("os.replace", mock_replace), patch("utils.storage._is_windows", return_value=True):
        _write_user_stocks_with_lock("{}", tmp_file, target_file, lock_file)

    assert replace_calls == 3
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == "{}"


def test_sync_worker_fallback_cancels_timed_out_futures():
    """Verify fetch_stocks_batch cancels uncompleted fallback futures on timeout."""
    mock_fut = MagicMock()
    items = [
        ("TIMEOUT_SYM", "Timeout Name", "us"),
        ("PRESENT_SYM", "Present Name", "us"),
    ]

    # MultiIndex columns where only PRESENT_SYM exists
    columns = pd.MultiIndex.from_tuples([("Close", "PRESENT_SYM"), ("Open", "PRESENT_SYM")])
    dummy_df = pd.DataFrame([[100.0, 99.0], [101.0, 100.0]], columns=columns)

    with (
        patch.object(app_bg, "fetch_stocks_batch", fetch_stocks_batch),
        patch("bg.sync_worker.acquire_yfinance_slot", return_value=True),
        patch.object(app_state.stock_provider, "download_batch", return_value=dummy_df),
        patch("app_bg.build_stock_payload", return_value={"symbol": "PRESENT_SYM", "price": 100.0}),
        patch.object(app_state.execution.data_executor, "submit", return_value=mock_fut),
        patch("concurrent.futures.wait", return_value=([], [mock_fut])),
    ):
        results = fetch_stocks_batch(items, lightweight=False)
        assert len(results) == 2
        assert results[0] is None
        mock_fut.cancel.assert_called_once()


def test_native_host_read_message_header_slicing():
    """Verify read_message handles header byte slicing safely."""
    # Construct a valid message frame: 4-byte length + JSON payload
    payload = b'{"action":"ping"}'
    header = len(payload).to_bytes(4, byteorder="little")
    raw_stream = header + payload

    class MockStdin:
        def __init__(self, data: bytes):
            self._data = data
            self._pos = 0

        def read(self, n: int):
            chunk = self._data[self._pos : self._pos + n]
            self._pos += len(chunk)
            return chunk

    with patch.object(native_host, "RAW_STDIN", MockStdin(raw_stream)):
        msg = native_host.read_message()
        assert msg == {"action": "ping"}


def test_invalidate_stock_caches_clears_info_prefix():
    """Verify invalidate_stock_caches purges info_{symbol} from cache."""
    symbol = "TEST_INVALIDATE_SYM"
    _set_cached_value(f"info_{symbol}", {"name": "Test Company"}, 3600)
    _set_cached_value(f"hist_{symbol}", {"history": []}, 3600)

    assert _get_cached_value(f"info_{symbol}", 3600) is not None
    assert _get_cached_value(f"hist_{symbol}", 3600) is not None

    invalidate_stock_caches(symbol)

    assert _get_cached_value(f"info_{symbol}", 3600) is None
    assert _get_cached_value(f"hist_{symbol}", 3600) is None
