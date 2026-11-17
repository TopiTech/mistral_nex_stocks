"""Regression tests for the residual root-cause fixes (2026-11-18).

Three defects were still reproducible after the earlier review rounds:

1. ``StockDiskCache._remove_stale_entries`` read the whole entry under the
   cache lock and sliced the first 64 KiB. Because ``stored_at`` was the last
   envelope field, every entry larger than that slice failed to parse and
   freshness silently fell back to the file mtime - disagreeing with
   ``get()`` / ``has()``, which use ``stored_at``.
2. ``bg.leader_election._verify_leader_ownership`` only checked that writes
   through the held descriptor succeeded. It never compared the descriptor's
   file identity with the current path, so an unlink+recreated lock file left
   this process "leader" on an orphaned inode. Conversely the atomic-marker
   mode (which intentionally has no descriptor) was demoted on every tick and
   immediately re-won, oscillating leader/follower.
3. ``/api/chat`` and ``/api/analyze-v2`` joined an in-flight job by
   ``request_token`` alone. Reusing the token with a different payload while
   the first job ran returned the first request's answer as if it belonged to
   the new payload. The completed-result cache already rejected that mixture.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import bg.leader_election as le
import routes.api_analysis as ra
from app import app
from app_state import app_state
from utils.disk_cache import StockDiskCache

_ORIGIN = "http://127.0.0.1:5000"


@pytest.fixture(autouse=True)
def _disable_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", False)


# ---------------------------------------------------------------------------
# 1. Disk cache cleanup must use the authoritative stored_at timestamp
# ---------------------------------------------------------------------------


def _write_envelope(path: Path, stored_at: float, value: Any, *, legacy_order: bool = False) -> None:
    payload: dict[str, Any] = (
        {"key": path.stem, "value": value, "stored_at": stored_at}
        if legacy_order
        else {"stored_at": stored_at, "key": path.stem, "value": value}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def test_disk_cache_writes_stored_at_first(tmp_path: Path) -> None:
    cache = StockDiskCache(tmp_path, max_entries=10, default_ttl=3600, enable_cleanup=False)
    cache.set("envelope-order", "value")
    raw = cache._entry_path("envelope-order").read_text(encoding="utf-8")
    assert raw.startswith('{"stored_at":')


def test_cleanup_keeps_large_fresh_entry_with_old_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A >64 KiB entry stays fresh via stored_at even when its mtime is ancient.

    The old implementation parsed a truncated slice, failed, and used the mtime,
    so this entry was deleted. It also called ``read_text`` on the whole file;
    the bounded prefix reader must not need that at all.
    """
    cache = StockDiskCache(tmp_path, max_entries=10, default_ttl=3600, enable_cleanup=False)
    value = "x" * (2 * 1024 * 1024)
    cache.set("large-fresh", value)
    path = cache._entry_path("large-fresh")
    old = time.time() - 30 * 24 * 3600
    os.utime(path, (old, old))

    original_read_text = Path.read_text

    def _fail_on_full_read(self: Path, *args: Any, **kwargs: Any) -> str:
        raise AssertionError(f"cleanup parsed the whole payload for {self}")

    monkeypatch.setattr(Path, "read_text", _fail_on_full_read)
    try:
        assert cache.cleanup() == 0
    finally:
        monkeypatch.setattr(Path, "read_text", original_read_text)

    assert path.exists()
    assert cache.get("large-fresh") == value


def test_cleanup_removes_entry_with_old_stored_at_despite_fresh_mtime(tmp_path: Path) -> None:
    """stored_at is authoritative, matching get()/has() (mtime was fresh here)."""
    cache = StockDiskCache(tmp_path, max_entries=10, default_ttl=3600, enable_cleanup=False)
    path = cache._entry_path("stale-by-stored-at")
    _write_envelope(path, time.time() - 10 * 24 * 3600, "stale")

    assert cache.cleanup() == 1
    assert not path.exists()


def test_cleanup_still_parses_small_legacy_envelopes(tmp_path: Path) -> None:
    """Legacy field order keeps working for entries small enough to parse."""
    cache = StockDiskCache(tmp_path, max_entries=10, default_ttl=3600, enable_cleanup=False)
    path = cache._entry_path("legacy-small")
    _write_envelope(path, time.time() - 10 * 24 * 3600, "stale", legacy_order=True)

    assert cache.cleanup() == 1
    assert not path.exists()


def test_remove_fields_rewrites_envelope_in_prefix_readable_order(tmp_path: Path) -> None:
    """Migrating a legacy entry must not leave stored_at behind the payload."""
    cache = StockDiskCache(tmp_path, max_entries=10, default_ttl=3600, enable_cleanup=False)
    path = cache._entry_path("legacy-migrate")
    _write_envelope(
        path,
        time.time() - 60,
        {"keep": 1, "drop": {"secret": True}},
        legacy_order=True,
    )

    assert cache.remove_fields_recursive(["secret"]) == 1
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith('{"stored_at":')
    assert cache.get("legacy-migrate") == {"keep": 1, "drop": {}}


# ---------------------------------------------------------------------------
# 2. Leader-lock ownership verification per acquisition mode
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_leader_state():
    le.release_leader_lock()
    yield
    le.release_leader_lock()


def test_atomic_marker_ownership_does_not_require_a_handle(
    tmp_path: Path, _clean_leader_state: None
) -> None:
    """The atomic mode owns a pid marker file, not a descriptor.

    Requiring a handle demoted a healthy leader every tick and the loop then
    re-won the same marker, so the process ran as follower half the time.
    """
    lock = tmp_path / ".mns_sync_leader.lock"
    assert le._try_acquire_atomic_lock(lock, os.getpid()) is True
    assert le._get_leader_lock_mode() == "atomic"
    # The election loop observes this state: the marker exists, no descriptor.
    handle = le._LEADER_LOCK_FILE
    le._set_leader_lock_file(None)
    if handle is not None:
        handle.close()
    le._set_is_sync_leader(True)

    le._verify_leader_ownership()

    assert le._is_sync_leader is True
    assert le._get_leader_lock_mode() == "atomic"


def test_atomic_marker_ownership_demotes_when_marker_replaced(
    tmp_path: Path, _clean_leader_state: None
) -> None:
    lock = tmp_path / ".mns_sync_leader.lock"
    assert le._try_acquire_atomic_lock(lock, os.getpid()) is True
    handle = le._LEADER_LOCK_FILE
    le._set_leader_lock_file(None)
    if handle is not None:
        handle.close()
    lock.write_text("999999", encoding="utf-8")
    le._set_is_sync_leader(True)

    le._verify_leader_ownership()

    assert le._is_sync_leader is False
    assert le._get_leader_lock_mode() is None


def test_advisory_ownership_keeps_leader_when_identity_matches(
    tmp_path: Path, _clean_leader_state: None
) -> None:
    lock = tmp_path / ".mns_sync_leader.lock"
    assert le._try_acquire_advisory_file_lock(lock, os.getpid(), lambda _handle: None)
    le._set_is_sync_leader(True)

    le._verify_leader_ownership()

    assert le._is_sync_leader is True


def test_advisory_ownership_demotes_when_lock_path_was_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_leader_state: None
) -> None:
    """A held fd on an unlinked/recreated file must not keep claiming leader."""
    lock = tmp_path / ".mns_sync_leader.lock"
    assert le._try_acquire_advisory_file_lock(lock, os.getpid(), lambda _handle: None)
    le._set_is_sync_leader(True)

    real_stat = os.stat

    def _replaced_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        try:
            same_path = Path(path) == lock
        except TypeError:
            same_path = False
        if same_path:
            fields = list(result)
            fields[1] = result.st_ino + 1  # st_ino: different inode at the path
            return os.stat_result(tuple(fields))
        return result

    monkeypatch.setattr(le.os, "stat", _replaced_stat)

    le._verify_leader_ownership()

    assert le._is_sync_leader is False


# ---------------------------------------------------------------------------
# 3. In-flight requests are bound to the payload that claimed the token
# ---------------------------------------------------------------------------


def _chat_payload(token: str, scope: str, message: str) -> dict[str, Any]:
    return {
        "market": "us",
        "symbol": "AAPL",
        "message": message,
        "request_token": token,
        "conversation_scope": scope,
        "stream": False,
    }


def test_chat_inflight_token_conflict_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "r19_conflict_chat_123456"
    scope = "scope-r19-chat-conflict"
    inflight_key = f"chat:{scope}:{token}"
    holder = {
        "result": None,
        "error": None,
        "done": threading.Event(),
        "step": "calling_llm",
        "start_time": time.time(),
        "message": "busy",
        "fingerprint": "fingerprint-of-the-other-request",
    }
    with ra.chat_fetch_lock:
        ra.chat_result_cache.pop(inflight_key, None)
        ra.chat_result_fingerprints.pop(inflight_key, None)
        ra.chat_fetch_inflight[inflight_key] = holder

    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test-1234567890")
    monkeypatch.setattr(ra, "CHAT_PREPARE_WAIT_SEC", 0.01)

    def _must_not_start(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("a conflicting token must not start a paid job")

    monkeypatch.setattr(ra, "_call_mistral_chat_with_retry", _must_not_start)
    try:
        with app.test_client() as client:
            resp = client.post(
                "/api/chat",
                json=_chat_payload(token, scope, "別の質問です"),
                headers={"Content-Type": "application/json", "Origin": _ORIGIN},
            )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body is not None
        assert body["ok"] is False
        assert "request_token" in json.dumps(body, ensure_ascii=False)
        with ra.chat_fetch_lock:
            assert ra.chat_fetch_inflight[inflight_key] is holder
        assert f"{scope}:us:AAPL" not in app_state.ai.chat_history
    finally:
        with ra.chat_fetch_lock:
            ra.chat_fetch_inflight.pop(inflight_key, None)
            ra.chat_result_cache.pop(inflight_key, None)
            ra.chat_result_fingerprints.pop(inflight_key, None)


def test_chat_inflight_same_payload_still_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A true retry of the same request must keep polling the running job."""
    token = "r19_join_chat_123456"
    scope = "scope-r19-chat-join"
    message = "同じ質問です"
    inflight_key = f"chat:{scope}:{token}"
    fingerprint = ra._request_fingerprint(
        {"symbol": "AAPL", "market": "us", "message": message}
    )
    holder = {
        "result": None,
        "error": None,
        "done": threading.Event(),
        "step": "calling_llm",
        "start_time": time.time(),
        "message": "busy",
        "fingerprint": fingerprint,
    }
    with ra.chat_fetch_lock:
        ra.chat_result_cache.pop(inflight_key, None)
        ra.chat_result_fingerprints.pop(inflight_key, None)
        ra.chat_fetch_inflight[inflight_key] = holder

    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test-1234567890")
    monkeypatch.setattr(ra, "CHAT_PREPARE_WAIT_SEC", 0.01)

    try:
        with app.test_client() as client:
            resp = client.post(
                "/api/chat",
                json=_chat_payload(token, scope, message),
                headers={"Content-Type": "application/json", "Origin": _ORIGIN},
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body is not None
        assert body["fetching"] is True
        with ra.chat_fetch_lock:
            assert ra.chat_fetch_inflight[inflight_key] is holder
    finally:
        holder["done"].set()
        with ra.chat_fetch_lock:
            ra.chat_fetch_inflight.pop(inflight_key, None)
            ra.chat_result_cache.pop(inflight_key, None)
            ra.chat_result_fingerprints.pop(inflight_key, None)


def test_analyze_inflight_token_conflict_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "r19_conflict_analyze_123456"
    scope = "scope-r19-analyze-conflict"
    inflight_key = f"analyze:{scope}:{token}"
    holder = {
        "result": None,
        "error": None,
        "done": threading.Event(),
        "step": "analyzing",
        "start_time": time.time(),
        "message": "busy",
        "fingerprint": "fingerprint-of-the-other-request",
    }
    with ra.analyze_fetch_lock:
        ra.analyze_result_cache.pop(inflight_key, None)
        ra.analyze_result_fingerprints.pop(inflight_key, None)
        ra.analyze_fetch_inflight[inflight_key] = holder

    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test-1234567890")

    try:
        with app.test_client() as client:
            resp = client.post(
                "/api/analyze-v2",
                json={
                    "market": "us",
                    "symbol": "AAPL",
                    "request_token": token,
                    "conversation_scope": scope,
                    "chart_data": [],
                },
                headers={"Content-Type": "application/json", "Origin": _ORIGIN},
            )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body is not None
        assert body["ok"] is False
        assert "request_token" in json.dumps(body, ensure_ascii=False)
        with ra.analyze_fetch_lock:
            assert ra.analyze_fetch_inflight[inflight_key] is holder
    finally:
        with ra.analyze_fetch_lock:
            ra.analyze_fetch_inflight.pop(inflight_key, None)
            ra.analyze_result_cache.pop(inflight_key, None)
            ra.analyze_result_fingerprints.pop(inflight_key, None)


def test_inflight_fingerprint_conflict_helper_contract() -> None:
    assert ra._inflight_fingerprint_conflict({"fingerprint": "a"}, "a") is False
    assert ra._inflight_fingerprint_conflict({"fingerprint": "a"}, "b") is True
    assert ra._inflight_fingerprint_conflict({}, "b") is False
    assert ra._inflight_fingerprint_conflict({"fingerprint": None}, "b") is False
    assert ra._inflight_fingerprint_conflict(None, "b") is False
