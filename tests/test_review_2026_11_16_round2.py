"""Regression tests for the 2026-11-16 review (second pass).

Covers four root-cause fixes:
  * a partially-created logging handler pair must not leak the first handler;
  * ``save_config`` must not erase API credentials when a caller omits the key;
  * request latency must use a monotonic clock;
  * stale-sync detection must not trust wall-clock jumps.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g, make_response

import config_store
import logging_config
from app import app
from app_factory import hooks
from app_state import app_state
from bg.sync import state as sync_state


class _ClosableHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.was_closed = False

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - never used
        pass

    def close(self) -> None:
        self.was_closed = True
        super().close()


def test_partial_file_handler_creation_closes_the_first_handler(monkeypatch):
    """A failure creating the second handler must close the first one."""
    first = _ClosableHandler()
    calls = {"count": 0}

    def _factory(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return first
        raise OSError("error log file is unavailable")

    monkeypatch.setattr(
        logging_config, "_RestrictiveRotatingFileHandler", _factory
    )
    with pytest.raises(OSError, match="error log file is unavailable"):
        logging_config._create_restricted_file_handlers(
            Path("backend.log"), Path("error.log")
        )

    assert calls["count"] == 2
    assert first.was_closed is True


def test_save_config_preserves_api_credentials_when_key_is_omitted(tmp_path):
    """Saving only preferences must not replace stored credentials with {}."""
    config_file = tmp_path / "config.json"
    credentials = {
        "mistral_api_key": {"scheme": "test", "value": "keep-me"},
        "langsearch_api_key": {"scheme": "test", "value": "keep-me-too"},
    }
    with patch.object(config_store, "CONFIG_FILE", config_file):
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        config_store._CONFIG_CORRUPTED = False
        try:
            config_store.save_config(
                {"mistral_model": "model-a", "api_credentials": credentials},
                create_backup=False,
            )
            config_store.save_config(
                {"mistral_model": "model-b"}, create_backup=False
            )
            saved = json.loads(config_file.read_text(encoding="utf-8"))
            assert saved["api_credentials"] == credentials
            assert saved["mistral_model"] == "model-b"

            # An explicit empty dict is still a clear request.
            config_store.save_config(
                {"mistral_model": "model-c", "api_credentials": {}},
                create_backup=False,
            )
            cleared = json.loads(config_file.read_text(encoding="utf-8"))
            assert cleared["api_credentials"] == {}
        finally:
            config_store._CONFIG_CACHE["data"] = None
            config_store._CONFIG_CACHE["key"] = None
            config_store._CONFIG_CORRUPTED = False


def test_request_elapsed_uses_monotonic_clock(caplog):
    """A wall-clock jump must not change the measured request duration."""
    fake_time = SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 0.0)
    with app.test_request_context("/"):
        with patch.object(hooks, "time", fake_time):
            hooks._log_request_start()
            assert g.request_start_ts == 1000.0

            fake_time.monotonic = lambda: 1000.25
            with caplog.at_level(logging.WARNING, logger=hooks.logger.name):
                hooks.add_extension_cors_headers(make_response("not found", 404))

    assert any("elapsed_ms=250" in record.getMessage() for record in caplog.records)


def _set_sync_state(start_time: float) -> None:
    with app_state.market.is_syncing_lock:
        app_state.market.is_syncing = True
    sync_state._set_sync_start_time(start_time)


def test_sync_staleness_ignores_forward_wall_clock_jump(monkeypatch):
    """A forward clock step must not mark a healthy sync as stale."""
    original_start = sync_state._get_sync_start_time()
    original_syncing = app_state.market.is_syncing
    try:
        _set_sync_state(time.monotonic() - 1.0)
        fake_time = SimpleNamespace(
            time=lambda: time.time() + 86400.0,
            monotonic=time.monotonic,
        )
        monkeypatch.setattr(sync_state, "time", fake_time)

        assert sync_state._recover_stale_sync_state_if_needed() is False
    finally:
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = original_syncing
        sync_state._set_sync_start_time(original_start)


def test_sync_staleness_survives_backward_wall_clock_jump(monkeypatch):
    """A backward clock step must not hide a genuinely wedged sync."""
    original_start = sync_state._get_sync_start_time()
    original_syncing = app_state.market.is_syncing
    try:
        _set_sync_state(
            time.monotonic() - (sync_state.SYNC_STALE_TIMEOUT_SEC + 10.0)
        )
        fake_time = SimpleNamespace(
            time=lambda: time.time() - 86400.0,
            monotonic=time.monotonic,
        )
        monkeypatch.setattr(sync_state, "time", fake_time)

        assert sync_state._recover_stale_sync_state_if_needed() is True
        with app_state.market.is_syncing_lock:
            assert app_state.market.is_syncing is False
    finally:
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = original_syncing
        sync_state._set_sync_start_time(original_start)
