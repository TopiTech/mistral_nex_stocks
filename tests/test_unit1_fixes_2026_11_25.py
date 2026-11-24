"""Regression and edge-case tests for Unit 1 core/config/state/persistence fixes (2026-11-25)."""

from __future__ import annotations

import errno
import gc
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config_store
import crypto_utils
import session_manager as sm_mod
from app_state import AppState
from bg import leader_election
from config import master_key
from market_state import MarketDataState
from session_manager import YFinanceSessionManager
from shutdown_manager import ShutdownTokenManager


def test_f01_master_key_rollback_removes_unverifiable_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-01: Rollback after readback failure must not re-inject broken mns_master_key."""
    cfg_file = tmp_path / "config.json"
    bak_file = tmp_path / "config.json.bak"
    legacy_file = tmp_path / "legacy_config.json"

    initial_cfg = {
        "mistral_model": "mistral-medium-3.5",
        "api_credentials": {},
        "preview_mode": False,
    }
    cfg_file.write_text(json.dumps(initial_cfg), encoding="utf-8")
    bak_file.write_text(json.dumps(initial_cfg), encoding="utf-8")

    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", legacy_file)
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})

    monkeypatch.setattr(
        config_store,
        "_encode_secret",
        lambda val, _name: {"enc": "keyring", "value": val},
    )
    # Simulate keyring write accepted, readback decode failing
    monkeypatch.setattr(config_store, "_decode_secret", lambda _entry, _name: "")
    monkeypatch.setattr(
        crypto_utils, "snapshot_secret_storage", lambda *_a, **_kw: ({}, {})
    )
    monkeypatch.setattr(
        crypto_utils, "restore_secret_storage", lambda *_a, **_kw: None
    )

    with pytest.raises(RuntimeError, match="Failed to verify persisted master key"):
        master_key.get_or_create_master_key()

    rolled_back = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "mns_master_key" not in rolled_back
    bak_data = json.loads(bak_file.read_text(encoding="utf-8"))
    assert "mns_master_key" not in bak_data


def test_f02_shared_session_refcount_survives_first_worker_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-02: Finishing one concurrent request on a shared session must not unmark it active."""
    mgr = YFinanceSessionManager()
    try:
        t2_entered = threading.Event()
        release_t2 = threading.Event()
        call_count = 0
        count_lock = threading.Lock()
        closed_flag = {"closed": False}

        class _FakeSession:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.proxies: dict[str, str] = {}
                self.impersonate = "chrome120"

            def mount(self, *_args, **_kwargs) -> None:
                pass

            def request(self, *_args, **_kwargs):
                nonlocal call_count
                with count_lock:
                    call_count += 1
                    idx = call_count
                if idx == 1:
                    t2_entered.set()
                    release_t2.wait(timeout=2.0)
                    return SimpleNamespace(status_code=200)
                return SimpleNamespace(status_code=200)

            def close(self) -> None:
                closed_flag["closed"] = True

        fake_sess = _FakeSession()
        monkeypatch.setattr(sm_mod, "CURL_CFFI_AVAILABLE", False)
        import requests

        with patch.object(requests, "Session", return_value=fake_sess):
            sess = mgr.get_session()

        t1 = threading.Thread(target=lambda: sess.request("GET", "https://example.com"))
        t2 = threading.Thread(target=lambda: sess.request("GET", "https://example.com"))
        t2.start()
        assert t2_entered.wait(timeout=2.0)
        t1.start()
        t1.join(timeout=2.0)

        # t1 finished, t2 is still inside original_request -> session MUST stay active
        mgr._reclaim_epoch_sessions(mgr._session_epoch + 1)
        assert closed_flag["closed"] is False
        with mgr._active_sessions_lock:
            assert id(sess) in mgr._active_sessions
            assert mgr._active_session_counts.get(id(sess)) == 1

        release_t2.set()
        t2.join(timeout=2.0)

        with mgr._active_sessions_lock:
            assert id(sess) not in mgr._active_sessions
            assert id(sess) not in mgr._active_session_counts

        mgr._reclaim_epoch_sessions(mgr._session_epoch + 1)
        assert closed_flag["closed"] is True
    finally:
        mgr.close_all()


def test_f03_enter_circuit_call_holds_lock_across_open_transition() -> None:
    """F-03: Concurrent OPEN transition mid-enter_circuit_call must deny with 'open'."""
    m = MarketDataState()
    m.circuit_states["mistral"] = {
        "status": "CLOSED",
        "timeout_streak": 0,
        "open_until": 0.0,
        "probing": False,
        "probing_since": 0.0,
    }

    orig_get_state = m.get_circuit_state

    def _flip_to_open(service: str, symbol: str | None = None):
        m.circuit_states[service] = {
            "status": "OPEN",
            "timeout_streak": 0,
            "open_until": time.time() + 60.0,
            "probing": False,
            "probing_since": 0.0,
        }
        return orig_get_state(service, symbol=symbol)

    with patch.object(m, "get_circuit_state", side_effect=_flip_to_open):
        allowed, claimed, reason = m.enter_circuit_call("mistral")

    assert allowed is False
    assert claimed is False
    assert reason == "open"


def test_f04_app_state_uses_payload_disk_cache_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-04: AppState.payload_disk_cache must honor STOCK_PAYLOAD_DISK_CACHE_TTL."""
    import constants

    monkeypatch.setattr(constants, "STOCK_PAYLOAD_DISK_CACHE_TTL", 1234)
    state = AppState()
    assert state.payload_disk_cache._default_ttl == 1234


def test_f05_shutdown_manager_cross_device_migration_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-05: EXDEV on Path.replace falls back to atomic write + unlink."""
    runtime_dir = tmp_path / "runtime"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir(parents=True)

    mgr = ShutdownTokenManager()
    mgr.runtime_state_dir = runtime_dir
    mgr.token_file = runtime_dir / ".mns_shutdown_token"
    mgr.used_marker = runtime_dir / ".mns_shutdown_token.used"
    mgr._legacy_token_file = legacy_dir / ".mns_shutdown_token"
    mgr._legacy_used_marker = legacy_dir / ".mns_shutdown_token.used"

    mgr._legacy_token_file.write_text('{"enc":"plain","value":"tok123"}', encoding="utf-8")
    mgr._legacy_used_marker.write_text("used", encoding="utf-8")

    orig_replace = Path.replace

    def _exdev_replace(self: Path, target: Path):
        if self in (mgr._legacy_token_file, mgr._legacy_used_marker):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return orig_replace(self, target)

    monkeypatch.setattr(Path, "replace", _exdev_replace)
    mgr._migrate_legacy_token_file()

    assert mgr.token_file.read_text(encoding="utf-8") == '{"enc":"plain","value":"tok123"}'
    assert mgr.used_marker.read_text(encoding="utf-8") == "used"
    assert not mgr._legacy_token_file.exists()
    assert not mgr._legacy_used_marker.exists()


def test_bg_util_02_remove_fields_recursive_handles_nan_value_error(tmp_path: Path) -> None:
    """BG-UTIL-02: Non-finite floats in legacy cache entry must not abort migration loop."""
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(cache_dir=tmp_path / "cache", max_entries=16, default_ttl=3600)
    cache._cache_dir.mkdir(parents=True, exist_ok=True)
    bad_entry = cache._cache_dir / "bad_nan.json"
    good_entry = cache._cache_dir / "good.json"
    bad_entry.write_text('{"stored_at": 1.0, "shares": 10, "val": NaN}', encoding="utf-8")
    good_entry.write_text('{"stored_at": 1.0, "shares": 5, "price": 100.0}', encoding="utf-8")

    migrated = cache.remove_fields_recursive(("shares",))
    assert migrated == 1
    assert not bad_entry.exists()
    cleaned = json.loads(good_entry.read_text(encoding="utf-8"))
    assert "shares" not in cleaned
    assert cleaned["price"] == 100.0


def test_bg_util_03_trend_session_closed_on_worker_thread_exit() -> None:
    """BG-UTIL-03: Thread-local requests.Session in trend_sources closes when thread exits."""
    import trend_sources

    trend_sources._cleanup_trend_session()
    created_sessions: list[object] = []

    def _worker() -> None:
        s = trend_sources._get_trend_session()
        created_sessions.append(s)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=2.0)
    gc.collect()

    assert len(created_sessions) == 1
    with trend_sources._TREND_SESSION_LOCK:
        assert created_sessions[0] not in trend_sources._TREND_SESSIONS


def test_bg_util_04_atomic_lock_toctou_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BG-UTIL-04: Atomic lock release removes owned file and TOCTOU avoids deleting fresh owner."""
    lock_path = tmp_path / "leader.lock"
    monkeypatch.setattr(leader_election, "_LEADER_LOCK_FILE", None)
    monkeypatch.setattr(leader_election, "_LEADER_LOCK_MODE", None)
    monkeypatch.setattr(leader_election, "_LEADER_LOCK_PATH", None)

    assert leader_election._try_acquire_atomic_lock(lock_path, 999991) is True
    assert lock_path.exists()
    lock_path.write_text(str(leader_election.os.getpid()), encoding="utf-8")
    leader_election._release_leader_lock()
    assert not lock_path.exists()

    # Simulate stale lock replaced by another process between read and unlink
    lock_path.write_text("111111", encoding="utf-8")
    orig_stat = Path.stat
    stat_calls = {"n": 0}

    def _race_stat(self: Path, *args, **kwargs):
        if self == lock_path:
            stat_calls["n"] += 1
            if stat_calls["n"] == 2:
                lock_path.write_text("222222", encoding="utf-8")
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(leader_election, "_pid_is_alive", lambda pid: pid == 222222)
    monkeypatch.setattr(Path, "stat", _race_stat)
    assert leader_election._try_acquire_atomic_lock(lock_path, 333333) is False
    assert lock_path.read_text(encoding="utf-8") == "222222"


def test_bg_util_05_merge_cache_dedups_new_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """BG-UTIL-05: _process_fetched_stocks must deduplicate duplicate new symbols in res_list."""
    from app_state import app_state
    from bg.sync import process as sync_process

    monkeypatch.setattr(app_state.market, "target_stocks_cache", {"us": [], "jp": [], "idx": []})
    monkeypatch.setattr(app_state.market, "is_watchlist_removed", lambda _s, _m: False)

    dup_us = [
        {"symbol": "NEW1", "market": "us", "price": 10.0},
        {"symbol": "NEW1", "market": "us", "price": 10.0},
        {"symbol": "NEW2", "market": "us", "price": 20.0},
    ]
    sync_process._process_fetched_stocks(dup_us, sync_generation=None)
    us_symbols = [row["symbol"] for row in app_state.market.target_stocks_cache["us"]]
    assert us_symbols == ["NEW1", "NEW2"]
