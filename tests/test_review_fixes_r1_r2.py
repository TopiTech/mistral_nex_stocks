import logging
from pathlib import Path
from unittest.mock import patch

from app_bg import _try_acquire_atomic_lock
from utils.caching import _get_cached_value, _has_cached_key, _set_cached_value, sanitize_cache_key
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


def test_r3_caching_negative_key_sanitization_roundtrip():
    """R3: _has/_set/_get must sanitize keys consistently (negative cache path).

    The negative-cache helpers in caching.py previously used raw keys for
    _has/_set, causing drift when sanitized positive keys contained special
    characters.
    """
    from utils.caching import global_cache

    with global_cache.cache_lock:
        global_cache.caches.clear()

    raw_key = "market_news_context_us_ddgs/evil?key"
    neg_key = f"{raw_key}__negative"
    sanitized_neg = sanitize_cache_key(neg_key)

    _set_cached_value(neg_key, True, 60)
    assert _has_cached_key(neg_key, 60) is True
    assert _get_cached_value(neg_key, 60) is True
    # Underlying storage uses the sanitized form
    with global_cache.cache_lock:
        cache = global_cache.caches.get(60)
        assert cache is not None
        assert sanitized_neg in cache


def test_r4_chat_history_move_to_end_single_count_query(tmp_path, monkeypatch):
    """R4: move_to_end must execute exactly one SELECT COUNT(*) (was double).

    Before the fix the method discarded a cursor by running the count query
    twice; this asserts the single-query contract by inspecting source.
    """
    import inspect

    from utils.chat_history import SQLiteChatHistoryStore

    source = inspect.getsource(SQLiteChatHistoryStore.move_to_end)
    # The method body should contain exactly one COUNT query string
    assert source.count("SELECT COUNT(*) FROM chat_sessions") == 1, (
        "move_to_end should contain exactly one COUNT query (found "
        f"{source.count('SELECT COUNT(*) FROM chat_sessions')})"
    )
    # Functional smoke: touching sessions still works and respects cap
    import utils.chat_history as ch_mod

    db_path = tmp_path / "test_r4_history.db"
    monkeypatch.setattr(ch_mod, "DB_PATH", db_path)
    ch_mod._reset_db_state()
    store = SQLiteChatHistoryStore(max_sessions=2, max_msgs_per_session=5)
    ch_mod.init_db()
    store["sess_a"] = [{"role": "user", "content": "hello a"}]
    store["sess_b"] = [{"role": "user", "content": "hello b"}]
    store.move_to_end("sess_a")
    assert "sess_a" in store
    store.move_to_end("sess_c")
    # sess_c is new, so total would be 3 but max is 2 -> oldest evicted
    assert len(store) == 2


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
        from app_bg import _release_leader_lock, _set_is_sync_leader

        _release_leader_lock()
        _set_is_sync_leader(True)
        lock_file.unlink(missing_ok=True)
