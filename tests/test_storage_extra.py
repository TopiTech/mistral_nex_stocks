"""Coverage-focused tests for utils/storage.py."""

import utils.storage as storage
from app_state import app_state


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
    assert storage.load_user_stocks() is None


def test_load_user_stocks_corrupt_is_handled(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.json"
    path.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(path))
    # Corrupt JSON must be caught internally and not raise
    assert storage.load_user_stocks() is None


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

