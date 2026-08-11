"""Coverage-focused tests for utils/storage.py."""

import json

import pytest

from app_state import app_state
from utils import storage


def test_save_and_load_user_stocks(tmp_path, monkeypatch):
    path = tmp_path / "user_stocks.json"
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))
    app_state.market.user_us = {"AAPL": {"symbol": "AAPL", "name": "Apple"}}
    app_state.market.user_jp = {}
    app_state.market.user_idx = {}
    app_state.market.last_usdjpy_rate = 150.0
    storage.save_user_stocks()

    # Reset in-memory state and reload from disk
    app_state.market.user_us = {}
    app_state.market.last_modified_ns = 0
    storage.load_user_stocks(force=True)
    assert app_state.market.user_us["AAPL"]["symbol"] == "AAPL"


def test_load_user_stocks_missing_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "missing.json"
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))
    assert storage.load_user_stocks(force=True) is None


def test_load_user_stocks_corrupt_is_handled(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.json"
    path.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))
    monkeypatch.setattr(app_state.market, "user_stocks_load_error", False)
    # Corrupt JSON must be caught internally, flagged, and not raise.
    assert storage.load_user_stocks(force=True) is None
    assert app_state.market.user_stocks_load_error is True


def test_unexpected_read_oserror_also_blocks_destructive_save(tmp_path, monkeypatch):
    path = tmp_path / "user_stocks.json"
    path.write_text('{"us": {"AAPL": {"name": "Apple"}}}', encoding="utf-8")
    original_bytes = path.read_bytes()
    original_us = {"KEEP": {"name": "Keep", "shares": 3.0}}
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))
    monkeypatch.setattr(app_state.market, "user_us", original_us.copy())
    monkeypatch.setattr(app_state.market, "user_stocks_load_error", False)

    def fail_read(_lock_file):
        raise OSError("simulated read failure")

    monkeypatch.setattr(storage, "_locked_read_user_stocks", fail_read)
    storage.load_user_stocks(force=True)

    assert app_state.market.user_us == original_us
    assert app_state.market.user_stocks_load_error is True
    with pytest.raises(storage.UserStocksPersistError):
        storage.save_user_stocks()
    assert path.read_bytes() == original_bytes


def test_user_stocks_backup_rotation_on_decryption_failure(tmp_path, monkeypatch):
    import glob
    import json

    path = tmp_path / "user_stocks.json"

    # Write a dict with scheme and value so it attempts decryption and fails
    corrupt_data = {"scheme": "fernet", "value": "invalid ciphertext"}
    path.write_text(json.dumps(corrupt_data), encoding="utf-8")
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))

    # We need to trigger decryption failure repeatedly.
    # Every time load_user_stocks(force=True) is called, decryption fails and creates a backup.
    for _ in range(7):
        storage.load_user_stocks(force=True)

    backups = glob.glob(str(tmp_path / "user_stocks.bak.*"))
    assert len(backups) <= 5


@pytest.mark.parametrize(
    "invalid_payload",
    [None, [], {"us": [], "jp": {}, "idx": {}}],
)
def test_invalid_user_stocks_never_replaces_memory_or_allows_save(
    tmp_path, monkeypatch, invalid_payload
):
    path = tmp_path / "user_stocks.json"
    path.write_text(json.dumps(invalid_payload), encoding="utf-8")
    original_bytes = path.read_bytes()
    original_us = {"AAPL": {"name": "Apple", "shares": 4.0, "avg_price": 100.0}}

    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))
    monkeypatch.setattr(app_state.market, "user_us", original_us.copy())
    monkeypatch.setattr(app_state.market, "user_jp", {})
    monkeypatch.setattr(app_state.market, "user_idx", {})
    monkeypatch.setattr(app_state.market, "user_stocks_load_error", False)

    storage.load_user_stocks(force=True)

    assert app_state.market.user_us == original_us
    assert app_state.market.user_stocks_load_error is True
    with pytest.raises(storage.UserStocksPersistError):
        storage.save_user_stocks()
    assert path.read_bytes() == original_bytes


def test_user_stocks_roundtrip_persists_fx_timestamp(tmp_path, monkeypatch):
    path = tmp_path / "user_stocks.json"
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))
    monkeypatch.setattr(app_state.market, "user_us", {"AAPL": {"name": "Apple"}})
    monkeypatch.setattr(app_state.market, "user_jp", {})
    monkeypatch.setattr(app_state.market, "user_idx", {})
    monkeypatch.setattr(app_state.market, "user_stocks_load_error", False)
    monkeypatch.setattr(app_state.market, "last_usdjpy_rate", 147.25)
    monkeypatch.setattr(app_state.market, "last_usdjpy_rate_ts", 1_765_000_123.5)
    monkeypatch.setattr(storage.config_store, "get_or_create_master_key", lambda: "test-key")
    monkeypatch.setattr(
        storage,
        "protect_data",
        lambda value, **_kwargs: {"scheme": "test", "value": value},
    )
    monkeypatch.setattr(
        storage,
        "unprotect_data",
        lambda envelope, **_kwargs: envelope["value"],
    )

    storage.save_user_stocks()
    app_state.market.last_usdjpy_rate = 150.0
    app_state.market.last_usdjpy_rate_ts = 0.0
    storage.load_user_stocks(force=True)

    assert app_state.market.last_usdjpy_rate == pytest.approx(147.25)
    assert app_state.market.last_usdjpy_rate_ts == pytest.approx(1_765_000_123.5)
