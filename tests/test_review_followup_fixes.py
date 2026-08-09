"""Regression coverage for persistence, startup, and cache review fixes."""

import json
import threading
import time
from unittest.mock import patch

from native_host import start_backend
from utils import storage
from utils.disk_cache import StockDiskCache


def test_legacy_portfolio_migration_writes_encrypted_envelope(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy-user-stocks.json"
    target = tmp_path / "runtime" / "user_stocks.json"
    holdings = {"us": {"AAPL": {"shares": 3, "avg_price": 175.25}}}
    legacy.write_text(json.dumps(holdings), encoding="utf-8")

    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", str(legacy))
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    monkeypatch.setattr(storage.config_store, "APP_DATA_DIR", target.parent)
    with (
        patch.object(storage.config_store, "get_or_create_master_key", return_value="test-key"),
        patch.object(
            storage,
            "protect_data",
            return_value={"scheme": "fernet", "value": "encrypted-payload"},
        ) as protect,
    ):
        storage._migrate_legacy_user_stocks()

    assert protect.call_count == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "scheme": "fernet",
        "value": "encrypted-payload",
    }
    assert "shares" not in target.read_text(encoding="utf-8")
    assert json.loads(legacy.read_text(encoding="utf-8")) == holdings


def test_legacy_migration_failure_keeps_existing_runtime_file(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    target = tmp_path / "runtime" / "user_stocks.json"
    legacy.write_text(json.dumps({"us": {"AAPL": {"shares": 3}}}), encoding="utf-8")
    target.parent.mkdir()
    target.write_text('{"scheme":"fernet","value":"existing"}', encoding="utf-8")

    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", str(legacy))
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    with patch.object(storage, "protect_data", side_effect=ValueError("cannot encrypt")):
        storage._migrate_legacy_user_stocks()

    assert target.read_text(encoding="utf-8") == '{"scheme":"fernet","value":"existing"}'
    assert legacy.exists()


def test_startup_lock_serializes_parallel_start_attempts(tmp_path, monkeypatch):
    lock_path = tmp_path / ".backend.start.lock"
    monkeypatch.setattr(start_backend, "STARTUP_LOCK_FILE", lock_path)
    active = 0
    max_active = 0
    guard = threading.Lock()

    def simulated_start(_extension_id):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return {"ok": True}

    with patch.object(start_backend, "_start", side_effect=simulated_start):
        threads = [threading.Thread(target=start_backend.start, args=("a" * 32,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert max_active == 1


def test_disk_cache_mutations_all_use_process_lock(tmp_path, monkeypatch):
    cache = StockDiskCache(tmp_path / "cache", max_entries=1, enable_cleanup=False)
    calls = 0
    original_lock = cache._process_lock

    def counted_lock():
        nonlocal calls
        calls += 1
        return original_lock()

    monkeypatch.setattr(cache, "_process_lock", counted_lock)
    cache.set("one", {"value": 1})
    cache.get("one")
    cache.has("one")
    cache.delete("one")
    cache.set("prefix-one", 1)
    cache.delete_prefix("prefix")
    cache.clear()
    cache.cleanup()
    cache.stats()

    assert calls == 9


def test_realtime_engine_client_context_cleanup():
    from services.realtime_engine import RealtimeMarketEngine

    engine = RealtimeMarketEngine()
    with engine.client_context() as cid:
        assert cid in engine._client_states
        assert cid in engine._client_events
    # Verified: unregistered after context exit
    assert cid not in engine._client_states
    assert cid not in engine._client_events


def test_scrapers_close_cleans_thread_local_sessions():
    from services.realtime_engine import SBISecuritiesScraper, YahooJPRealtimeScraper

    yp_scraper = YahooJPRealtimeScraper()
    _ = yp_scraper.session
    assert getattr(yp_scraper._thread_local, "session", None) is not None
    yp_scraper.close()
    assert getattr(yp_scraper._thread_local, "session", None) is None

    sbi_scraper = SBISecuritiesScraper()
    _ = sbi_scraper.session
    assert getattr(sbi_scraper._thread_local, "session", None) is not None
    sbi_scraper.close()
    assert getattr(sbi_scraper._thread_local, "session", None) is None
