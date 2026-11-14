"""Phase 3 Track B unit & regression tests for core app, state, config, utils, and native host."""

from __future__ import annotations

import copy
import errno
import io
import json
import logging
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# CORE-001: signal-safe shutdown handler & execution shutdown flag ordering
# ---------------------------------------------------------------------------


def test_core001_signal_handler_avoids_logging_and_executor_shutdown(monkeypatch):
    from flask import Flask

    from app_factory import wiring
    from app_state import app_state

    recorded: dict[int, object] = {}

    monkeypatch.setattr(wiring, "_is_testing", lambda: False)
    monkeypatch.delitem(sys.modules, "gunicorn", raising=False)
    monkeypatch.delitem(sys.modules, "uwsgi", raising=False)
    monkeypatch.setattr(signal, "signal", lambda sig, fn: recorded.__setitem__(sig, fn))

    app_state.execution.shutdown_event.clear()
    wiring._register_signal_handlers(Flask(__name__))

    handler = recorded.get(signal.SIGTERM)
    assert callable(handler)

    with (
        patch.object(wiring.logger, "info", side_effect=AssertionError("must not log in signal")),
        patch.object(
            wiring.logger, "error", side_effect=AssertionError("must not log in signal")
        ),
        patch.object(
            app_state,
            "shutdown_executors",
            side_effect=AssertionError("must not shut down executors inside signal handler"),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            handler(signal.SIGTERM, None)
        assert exc_info.value.code == 0

    assert app_state.execution.shutdown_event.is_set() is True
    app_state.execution.shutdown_event.clear()


def test_core001_execution_state_shutdown_runs_once_even_after_signal_event():
    from execution_state import ExecutionState

    state = ExecutionState()
    mock_executor = MagicMock()
    state.executor = mock_executor

    # Signal handler sets shutdown_event without holding executor locks.
    state.shutdown_event.set()
    # atexit handler must STILL run executor shutdown exactly once.
    state.shutdown()
    assert mock_executor.shutdown.call_count == 1
    state.shutdown()
    assert mock_executor.shutdown.call_count == 1


# ---------------------------------------------------------------------------
# CORE-002 & CORE-006: OpenAI/Mistral client redirects, SSRF check & LRU eviction
# ---------------------------------------------------------------------------


def test_core002_openai_client_disables_redirects_and_blocks_imds(monkeypatch):
    from ai_state import AIState
    from utils.networking import PinnedOutboundHTTPTransport

    monkeypatch.setattr(
        "utils.networking.socket.getaddrinfo",
        lambda host, port=None, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    ai = AIState()
    try:
        client = ai.get_or_create_openai_client(base_url="http://example.test/v1")
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert isinstance(client._transport, PinnedOutboundHTTPTransport)

        with pytest.raises(ValueError):
            ai.get_or_create_openai_client(base_url="http://169.254.169.254/latest/meta-data")
        with pytest.raises(ValueError):
            ai.get_or_create_openai_client(base_url="http://[::ffff:169.254.169.254]/v1")
    finally:
        ai.close_all_clients()


def test_core006_closed_cached_client_popped_without_evicting_live_client(monkeypatch):
    from cachetools import LRUCache

    from ai_state import AIState

    monkeypatch.setattr(
        "utils.networking.socket.getaddrinfo",
        lambda host, port=None, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    ai = AIState()
    ai.openai_clients = LRUCache(maxsize=2)

    dead_client = MagicMock()
    dead_client.is_closed = True
    live_client = MagicMock()
    live_client.is_closed = False

    ai.openai_clients[("http://dead.test/v1", "")] = dead_client
    ai.openai_clients[("http://live.test/v1", "")] = live_client

    created: list[MagicMock] = []

    def fake_httpx_client(**_kwargs):
        c = MagicMock()
        c.is_closed = False
        created.append(c)
        return c

    monkeypatch.setattr("ai_state.httpx.Client", fake_httpx_client)
    res = ai.get_or_create_openai_client(base_url="http://dead.test/v1")
    assert res is created[0]
    assert ("http://live.test/v1", "") in ai.openai_clients
    assert live_client.close.call_count == 0


def test_core006_live_client_eviction_defers_close_until_retired_cleanup(monkeypatch):
    from cachetools import LRUCache

    from ai_state import AIState

    monkeypatch.setattr(
        "utils.networking.socket.getaddrinfo",
        lambda host, port=None, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    ai = AIState()
    ai.openai_clients = LRUCache(maxsize=1)

    first_client = MagicMock()
    first_client.is_closed = False

    def fake_httpx_client(**_kwargs):
        c = MagicMock()
        c.is_closed = False
        return c

    monkeypatch.setattr("ai_state.httpx.Client", fake_httpx_client)
    ai.openai_clients[("http://one.test/v1", "")] = first_client

    # Overflowing maxsize retires first_client without calling close() synchronously.
    _second = ai.get_or_create_openai_client(base_url="http://two.test/v1")
    assert first_client.close.call_count == 0
    assert any(client is first_client for _, client in ai._retired_openai_clients)

    # Once all active/retired clients are closed on shutdown, close() runs.
    ai.close_all_clients()
    assert first_client.close.call_count == 1
    assert len(ai._retired_openai_clients) == 0


# ---------------------------------------------------------------------------
# CORE-003: HEAD requests honor Sec-Fetch-Site costly endpoint checks
# ---------------------------------------------------------------------------


def test_core003_head_cross_site_costly_path_blocked():
    from app import create_app

    app = create_app(config_override={"TESTING": True})
    client = app.test_client()

    resp_cross = client.head("/api/stocks", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp_cross.status_code == 403

    resp_same = client.head("/api/stocks", headers={"Sec-Fetch-Site": "same-origin"})
    assert resp_same.status_code != 403


# ---------------------------------------------------------------------------
# CORE-004: Unified remote API worker & admin token validation
# ---------------------------------------------------------------------------


def test_core004_validate_remote_api_configuration_parity(monkeypatch):
    from utils.worker_validation import (
        RemoteApiConfigurationError,
        validate_remote_api_configuration,
    )

    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)
    monkeypatch.delenv("MNS_PROXY_FIX", raising=False)
    monkeypatch.delenv("MNS_PROXY_FIX_X_FOR", raising=False)
    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    validate_remote_api_configuration()

    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    with pytest.raises(RemoteApiConfigurationError, match="MNS_PROXY_FIX=1"):
        validate_remote_api_configuration()

    monkeypatch.setenv("MNS_PROXY_FIX", "1")
    monkeypatch.setenv("MNS_ADMIN_TOKEN", "too-short")
    with pytest.raises(RemoteApiConfigurationError, match="MNS_ADMIN_TOKEN"):
        validate_remote_api_configuration()

    monkeypatch.setenv("MNS_ADMIN_TOKEN", "x" * 32)
    monkeypatch.setenv("MNS_PROXY_FIX_X_FOR", "abc")
    with pytest.raises(RemoteApiConfigurationError, match="MNS_PROXY_FIX_X_FOR"):
        validate_remote_api_configuration()

    monkeypatch.setenv("MNS_PROXY_FIX_X_FOR", "")
    validate_remote_api_configuration()


# ---------------------------------------------------------------------------
# CORE-007 & BE-001: Market cache variants, finite volume guards, FX rate lock
# ---------------------------------------------------------------------------


def test_core007_market_cache_variants_and_volume_guards():
    from market_state import MarketDataState

    ms = MarketDataState()
    ms.update_previous_close_cache("7203", 2500.0)
    ms.update_previous_close_cache("7203.T", 2505.0)
    ms.clear_previous_close_cache("7203")
    assert ms.get_previous_close_cached("7203") is None
    assert ms.get_previous_close_cached("7203.T") is None

    bad_volumes: list[Any] = [None, -1, 0, True, False, float("nan"), float("inf"), "not-a-num"]
    for bad_vol in bad_volumes:
        ms.update_volume_cache("7203", 1000)
        ms.update_volume_cache("7203.T", 1000)
        ms.update_volume_cache("7203", bad_vol)
        assert ms.get_volume_cached("7203") is None
        assert ms.get_volume_cached("7203.T") is None

    ms.update_volume_cache("AAPL", 12345.0)
    assert ms.get_volume_cached("AAPL") == 12345


def test_be001_usdjpy_rate_snapshot_atomic_under_concurrency():
    from ai_state import _tracked_usdjpy_rate
    from app_state import app_state
    from market_state import MarketDataState
    from utils.stock_payload import get_current_usdjpy_rate

    ms = MarketDataState()
    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        idx = 1
        while not stop.is_set():
            val = 150.0 + (idx % 10)
            ms.update_usdjpy_rate(val, float(idx * 1000 + (idx % 10)))
            idx += 1

    def reader() -> None:
        while not stop.is_set():
            rate, ts = ms.get_usdjpy_rate_snapshot()
            if ts > 0 and round(rate - 150.0) != round(ts % 1000):
                errors.append(AssertionError(f"torn read rate={rate} ts={ts}"))

    t_w = threading.Thread(target=writer, daemon=True)
    t_r = threading.Thread(target=reader, daemon=True)
    t_w.start()
    t_r.start()
    time.sleep(0.12)
    stop.set()
    t_w.join(timeout=2)
    t_r.join(timeout=2)
    assert not errors

    orig_rate, orig_ts = app_state.market.get_usdjpy_rate_snapshot()
    try:
        app_state.market.update_usdjpy_rate(156.75, 123456.0)
        assert _tracked_usdjpy_rate() == pytest.approx(156.75)
        with patch.object(app_state.stock_disk_cache, "get", return_value=None):
            rate_stale, is_estimated_stale = get_current_usdjpy_rate()
            assert rate_stale == pytest.approx(156.75)
            assert is_estimated_stale is True

            app_state.market.update_usdjpy_rate(157.25, time.time())
            rate_fresh, is_estimated_fresh = get_current_usdjpy_rate()
            assert rate_fresh == pytest.approx(157.25)
            assert is_estimated_fresh is False
    finally:
        app_state.market.update_usdjpy_rate(orig_rate, orig_ts)


# ---------------------------------------------------------------------------
# CORE-008: config_override applied before ProxyFix / secret / init_security
# ---------------------------------------------------------------------------


def test_core008_config_override_visible_during_init_security(monkeypatch):
    import app_factory.factory as factory_mod

    observed: dict[str, object] = {}
    orig_init_security = factory_mod.init_security

    def spy_init_security(app):
        observed["marker_before_security"] = app.config.get("TRACK_B_PRE_SECURITY_FLAG")
        return orig_init_security(app)

    monkeypatch.setattr(factory_mod, "init_security", spy_init_security)
    app = factory_mod.create_app(
        config_override={
            "TESTING": True,
            "TRACK_B_PRE_SECURITY_FLAG": "visible-early",
            "SESSION_COOKIE_SECURE": True,
        }
    )
    assert observed["marker_before_security"] == "visible-early"
    assert app.config["SESSION_COOKIE_SECURE"] is True


# ---------------------------------------------------------------------------
# CFG-001: Shared process umask lock prevents interleaved umask tampering
# ---------------------------------------------------------------------------


def test_cfg001_restrictive_umask_lock_serializes_concurrent_sections():
    from utils.threading import restrictive_umask_lock

    active = 0
    overlap_detected = False
    state_lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker() -> None:
        nonlocal active, overlap_detected
        barrier.wait()
        for _ in range(15):
            with restrictive_umask_lock():
                with state_lock:
                    active += 1
                    if active > 1:
                        overlap_detected = True
                time.sleep(0.001)
                with state_lock:
                    active -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert overlap_detected is False


# ---------------------------------------------------------------------------
# CFG-002: UnicodeDecodeError backup on save_config & config_update_lock merge
# ---------------------------------------------------------------------------


def test_cfg002_save_config_handles_corrupt_utf8_bytes(tmp_path, monkeypatch):
    import config_store
    from config import io as config_io
    from config import merge as config_merge

    cfg_dir = tmp_path / "appdata"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    legacy_file = cfg_dir / "legacy_config.json"
    cfg_file.write_bytes(b"\xff\xfe\xfd{invalid-utf8")

    monkeypatch.setattr(config_store, "APP_DATA_DIR", cfg_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", legacy_file)
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)

    with pytest.raises(RuntimeError, match="corrupted configuration file"):
        config_io.save_config({"custom_ai_prompt": "x"})
    assert config_store._CONFIG_CORRUPTED is True
    assert list(cfg_dir.glob("config.json.corrupt.*.bak"))

    # Verify _merge_configs runs cleanly under config_update_lock.
    src = cfg_dir / "src.json"
    dst = cfg_dir / "dst.json"
    src.write_text(json.dumps({"ui_theme": "dark"}), encoding="utf-8")
    dst.write_text(json.dumps({"ui_theme": "light"}), encoding="utf-8")
    assert config_merge._merge_configs(src, dst) is True


# ---------------------------------------------------------------------------
# CFG-003: Credential trimming, type validation, and internal ephemeral key filter
# ---------------------------------------------------------------------------


def test_cfg003_credential_validation_trimming_and_ephemeral_filter(tmp_path, monkeypatch):
    import config_store
    import credential_manager
    import crypto_utils

    cfg_dir = tmp_path / "cred_cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(config_store, "APP_DATA_DIR", cfg_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", cfg_dir / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", True)

    bad_inputs: list[tuple[str, dict[str, Any]]] = [
        ("agent_id", {"agent_id": 12345}),
        ("openai_base_url", {"openai_base_url": 99}),
        ("openai_model", {"openai_model": ["gpt-4"]}),
        ("openai_embeddings_model", {"openai_embeddings_model": object()}),
    ]
    for pattern, kwargs in bad_inputs:
        with pytest.raises(TypeError, match=pattern):
            credential_manager.save_api_credentials(**kwargs)

    with crypto_utils._EPHEMERAL_LOCK:
        orig_ephemeral = dict(crypto_utils._EPHEMERAL_CREDENTIALS)
    try:
        crypto_utils.clear_ephemeral_credentials()
        crypto_utils._EPHEMERAL_CREDENTIALS["mns_master_key"] = {"dummy": "1"}
        assert crypto_utils.is_ephemeral_active() is True
        state_only_master = credential_manager.get_api_credential_state()
        assert state_only_master["credentials_ephemeral"] is False
        assert state_only_master["credentials_ephemeral_keys"] == []
        assert state_only_master["credentials_ephemeral_warning"] is None

        credential_manager.save_api_credentials(mistral_api_key="  trimmed-secret-value  ")
        assert os.environ.get("MISTRAL_API_KEY") == "trimmed-secret-value"

        with crypto_utils._EPHEMERAL_LOCK:
            crypto_utils._EPHEMERAL_CREDENTIALS["mistral_api_key"] = "enc"
        state_with_api = credential_manager.get_api_credential_state()
        assert state_with_api["credentials_ephemeral"] is True
        assert state_with_api["credentials_ephemeral_keys"] == ["mistral_api_key"]
        assert state_with_api["credentials_ephemeral_warning"] is not None
    finally:
        crypto_utils.clear_ephemeral_credentials()
        with crypto_utils._EPHEMERAL_LOCK:
            crypto_utils._EPHEMERAL_CREDENTIALS.update(orig_ephemeral)
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# UTIL-001 & UTIL-009 part 1: IPv4-mapped IPv6 blocking & CORS origin lock
# ---------------------------------------------------------------------------


def test_util001_and_util009_ipv4_mapped_blocking_and_cors_lock(monkeypatch):
    from utils import networking

    assert networking.is_blocked_outbound_host("::ffff:169.254.169.254") is True
    assert networking.is_blocked_outbound_host("fe80::1%eth0") is True
    ok_v4_mapped, _ = networking.validate_outbound_base_url("http://[::ffff:169.254.169.254]/v1")
    assert ok_v4_mapped is False
    ok_userinfo, _ = networking.validate_outbound_base_url("https://user:pass@127.0.0.1:11434/v1")
    assert ok_userinfo is False
    ok_ctrl, _ = networking.validate_outbound_base_url("http://127.0.0.1:11434/v1\nbad")
    assert ok_ctrl is False
    ok_port0, _ = networking.validate_outbound_base_url("http://127.0.0.1:0/v1")
    assert ok_port0 is False

    # DNS resolution failures fail closed (block outbound target); valid public DNS succeeds.
    ok_test, _ = networking.validate_outbound_base_url("http://example.test/v1")
    assert ok_test is False

    with monkeypatch.context() as mp:
        mp.setattr(
            "utils.networking.socket.getaddrinfo",
            MagicMock(side_effect=OSError("dns timeout")),
        )
        assert networking.is_blocked_outbound_host("dns-error.test") is True

        mp.setattr(
            "utils.networking.socket.getaddrinfo",
            MagicMock(side_effect=UnicodeError("bad idna")),
        )
        assert networking.is_blocked_outbound_host("bad-idna.test") is True

        mp.setattr(
            "utils.networking.socket.getaddrinfo",
            MagicMock(return_value=[]),
        )
        assert networking.is_blocked_outbound_host("empty-dns.test") is True

        mp.setattr(
            "utils.networking.socket.getaddrinfo",
            MagicMock(
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            ),
        )
        ok_public, _ = networking.validate_outbound_base_url("https://example.test/v1")
        assert ok_public is True

    calls = 0
    orig_loader = networking._load_allowed_extension_origins

    def slow_loader():
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return orig_loader()

    monkeypatch.setattr(networking, "_load_allowed_extension_origins", slow_loader)
    monkeypatch.setattr(networking, "_cors_origins_cache", None)
    monkeypatch.setattr(networking, "_cors_origins_cache_ts", 0.0)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(networking.get_allowed_cors_origins()))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert calls == 1
    assert len(results) == 5
    assert all(r == results[0] for r in results)


# ---------------------------------------------------------------------------
# UTIL-002: NFKC before dangerous_chars check in is_valid_symbol
# ---------------------------------------------------------------------------


def test_util002_is_valid_symbol_nfkc_before_dangerous_chars():
    from utils.normalization import is_valid_symbol

    assert is_valid_symbol("A\uff0e\uff0eB") is False
    assert is_valid_symbol("A\uff0fB") is False
    assert is_valid_symbol("\uff21\uff21\uff30\uff2c") is True


# ---------------------------------------------------------------------------
# UTIL-003: Disk cache UnicodeDecodeError & exact boundary delete_prefix
# ---------------------------------------------------------------------------


def test_util003_disk_cache_unicode_decode_and_delete_prefix_boundary(tmp_path):
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(tmp_path)
    corrupt_path = cache._entry_path("corrupt_key")
    corrupt_path.write_bytes(b"\xff\xfe{invalid")
    assert cache.get("corrupt_key") is None
    assert not corrupt_path.exists()

    corrupt_path2 = cache._entry_path("corrupt_remove")
    corrupt_path2.write_bytes(b"\xff\xfe{invalid")
    assert cache.remove_fields_recursive("x") == 0
    assert not corrupt_path2.exists()

    cache.set("AAPL", {"price": 200})
    cache.set("AAPL_info", {"sector": "Tech"})
    cache.set("AAPL.T", {"price": 300})
    removed = cache.delete_prefix("AAPL")
    assert removed == 2
    assert cache.get("AAPL") is None
    assert cache.get("AAPL_info") is None
    assert cache.get("AAPL.T") == {"price": 300}


# ---------------------------------------------------------------------------
# UTIL-004: chat_history role validation, session limit on touch, key rotation
# ---------------------------------------------------------------------------


def test_util004_chat_history_role_validation_limit_and_fernet_rotation(tmp_path, monkeypatch):
    import config_store
    from utils import chat_history as ch

    monkeypatch.setattr(ch, "DB_PATH", tmp_path / "chat.db")
    ch._reset_db_state()
    store = ch.SQLiteChatHistoryStore(max_sessions=2)
    try:
        with pytest.raises(TypeError, match="role"):
            store.add_message("s1", {"role": 123, "content": "hi"})
        with pytest.raises(ValueError, match="non-empty"):
            store.add_message("s1", {"role": "   ", "content": "hi"})

        # record_turn with append=None / init_messages=None touches/creates session
        # and still enforces max_sessions.
        store.record_turn("sess_a")
        time.sleep(0.01)
        store.record_turn("sess_b")
        time.sleep(0.01)
        store.record_turn("sess_c")
        assert len(store) == 2

        from cryptography.fernet import Fernet

        key1 = Fernet.generate_key().decode("ascii")
        key2 = Fernet.generate_key().decode("ascii")
        current_key = {"val": key1}
        monkeypatch.setattr(config_store, "get_or_create_master_key", lambda: current_key["val"])
        ch._reset_db_state()
        f1 = ch._get_fernet()
        current_key["val"] = key2
        f2 = ch._get_fernet()
        assert f1 is not f2
    finally:
        ch.SQLiteChatHistoryStore._reset_for_testing()


# ---------------------------------------------------------------------------
# UTIL-005: stock_payload sentinel filtering & finite snapshot fields
# ---------------------------------------------------------------------------


def test_util005_stock_payload_sentinel_and_non_finite_guards():
    from app_state import app_state
    from services.realtime_engine import realtime_market_engine
    from utils import stock_payload

    orig_short_cache = dict(app_state.market.yfinance_short_cache)
    orig_current = copy.deepcopy(app_state.market.current_stocks_cache)
    orig_target = copy.deepcopy(app_state.market.target_stocks_cache)
    hist = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [100.0, 101.0],
            "Volume": [1000, 1100],
        }
    )
    try:
        app_state.market.yfinance_short_cache["info_short_BADSENTINEL"] = {
            "failed": True,
            "sector": "CorruptSector",
        }
        app_state.market.yfinance_short_cache["info_short_ERRSENTINEL"] = {
            "error": True,
            "sector": "ErrorSector",
        }
        with (
            patch.object(app_state.stock_disk_cache, "get", return_value=None),
            patch.object(stock_payload, "is_market_open", return_value=False),
        ):
            payload = stock_payload.build_stock_payload(
                "BADSENTINEL", {"name": "Bad"}, "us", hist, lightweight=True
            )
            assert payload is not None
            assert payload["sector"] == "Other"
            assert payload["name"] == "Bad"

            payload_err = stock_payload.build_stock_payload(
                "ERRSENTINEL", {"name": "Err"}, "us", hist, lightweight=True
            )
            assert payload_err is not None
            assert payload_err["sector"] == "Other"

        base_row = {
            "symbol": "TEST",
            "price": 100.0,
            "change": 1.0,
            "change_percent": 1.0,
            "volume": 500,
            "source": "yfinance",
        }
        app_state.market.current_stocks_cache = {"us": [dict(base_row)], "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        with (
            patch.object(
                realtime_market_engine,
                "get_market_snapshot",
                return_value={
                    "TEST": {
                        "price": 105.0,
                        "change": float("nan"),
                        "change_percent": float("inf"),
                        "volume": True,
                        "source": "tradingview",
                    }
                },
            ),
            patch.object(realtime_market_engine, "get_pts_snapshot", return_value={}),
            patch.object(stock_payload, "get_stock_volume", return_value=None),
        ):
            resolved = stock_payload._resolve_stocks_for_response()
            row = resolved["us"][0]
            assert row["price"] == 105.0
            assert row["change"] == 1.0
            assert row["change_percent"] == 1.0
            assert row["volume"] == 500
    finally:
        app_state.market.yfinance_short_cache.clear()
        app_state.market.yfinance_short_cache.update(orig_short_cache)
        app_state.market.current_stocks_cache = orig_current
        app_state.market.target_stocks_cache = orig_target


# ---------------------------------------------------------------------------
# UTIL-006: utils.storage dynamic paths & concurrent legacy migration
# ---------------------------------------------------------------------------


def test_util006_storage_dynamic_paths_and_atomic_legacy_migration(tmp_path, monkeypatch):
    import config_store
    from app_state import app_state
    from utils import storage

    app_dir = tmp_path / "runtime"
    base_dir = tmp_path / "repo"
    app_dir.mkdir()
    base_dir.mkdir()

    monkeypatch.setattr(config_store, "APP_DATA_DIR", app_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", app_dir / "config.json")
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", base_dir / "config.json")
    monkeypatch.setattr(config_store, "USER_STOCKS_FILE", app_dir / "user_stocks.json")
    monkeypatch.setattr(config_store, "BASE_DIR", base_dir)
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", True)
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", storage._DEFAULT_USER_STOCKS_FILE)
    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", storage._DEFAULT_LEGACY_USER_STOCKS_FILE)

    assert storage._get_user_stocks_path() == app_dir / "user_stocks.json"
    assert storage._get_legacy_user_stocks_path() == base_dir / "user_stocks.json"

    legacy_file = base_dir / "user_stocks.json"
    legacy_file.write_text('{"us": {"AAPL": {"name": "Apple"}}}', encoding="utf-8")

    # Concurrent migrations must succeed safely without losing the migrated payload.
    barrier = threading.Barrier(4)
    results: list[bool] = []
    res_lock = threading.Lock()

    def run_mig() -> None:
        barrier.wait()
        ok = storage._migrate_legacy_user_stocks()
        with res_lock:
            results.append(ok)

    threads = [threading.Thread(target=run_mig) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert any(results)
    assert (app_dir / "user_stocks.json").exists()

    app_state.market.last_modified_ns = 0
    storage.load_user_stocks(force=True)
    assert app_state.market.user_us["AAPL"]["name"] == "Apple"


# ---------------------------------------------------------------------------
# UTIL-007: market_utils Mapping support & weekday fallback status update
# ---------------------------------------------------------------------------


def test_util007_market_utils_mapping_and_fallback_status():
    from app_state import app_state
    from utils import market_utils

    class ReadOnlyMapping(Mapping[str, Any]):
        def __init__(self, data: dict[str, Any]):
            self._data = data

        def __getitem__(self, k: str) -> Any:
            return self._data[k]

        def __iter__(self) -> Iterator[str]:
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

    now_ts = time.time()
    meta = ReadOnlyMapping(
        {
            "currentTradingPeriod": ReadOnlyMapping(
                {"regular": ReadOnlyMapping({"start": now_ts - 60, "end": now_ts + 60})}
            )
        }
    )
    assert market_utils._market_state_from_metadata(meta) == "REGULAR"

    with (
        patch.object(market_utils, "_fetch_live_market_state", return_value=None),
        patch.object(market_utils, "is_jp_market_holiday", return_value=False),
        patch.object(market_utils, "_is_market_session_open", return_value=True),
        patch.object(app_state.market, "update_market_status") as mock_upd,
    ):
        assert market_utils.is_market_open("jp", bypass_cache=True, ignore_weekend=True) is True
        mock_upd.assert_called_once_with("jp", "REGULAR")

    with (
        patch.object(market_utils, "_fetch_live_market_state", return_value=None),
        patch.object(market_utils, "is_us_market_holiday", return_value=False),
        patch.object(market_utils, "_is_market_session_open", return_value=False),
        patch.object(app_state.market, "update_market_status") as mock_upd_us,
    ):
        assert market_utils.is_market_open("us", bypass_cache=True, ignore_weekend=True) is False
        mock_upd_us.assert_called_once_with("us", "CLOSED")


# ---------------------------------------------------------------------------
# UTIL-008: validators upside_3m preserves 0 and guards bool/nan aliases
# ---------------------------------------------------------------------------


def test_util008_validators_upside_zero_and_alias_guards():
    from utils.validators import _normalize_analysis_aliases

    preserved = _normalize_analysis_aliases({"upside_3m": 0, "upside": 25})
    assert preserved["upside_3m"] == "0.0%"

    bool_alias = _normalize_analysis_aliases({"upside": True})
    assert bool_alias["upside_3m"] == "True"

    nan_alias = _normalize_analysis_aliases({"upside": float("nan")})
    assert math.isnan(float(nan_alias["upside_3m"]))


# ---------------------------------------------------------------------------
# UTIL-009 part 2: bypass_negative_cache clears stale negative entry on hit
# ---------------------------------------------------------------------------


def test_util009_bypass_negative_cache_clears_negative_entry():
    from utils.caching import (
        _set_cached_value,
        clear_cache_prefix,
        get_cached_context_with_negative_cache,
    )

    key = "track_b_neg_clear_key"
    clear_cache_prefix(key)
    try:
        _set_cached_value(f"{key}__negative", True, duration=90)
        val = get_cached_context_with_negative_cache(
            key,
            lambda: "fresh context",
            success_ttl=60,
            negative_ttl=90,
            bypass_negative_cache=True,
        )
        assert val == "fresh context"
        cached = get_cached_context_with_negative_cache(
            key,
            lambda: "should-not-run",
            success_ttl=60,
            negative_ttl=90,
            bypass_negative_cache=False,
        )
        assert cached == "fresh context"
    finally:
        clear_cache_prefix(key)


# ---------------------------------------------------------------------------
# NH-001..NH-005: Native host log fallback, size cap, lock retry, PowerShell
# ---------------------------------------------------------------------------


def test_nh_start_backend_log_handler_falls_back_on_oserror(tmp_path):
    from native_host import start_backend

    unwritable_path = tmp_path / "blocked" / "backend.log"
    orig_mkdir = Path.mkdir

    def fail_primary(self, *args, **kwargs):
        if self == unwritable_path.parent or self.name == "mistral_nex_stocks":
            raise OSError("permission denied")
        return orig_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", fail_primary):
        handler = start_backend._build_backend_log_handler(unwritable_path)
        try:
            assert isinstance(handler, logging.StreamHandler)
        finally:
            handler.close()


def test_nh_native_host_send_message_enforces_max_bytes():
    from native_host import native_host

    buf = io.BytesIO()
    with patch.object(native_host, "RAW_STDOUT", buf):
        oversized = {"ok": True, "blob": "x" * (native_host.MAX_MESSAGE_BYTES + 100)}
        native_host.send_message(oversized)
        assert buf.getvalue() == b""


def test_nh_native_host_posix_flock_eagain_retries_and_mode_check(tmp_path):
    from native_host import native_host

    token_file = tmp_path / ".mns_shutdown_token"
    token_file.write_text('{"scheme":"fernet","value":"abc"}', encoding="utf-8")
    sent: list[dict[str, Any]] = []
    valid_id = "a" * 32

    def fake_flock(_fd, flags):
        if flags & 1:  # LOCK_SH
            raise BlockingIOError(errno.EAGAIN, "busy")

    fake_fcntl = types.SimpleNamespace(
        LOCK_SH=1,
        LOCK_EX=2,
        LOCK_NB=4,
        LOCK_UN=8,
        flock=MagicMock(side_effect=fake_flock),
    )

    # Only the token file reports the permissive mode. Patching Path.stat with a
    # blanket return value would also make Path.exists() report the ".used"
    # marker as present on Python <= 3.13 (where exists() delegates to stat()),
    # short-circuiting the code path under test.
    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        if self == token_file:
            return SimpleNamespace(st_mode=0o100640)
        return real_stat(self, *args, **kwargs)

    with (
        patch.object(
            native_host,
            "read_message",
            side_effect=[{"extensionId": valid_id, "action": "get_shutdown_token"}, None],
        ),
        patch.object(native_host, "send_message", side_effect=lambda m: sent.append(m)),
        patch.object(native_host, "_check_rate_limit", return_value=True),
        patch.object(native_host, "_token_action_allowed", return_value=True),
        patch.object(native_host, "is_backend_healthy_once", return_value=True),
        patch.object(native_host, "_load_allowed_manifest_origins", return_value={valid_id}),
        patch("sys.argv", ["native_host.py", f"chrome-extension://{valid_id}/"]),
        patch.object(native_host, "_get_ancestor_process_names", return_value=["chrome.exe"]),
        patch("config_store.APP_DATA_DIR", tmp_path),
        patch("os.name", "posix"),
        patch.object(Path, "stat", fake_stat),
        patch.object(Path, "chmod") as mock_chmod,
        patch("time.sleep", return_value=None),
        patch.dict(sys.modules, {"fcntl": fake_fcntl}),
    ):
        native_host.main()

    # Group-readable 0o640 must trigger 0o600 restriction on POSIX (0o077 mask).
    mock_chmod.assert_called_once_with(0o600)
    assert len(sent) == 1
    assert sent[0]["ok"] is False
    assert "Failed to read token file" in sent[0]["error"]


def test_nh_powershell_scripts_security_fixes(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is required")

    root = Path(__file__).resolve().parent.parent

    # 1) validate_native_host_windows.ps1 rejects whitespace & empty origin entries
    for bad_origins in ([""], ["   "], [f"chrome-extension://{'a'*32}/", ""]):
        manifest = tmp_path / "bad_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "name": "com.mistral_nex_stocks.host",
                    "type": "stdio",
                    "path": str(root / "native_host" / "native_host.py"),
                    "allowed_origins": bad_origins,
                }
            ),
            encoding="utf-8",
        )
        res = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(root / "native_host" / "validate_native_host_windows.ps1"),
                "-ManifestPath",
                str(manifest),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res.returncode != 0

    # 2) install_host_windows.ps1 helper unit tests:
    #    - Test-SafePath rejects '!', '\n', '\0'
    #    - Test-DirectoryUserWritable fails closed on Get-Acl error
    #    - Test-DirectoryUserWritable survives unresolvable ACE SID and still catches later Users WriteData ACE
    #    - Resolve-PythonPath throws when explicit -Requested does not exist
    ps_script = r"""
$src = Get-Content -LiteralPath 'native_host/install_host_windows.ps1' -Raw -Encoding UTF8
$s1 = $src.IndexOf('function Test-SafePath')
$e1 = $src.IndexOf('function Write-FileNoBom')
Invoke-Expression $src.Substring($s1, $e1 - $s1)

$badBang = Test-SafePath -Path 'C:\Apps\bang!\python.exe'
$badCrLf = Test-SafePath -Path ("C:\Apps\foo`nbar\python.exe")
$badNul = Test-SafePath -Path ("C:\Apps\foo" + [char]0 + "bar")
function Get-Acl { param([string]$Path) throw 'ACL unreadable' }
$failClosedAcl = Test-DirectoryUserWritable -Dir 'C:\denied'

$badRef = [pscustomobject]@{}
Add-Member -InputObject $badRef -MemberType ScriptMethod -Name Translate -Value { param($t) throw 'orphan SID' }
$badRule = [pscustomobject]@{
  IdentityReference = $badRef
  AccessControlType = [System.Security.AccessControl.AccessControlType]::Allow
  FileSystemRights = [System.Security.AccessControl.FileSystemRights]::Read
}
$goodRule = [pscustomobject]@{
  IdentityReference = (New-Object System.Security.Principal.NTAccount('BUILTIN\Users'))
  AccessControlType = [System.Security.AccessControl.AccessControlType]::Allow
  FileSystemRights = [System.Security.AccessControl.FileSystemRights]::WriteData
}
$mixedAcl = [pscustomobject]@{ Access = @($badRule, $goodRule) }
function Get-Acl { param([string]$Path) return $mixedAcl }
$mixedAclWritable = Test-DirectoryUserWritable -Dir 'C:\mixed-acl'

$missingPyThrew = $false
try { [void](Resolve-PythonPath -Requested 'C:\definitely_missing_12345\python.exe' -RootDir '.') } catch { $missingPyThrew = $true }

[pscustomobject]@{
  bad_bang = $badBang
  bad_crlf = $badCrLf
  bad_nul = $badNul
  fail_closed_acl = $failClosedAcl
  mixed_acl_writable = $mixedAclWritable
  missing_py_threw = $missingPyThrew
} | ConvertTo-Json -Compress
"""
    res2 = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr
    json_line = res2.stdout.strip().splitlines()[-1]
    payload = json.loads(json_line)
    assert payload["bad_bang"] is False
    assert payload["bad_crlf"] is False
    assert payload["bad_nul"] is False
    assert payload["fail_closed_acl"] is True
    assert payload["mixed_acl_writable"] is True
    assert payload["missing_py_threw"] is True

    # 3) Every Test-Path in all 4 scripts uses -LiteralPath and AST parses cleanly
    for script_name in (
        "install_host_windows.ps1",
        "uninstall_host_windows.ps1",
        "validate_native_host_windows.ps1",
        "diagnose_native_host_windows.ps1",
    ):
        raw_bytes = (root / "native_host" / script_name).read_bytes()
        assert not raw_bytes.startswith(b"\xef\xbb\xbf"), f"{script_name} must not have UTF-8 BOM"
        text = raw_bytes.decode("utf-8")
        for cmdlet in ("Test-Path", "Get-Acl"):
            idx = 0
            while True:
                pos = text.find(cmdlet, idx)
                if pos == -1:
                    break
                tail = text[pos : pos + 40]
                assert "-LiteralPath" in tail, (
                    f"{script_name} has {cmdlet} without -LiteralPath: {tail}"
                )
                idx = pos + len(cmdlet)

    uninstall_src = (root / "native_host" / "uninstall_host_windows.ps1").read_text(encoding="utf-8")
    assert "$failedKeys" in uninstall_src
    assert "$Browser" in uninstall_src
    assert "$Scope" in uninstall_src
    install_src = (root / "native_host" / "install_host_windows.ps1").read_text(encoding="utf-8")
    assert "Test-DirectoryUserWritable -Dir $RootDir" in install_src
    assert "$Scope -eq 'LocalMachine'" in install_src


# ---------------------------------------------------------------------------
# QA-001: Connect-time DNS pinning & fail-closed outbound SSRF transport
# ---------------------------------------------------------------------------


def test_qa001_pinned_outbound_transport_blocks_dns_rebinding_and_pins_ip(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpcore

    from services.openai_service import _create_httpx_client
    from utils.networking import PinnedOutboundHTTPTransport, PinnedOutboundSyncBackend

    backend = PinnedOutboundSyncBackend()

    # 1) Mixed or rebound DNS containing link-local/metadata must fail BEFORE TCP connect.
    with patch.object(httpcore.SyncBackend, "connect_tcp") as super_connect:
        with monkeypatch.context() as mp:
            mp.setattr(
                "utils.networking.socket.getaddrinfo",
                MagicMock(
                    return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
                    ]
                ),
            )
            with pytest.raises(httpcore.ConnectError, match="Blocked outbound IP"):
                backend.connect_tcp("rebind.test", 443, timeout=1.0)
            super_connect.assert_not_called()

            mp.setattr(
                "utils.networking.socket.getaddrinfo",
                MagicMock(
                    return_value=[
                        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:169.254.169.254", 443, 0, 0)),
                    ]
                ),
            )
            with pytest.raises(httpcore.ConnectError, match="Blocked outbound IP"):
                backend.connect_tcp("rebind-v6.test", 443, timeout=1.0)
            super_connect.assert_not_called()

            # 2) Valid public IP pins connect_tcp(host=<ip_literal>) rather than the domain name.
            mp.setattr(
                "utils.networking.socket.getaddrinfo",
                MagicMock(
                    return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                    ]
                ),
            )
            sentinel_stream = MagicMock()
            super_connect.return_value = sentinel_stream
            stream = backend.connect_tcp("pinned.test", 443, timeout=2.0)
            assert stream is sentinel_stream
            assert super_connect.call_args.kwargs["host"] == "93.184.216.34"
            assert super_connect.call_args.kwargs["port"] == 443

    # 3) _create_httpx_client enforces PinnedOutboundHTTPTransport + trust_env=False + no redirects.
    direct_client = _create_httpx_client(timeout=2.0)
    try:
        assert direct_client.follow_redirects is False
        assert direct_client.trust_env is False
        assert isinstance(direct_client._transport, PinnedOutboundHTTPTransport)
    finally:
        direct_client.close()

    # 4) Live loopback request through PinnedOutboundHTTPTransport connects using pinned IP.
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            _ = (fmt, args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = _create_httpx_client(timeout=3.0)
        try:
            resp = client.get(f"http://127.0.0.1:{port}/models")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
        finally:
            client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
