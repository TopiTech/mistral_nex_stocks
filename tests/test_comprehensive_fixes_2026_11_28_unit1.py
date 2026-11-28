"""Unit 1 comprehensive review fixes regression tests (2026-11-28)."""

from __future__ import annotations

import contextlib
import ctypes
import gc
import os
import shutil
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests


def test_validators_salvage_no_redos() -> None:
    from utils.validators import _trim_trailing_partial_token, extract_json_payload

    assert _trim_trailing_partial_token('{"a": 1, "b": ') == '{"a": 1'
    assert _trim_trailing_partial_token('{"a": 1, "partial') == '{"a": 1'
    assert _trim_trailing_partial_token('{"a": 1, ') == '{"a": 1'

    payload = '{"a":' + (" " * 2000) + "!"
    t0 = time.perf_counter()
    assert extract_json_payload(payload) == "{}"
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"extract_json_payload took {elapsed:.3f}s (ReDoS regression)"

    unparseable = 'no json here {"a":' + (" " * 2000) + "!"
    t1 = time.perf_counter()
    assert extract_json_payload(unparseable) == "{}"
    assert (time.perf_counter() - t1) < 1.0

    # Unsalvageable chunk that exercises all 500 cutback attempts on whitespace + non-matching char
    heavy = '{"k": "v"' + (" " * 1200) + "!@#"
    t2 = time.perf_counter()
    with pytest.raises(ValueError):
        extract_json_payload(heavy)
    assert (time.perf_counter() - t2) < 1.0

    salvaged = extract_json_payload('{"a": 1, "b":' + (" " * 500) + "!")
    assert salvaged == '{"a": 1}'


def test_config_io_readonly_app_data_dir_returns_transient_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.io as config_io
    import config_store

    ro_dir = tmp_path / "ro_app_data"
    cfg_file = ro_dir / "config.json"
    monkeypatch.setattr(config_store, "APP_DATA_DIR", ro_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", tmp_path / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", False)
    monkeypatch.setattr(config_store, "_CONFIG_TRANSIENT_READ_FAILED", False)

    # Case 1: _ensure_runtime_dir raises PermissionError
    monkeypatch.setattr(
        config_io,
        "_ensure_runtime_dir",
        lambda: (_ for _ in ()).throw(PermissionError("read-only dir")),
    )
    cfg = config_io.load_config()
    assert config_store.is_config_unreadable(cfg) is True
    assert config_store._CONFIG_TRANSIENT_READ_FAILED is True
    assert isinstance(cfg, dict)

    # Case 2: _ensure_runtime_dir succeeds, but config_update_lock fails on first-run init
    ro_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_io, "_ensure_runtime_dir", lambda: ro_dir)
    monkeypatch.setattr(config_store, "_CONFIG_TRANSIENT_READ_FAILED", False)

    def _deny_lock():
        raise PermissionError("cannot open update lock")

    monkeypatch.setattr(config_store, "config_update_lock", _deny_lock)
    cfg2 = config_io.load_config()
    assert config_store.is_config_unreadable(cfg2) is True
    assert config_store._CONFIG_TRANSIENT_READ_FAILED is True


def test_trend_sources_streaming_enforces_size_limit_without_full_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trend_sources

    monkeypatch.setattr(trend_sources, "_MAX_TREND_RESPONSE_BYTES", 64)

    class _StreamingFakeResponse(requests.Response):
        def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
            super().__init__()
            self.status_code = 200
            self.headers.update(headers or {})
            self.encoding = "utf-8"
            self._chunks = chunks
            self.closed = False
            self.consumed_chunks = 0

        @property
        def content(self) -> bytes:  # type: ignore[override]
            raise AssertionError("response.content must not be accessed during streaming read")

        def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
            for c in self._chunks:
                self.consumed_chunks += 1
                yield c

        def close(self) -> None:
            self.closed = True

    # 1) Chunked overflow without Content-Length stops before reading remaining chunks
    oversized_resp = _StreamingFakeResponse([b"a" * 40, b"b" * 40, b"c" * 40])
    seen_stream_flags: list[bool | None] = []

    def _fake_get(*_args, **kwargs):
        seen_stream_flags.append(kwargs.get("stream"))
        return oversized_resp

    monkeypatch.setattr(trend_sources, "_get_trend_session", lambda: SimpleNamespace(get=_fake_get))
    with pytest.raises(ValueError, match="exceeds size limit"):
        trend_sources._request_json("https://example.com/big.json")
    assert seen_stream_flags == [True]
    assert oversized_resp.consumed_chunks == 2
    assert oversized_resp.closed is True

    # 2) Valid small chunked JSON response succeeds
    ok_json_resp = _StreamingFakeResponse([b'{"ok":', b" true}"])
    monkeypatch.setattr(
        trend_sources,
        "_get_trend_session",
        lambda: SimpleNamespace(get=lambda *_a, **_kw: ok_json_resp),
    )
    assert trend_sources._request_json("https://example.com/ok.json") == {"ok": True}
    assert ok_json_resp.closed is True

    # 3) Content-Length overflow rejects without consuming any chunks
    cl_resp = _StreamingFakeResponse([b"x" * 100], headers={"Content-Length": "200"})
    monkeypatch.setattr(
        trend_sources,
        "_get_trend_session",
        lambda: SimpleNamespace(get=lambda *_a, **_kw: cl_resp),
    )
    monkeypatch.setattr(
        trend_sources,
        "feedparser",
        SimpleNamespace(parse=lambda data: {"data": data}),
    )
    with pytest.raises(ValueError, match="exceeds size limit"):
        trend_sources._fetch_rss_feed("https://example.com/big.rss")
    assert cl_resp.consumed_chunks == 0
    assert cl_resp.closed is True


def test_messaging_closed_queues_uses_weakset_without_leak_or_aba() -> None:
    from messaging import MessageAnnouncer

    ann = MessageAnnouncer()
    assert isinstance(ann._closed_queues, weakref.WeakSet)

    q1 = ann.listen(maxsize=1, enforce_limit=False)
    q1.put_nowait("fill")
    ann.announce("overflow")
    assert ann.listener_count() == 0
    assert len(ann._closed_queues) == 1

    # Drop reference without calling unlisten(); WeakSet releases it once GC runs.
    del q1
    gc.collect()
    assert len(ann._closed_queues) == 0

    # A subsequent queue (even if CPython reuses the memory address) receives announcements.
    q2 = ann.listen(maxsize=4, enforce_limit=False)
    ann.announce("hello")
    assert q2.get_nowait() == "hello"
    ann.close()


def test_credential_manager_clear_readonly_keyring_failure_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config_store
    import credential_manager

    monkeypatch.setattr(credential_manager, "_keyring_available", lambda: True)
    monkeypatch.setattr(config_store, "load_config", lambda: {"api_credentials": {}})
    monkeypatch.setattr(config_store, "config_update_lock", contextlib.nullcontext)

    mutations: list[tuple[str, str]] = []
    fake_kr = SimpleNamespace(
        get_password=lambda _svc, key: (
            (_ for _ in ()).throw(RuntimeError("keyring locked"))
            if key == "openai_api_key"
            else "existing-secret"
        ),
        set_password=lambda _svc, key, _val: mutations.append(("set", key)),
        delete_password=lambda _svc, key: mutations.append(("delete", key)),
    )
    monkeypatch.setattr(credential_manager, "_keyring", lambda: fake_kr)

    failed = credential_manager.clear_api_credentials()
    assert failed == ["openai_api_key"]
    assert mutations == []


def test_session_manager_get_session_closes_pool_cap_evictions_outside_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import session_manager as sm

    sm.YFinanceSessionManager._reset_for_testing()
    mgr = sm.YFinanceSessionManager()
    try:
        monkeypatch.setattr(sm, "YFINANCE_SESSION_POOL_MAX", 1)
        lock_state_on_close: list[bool] = []
        created: list[Any] = []

        class _FakeSession:
            def __init__(self, idx: int) -> None:
                self.idx = idx
                self.headers: dict[str, str] = {}
                self.proxies: dict[str, str] = {}
                self.closed = False

            def mount(self, *_a, **_kw) -> None:
                return None

            def request(self, *_a, **_kw):
                return SimpleNamespace(status_code=200)

            def close(self) -> None:
                lock_state_on_close.append(bool(mgr._lock._is_owned()))  # type: ignore[attr-defined]
                self.closed = True

        def _fake_make_session(*_a, **_kw):
            s = _FakeSession(len(created))
            created.append(s)
            return s

        monkeypatch.setattr(sm, "CURL_CFFI_AVAILABLE", False)
        monkeypatch.setattr(requests, "Session", _fake_make_session)

        s0 = mgr.get_session()
        with mgr._lock:
            mgr._ua_index = (mgr._ua_index + 1) % len(sm.YFINANCE_USER_AGENTS)
            mgr._session_epoch += 1
        # Simulate another thread creating a second session when pool max is 1
        mgr._local.sessions.clear()
        s1 = mgr.get_session()
        assert s1 is not s0
        assert s0.closed is True
        assert lock_state_on_close == [False]
    finally:
        sm.YFinanceSessionManager._reset_for_testing()


def test_config_locks_master_key_lock_does_not_mutate_global_windll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific WinDLL mutex check")

    import config_store
    from config import locks

    monkeypatch.setattr(config_store, "CONFIG_FILE", tmp_path / "config.json")
    sentinel_argtypes = (ctypes.c_void_p,)
    sentinel_handle = 12345

    fake_private_kernel32 = MagicMock()
    fake_private_kernel32.CreateMutexW.return_value = sentinel_handle
    fake_private_kernel32.WaitForSingleObject.return_value = 0

    fake_global_kernel32 = SimpleNamespace(
        CreateMutexW=SimpleNamespace(argtypes=sentinel_argtypes, restype=None)
    )
    fake_windll_namespace = SimpleNamespace(kernel32=fake_global_kernel32)

    monkeypatch.setattr(ctypes, "windll", fake_windll_namespace, raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_kw: fake_private_kernel32, raising=False)
    monkeypatch.setattr(locks, "config_update_lock", contextlib.nullcontext)

    with locks._master_key_update_lock():
        pass

    assert fake_global_kernel32.CreateMutexW.argtypes == sentinel_argtypes
    fake_private_kernel32.ReleaseMutex.assert_called_once_with(sentinel_handle)
    fake_private_kernel32.CloseHandle.assert_called_once_with(sentinel_handle)


def test_text_utils_sanitize_log_record_data_depth_bound() -> None:
    from utils.text_utils import sanitize_log_record_data

    nested: dict[str, object] = {"token": "secret-val", "msg": "ok"}
    cur = nested
    for _ in range(50):
        nxt: dict[str, object] = {}
        cur["child"] = nxt
        cur = nxt
    cur["leaf"] = "deep"

    sanitized = sanitize_log_record_data(nested)
    assert sanitized["token"] == "[REDACTED]"

    walk = sanitized
    depth = 0
    while isinstance(walk, dict) and "child" in walk:
        walk = walk["child"]
        depth += 1
    assert depth == 32
    assert walk == "[max-depth]"


def test_disk_cache_recovers_if_cache_dir_deleted_at_runtime(tmp_path: Path) -> None:
    from utils.disk_cache import StockDiskCache

    cache_dir = tmp_path / "stock_cache"
    cache = StockDiskCache(cache_dir=cache_dir, default_ttl=60.0)
    cache.set("AAPL", {"price": 100.0})
    assert cache.get("AAPL") == {"price": 100.0}

    # Remove the directory externally while the cache instance remains initialized.
    shutil.rmtree(cache_dir)
    assert not cache_dir.exists()

    cache.set("MSFT", {"price": 200.0})
    assert cache.get("MSFT") == {"price": 200.0}
