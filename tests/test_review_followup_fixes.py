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
    # The legacy plaintext file is removed after a successful migration so the
    # raw portfolio data does not linger unencrypted on disk.
    assert not legacy.exists()


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


def test_resolve_stocks_for_response_overlays_realtime_market_snapshot(monkeypatch):
    from app_state import app_state
    from utils.stock_payload import _resolve_stocks_for_response

    mock_target = {
        "us": [{"symbol": "AAPL", "price": 150.0, "change": 1.0}],
        "jp": [{"symbol": "7203.T", "price": 2000.0, "change": 10.0}],
        "idx": [],
    }
    with app_state.cache.sse_data_lock:
        app_state.market.target_stocks_cache = mock_target
        app_state.market.current_stocks_cache = mock_target

    from services.realtime_engine import realtime_market_engine

    try:
        with realtime_market_engine.store_lock:
            realtime_market_engine.market_store["AAPL"] = {
                "symbol": "AAPL",
                "price": 155.5,
                "change": 5.5,
                "change_percent": 3.67,
                "volume": 1000000,
                "source": "tradingview",
            }
            realtime_market_engine.market_store["7203.T"] = {
                "symbol": "7203.T",
                "price": 2050.0,
                "change": 50.0,
                "change_percent": 2.5,
                "volume": 500000,
                "source": "yahoojp",
            }

        resolved = _resolve_stocks_for_response(real_data_only=True)
        us_aapl = next((s for s in resolved["us"] if s["symbol"] == "AAPL"), None)
        jp_7203 = next((s for s in resolved["jp"] if s["symbol"] == "7203.T"), None)

        assert us_aapl is not None
        assert us_aapl["price"] == 155.5
        assert us_aapl["source"] == "tradingview"

        assert jp_7203 is not None
        assert jp_7203["price"] == 2050.0
        assert jp_7203["source"] == "yahoojp"
    finally:
        with realtime_market_engine.store_lock:
            realtime_market_engine.market_store.pop("AAPL", None)
            realtime_market_engine.market_store.pop("7203.T", None)


def test_sse_ticket_cookie_path_is_api_stocks(client):
    res = client.post("/api/stocks/stream/ticket")
    assert res.status_code == 200
    cookie_header = res.headers.get("Set-Cookie", "")
    assert "Path=/api/stocks" in cookie_header
    assert "Path=/api/stocks/stream" not in cookie_header


def test_ai_portfolio_copy_dynamically_resolves_usdjpy_rate(client, monkeypatch):
    from app_state import app_state

    # Set last_usdjpy_rate to stale timestamp
    app_state.market.last_usdjpy_rate = 150.0
    app_state.market.last_usdjpy_rate_ts = 1.0  # ancient

    # Set live price in current_indices_cache
    with app_state.cache.sse_data_lock:
        app_state.market.current_indices_cache = {"USDJPY": {"symbol": "USDJPY=X", "price": 158.5}}

    payload = {
        "items": [{"symbol": "AAPL", "market": "us", "target_price": 200.0, "weight_pct": 100.0}]
    }
    with (
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
        patch("routes.api_stocks.save_user_stocks"),
        patch("routes.api_stocks._sync_realtime_symbol"),
        patch("app_bg.announce_current_market_state"),
        patch("routes.api_stocks.schedule_sync_all_stocks_now"),
    ):
        res = client.post("/api/ai-portfolio/copy-to-my", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert app_state.market.last_usdjpy_rate == 158.5
    assert data.get("stale_warning") is None
