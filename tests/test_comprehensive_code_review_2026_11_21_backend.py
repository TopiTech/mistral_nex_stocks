"""Regression tests for comprehensive code review Units 1-3 (ISS-01..ISS-12)."""

from __future__ import annotations

import gc
import json
import math
import sys
import threading
import time
import weakref
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app_state import app_state


# ---------------------------------------------------------------------------
# ISS-01: config/master_key.py rollback on persistence verification failure
# ---------------------------------------------------------------------------
def test_master_key_verification_failure_rolls_back_config_and_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config_store
    import crypto_utils
    from config import master_key

    saved_configs: list[dict[str, Any]] = [
        {"theme": "dark", "existing_flag": True},
    ]

    def fake_load_config() -> dict[str, Any]:
        # First two calls (fast-path + locked reload) return pre-existing config.
        # Third call (post-save verification) returns empty dict to simulate
        # persistence failure where saved key cannot be decoded back.
        if len(saved_configs) >= 2:
            return {}
        return dict(saved_configs[-1])

    def fake_save_config(cfg: dict[str, Any]) -> None:
        saved_configs.append(dict(cfg))

    monkeypatch.setattr(config_store, "load_config", fake_load_config)
    monkeypatch.setattr(config_store, "save_config", fake_save_config)
    monkeypatch.setattr(config_store, "is_config_corrupted", lambda: False)
    monkeypatch.setattr(
        config_store,
        "_encode_secret",
        lambda val, name: {"enc": val, "name": name},
    )
    monkeypatch.setattr(
        config_store,
        "_decode_secret",
        lambda entry, name: "",
    )
    snapshot_calls: list[tuple[tuple[str, ...], Any]] = []
    restore_calls: list[tuple[Any, Any]] = []

    def fake_snapshot(
        keys: tuple[str, ...], log: Any = None
    ) -> tuple[dict[str, str], dict[str, str]]:
        snapshot_calls.append((keys, log))
        return {"mns_master_key": "old"}, {}

    def fake_restore(k_vals: Any, e_vals: Any, log: Any = None) -> None:
        restore_calls.append((k_vals, e_vals))

    monkeypatch.setattr(crypto_utils, "snapshot_secret_storage", fake_snapshot)
    monkeypatch.setattr(crypto_utils, "restore_secret_storage", fake_restore)

    with pytest.raises(RuntimeError, match="verify persisted master key"):
        master_key.get_or_create_master_key()

    # Initial save wrote mns_master_key; rollback save restored original config without mns_master_key
    assert len(saved_configs) == 3
    assert "mns_master_key" in saved_configs[1]
    assert saved_configs[2] == {"theme": "dark", "existing_flag": True}
    assert len(snapshot_calls) == 1
    assert restore_calls == [({"mns_master_key": "old"}, {})]
    assert config_store._CONFIG_CACHE["data"] is None
    assert config_store._CONFIG_CACHE["key"] is None


# ---------------------------------------------------------------------------
# ISS-02: utils/market_utils.py _fetch_live_market_state skips when rate-limited
# ---------------------------------------------------------------------------
def test_fetch_live_market_state_short_circuits_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from utils import market_utils

    with app_state.market.yfinance_lock:
        app_state.market.is_yfinance_rate_limited = True
        app_state.market.yfinance_rate_limit_until = time.time() + 60.0

    calls: list[str] = []

    def fake_get_ticker(sym: str) -> Any:
        calls.append(sym)
        return None

    monkeypatch.setattr(market_utils, "safe_get_ticker", fake_get_ticker)

    assert market_utils._fetch_live_market_state("us") is None
    assert market_utils._fetch_live_market_state("jp") is None
    assert market_utils._fetch_live_market_state("idx") is None
    assert calls == []


# ---------------------------------------------------------------------------
# ISS-03: utils/stock_payload.py partial fast_info must not poison 24h cache
# ---------------------------------------------------------------------------
def test_get_stock_info_cached_skips_24h_disk_cache_for_fast_only_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from utils import stock_payload

    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()

    disk_store: dict[str, dict[str, Any]] = {}
    mem_store: dict[str, Any] = {}

    fake_disk = MagicMock()
    fake_disk.get.side_effect = lambda key, ttl=None, ignore_ttl=False: disk_store.get(key)
    fake_disk.set.side_effect = lambda key, val: disk_store.__setitem__(key, val)
    monkeypatch.setattr(app_state, "stock_disk_cache", fake_disk)
    monkeypatch.setattr(stock_payload, "_has_cached_key", lambda k, d: False)
    monkeypatch.setattr(stock_payload, "peek_cached", lambda k, duration=0: mem_store.get(k))
    monkeypatch.setattr(
        stock_payload,
        "_set_cached_value",
        lambda k, v, d: mem_store.__setitem__(k, v),
    )

    monkeypatch.setattr(
        app_state.stock_provider,
        "get_fast_info",
        lambda sym: {
            "regularMarketPrice": 185.0,
            "marketCap": 3_000_000_000_000,
            "fiftyTwoWeekHigh": 200.0,
        },
    )
    info_calls = 0

    def fake_get_info(sym: str) -> dict[str, Any]:
        nonlocal info_calls
        info_calls += 1
        if info_calls == 1:
            raise RuntimeError("transient quoteSummary error")
        return {
            "sector": "Technology",
            "shortName": "Apple Inc.",
            "marketCap": 3_000_000_000_000,
        }

    monkeypatch.setattr(app_state.stock_provider, "get_info", fake_get_info)

    # First call: fast-only payload must be returned from memory, NOT saved to 24h disk cache
    first = stock_payload.get_stock_info_cached("AAPL")
    assert first.get("regularMarketPrice") == 185.0
    assert "info_disk_AAPL" not in disk_store

    # Expire short cache -> second call must re-fetch full info and then persist to 24h disk cache
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()
    second = stock_payload.get_stock_info_cached("AAPL")
    assert second.get("sector") == "Technology"
    assert info_calls == 2
    assert "info_disk_AAPL" in disk_store


def test_get_stock_info_cached_rejects_partial_disk_entry_unless_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from utils import stock_payload

    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()

    disk_store: dict[str, dict[str, Any]] = {
        "info_disk_MSFT": {"regularMarketPrice": 400.0},  # lacks fundamentals
        "info_disk_^TOPX": {"regularMarketPrice": 2800.0},  # index exempt
    }
    fake_disk = MagicMock()
    fake_disk.get.side_effect = lambda key, ttl=None, ignore_ttl=False: disk_store.get(key)
    fake_disk.set.side_effect = lambda key, val: disk_store.__setitem__(key, val)
    monkeypatch.setattr(app_state, "stock_disk_cache", fake_disk)
    monkeypatch.setattr(stock_payload, "_has_cached_key", lambda k, d: False)
    monkeypatch.setattr(stock_payload, "peek_cached", lambda k, duration=0: None)
    monkeypatch.setattr(stock_payload, "_set_cached_value", lambda k, v, d: None)

    # Index symbol with fast price is complete and served from disk without hitting provider
    idx_res = stock_payload.get_stock_info_cached("^TOPX")
    assert idx_res == {"regularMarketPrice": 2800.0}

    # Stock with partial fast-only disk data still refreshes from provider when unblocked
    monkeypatch.setattr(
        app_state.stock_provider,
        "get_fast_info",
        lambda sym: {"regularMarketPrice": 405.0},
    )
    monkeypatch.setattr(
        app_state.stock_provider,
        "get_info",
        lambda sym: {"industry": "Software - Infrastructure"},
    )
    msft_res = stock_payload.get_stock_info_cached("MSFT")
    assert msft_res.get("industry") == "Software - Infrastructure"
    assert msft_res.get("regularMarketPrice") == 405.0


# ---------------------------------------------------------------------------
# ISS-04: utils/stock_payload.py _resolve_stocks_for_response validates PTS prices
# ---------------------------------------------------------------------------
def test_resolve_stocks_for_response_filters_invalid_pts_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.realtime_engine import realtime_market_engine
    from utils import stock_payload

    with app_state.cache.sse_data_lock:
        app_state.market.current_stocks_cache = {
            "us": [],
            "jp": [
                {"symbol": "7203.T", "price": 2500.0},
                {"symbol": "6758.T", "price": 3000.0},
                {"symbol": "9984.T", "price": 8000.0},
                {"symbol": "8306.T", "price": 1500.0},
            ],
            "idx": [],
        }

    monkeypatch.setattr(realtime_market_engine, "get_market_snapshot", dict)
    monkeypatch.setattr(
        realtime_market_engine,
        "get_pts_snapshot",
        lambda: {
            "7203": {"price": float("nan"), "change": 10.0, "pts_trading": True},
            "6758": {"price": True, "change": 5.0, "pts_trading": True},
            "9984": {"price": -100.0, "change": 5.0, "pts_trading": True},
            "8306": {
                "price": 1525.0,
                "change": float("inf"),
                "pts_trading": True,
                "pts_time": "18:00",
            },
        },
    )
    registered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        realtime_market_engine,
        "register_symbol",
        lambda sym, mkt: registered.append((sym, mkt)),
    )

    resolved = stock_payload._resolve_stocks_for_response()
    by_sym = {row["symbol"]: row for row in resolved["jp"]}
    for invalid_sym in ("7203.T", "6758.T", "9984.T"):
        assert "pts_price" not in by_sym[invalid_sym]
    assert by_sym["8306.T"]["pts_price"] == 1525.0
    assert by_sym["8306.T"]["pts_change"] is None
    assert set(registered) == {("7203.T", "jp"), ("6758.T", "jp"), ("9984.T", "jp")}


# ---------------------------------------------------------------------------
# ISS-05: Cycle / depth / non-str key guards across sanitizers & cache delete
# ---------------------------------------------------------------------------
def test_recursive_sanitizers_handle_cycles_depth_and_nonstr_keys() -> None:
    from services.stock.sanitize import sanitize_fundamental_dict
    from utils.stock_payload import _sanitize_error_value
    from utils.text_utils import json_safe_tool_value

    cyc_dict: dict[Any, Any] = {123: 1.5, 12.5: 2.5, "bad_nan": float("nan")}
    cyc_dict["self"] = cyc_dict
    cyc_list: list[Any] = [1, 2]
    cyc_list.append(cyc_list)
    cyc_dict["list_self"] = cyc_list

    # Sanitize fundamentals without RecursionError and preserve valid scalars
    fund: dict[Any, Any] = sanitize_fundamental_dict(cyc_dict)
    assert fund[123] == 1.5
    assert fund["12.5"] == 2.5
    assert "bad_nan" not in fund
    assert "self" not in fund
    assert fund["list_self"] == [1, 2]
    json.dumps(fund, allow_nan=False)

    # Error payload sanitizer
    err_cyc: dict[str, Any] = {"msg": "secret?api_key=ABCDEF123456"}
    err_cyc["loop"] = err_cyc
    sanitized_err = _sanitize_error_value(err_cyc)
    assert "ABCDEF123456" not in sanitized_err["msg"]
    assert sanitized_err["loop"] == "[circular]"

    # Tool value sanitizer
    tool_cyc: dict[str, Any] = {"val": float("inf"), "ok": 42}
    tool_cyc["self"] = tool_cyc
    safe_tool = json_safe_tool_value(tool_cyc)
    assert safe_tool["val"] is None
    assert safe_tool["ok"] == 42
    assert safe_tool["self"] == "[circular]"

    # Excessive nesting hits depth cap cleanly
    deep_fund: dict[str, Any] = {}
    cur_f = deep_fund
    deep_err: dict[str, Any] = {}
    cur_e = deep_err
    deep_tool: dict[str, Any] = {}
    cur_t = deep_tool
    for i in range(50):
        nxt_f: dict[str, Any] = {"v": i}
        cur_f["child"] = nxt_f
        cur_f = nxt_f
        nxt_e: dict[str, Any] = {"v": i}
        cur_e["child"] = nxt_e
        cur_e = nxt_e
        nxt_t: dict[str, Any] = {"v": i}
        cur_t["child"] = nxt_t
        cur_t = nxt_t

    assert isinstance(sanitize_fundamental_dict(deep_fund), dict)
    assert isinstance(_sanitize_error_value(deep_err), dict)
    assert isinstance(json_safe_tool_value(deep_tool), dict)


def test_disk_cache_delete_prefix_handles_recursion_error(tmp_path: Path) -> None:
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(tmp_path)
    # Build JSON deeply nested enough to trigger json.load RecursionError (> 1000 levels)
    corrupt_path = tmp_path / "hist_AAPL_abcdef1234.json"
    corrupt_path.write_text("[" * 2500 + "]" * 2500, encoding="utf-8")

    removed = cache.delete_prefix("hist_AAPL_")
    assert removed == 1
    assert not corrupt_path.exists()


# ---------------------------------------------------------------------------
# ISS-06: utils/tradingview_mapper.py bounded LRU _TICKER_EXCHANGE_CACHE
# ---------------------------------------------------------------------------
def test_ticker_exchange_cache_evicts_lru_non_default_entries() -> None:
    from utils import tradingview_mapper as tvm

    with tvm._CACHE_LOCK:
        original = OrderedDict(tvm._TICKER_EXCHANGE_CACHE)
        orig_max = tvm._MAX_TICKER_EXCHANGE_CACHE_SIZE
    try:
        default_keys = list(tvm._DEFAULT_TICKER_EXCHANGES)[:2]
        with tvm._CACHE_LOCK:
            tvm._TICKER_EXCHANGE_CACHE.clear()
            for default_k in default_keys:
                tvm._TICKER_EXCHANGE_CACHE[default_k] = original[default_k]
            tvm._MAX_TICKER_EXCHANGE_CACHE_SIZE = 4

        tvm.register_ticker_exchange("CUSTOM1", "NASDAQ")
        tvm.register_ticker_exchange("CUSTOM2", "NYSE")
        # Cache holds 2 defaults + CUSTOM1 + CUSTOM2 = 4. Touch CUSTOM1 via get -> becomes MRU
        assert tvm.get_ticker_exchange("CUSTOM1") == "NASDAQ"
        # Adding CUSTOM3 evicts LRU non-default CUSTOM2 while keeping defaults and CUSTOM1
        tvm.register_ticker_exchange("CUSTOM3", "AMEX")

        with tvm._CACHE_LOCK:
            assert len(tvm._TICKER_EXCHANGE_CACHE) <= 4
            assert "CUSTOM1" in tvm._TICKER_EXCHANGE_CACHE
            assert "CUSTOM3" in tvm._TICKER_EXCHANGE_CACHE
            assert "CUSTOM2" not in tvm._TICKER_EXCHANGE_CACHE
            for dk in default_keys:
                assert dk in tvm._TICKER_EXCHANGE_CACHE
    finally:
        with tvm._CACHE_LOCK:
            tvm._MAX_TICKER_EXCHANGE_CACHE_SIZE = orig_max
            tvm._TICKER_EXCHANGE_CACHE.clear()
            tvm._TICKER_EXCHANGE_CACHE.update(original)


# ---------------------------------------------------------------------------
# ISS-07: bg/common.py Mode-1 and Mode-2 concurrent publish serialization
# ---------------------------------------------------------------------------
def test_raw_announce_and_mode2_serialize_snapshot_and_announce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app_bg
    from bg import common

    monkeypatch.setattr(common, "is_market_open", lambda m: True)

    with app_state.cache.sse_data_lock:
        app_state.market.current_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 100.0}],
            "jp": [],
            "idx": [],
        }
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 100.0}],
            "jp": [],
            "idx": [],
        }
        app_state.market.current_indices_cache = {}

    announced_mode1: list[str] = []
    fake_announcer1 = MagicMock()
    fake_announcer1.listener_count.return_value = 1
    fake_announcer1.announce.side_effect = lambda frame: announced_mode1.append(frame)
    monkeypatch.setattr(app_state, "sse_announcer", fake_announcer1, raising=False)
    monkeypatch.setattr(app_state, "sse_announcer_mode1", fake_announcer1, raising=False)

    monkeypatch.setattr(common, "_sse_prev_stocks", {}, raising=False)
    monkeypatch.setattr(common, "_sse_prev_indices", {}, raising=False)
    monkeypatch.setattr(app_bg, "_sse_prev_stocks", {}, raising=False)
    monkeypatch.setattr(app_bg, "_sse_prev_indices", {}, raising=False)
    monkeypatch.setattr(common, "_sse_full_snapshot_counter", 0, raising=False)
    monkeypatch.setattr(app_bg, "_sse_full_snapshot_counter", 0, raising=False)

    active_mode1 = 0
    max_active_mode1 = 0
    count_lock = threading.Lock()
    orig_diff = common._build_sse_diff

    def counting_diff(*args: Any, **kwargs: Any) -> Any:
        nonlocal active_mode1, max_active_mode1
        with count_lock:
            active_mode1 += 1
            max_active_mode1 = max(max_active_mode1, active_mode1)
        time.sleep(0.01)
        try:
            return orig_diff(*args, **kwargs)
        finally:
            with count_lock:
                active_mode1 -= 1

    monkeypatch.setattr(common, "_build_sse_diff", counting_diff)

    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker_mode1() -> None:
        try:
            barrier.wait(timeout=5)
            common._raw_announce_current_market_state()
        except BaseException as exc:
            errors.append(exc)

    threads1 = [threading.Thread(target=worker_mode1) for _ in range(4)]
    for t in threads1:
        t.start()
    for t in threads1:
        t.join(timeout=5)

    assert not errors
    assert max_active_mode1 == 1
    assert len(announced_mode1) == 4

    # Mode 2 concurrency check
    fake_announcer2 = MagicMock()
    fake_announcer2.listener_count.return_value = 1
    monkeypatch.setattr(app_state, "sse_announcer_mode2", fake_announcer2, raising=False)
    monkeypatch.setattr(app_bg, "announce_real_market_state", common.announce_real_market_state)

    active_mode2 = 0
    max_active_mode2 = 0
    announced_mode2: list[str] = []

    def slow_announce(ann: Any, frame: str, mode: int) -> None:
        nonlocal active_mode2, max_active_mode2
        with count_lock:
            active_mode2 += 1
            max_active_mode2 = max(max_active_mode2, active_mode2)
        time.sleep(0.01)
        announced_mode2.append(frame)
        with count_lock:
            active_mode2 -= 1

    monkeypatch.setattr(app_bg, "_announce_frame", slow_announce)
    monkeypatch.setattr(common, "_announce_frame", slow_announce)

    barrier2 = threading.Barrier(4)

    def worker_mode2() -> None:
        try:
            barrier2.wait(timeout=5)
            common.announce_real_market_state()
        except BaseException as exc:
            errors.append(exc)

    threads2 = [threading.Thread(target=worker_mode2) for _ in range(4)]
    for t in threads2:
        t.start()
    for t in threads2:
        t.join(timeout=5)

    assert not errors
    assert max_active_mode2 == 1
    assert len(announced_mode2) == 4


# ---------------------------------------------------------------------------
# ISS-08: services/realtime/engine.py register_symbol / unregister_symbol "idx"
# ---------------------------------------------------------------------------
def test_realtime_engine_register_and_unregister_idx_symbol() -> None:
    from services.realtime.engine import RealtimeMarketEngine

    engine = RealtimeMarketEngine()
    engine.tv_client = MagicMock()
    engine.yahoojp_scraper = MagicMock()

    engine.register_symbol("^GSPC", "idx")
    engine.tv_client.add_symbol.assert_called_once_with("FOREXCOM:SPXUSD")
    assert ("idx", "^GSPC") in engine._registration_tokens

    with engine.store_lock:
        engine.market_store["SPXUSD"] = {"symbol": "SPXUSD", "price": 5000.0}
        engine.previous_store["SPXUSD"] = {"symbol": "SPXUSD", "price": 4990.0}
        engine.market_store["^GSPC"] = {"symbol": "^GSPC", "price": 5000.0}

    engine.unregister_symbol("^GSPC", "idx")
    engine.tv_client.remove_symbol.assert_called_once_with("FOREXCOM:SPXUSD")
    assert ("idx", "^GSPC") not in engine._registration_tokens
    with engine.store_lock:
        assert "SPXUSD" not in engine.market_store
        assert "SPXUSD" not in engine.previous_store
        assert "^GSPC" not in engine.market_store


# ---------------------------------------------------------------------------
# ISS-09: scrapers.py WeakSet session release & tv_client.py locked symbols read
# ---------------------------------------------------------------------------
def test_fallback_scraper_all_sessions_uses_weakref_and_releases_dead_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.realtime import scrapers

    class _DummySession:
        def close(self) -> None:
            pass

    monkeypatch.setattr(scrapers, "_create_cffi_session", lambda: _DummySession())

    base_scraper = scrapers._BaseFallbackScraper()
    jp_scraper = scrapers.YahooJPRealtimeScraper()
    assert isinstance(base_scraper._all_sessions, weakref.WeakSet)
    assert isinstance(jp_scraper._all_sessions, weakref.WeakSet)

    refs: list[weakref.ref[Any]] = []

    def touch_in_thread() -> None:
        s1 = base_scraper._get_session()
        s2 = jp_scraper._get_session()
        refs.append(weakref.ref(s1))
        refs.append(weakref.ref(s2))

    t = threading.Thread(target=touch_in_thread)
    t.start()
    t.join(timeout=5)
    del t
    gc.collect()

    assert all(r() is None for r in refs)
    assert len(base_scraper._all_sessions) == 0
    assert len(jp_scraper._all_sessions) == 0


def test_tv_client_connect_reads_symbols_under_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import realtime_engine as realtime_engine_facade
    from services.realtime import tv_client as tv_client_mod
    from services.realtime.tv_client import TradingViewWSClient

    client = TradingViewWSClient(on_update_callback=lambda payload: None)
    observed_counts: list[int] = []
    open_called = threading.Event()

    class FakeWSApp:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.on_open = None

        def run_forever(self, **kwargs: Any) -> None:
            if callable(self.on_open):
                self.on_open(self)
                open_called.set()
            client.running = False

        def close(self) -> None:
            pass

        def send(self, msg: str) -> None:
            pass

    # Rapidly mutate symbols while worker connects/stops to verify no Set changed size error
    errors: list[BaseException] = []

    def mutator() -> None:
        try:
            for i in range(100):
                client.add_symbol(f"NASDAQ:SYM{i}")
                client.remove_symbol(f"NASDAQ:SYM{i}")
        except BaseException as exc:
            errors.append(exc)

    fake_ws_mod = MagicMock()
    fake_ws_mod.WebSocketApp = FakeWSApp
    monkeypatch.setattr(tv_client_mod, "websocket", fake_ws_mod)
    monkeypatch.setattr(realtime_engine_facade, "websocket", fake_ws_mod)
    monkeypatch.setitem(sys.modules, "websocket", fake_ws_mod)

    with client.lock:
        client.symbols.add("NASDAQ:AAPL")
        observed_counts.append(len(client.symbols))
    t = threading.Thread(target=mutator)
    t.start()
    client.start()
    try:
        assert open_called.wait(timeout=5)
    finally:
        t.join(timeout=5)
        client.stop()

    assert not errors
    assert observed_counts == [1]


# ---------------------------------------------------------------------------
# ISS-10: services/stock/provider.py batch concat preservation & epoch=0 merge
# ---------------------------------------------------------------------------
def test_concat_symbol_dataframes_dedupes_and_preserves_valid_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.stock.provider import YFinanceProvider

    idx1 = pd.to_datetime(["2026-01-01", "2026-01-01", "2026-01-02"])
    df1 = pd.DataFrame({"Close": [100.0, 101.0, 102.0]}, index=idx1)
    df1.columns = pd.MultiIndex.from_product([df1.columns, ["AAPL"]])

    idx2 = pd.to_datetime(["2026-01-01", "2026-01-02"])
    df2 = pd.DataFrame({"Close": [200.0, 201.0]}, index=idx2)
    df2.columns = pd.MultiIndex.from_product([df2.columns, ["MSFT"]])

    idx_bad = pd.to_datetime(["2026-01-01", "2026-01-02"])
    df_bad = pd.DataFrame({"Close": [300.0, 301.0]}, index=idx_bad)
    df_bad.columns = pd.MultiIndex.from_product([df_bad.columns, ["BAD"]])

    real_concat = pd.concat

    def flaky_concat(objs: Any, **kwargs: Any) -> pd.DataFrame:
        objs_list = list(objs)
        if any(
            isinstance(obj.columns, pd.MultiIndex)
            and "BAD" in obj.columns.get_level_values(1)
            for obj in objs_list
        ):
            raise pd.errors.InvalidIndexError("incompatible index for BAD")
        return real_concat(objs_list, **kwargs)

    monkeypatch.setattr(pd, "concat", flaky_concat)

    combined = YFinanceProvider._concat_symbol_dataframes([df1, df_bad, df2])
    assert isinstance(combined.columns, pd.MultiIndex)
    syms = set(combined.columns.get_level_values(1))
    assert syms == {"AAPL", "MSFT"}
    assert len(combined) == 2
    assert math.isclose(float(combined[("Close", "AAPL")].iloc[0]), 101.0)
    assert math.isclose(float(combined[("Close", "MSFT")].iloc[1]), 201.0)


def test_merge_quote_into_history_accepts_epoch_zero_timestamp() -> None:
    from services.stock.provider import YFinanceProvider

    provider = YFinanceProvider()
    idx = pd.to_datetime(["1969-12-30", "1969-12-31"])
    df = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [12.0, 12.0],
            "Low": [9.0, 10.0],
            "Close": [11.0, 11.5],
            "Volume": [100, 100],
        },
        index=idx,
    )
    merged = provider._merge_quote_into_history(
        df,
        {"regularMarketPrice": 12.0, "regularMarketTime": 0.0},
        "AAPL",
    )
    expected_date = datetime.fromtimestamp(0.0, UTC).strftime("%Y-%m-%d")
    today_date = datetime.now(UTC).strftime("%Y-%m-%d")
    assert expected_date[:3] == "197"
    assert str(merged.index[-1])[:10] != today_date
    assert math.isclose(float(merged["Close"].iloc[-1]), 12.0)


# ---------------------------------------------------------------------------
# ISS-11: services/market_data_service.py screener enrichment skips empty prices
# ---------------------------------------------------------------------------
def test_build_screener_enrichment_skips_unresolved_zero_price_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import market_data_service

    fake_payload_cache = MagicMock()
    fake_payload_cache.get.return_value = None
    monkeypatch.setattr(app_state, "payload_disk_cache", fake_payload_cache)

    rows = market_data_service.build_screener_enrichment(
        [("DEAD1", "Dead Corp", "us"), ("LIVE1", "Live Corp", "us")],
        full_fetch_symbol="DEAD1",
        fetch_batch_fn=lambda items, **kw: [
            {
                "symbol": "LIVE1",
                "name": "Live Corp",
                "price": 55.0,
                "change": 1.0,
                "change_percent": 1.8,
            },
        ],
        get_info_fn=lambda sym, cache_only=False: {},
    )
    assert "DEAD1" not in rows
    assert rows["LIVE1"]["price"] == 55.0


# ---------------------------------------------------------------------------
# ISS-12: route_helpers.resolve_conversation_scope shared between AI endpoints
# ---------------------------------------------------------------------------
def test_resolve_conversation_scope_unified_precedence_and_validation() -> None:
    from flask import session

    import route_helpers
    from app import app
    from routes import api_analysis
    from routes.stocks import ai_portfolio

    valid_hdr = "HeaderScope_1234567890"
    valid_body = "BodyScope_123456789012"
    valid_qry = "QueryScope_12345678901"
    valid_sess = "SessionScope_123456789"

    # 1. Header takes highest priority even over invalid/other sources
    with app.test_request_context(
        f"/?conversation_scope={valid_qry}",
        method="POST",
        headers={"X-Conversation-Scope": valid_hdr},
        json={"conversation_scope": valid_body},
    ):
        session["mns_analysis_conversation"] = valid_sess
        assert route_helpers.resolve_conversation_scope() == valid_hdr
        assert ai_portfolio._get_conversation_scope({"conversation_scope": valid_body}) == valid_hdr
        assert api_analysis._get_conversation_scope({"conversation_scope": valid_body}) == valid_hdr

    # 2. Explicit data/body beats query and session; session is updated
    with app.test_request_context(
        f"/?conversation_scope={valid_qry}",
        method="POST",
        json={"conversation_scope": "bad"},
    ):
        session["mns_analysis_conversation"] = valid_sess
        assert (
            route_helpers.resolve_conversation_scope({"conversation_scope": valid_body})
            == valid_body
        )
        assert ai_portfolio._get_conversation_scope({"conversation_scope": valid_body}) == valid_body
        assert api_analysis._get_conversation_scope({"conversation_scope": valid_body}) == valid_body
        assert session["mns_analysis_conversation"] == valid_body

    # 3. Query fallback works when header & body absent/invalid
    with app.test_request_context(
        f"/?conversation_scope={valid_qry}",
        method="POST",
        headers={"X-Conversation-Scope": "short"},
    ):
        assert route_helpers.resolve_conversation_scope() == valid_qry
        assert ai_portfolio._get_conversation_scope() == valid_qry
        assert api_analysis._get_conversation_scope() == valid_qry

    # 4. Invalid query/session generates a fresh compliant token (reused via session)
    with app.test_request_context("/?conversation_scope=../bad", method="GET"):
        session["mns_analysis_conversation"] = "too_short"
        generated = route_helpers.resolve_conversation_scope()
        assert route_helpers._CONVERSATION_SCOPE_TOKEN_RE.fullmatch(generated)
        assert generated != "too_short"
        assert ai_portfolio._get_conversation_scope() == generated
        assert api_analysis._get_conversation_scope() == generated
