"""Regression tests for Unit A code review fixes ([A-1] through [A-7])."""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app_state import app_state

# ---------------------------------------------------------------------------
# [A-1] bg/sync/process.py: announce_real_market_state runs outside sse_data_lock
# ---------------------------------------------------------------------------


def test_process_fetched_stocks_announces_outside_lock_after_current_cache_update(monkeypatch):
    from bg.sync import process as sync_process

    app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}

    observations: list[tuple[bool, list[dict]]] = []

    def spy_announce() -> None:
        observations.append(
            (
                app_state.cache.sse_data_lock._is_owned(),
                copy.deepcopy(app_state.market.current_stocks_cache.get("us", [])),
            )
        )

    monkeypatch.setattr(sync_process, "announce_real_market_state", spy_announce)

    fetched = [
        {"symbol": "AAPL", "market": "us", "price": 195.0, "prev_close": 190.0},
    ]
    result = sync_process._process_fetched_stocks(fetched)

    assert len(result.us) == 1
    assert len(observations) == 1
    lock_owned, snapshot_us = observations[0]
    assert lock_owned is False
    assert [row.get("symbol") for row in snapshot_us] == ["AAPL"]


# ---------------------------------------------------------------------------
# [A-2] Warm-from-disk & leader empty-result early return invalidate SSE cache
# ---------------------------------------------------------------------------


def test_warm_payload_cache_from_disk_invalidates_sse_payload_cache(tmp_path, monkeypatch):
    from bg.sync import process as sync_process
    from bg.sync import state as sync_state

    payload_file = tmp_path / "payload_AAPL_us.json"
    payload_file.write_text("{}", encoding="utf-8")

    fake_cache = MagicMock()
    fake_cache._entry_path.return_value = payload_file
    fake_cache.get.side_effect = lambda key, ignore_ttl=True: (
        {"nikkei225": {"price": 39000.0}}
        if key == "indices_cache"
        else {"symbol": "AAPL", "market": "us", "price": 200.0}
    )

    monkeypatch.setattr(app_state, "payload_disk_cache", fake_cache)
    monkeypatch.setattr(sync_process, "load_user_stocks", lambda force=True: None)
    monkeypatch.setattr(sync_process, "_default_stock_names", lambda m: ["AAPL"] if m == "us" else [])
    sync_state._last_loaded_mtimes.clear()

    invalidate_calls: list[bool] = []
    monkeypatch.setattr(
        sync_process,
        "_invalidate_sse_payload_cache",
        lambda: invalidate_calls.append(True),
    )
    monkeypatch.setattr(
        sync_process,
        "_get_app_bg_attr",
        lambda name, default=None: (
            (lambda: invalidate_calls.append(True))
            if name == "_invalidate_sse_payload_cache"
            else default
        ),
    )

    sync_process._warm_payload_cache_from_disk()
    assert invalidate_calls == [True]
    assert app_state.market.current_indices_cache.get("nikkei225", {}).get("price") == 39000.0
    assert any(row.get("symbol") == "AAPL" for row in app_state.market.current_stocks_cache["us"])


def test_leader_empty_sync_early_return_invalidates_and_announces_when_warmed(monkeypatch):
    from bg.sync import control as sync_control
    from bg.sync.process import ProcessedStocks

    app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
    app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}

    warmed_flag: list[bool] = []
    invalidated: list[bool] = []
    announced: list[bool] = []

    def fake_warm() -> None:
        warmed_flag.append(True)
        app_state.market.target_stocks_cache["us"] = [{"symbol": "AAPL", "price": 180.0}]

    monkeypatch.setattr(sync_control, "_warm_payload_cache_from_disk", fake_warm)
    monkeypatch.setattr(
        sync_control, "_invalidate_sse_payload_cache", lambda: invalidated.append(True)
    )
    monkeypatch.setattr(
        sync_control, "announce_current_market_state", lambda: announced.append(True)
    )
    monkeypatch.setattr(
        sync_control,
        "_get_app_bg_attr",
        lambda name, default=None: default,
    )
    monkeypatch.setattr(
        sync_control, "_prepare_sync_items", lambda force_load=False, force_fetch=False: [("AAPL", "us")]
    )
    monkeypatch.setattr(sync_control, "fetch_stocks_batch", lambda items, **kwargs: [])
    monkeypatch.setattr(sync_control, "_auto_remove_invalid_symbols", lambda items, fetched: None)
    monkeypatch.setattr(
        sync_control,
        "_process_fetched_stocks",
        lambda fetched, gen=None: ProcessedStocks(us=[], jp=[], idx=[]),
    )
    monkeypatch.setattr(sync_control, "is_leader", lambda: True)
    monkeypatch.setattr(sync_control, "_get_sync_execution_lock", threading.Lock)

    sync_control.sync_all_stocks_now()
    assert warmed_flag == [True]
    assert invalidated == [True]
    assert announced == [True]


# ---------------------------------------------------------------------------
# [A-3] utils/stock_payload.py: realtime merge & register_symbol outside sse_data_lock
# ---------------------------------------------------------------------------


def test_resolve_stocks_for_response_releases_sse_data_lock_before_realtime_engine(monkeypatch):
    from services import realtime_engine as rt_mod
    from utils.stock_payload import _resolve_stocks_for_response

    app_state.market.current_stocks_cache = {
        "us": [{"symbol": "AAPL", "market": "us", "price": 100.0}],
        "jp": [{"symbol": "7203.T", "market": "jp", "price": 2500.0}],
        "idx": [],
    }
    app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}

    lock_states: list[tuple[str, bool]] = []
    registered: list[tuple[str, str]] = []

    class FakeRealtimeEngine:
        def get_market_snapshot(self):
            lock_states.append(("market", app_state.cache.sse_data_lock._is_owned()))
            return {"AAPL": {"price": 199.5, "change": 1.5, "change_percent": 0.75, "source": "tv"}}

        def get_pts_snapshot(self):
            lock_states.append(("pts", app_state.cache.sse_data_lock._is_owned()))
            return {}

        def register_symbol(self, sym: str, market: str):
            lock_states.append(("register", app_state.cache.sse_data_lock._is_owned()))
            registered.append((sym, market))

    monkeypatch.setattr(rt_mod, "realtime_market_engine", FakeRealtimeEngine())

    resolved = _resolve_stocks_for_response(include_portfolio=False)

    assert resolved["us"][0]["price"] == 199.5
    assert registered == [("7203.T", "jp")]
    assert lock_states == [("market", False), ("pts", False), ("register", False)]


# ---------------------------------------------------------------------------
# [A-4] config/io.py: retry transient OSError & fail closed on unreadable merge
# ---------------------------------------------------------------------------


def test_save_config_retries_transient_oserror_and_preserves_trust_anchors(tmp_path, monkeypatch):
    import config_store
    from config import io as config_io

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "mns_master_key": {"scheme": "keyring", "value": ""},
                "flask_secret_key": {"scheme": "keyring", "value": ""},
                "extension_api_token": "tok-123",
                "api_credentials": {"mistral_api_key": {"scheme": "enc", "value": "x"}},
                "mistral_model": "old-model",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_store, "APP_DATA_DIR", cfg_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", cfg_dir / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)

    real_open = Path.open
    attempts = {"n": 0}

    def flaky_open(self, *args, **kwargs):
        if self == cfg_file and args and args[0] == "r":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise PermissionError("sharing violation")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)

    config_io.save_config({"mistral_model": "new-model"}, create_backup=False)
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert attempts["n"] == 2
    assert saved["mistral_model"] == "new-model"
    assert saved["mns_master_key"] == {"scheme": "keyring", "value": ""}
    assert saved["extension_api_token"] == "tok-123"
    assert saved["api_credentials"] == {"mistral_api_key": {"scheme": "enc", "value": "x"}}


def test_save_config_fails_closed_on_persistent_read_error_or_corrupt_no_backup(tmp_path, monkeypatch):
    import config_store
    from config import io as config_io

    cfg_dir = tmp_path / "cfg2"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    original = json.dumps({"mns_master_key": "keep-me", "mistral_model": "old"})
    cfg_file.write_text(original, encoding="utf-8")

    monkeypatch.setattr(config_store, "APP_DATA_DIR", cfg_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", cfg_dir / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)

    real_open = Path.open

    def locked_open(self, *args, **kwargs):
        if self == cfg_file and args and args[0] == "r":
            raise PermissionError("file locked by antivirus")
        return real_open(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "open", locked_open)
        m.setattr(config_io.time, "sleep", lambda _: None)
        with pytest.raises(RuntimeError, match="protected keys"):
            config_io.save_config({"mistral_model": "new"}, create_backup=False)
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["mns_master_key"] == "keep-me"

    # Also fail closed & quarantine corrupted config even when create_backup=False.
    cfg_file.write_text("{corrupt json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupted configuration file"):
        config_io.save_config({"mistral_model": "new"}, create_backup=False)
    assert config_store._CONFIG_CORRUPTED is True
    assert list(cfg_dir.glob("config.json.corrupt.*.bak"))


# ---------------------------------------------------------------------------
# [A-5] market_state.py & /api/metrics: peek_circuit_open is strictly read-only
# ---------------------------------------------------------------------------


def test_metrics_uses_read_only_circuit_peek_without_half_open_mutation():
    app = create_app()
    client = app.test_client()

    with app_state.market.circuit_lock:
        state = app_state.market.circuit_states["mistral"]
        state["status"] = "OPEN"
        state["open_until"] = time.time() - 10.0
        state["failures"] = 3

    resp = client.get("/api/metrics", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["mistral"]["circuit_open"] is False

    # State must NOT have flipped to HALF_OPEN because telemetry is read-only.
    with app_state.market.circuit_lock:
        assert app_state.market.circuit_states["mistral"]["status"] == "OPEN"

    with app_state.market.circuit_lock:
        app_state.market.circuit_states["mistral"]["open_until"] = time.time() + 60.0
    resp_open = client.get("/api/metrics", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert resp_open.get_json()["mistral"]["circuit_open"] is True


# ---------------------------------------------------------------------------
# [A-6] utils/validators.py: strip list prefixes without eating leading years/negatives
# ---------------------------------------------------------------------------


def test_normalize_analysis_aliases_preserves_leading_years_and_negative_numbers():
    from utils.validators import _normalize_analysis_aliases

    sample = {
        "key_catalysts": "- 2026年Q4の黒字化\n• 1.5倍の売上拡大\n1. 2027年新製品投入",
        "risk_factors": "- -10%の利益率低下リスク\n* 3つの競合参入",
    }
    normalized = _normalize_analysis_aliases(sample)
    assert normalized["key_catalysts"] == [
        "2026年Q4の黒字化",
        "1.5倍の売上拡大",
        "2027年新製品投入",
    ]
    assert normalized["risk_factors"] == [
        "-10%の利益率低下リスク",
        "3つの競合参入",
    ]


# ---------------------------------------------------------------------------
# [A-7] native_host/start_backend.py: extensionId requires ^[a-z0-9]{32}$
# ---------------------------------------------------------------------------


def test_start_backend_enforces_lowercase_ascii_extension_id(tmp_path, monkeypatch):
    from native_host import start_backend

    captured_env: list[dict[str, str]] = []
    pid_file = tmp_path / "backend.pid"
    legacy_pid = tmp_path / "legacy.pid"

    monkeypatch.setattr(start_backend, "PID_FILE", pid_file)
    monkeypatch.setattr(start_backend, "_LEGACY_PID_FILE", legacy_pid)
    monkeypatch.setattr(start_backend, "get_backend_port", lambda: 5000)
    monkeypatch.setattr(start_backend, "is_port_in_use", lambda port: False)
    monkeypatch.setattr(start_backend, "wait_for_backend_ready", lambda timeout_sec=2.0: True)
    monkeypatch.delenv("MNS_EXTENSION_ORIGIN", raising=False)

    class DummyProc:
        pid = 99999
        returncode = 0

    def fake_popen(cmd, env=None, **kwargs):
        captured_env.append(dict(env or {}))
        return DummyProc()

    monkeypatch.setattr(start_backend.subprocess, "Popen", fake_popen)

    valid_id = "a" * 32
    with patch.object(start_backend.logger, "warning"):
        start_backend._start(valid_id)
    assert captured_env[-1].get("MNS_EXTENSION_ORIGIN") == f"chrome-extension://{valid_id}"

    for bad_id in ("A" * 32, "\u00e4" * 32, "abc", "a" * 33, "a" * 31 + "/"):
        captured_env.clear()
        with patch.object(start_backend.logger, "warning") as warn_mock:
            start_backend._start(bad_id)
        assert "MNS_EXTENSION_ORIGIN" not in captured_env[-1]
        assert warn_mock.called
