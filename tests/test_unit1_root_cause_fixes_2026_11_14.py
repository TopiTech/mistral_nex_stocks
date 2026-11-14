"""Comprehensive regression tests for Unit 1 root-cause fixes (2026-11-14)."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import keyring
import pytest
from cachetools import LRUCache

import app_bg
import bg
import config_store
import crypto_utils
from ai_state import AIState
from app_factory.hooks import _enforce_sec_fetch_site_check
from app_state import app_state
from bg import common as bg_common
from bg import leader_election
from bg.sync import control as sync_control
from bg.sync.fetch import _reset_yfinance_streak_on_success
from config import master_key
from config.corruption import _sanitize_and_backup_corrupt_config
from config.merge import _merge_configs
from config_utils import resolve_model_target
from credential_manager import is_local_openai_base_url
from messaging import SSEEventLog
from session_manager import YFinanceSessionManager
from shutdown_manager import ShutdownTokenManager
from utils.caching import (
    _CACHE_KEY_DIGEST_LENGTH,
    _CACHE_KEY_HASH_MARKER,
    _CACHE_KEY_MAX_LENGTH,
    CACHE_FETCHING,
    clear_cache_prefix,
    get_cached,
    global_cache,
    peek_cached,
    sanitize_cache_key,
)
from utils.chat_history import SQLiteChatHistoryStore
from utils.disk_cache import StockDiskCache
from utils.formatting import _parse_datetime_to_utc
from utils.market_utils import _market_state_from_metadata
from utils.normalization import is_valid_symbol
from utils.stock_payload import _resolve_indices_for_response, fetch_stock_info_async
from utils.text_utils import _sanitize_error_message
from utils.validators import extract_json_payload, validate_analysis_result
from utils.worker_validation import _uwsgi_cheaper_is_configured


def test_master_key_refuses_corrupted_config_and_restores_keyring_on_save_failure(
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{corrupt json", encoding="utf-8")
    keyring.set_password(crypto_utils.KEYRING_SERVICE_NAME, "mns_master_key", "original-keyring-val")

    with (
        patch.object(config_store, "CONFIG_FILE", cfg_file),
        patch.object(config_store, "LEGACY_CONFIG_FILE", tmp_path / "legacy_missing.json"),
    ):
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        with pytest.raises(RuntimeError, match="corrupted"):
            master_key.get_or_create_master_key()

    assert (
        keyring.get_password(crypto_utils.KEYRING_SERVICE_NAME, "mns_master_key")
        == "original-keyring-val"
    )

    config_store._CONFIG_CORRUPTED = False
    with (
        patch.object(config_store, "load_config", return_value={}),
        patch.object(config_store, "is_config_corrupted", return_value=False),
        patch.object(config_store, "save_config", side_effect=OSError("disk full")),
    ):
        with pytest.raises(RuntimeError, match="Failed to persist"):
            master_key.get_or_create_master_key()

    assert (
        keyring.get_password(crypto_utils.KEYRING_SERVICE_NAME, "mns_master_key")
        == "original-keyring-val"
    )


def test_config_same_path_legacy_nondict_runtime_and_nested_corrupt_backup(
    tmp_path: Path,
) -> None:
    shared_cfg = tmp_path / "config.json"
    shared_cfg.write_text(json.dumps({"ai_provider": "mistral"}), encoding="utf-8")

    with (
        patch.object(config_store, "APP_DATA_DIR", tmp_path),
        patch.object(config_store, "CONFIG_FILE", shared_cfg),
        patch.object(config_store, "LEGACY_CONFIG_FILE", shared_cfg),
    ):
        config_store._LEGACY_MERGE_DONE = False
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        cfg = config_store.load_config()
        assert cfg["ai_provider"] == "mistral"
        assert shared_cfg.exists()

    legacy_file = tmp_path / "legacy.json"
    runtime_list = tmp_path / "runtime_list.json"
    legacy_file.write_text(json.dumps({"ai_provider": "mistral"}), encoding="utf-8")
    runtime_list.write_text("[1, 2, 3]", encoding="utf-8")
    assert _merge_configs(legacy_file, runtime_list) is False
    assert runtime_list.read_text(encoding="utf-8") == "[1, 2, 3]"

    corrupt_cfg = tmp_path / "corrupt_config.json"
    backup_path = tmp_path / "corrupt_config.json.bak"
    corrupt_raw = (
        '{"theme": "dark", '
        '"api_credentials": {"mistral": {"api_key": "sk-super-secret-value"}}, '
        '"extension_api_token": "ext-secret-tok"'
    )
    corrupt_cfg.write_text(corrupt_raw, encoding="utf-8")
    _sanitize_and_backup_corrupt_config(corrupt_cfg, backup_path)
    assert backup_path.exists()
    backup_text = backup_path.read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in backup_text
    assert "ext-secret-tok" not in backup_text


def test_sanitize_error_message_redacts_auth_schemes_and_userinfo() -> None:
    raw = (
        "Failed calling https://alice-user:p@ss#1!@api.example.com/v1 "
        "Authorization: Bearer tok-secret-1234567890 and "
        "Authorization: Basic dXNlcjpwYXNz"
    )
    sanitized = _sanitize_error_message(raw)
    assert "tok-secret-1234567890" not in sanitized
    assert "dXNlcjpwYXNz" not in sanitized
    assert "alice-user" not in sanitized
    assert "p@ss#1!" not in sanitized


def test_disk_cache_respects_disable_cleanup_and_ignore_ttl(tmp_path: Path) -> None:
    def _age_entry(path: Path, age_sec: float) -> None:
        ts = time.time() - age_sec
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["stored_at"] = ts
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(path, (ts, ts))

    cache_disabled = StockDiskCache(
        cache_dir=tmp_path / "no_gc", default_ttl=1, enable_cleanup=False
    )
    cache_disabled.set("expired_k", {"v": 1})
    cache_disabled.set("stale_other", {"v": 2})
    p_disabled = cache_disabled._entry_path("expired_k")
    p_other = cache_disabled._entry_path("stale_other")
    _age_entry(p_disabled, 600.0)
    _age_entry(p_other, 90000.0)
    cache_disabled._last_cleanup_ts = 0.0
    assert cache_disabled.get("expired_k") is None
    assert p_other.exists()

    cache_enabled = StockDiskCache(cache_dir=tmp_path / "gc", default_ttl=1, enable_cleanup=True)
    cache_enabled.set("stale_k", {"v": 42})
    p_enabled = cache_enabled._entry_path("stale_k")
    _age_entry(p_enabled, 90000.0)
    cache_enabled._last_cleanup_ts = 0.0
    assert cache_enabled.get("stale_k", ignore_ttl=True) == {"v": 42}
    assert p_enabled.exists()


def test_stock_payload_async_inflight_and_index_deepcopy() -> None:
    key = sanitize_cache_key("info_AAPL")
    ev = threading.Event()
    with global_cache.fetch_events_lock:
        global_cache.fetch_events[(key, 86400)] = ev

    try:
        with patch.object(
            app_state.stock_provider, "get_fast_info", side_effect=RuntimeError("boom")
        ):
            fetch_stock_info_async("AAPL")
        assert peek_cached(key, duration=86400) is not CACHE_FETCHING
    finally:
        with global_cache.fetch_events_lock:
            global_cache.fetch_events.pop((key, 86400), None)

    with app_state.cache.sse_data_lock:
        app_state.market.current_indices_cache["SP500"] = {"name": "S&P 500", "price": 5000.0}
    resolved = _resolve_indices_for_response()
    resolved["SP500"]["price"] = 9999.0
    with app_state.cache.sse_data_lock:
        assert app_state.market.current_indices_cache["SP500"]["price"] == 5000.0


def test_validators_multi_fence_and_nullable_optional_fields() -> None:
    text = (
        "Ticker:\n```\nAAPL\n```\n"
        "Result:\n```json\n"
        '{"analysis_summary": "Strong quarter", "target_price_3m": null, "risk_factors": null}\n'
        "```"
    )
    parsed = extract_json_payload(text)
    assert isinstance(parsed, str)
    parsed_obj = json.loads(parsed)
    assert parsed_obj["analysis_summary"] == "Strong quarter"

    ok, err = validate_analysis_result(
        {
            "analysis_summary": "Looks solid",
            "target_price_3m": None,
            "key_catalysts": None,
            "risk_factors": None,
        }
    )
    assert ok is True
    assert not err

    ok_empty, _ = validate_analysis_result({"analysis_summary": "   ", "short_term_outlook": None})
    assert ok_empty is False


def test_utils_edge_cases_across_helpers(tmp_path: Path) -> None:
    dt_ms = _parse_datetime_to_utc("1700000000123")
    assert dt_ms is not None and dt_ms.year == 2023

    now_utc = datetime.now(UTC)
    assert (
        _market_state_from_metadata(
            {
                "currentTradingPeriod": {
                    "regular": {
                        "start": now_utc - timedelta(minutes=5),
                        "end": now_utc + timedelta(minutes=5),
                    }
                }
            }
        )
        == "REGULAR"
    )
    assert (
        _market_state_from_metadata(
            {
                "currentTradingPeriod": {
                    "regular": {"start": float("nan"), "end": now_utc.timestamp() + 60}
                }
            }
        )
        is None
    )

    assert is_valid_symbol(True) is False
    assert is_valid_symbol(False) is False
    assert is_valid_symbol(7203) is True

    prefix = "very_long_prefix_" + "a" * (_CACHE_KEY_MAX_LENGTH + 20)
    full_key = sanitize_cache_key(prefix + "_suffix_1")
    assert _CACHE_KEY_HASH_MARKER in full_key
    assert len(full_key) <= _CACHE_KEY_MAX_LENGTH
    assert get_cached(prefix + "_suffix_1", lambda: {"ok": True}, duration=3600) == {"ok": True}
    clear_cache_prefix(prefix)
    assert peek_cached(prefix + "_suffix_1", duration=3600) is None
    assert _CACHE_KEY_MAX_LENGTH - len(_CACHE_KEY_HASH_MARKER) - _CACHE_KEY_DIGEST_LENGTH > 0

    db_file = tmp_path / "chat_zero.db"
    with patch("utils.chat_history.DB_PATH", db_file):
        store = SQLiteChatHistoryStore(max_msgs_per_session=0)
        store["s1"] = [{"role": "user", "content": "hi"}]
        assert store["s1"] == []

    assert resolve_model_target("   ") is None
    assert resolve_model_target(cast(str, cast(object, 123))) is None

    with patch("credential_manager.get_openai_base_url", return_value="http://127.0.0.1:8080/v1"):
        assert is_local_openai_base_url("") is False
        assert is_local_openai_base_url(None) is True

    assert _uwsgi_cheaper_is_configured({}, ["uwsgi", "--cheaper=0"], None) is False
    assert _uwsgi_cheaper_is_configured({}, ["uwsgi", "--cheaper=false"], None) is False
    assert _uwsgi_cheaper_is_configured({}, ["uwsgi", "--cheaper=2"], None) is True
    assert _uwsgi_cheaper_is_configured({}, ["uwsgi"], SimpleNamespace(opt={"cheaper": [b"0"]})) is False
    assert _uwsgi_cheaper_is_configured({}, ["uwsgi"], SimpleNamespace(opt={"cheaper": [b"2"]})) is True


def test_market_state_rate_limit_variants_and_nonfinite_retry_after() -> None:
    app_state.market.is_yfinance_rate_limited = True
    app_state.market.yfinance_rate_limit_until = time.time() - 5.0
    app_state.market.yfinance_429_streak = 4
    _reset_yfinance_streak_on_success()
    assert app_state.market.is_yfinance_rate_limited is False
    assert app_state.market.yfinance_429_streak == 0

    app_state.market.update_previous_close_cache("7203.T", 2510.25)
    assert app_state.market.get_previous_close_cached("7203") == 2510.25
    assert app_state.market.get_previous_close_cached("7203.T") == 2510.25

    backoff_yf = app_state.market.mark_yf_429(float("nan"))
    backoff_sc = app_state.market.mark_scraper_blocked(float("inf"))
    assert math.isfinite(backoff_yf) and backoff_yf > 0
    assert math.isfinite(backoff_sc) and backoff_sc > 0
    assert math.isfinite(app_state.market.yfinance_rate_limit_until)
    assert math.isfinite(app_state.market.scraper_block_until)


def test_session_manager_closed_retry_non_recursive_and_evicts_session() -> None:
    YFinanceSessionManager._reset_for_testing()
    calls = 0

    class AlwaysClosedSession:
        def __init__(self, impersonate: str | None = None) -> None:
            self.headers: dict[str, str] = {}
            self.impersonate = impersonate
            self.closed = False

        def request(self, *_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise RuntimeError("session already closed")

        def close(self) -> None:
            self.closed = True

    s1 = AlwaysClosedSession()
    s2 = AlwaysClosedSession()
    created = [s1, s2]

    class FakeCurlRequests:
        @staticmethod
        def Session(impersonate: str | None = None) -> AlwaysClosedSession:
            return created.pop(0)

    with (
        patch("session_manager.CURL_CFFI_AVAILABLE", True),
        patch("session_manager.curl_requests", FakeCurlRequests),
        patch("session_manager.reset_yfinance_auth"),
    ):
        YFinanceSessionManager._reset_for_testing()
        mgr2 = YFinanceSessionManager()
        with patch.object(mgr2, "_is_shutting_down", return_value=False):
            first = mgr2.get_session()
            with pytest.raises(RuntimeError, match="closed"):
                first.request("GET", "https://example.test")
    assert s1.closed is True
    assert calls == 2


def test_sync_all_stocks_now_sets_pending_and_reschedules() -> None:
    app_state.market.sync_pending = False
    app_state.market.sync_forced = False
    app_state.market.sync_scheduled = False

    held_lock = threading.Lock()
    held_lock.acquire()
    try:
        with (
            patch.object(sync_control, "_get_sync_execution_lock", return_value=held_lock),
            patch.object(sync_control, "_recover_stale_sync_state_if_needed", return_value=False),
            patch.object(sync_control, "_is_wedged_sync_lock", return_value=False),
        ):
            sync_control.sync_all_stocks_now(force_fetch=True)
    finally:
        held_lock.release()

    assert app_state.market.sync_pending is True
    assert app_state.market.sync_forced is True

    app_state.market.sync_pending = True
    app_state.market.sync_scheduled = False
    scheduled_calls: list[bool] = []

    def fake_prepare(_forced: bool) -> None:
        return None

    with (
        patch.object(sync_control, "_get_sync_execution_lock", return_value=threading.Lock()),
        patch.object(sync_control, "_prepare_sync_items", side_effect=fake_prepare),
        patch.object(
            app_bg,
            "schedule_sync_all_stocks_now",
            side_effect=lambda *a, **kw: scheduled_calls.append(True) or True,
        ),
    ):
        sync_control.sync_all_stocks_now()

    assert scheduled_calls == [True]
    assert app_state.market.sync_pending is False


def test_sse_status_only_diff_and_replay_after_preserves_active_mode() -> None:
    announcer = MagicMock()
    announcer.listener_count.return_value = 1

    orig_ann = app_state.sse_announcer
    orig_cache_vars = (
        bg_common._sse_payload_cached_generation,
        bg_common._sse_payload_generation,
        bg_common._sse_payload_yf_limited,
        bg_common._sse_payload_us_open,
        bg_common._sse_payload_jp_open,
        bg_common._sse_full_snapshot_counter,
        app_bg._sse_payload_cached_generation,
        app_bg._sse_payload_generation,
        app_bg._sse_payload_yf_limited,
        app_bg._sse_payload_us_open,
        app_bg._sse_payload_jp_open,
        app_bg._sse_full_snapshot_counter,
    )
    try:
        app_state.sse_announcer = announcer
        bg_common._sse_payload_cached_generation = 1
        bg_common._sse_payload_generation = 1
        bg_common._sse_payload_yf_limited = False
        bg_common._sse_payload_us_open = False
        bg_common._sse_payload_jp_open = False
        bg_common._sse_full_snapshot_counter = 0
        app_bg._sse_payload_cached_generation = 1
        app_bg._sse_payload_generation = 1
        app_bg._sse_payload_yf_limited = False
        app_bg._sse_payload_us_open = False
        app_bg._sse_payload_jp_open = False
        app_bg._sse_full_snapshot_counter = 0
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.current_indices_cache = {}
        bg_common._sse_prev_stocks = {"us": {}, "jp": {}, "idx": {}}
        bg_common._sse_prev_indices = {}
        app_bg._sse_prev_stocks = {"us": {}, "jp": {}, "idx": {}}
        app_bg._sse_prev_indices = {}

        with (
            patch.object(app_state.market, "is_yf_rate_limited", return_value=True),
            patch.object(bg_common, "is_market_open", return_value=False),
        ):
            bg_common._raw_announce_current_market_state()

        announcer.announce.assert_called_once()
        raw_frame = announcer.announce.call_args.args[0]
        frame_text = raw_frame[1] if isinstance(raw_frame, tuple) else raw_frame
        assert '"stream_event": "diff"' in frame_text
    finally:
        app_state.sse_announcer = orig_ann
        (
            bg_common._sse_payload_cached_generation,
            bg_common._sse_payload_generation,
            bg_common._sse_payload_yf_limited,
            bg_common._sse_payload_us_open,
            bg_common._sse_payload_jp_open,
            bg_common._sse_full_snapshot_counter,
            app_bg._sse_payload_cached_generation,
            app_bg._sse_payload_generation,
            app_bg._sse_payload_yf_limited,
            app_bg._sse_payload_us_open,
            app_bg._sse_payload_jp_open,
            app_bg._sse_full_snapshot_counter,
        ) = orig_cache_vars

    event_log = SSEEventLog()
    seq1 = event_log.emit(1, "diff", 'data: {"ok":1}\n\n')
    seq2 = event_log.emit(1, "diff", 'data: {"ok":2}\n\n')
    event_log.emit(2, "diff", 'data: {"other":true}\n\n')
    event_log._last_used[1] = 0.0
    event_log._last_used[2] = 0.0
    with patch("time.time", return_value=5000.0):
        replayed = event_log.replay_after(seq1, mode=1)
    assert replayed is not None and len(replayed) == 1 and replayed[0][0] == seq2
    assert 2 not in event_log._last_used


def test_sec_fetch_site_strict_allows_remote_admin_token(client, monkeypatch) -> None:
    monkeypatch.setenv("MNS_STRICT_SEC_FETCH_SITE", "1")
    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    monkeypatch.setenv("MNS_ADMIN_TOKEN", "admin-token-123456")

    with client.application.test_request_context(
        "/api/stocks/refresh",
        method="POST",
        headers={"Sec-Fetch-Site": "none", "X-MNS-Admin-Token": "admin-token-123456"},
    ):
        assert _enforce_sec_fetch_site_check() is None

    with client.application.test_request_context(
        "/api/stocks/refresh",
        method="POST",
        headers={"Sec-Fetch-Site": "none"},
    ):
        resp = _enforce_sec_fetch_site_check()
        status = resp[1] if isinstance(resp, tuple) else resp.status_code
        assert resp is not None and status == 403


def test_leader_election_detaches_inherited_lock_on_fork() -> None:
    parent_handle = SimpleNamespace(close=MagicMock())
    try:
        with patch.object(leader_election.os, "getpid", return_value=11111):
            leader_election._set_leader_lock_file(parent_handle)
            leader_election._set_is_sync_leader(True)

        with patch.object(leader_election.os, "getpid", return_value=22222):
            assert leader_election.is_leader() is False
            assert leader_election._get_leader_lock_file() is None

        parent_handle.close.assert_not_called()
    finally:
        leader_election._set_leader_lock_file(None)
        leader_election._set_is_sync_leader(False)


def test_ai_state_eviction_and_shutdown_token_used_flag(tmp_path: Path) -> None:
    assert "_sse_prev_indices" in bg.__all__
    assert "_sse_prev_indices" in app_bg.__all__

    class _ShrinkablePool(LRUCache[str, object]):
        _effective_maxsize = 2

        @property
        def maxsize(self) -> int:
            return self._effective_maxsize

    pool = _ShrinkablePool(maxsize=2)
    pool["k1"] = SimpleNamespace(is_closed=False)
    pool["k2"] = SimpleNamespace(is_closed=False)
    pool._effective_maxsize = 1
    retired: deque[tuple[float, object]] = deque()
    assert AIState._lookup_or_evict_locked(pool, retired, "k3", 1, "test") is None
    assert len(pool) == 0
    assert len(retired) == 2

    mgr = ShutdownTokenManager()
    mgr.runtime_state_dir = tmp_path
    mgr.token_file = tmp_path / ".mns_shutdown_token"
    mgr.used_marker = tmp_path / ".mns_shutdown_token.used"
    mgr._legacy_token_file = tmp_path / "legacy_tok"
    mgr._legacy_used_marker = tmp_path / "legacy_tok.used"
    mgr.shutdown_token = "old-used-tok"
    mgr.shutdown_token_used = True
    tok = mgr.get_or_create_shutdown_token()
    assert tok != "old-used-tok"
    assert mgr.shutdown_token_used is False
