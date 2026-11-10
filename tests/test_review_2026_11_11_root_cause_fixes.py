"""Regression tests for the 2026-11-11 root-cause review fixes.

Each test pins one invariant that the corresponding fix restores:

* legacy user-stock migration fsyncs the target before deleting the source;
* legacy config migration fsyncs the target directory before deleting the source;
* a load cannot publish the pre-save disk snapshot over an in-flight save;
* the runtime data directory is tightened to 0o700 on POSIX;
* a BaseException in a chat-history transaction leaves no open transaction.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

import config_store
from app_state import app_state
from config import atomic_write
from shutdown_manager import ShutdownTokenManager
from utils import storage


def test_legacy_user_stocks_migration_fsyncs_before_removing_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy-user-stocks.json"
    target = tmp_path / "runtime" / "user_stocks.json"
    legacy.write_text(
        json.dumps({"us": {"AAPL": {"shares": 3, "avg_price": 175.25}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", str(legacy))
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    monkeypatch.setattr(storage.config_store, "APP_DATA_DIR", target.parent)

    events: list[str] = []
    real_fsync = storage.os.fsync
    real_unlink = Path.unlink

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def recording_unlink(self: Path, *args, **kwargs):
        if self == legacy:
            events.append("unlink_legacy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(storage.os, "fsync", recording_fsync)
    monkeypatch.setattr(Path, "unlink", recording_unlink)

    with (
        patch.object(storage.config_store, "get_or_create_master_key", return_value="test-key"),
        patch.object(
            storage,
            "protect_data",
            return_value={"scheme": "fernet", "value": "encrypted-payload"},
        ),
    ):
        assert storage._migrate_legacy_user_stocks() is True

    assert not legacy.exists()
    assert "unlink_legacy" in events
    unlink_index = events.index("unlink_legacy")
    assert "fsync" in events[:unlink_index], (
        "the encrypted target must be fsynced before the only plaintext copy is removed"
    )


def test_legacy_config_migration_fsyncs_target_directory_before_removing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source" / "config.json"
    target = tmp_path / "runtime" / "config.json"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_text(json.dumps({"mistral_model": "legacy"}), encoding="utf-8")

    events: list[tuple[str, Path]] = []
    real_unlink = Path.unlink

    def recording_fsync(directory: Path) -> bool:
        events.append(("fsync", directory))
        return True

    def recording_unlink(self: Path, *args, **kwargs):
        if self == source:
            events.append(("unlink", self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(atomic_write, "_fsync_directory", recording_fsync)
    monkeypatch.setattr(Path, "unlink", recording_unlink)

    assert atomic_write._migrate_legacy_runtime_file(source, target) is True
    assert ("fsync", target.parent) in events
    assert ("unlink", source) in events
    assert events.index(("fsync", target.parent)) < events.index(("unlink", source))


def test_legacy_config_migration_keeps_source_when_target_directory_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source" / "config.json"
    target = tmp_path / "runtime" / "config.json"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_text(json.dumps({"mns_master_key": "secret"}), encoding="utf-8")

    monkeypatch.setattr(
        atomic_write,
        "_fsync_directory",
        lambda directory: directory != target.parent,
    )

    assert atomic_write._migrate_legacy_runtime_file(source, target) is True
    assert target.exists()
    assert source.exists(), "the legacy source is the only recoverable copy if the target is not durable"


def test_load_user_stocks_does_not_publish_over_save_started_after_early_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "user_stocks.json"
    target.write_text(json.dumps({"us": {}, "jp": {}, "idx": {}}), encoding="utf-8")
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(storage.config_store, "APP_DATA_DIR", tmp_path)

    original_us = app_state.market.user_us
    original_pending = app_state.market.user_stocks_save_pending
    original_rev = app_state.market.user_stocks_rev
    original_loaded_rev = app_state.market.last_loaded_rev
    original_load_error = app_state.market.user_stocks_load_error

    app_state.market.user_us = {"AAPL": {"shares": 3}}
    app_state.market.user_stocks_save_pending = 0
    app_state.market.user_stocks_rev = 0
    app_state.market.last_loaded_rev = -1
    app_state.market.user_stocks_load_error = False

    def simulate_save_start() -> None:
        # The load's early pending check has already passed. This models a
        # save_user_stocks() call that increments the counter while the load
        # is doing its file-system work, before it takes user_stocks_lock.
        with app_state.market.user_stocks_lock:
            app_state.market.user_stocks_save_pending += 1

    monkeypatch.setattr(storage, "_remove_stale_user_stocks_tmp_files", simulate_save_start)

    try:
        storage.load_user_stocks(force=True)
        assert app_state.market.user_us == {"AAPL": {"shares": 3}}, (
            "the pre-save disk snapshot must not replace the in-memory mutation"
        )
    finally:
        app_state.market.user_us = original_us
        app_state.market.user_stocks_save_pending = original_pending
        app_state.market.user_stocks_rev = original_rev
        app_state.market.last_loaded_rev = original_loaded_rev
        app_state.market.user_stocks_load_error = original_load_error


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_posix_ensure_runtime_dir_tightens_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    monkeypatch.setattr(config_store, "APP_DATA_DIR", runtime)

    config_store._ensure_runtime_dir()

    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_posix_shutdown_token_manager_tightens_existing_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    manager = ShutdownTokenManager()
    manager.runtime_state_dir = runtime

    manager._ensure_runtime_state_dir()

    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700


def test_chat_history_base_exception_rolls_back_open_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from utils import chat_history

    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat_history.db")
    monkeypatch.setattr(chat_history, "_db_initialized", False)
    store = chat_history.SQLiteChatHistoryStore()

    try:

        def interrupted_callback(conn, cursor):
            cursor.execute("SELECT 1")
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            store._execute_in_transaction(interrupted_callback)

        conn = store._get_connection()
        assert conn.in_transaction is False

        result = store._execute_in_transaction(
            lambda conn, cursor: cursor.execute("SELECT 1").fetchone()[0]
        )
        assert result == 1
    finally:
        store.close_all()
        chat_history.SQLiteChatHistoryStore._reset_for_testing()
