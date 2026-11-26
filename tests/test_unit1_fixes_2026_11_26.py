"""Regression tests for Unit 1 (core config, session, cookie cache, cache bucket) fixes."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest


def _setup_transient_config_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any]]:
    import config_store

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    real_payload = {
        "mistral_model": "mistral-medium-3.5",
        "api_credentials": {
            "mistral_api_key": {"method": "dpapi_fallback", "value": "encrypted-blob"}
        },
        "custom_ai_prompt": "keep me",
    }
    cfg_file.write_text(json.dumps(real_payload), encoding="utf-8")

    monkeypatch.setattr(config_store, "APP_DATA_DIR", cfg_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", cfg_dir / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)
    monkeypatch.setattr(config_store, "_CONFIG_TRANSIENT_READ_FAILED", False)
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", True)

    import builtins

    orig_open = builtins.open
    orig_path_open = Path.open

    def _failing_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if Path(file) == cfg_file and "r" in str(mode):
            raise PermissionError("simulated AV lock")
        return orig_open(file, mode, *args, **kwargs)

    def _failing_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == cfg_file and (not args or "r" in str(args[0])):
            raise PermissionError("simulated AV lock")
        return orig_path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _failing_open)
    monkeypatch.setattr(Path, "open", _failing_path_open)
    monkeypatch.setattr("config.io.time.sleep", lambda _s: None)
    return cfg_file, real_payload


def test_u1_1_credential_mutations_fail_closed_on_transient_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential deletion and secret/pref writes refuse transient fallback defaults."""
    import config_store
    import credential_manager

    cfg_file, real_payload = _setup_transient_config_failure(tmp_path, monkeypatch)

    monkeypatch.setattr(config_store, "get_or_create_master_key", lambda: "A" * 44)
    monkeypatch.setattr(credential_manager, "_keyring_available", lambda: False)

    with pytest.raises(RuntimeError, match="unreadable"):
        credential_manager.clear_api_credentials()

    with pytest.raises(RuntimeError, match="unreadable"):
        credential_manager.get_or_create_flask_secret_key()

    with pytest.raises(RuntimeError, match="unreadable"):
        credential_manager.get_or_create_extension_api_token()

    with pytest.raises(RuntimeError, match="unreadable"):
        credential_manager.set_model_name("mistral-small-latest")

    with pytest.raises(RuntimeError, match="unreadable"):
        credential_manager.set_custom_ai_prompt("")

    monkeypatch.undo()
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk == real_payload


def test_u1_1_delete_credentials_route_returns_503_when_config_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE /api/credentials maps unreadable-config RuntimeError to a clean 503 envelope."""
    import app as app_mod
    import routes.api_system

    monkeypatch.delenv("MNS_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(
        routes.api_system,
        "clear_api_credentials",
        lambda: (_ for _ in ()).throw(RuntimeError("Config is temporarily unreadable")),
    )
    with app_mod.app.test_client() as client:
        csrf_resp = client.get(
            "/api/csrf-token",
            headers={"Host": "localhost:5000"},
        )
        csrf_token = csrf_resp.get_json()["csrf_token"]
        resp = client.delete(
            "/api/credentials",
            headers={
                "Origin": "http://localhost:5000",
                "Host": "localhost:5000",
                "X-CSRFToken": csrf_token,
            },
        )
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ok"] is False
    assert "temporarily unreadable" not in json.dumps(body, ensure_ascii=False).lower()


def test_u1_2_get_session_aborts_during_shutdown_without_clearing_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_session() must raise RuntimeError when shutdown_event is set without reopening pool."""
    from app_state import app_state
    from session_manager import YFinanceSessionManager

    YFinanceSessionManager._reset_for_testing()
    mgr = YFinanceSessionManager()
    orig_shutdown_state = app_state.execution.shutdown_event.is_set()
    try:
        mgr.close_all()
        assert mgr._closed is True

        app_state.execution.shutdown_event.set()
        with pytest.raises(RuntimeError, match="shutting down"):
            mgr.get_session()

        assert mgr._closed is True
        assert mgr._all_sessions == []
    finally:
        if not orig_shutdown_state:
            app_state.execution.shutdown_event.clear()
        YFinanceSessionManager._reset_for_testing()


def test_u1_2_get_ticker_returns_none_during_shutdown() -> None:
    """YFinanceProvider.get_ticker() gracefully returns None when get_session() aborts on shutdown."""
    from app_state import app_state
    from services.stock.provider import YFinanceProvider

    provider = YFinanceProvider()
    orig_shutdown_state = app_state.execution.shutdown_event.is_set()
    try:
        app_state.execution.shutdown_event.set()
        assert provider.get_ticker("AAPL") is None
    finally:
        if not orig_shutdown_state:
            app_state.execution.shutdown_event.clear()


def test_u1_3_in_memory_cookie_cache_tracks_real_age(monkeypatch: pytest.MonkeyPatch) -> None:
    """_InMemoryCookieCache reports elapsed monotonic age and evicts on store(key, None)."""
    import app_state as app_state_mod
    from app_state import _InMemoryCookieCache

    now = [100.0]
    monkeypatch.setattr(app_state_mod.time, "monotonic", lambda: now[0])

    cache = _InMemoryCookieCache()
    assert cache.lookup("yahoo") is None

    cache.store("yahoo", "cookie-val")
    first = cache.lookup("yahoo")
    assert first == {"cookie": "cookie-val", "age": timedelta(seconds=0.0)}

    now[0] = 142.5
    aged = cache.lookup("yahoo")
    assert aged == {"cookie": "cookie-val", "age": timedelta(seconds=42.5)}

    cache.store("yahoo", None)
    assert cache.lookup("yahoo") is None


def test_u1_4_clear_cache_key_in_bucket_evicts_inflight_token() -> None:
    """clear_cache_key_in_bucket prevents an in-flight fetcher from repopulating stale data."""
    from utils.caching import (
        _ensure_cache_bucket,
        clear_cache_key_in_bucket,
        get_cached,
        global_cache,
    )

    duration = 12345
    key = "u1_4_inflight_bucket_test"
    entered = threading.Event()
    release = threading.Event()
    result_box: list[Any] = []

    def _fetcher() -> str:
        entered.set()
        assert release.wait(timeout=2.0)
        return "stale-value"

    clear_cache_key_in_bucket(key, duration)
    t = threading.Thread(
        target=lambda: result_box.append(get_cached(key, _fetcher, duration=duration))
    )
    t.start()
    try:
        assert entered.wait(timeout=2.0)
        clear_cache_key_in_bucket(key, duration)
        release.set()
        t.join(timeout=2.0)

        assert result_box == ["stale-value"]
        bucket = _ensure_cache_bucket(duration)
        assert key not in bucket
        assert (key, duration) not in global_cache.inflight_tokens
    finally:
        release.set()
        with global_cache.cache_lock:
            global_cache.caches.pop(duration, None)
            global_cache.inflight_tokens.pop((key, duration), None)
