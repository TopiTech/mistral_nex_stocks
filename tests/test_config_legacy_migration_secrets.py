"""Regression guards for the legacy -> runtime config migration.

The migration path in ``config/io.py`` used to run the preference-only merge
(``_merge_configs``) when the runtime config was missing and then always delete
the legacy file.  Only ``_MERGE_SEED_KEYS`` survived that merge, so
``api_credentials``, ``mns_master_key``, ``flask_secret_key`` and
``extension_api_token`` were silently discarded.  Losing ``mns_master_key`` in
particular orphans every value already encrypted with it.

These tests pin the observable contract:

* runtime config missing  -> the legacy file is migrated wholesale and removed;
* the copy fails          -> the legacy file is kept so a later start retries;
* a peer created the file -> preferences are synced, runtime stays authoritative
                             for secrets, legacy is cleaned up.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import config_store
from config import atomic_write

SECRETS: dict[str, Any] = {
    "api_credentials": {"mistral_api_key": "KEYREF"},
    "mns_master_key": "MASTERKEY",
    "flask_secret_key": {"scheme": "fernet", "value": "SEKRET"},
    "extension_api_token": "TOKENVALUE",
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture()
def store_paths(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Point ``config_store`` at a throwaway runtime/legacy pair."""
    runtime = tmp_path / "runtime" / "config.json"
    legacy = tmp_path / "legacy" / "config.json"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    with (
        patch.object(config_store, "CONFIG_FILE", runtime),
        patch.object(config_store, "LEGACY_CONFIG_FILE", legacy),
        patch.object(config_store, "APP_DATA_DIR", runtime.parent),
    ):
        yield runtime, legacy


def test_missing_runtime_migrates_every_secret(store_paths: tuple[Path, Path]) -> None:
    runtime, legacy = store_paths
    _write_json(legacy, {**SECRETS, "mistral_model": "legacy-model"})

    cfg = config_store.load_config()

    assert not legacy.exists(), "the migrated legacy file must be removed"
    assert _read_json(runtime) == {**SECRETS, "mistral_model": "legacy-model"}
    for key, value in SECRETS.items():
        assert cfg.get(key) == value, f"{key} was dropped by the migration"
    assert cfg.get("mistral_model") == "legacy-model"


def test_missing_runtime_migrates_without_seed_keys(store_paths: tuple[Path, Path]) -> None:
    runtime, legacy = store_paths
    _write_json(legacy, dict(SECRETS))

    cfg = config_store.load_config()

    assert not legacy.exists()
    for key, value in SECRETS.items():
        assert cfg.get(key) == value, f"{key} was dropped by the migration"
        assert _read_json(runtime).get(key) == value


def test_failed_copy_keeps_legacy_file(store_paths: tuple[Path, Path]) -> None:
    runtime, legacy = store_paths
    _write_json(legacy, {**SECRETS, "mistral_model": "legacy-model"})

    with patch("config.io._migrate_legacy_runtime_file", return_value=False):
        cfg = config_store.load_config()

    assert legacy.exists(), "a failed migration must keep the only copy of the secrets"
    assert _read_json(legacy).get("mns_master_key") == "MASTERKEY"
    # The runtime config falls back to defaults; nothing secret was copied.
    assert runtime.exists()
    assert "mns_master_key" not in _read_json(runtime)
    assert cfg.get("mns_master_key") is None


def test_peer_won_race_syncs_preferences_and_removes_legacy(
    store_paths: tuple[Path, Path],
) -> None:
    runtime, legacy = store_paths
    _write_json(legacy, {**SECRETS, "mistral_model": "legacy-model"})

    def _peer_creates_runtime(source: Path, target: Path) -> bool:
        # Simulate the concurrent process that won the O_EXCL race: the target
        # now exists with its own (authoritative) secrets.
        _write_json(
            target,
            {
                "mistral_model": "peer-model",
                "mns_master_key": "PEERKEY",
                "api_credentials": {"mistral_api_key": "PEERREF"},
            },
        )
        return False

    with patch(
        "config.io._migrate_legacy_runtime_file", side_effect=_peer_creates_runtime
    ):
        cfg = config_store.load_config()

    assert not legacy.exists(), "the legacy copy must be cleaned up after the sync"
    runtime_data = _read_json(runtime)
    assert runtime_data["mistral_model"] == "legacy-model", "seed key must follow the legacy file"
    assert runtime_data["mns_master_key"] == "PEERKEY", "runtime config owns the master key"
    assert runtime_data["api_credentials"] == {"mistral_api_key": "PEERREF"}
    assert cfg.get("mns_master_key") == "PEERKEY"


def test_sync_failure_is_survived(store_paths: tuple[Path, Path]) -> None:
    _runtime, legacy = store_paths
    _write_json(legacy, {**SECRETS, "mistral_model": "legacy-model"})

    def _peer_creates_runtime(source: Path, target: Path) -> bool:
        _write_json(target, {"mistral_model": "peer-model"})
        return False

    with (
        patch("config.io._migrate_legacy_runtime_file", side_effect=_peer_creates_runtime),
        patch("config.io._merge_configs", side_effect=RuntimeError("merge exploded")),
    ):
        cfg = config_store.load_config()

    # A failed sync must not raise out of load_config, and must not delete the
    # legacy file while it is still the only copy of the secrets.
    assert legacy.exists()
    assert _read_json(legacy).get("mns_master_key") == "MASTERKEY"
    assert isinstance(cfg, dict)


def test_migrate_runtime_file_reports_missing_source(store_paths: tuple[Path, Path]) -> None:
    target, source = store_paths

    assert atomic_write._migrate_legacy_runtime_file(source, target) is False
    assert not target.exists()


def test_migrate_runtime_file_reports_existing_target(store_paths: tuple[Path, Path]) -> None:
    target, source = store_paths
    _write_json(source, {"a": 1})
    _write_json(target, {"b": 2})

    assert atomic_write._migrate_legacy_runtime_file(source, target) is True
    assert _read_json(target) == {"b": 2}, "a peer-owned target must not be overwritten"
    assert source.exists(), "the peer owns the migration and the source cleanup"


def test_migrate_runtime_file_copies_and_removes_source(
    store_paths: tuple[Path, Path],
) -> None:
    target, source = store_paths
    payload = {**SECRETS, "mistral_model": "legacy-model"}
    _write_json(source, payload)

    assert atomic_write._migrate_legacy_runtime_file(source, target) is True
    assert _read_json(target) == payload
    assert not source.exists()
    assert target.stat().st_size > 0


@pytest.mark.parametrize(
    "peer_content",
    ["", "{", '{"mns_master_key": "PEER"', "[1, 2]", '"just a string"'],
    ids=[
        "empty-file",
        "truncated-object",
        "unterminated-object",
        "array-root",
        "string-root",
    ],
)
def test_migrate_runtime_file_rejects_incomplete_peer_target(
    store_paths: tuple[Path, Path], peer_content: str
) -> None:
    """A peer that lost the ``O_EXCL`` race may still be streaming the copy.

    The old ``except FileExistsError: return True`` reported that half-written
    target as a completed migration, so ``load_config`` logged success and gave
    up while the only complete copy of the secrets stayed in the legacy file
    for the wrong reason. A target without a complete JSON object must be
    reported as *not* migrated without touching either file.
    """
    target, source = store_paths
    payload = {**SECRETS, "mistral_model": "legacy-model"}
    _write_json(source, payload)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(peer_content, encoding="utf-8")

    assert atomic_write._migrate_legacy_runtime_file(source, target) is False
    assert target.read_text(encoding="utf-8") == peer_content, "peer target must stay untouched"
    assert _read_json(source) == payload, "the source must stay for a later retry"


def test_migrate_runtime_file_accepts_complete_peer_target(
    store_paths: tuple[Path, Path],
) -> None:
    target, source = store_paths
    _write_json(source, dict(SECRETS))
    _write_json(target, {"mns_master_key": "PEERKEY"})

    assert atomic_write._migrate_legacy_runtime_file(source, target) is True
    assert _read_json(target) == {"mns_master_key": "PEERKEY"}


def test_partial_peer_target_is_not_reported_as_migrated(
    store_paths: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``O_EXCL`` race, end to end.

    ``load_config`` calls the migration only when the runtime config looks
    absent, so the peer's file has to appear inside that call. The real
    ``_migrate_legacy_runtime_file`` then hits ``FileExistsError``; reporting
    success there made the caller log a completed migration and skip the
    preference sync while the partial peer file stayed in place.
    """
    _runtime, legacy = store_paths
    _write_json(legacy, dict(SECRETS))
    real_migrate = atomic_write._migrate_legacy_runtime_file

    def _peer_wins_race(source: Path, target: Path) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")  # created, not yet streamed
        return real_migrate(source, target)

    with (
        patch("config.io._migrate_legacy_runtime_file", side_effect=_peer_wins_race),
        caplog.at_level(logging.WARNING, logger="config_store"),
    ):
        cfg = config_store.load_config()

    assert legacy.exists(), "the only complete copy of the secrets must survive"
    assert _read_json(legacy) == SECRETS
    assert isinstance(cfg, dict), "load_config must survive the partial peer file"
    messages = [record.getMessage() for record in caplog.records]
    assert any("Failed to sync legacy config" in msg for msg in messages), messages


def test_corrupt_newer_runtime_keeps_legacy_secrets(
    store_paths: tuple[Path, Path],
) -> None:
    """A newer but unreadable runtime config must not cost the legacy secrets."""
    runtime, legacy = store_paths
    _write_json(legacy, dict(SECRETS))
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("", encoding="utf-8")

    cfg = config_store.load_config()

    assert legacy.exists(), "the legacy file is still the only complete copy"
    assert _read_json(legacy) == SECRETS
    assert isinstance(cfg, dict), "load_config must survive the corrupt runtime file"
