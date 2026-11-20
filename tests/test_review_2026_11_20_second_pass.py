"""Regression tests for N21-01 / N21-02 (second verification pass, 2026-11-20).

N21-01: legacy user-stocks migration check is memoized once the terminal
state (no legacy file + complete target) is reached.
N21-02: crash-orphaned uuid tmp files are swept by the config load path
(config.json.*.tmp) and the shutdown token dir (.*.tmp).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from utils import storage


def _write_target(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"us": {"AAPL": {}}, "jp": {}, "idx": {}}), encoding="utf-8")


def test_n21_01_migration_memo_skips_reparse(tmp_path, monkeypatch):
    target = tmp_path / "user_stocks.json"
    legacy = tmp_path / "legacy" / "user_stocks.json"
    _write_target(target)
    assert not legacy.exists()
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", str(legacy))
    monkeypatch.setattr(storage, "_DEFAULT_USER_STOCKS_FILE", "__test__")
    monkeypatch.setattr(storage, "_DEFAULT_LEGACY_USER_STOCKS_FILE", "__test_legacy__")
    storage._reset_legacy_migration_memo()

    assert storage._migrate_legacy_user_stocks() is True
    assert storage._LEGACY_MIGRATION_DONE.get((str(legacy), str(target))) is True

    calls = {"n": 0}
    orig = storage._user_stocks_target_is_complete

    def _counting(path):
        calls["n"] += 1
        return orig(path)

    monkeypatch.setattr(storage, "_user_stocks_target_is_complete", _counting)
    assert storage._migrate_legacy_user_stocks() is True
    assert calls["n"] == 0


def test_n21_01_migration_memo_not_set_when_legacy_present(tmp_path, monkeypatch):
    target = tmp_path / "user_stocks.json"
    legacy = tmp_path / "user_stocks_legacy.json"
    _write_target(target)
    legacy.write_text(json.dumps({"us": {}, "jp": {}, "idx": {}}), encoding="utf-8")
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", str(legacy))
    monkeypatch.setattr(storage, "_DEFAULT_USER_STOCKS_FILE", "__test__")
    monkeypatch.setattr(storage, "_DEFAULT_LEGACY_USER_STOCKS_FILE", "__test_legacy__")
    storage._reset_legacy_migration_memo()

    # Target complete + legacy still present: returns True but must NOT memoize
    # (legacy may still need migration work on a later call).
    assert storage._migrate_legacy_user_stocks() is True
    assert (str(legacy), str(target)) not in storage._LEGACY_MIGRATION_DONE


def test_n21_02_config_load_sweeps_stale_tmp(tmp_path, monkeypatch):
    import config_store
    from config.atomic_write import _sweep_stale_tmp_files

    stale = tmp_path / "config.json.deadbeef1234.tmp"
    fresh = tmp_path / "config.json.freshcafe5678.tmp"
    stale.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    old = time.time() - 3600.0
    os.utime(stale, (old, old))

    removed = _sweep_stale_tmp_files(tmp_path, "config.json.*.tmp")
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()

    # End-to-end: load_config sweeps through the real path.
    stale2 = tmp_path / "config.json.aaaabbbbcccc.tmp"
    stale2.write_text("{}", encoding="utf-8")
    os.utime(stale2, (old, old))
    monkeypatch.setattr(config_store, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    from config.io import load_config

    load_config()
    assert not stale2.exists()


def test_n21_02_shutdown_dir_sweeps_stale_tmp(tmp_path):
    from config.atomic_write import _STALE_TMP_GRACE_SEC, _sweep_stale_tmp_files

    stale = tmp_path / ".mns_shutdown_token.deadbeef1234.tmp"
    fresh = tmp_path / ".mns_shutdown_token.freshcafe5678.tmp"
    stale.write_text("x", encoding="utf-8")
    fresh.write_text("y", encoding="utf-8")
    old = time.time() - _STALE_TMP_GRACE_SEC - 60.0
    os.utime(stale, (old, old))

    removed = _sweep_stale_tmp_files(tmp_path, ".*.tmp")
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
