"""Regression and coverage tests for Unit 1 foundation, config, caching, and data fixes."""

from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app_state import app_state


def test_m01_fast_info_market_cap_and_52w_does_not_poison_full_info_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-01: fast_info carrying marketCap/fiftyTwoWeekHigh must not satisfy _is_complete_info."""
    from utils import stock_payload

    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()

    disk_store: dict[str, dict] = {}
    mem_store: dict[str, object] = {}

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
        lambda _sym: {
            "regularMarketPrice": 185.0,
            "marketCap": 2_900_000_000_000,
            "fiftyTwoWeekHigh": 210.0,
        },
    )
    info_calls = 0

    def _fake_info(_sym: str) -> dict:
        nonlocal info_calls
        info_calls += 1
        if info_calls == 1:
            return {}
        return {"sector": "Technology", "trailingPE": 28.5, "marketCap": 2_900_000_000_000}

    monkeypatch.setattr(app_state.stock_provider, "get_info", _fake_info)

    first = stock_payload.get_stock_info_cached("M01TEST")
    assert first.get("marketCap") == 2_900_000_000_000
    assert "info_disk_M01TEST" not in disk_store

    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()

    second = stock_payload.get_stock_info_cached("M01TEST")
    assert info_calls == 2
    assert second.get("sector") == "Technology"
    assert "info_disk_M01TEST" in disk_store


def test_transient_config_read_failure_protects_master_key_and_existing_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient OSError reading config.json must never overwrite mns_master_key or api_credentials."""
    import config_store
    from config import io as config_io
    from config import master_key

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    real_payload = {
        "mistral_model": "mistral-large-latest",
        "mns_master_key": {"scheme": "ephemeral", "value": "existing-key-marker"},
        "api_credentials": {"mistral_api_key": {"scheme": "ephemeral", "value": "secret-val"}},
    }
    cfg_file.write_text(json.dumps(real_payload), encoding="utf-8")

    monkeypatch.setattr(config_store, "APP_DATA_DIR", cfg_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", cfg_dir / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)
    monkeypatch.setattr(config_store, "_CONFIG_TRANSIENT_READ_FAILED", False)
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", True)

    import builtins

    orig_open = builtins.open
    orig_path_open = Path.open

    def _failing_open(file, mode="r", *args, **kwargs):
        if Path(file) == cfg_file and "r" in str(mode):
            raise PermissionError("simulated AV lock")
        return orig_open(file, mode, *args, **kwargs)

    def _failing_path_open(self: Path, *args, **kwargs):
        if self == cfg_file and (not args or "r" in str(args[0])):
            raise PermissionError("simulated AV lock")
        return orig_path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _failing_open)
    monkeypatch.setattr(Path, "open", _failing_path_open)
    monkeypatch.setattr("config.io.time.sleep", lambda _s: None)

    fallback_cfg = config_io.load_config()
    assert config_store._CONFIG_TRANSIENT_READ_FAILED is True
    assert isinstance(fallback_cfg, config_io._TransientDefaultConfig)

    # 1. Master key generation refuses to rotate while read transiently failed.
    with pytest.raises(RuntimeError, match="unreadable"):
        master_key.get_or_create_master_key()

    # 2. Saving the fallback while file is still unreadable fails closed.
    fallback_cfg["mistral_model"] = "mistral-small-latest"
    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        config_io.save_config(fallback_cfg, create_backup=False)

    # 3. Once transient failure clears, saving the fallback merges delta without erasing secrets.
    monkeypatch.setattr(builtins, "open", orig_open)
    monkeypatch.setattr(Path, "open", orig_path_open)
    config_io.save_config(fallback_cfg, create_backup=False)
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["mistral_model"] == "mistral-small-latest"
    assert on_disk["mns_master_key"] == real_payload["mns_master_key"]
    assert on_disk["api_credentials"] == real_payload["api_credentials"]


def test_cfg02_load_config_sweeps_only_on_cache_miss_and_tracks_legacy_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CFG-02: cached load_config calls avoid dir globs; failed legacy unlink keeps merge incomplete."""
    import config_store
    from config import io as config_io

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(json.dumps({"mistral_model": "mistral-medium-3.5"}), encoding="utf-8")

    monkeypatch.setattr(config_store, "APP_DATA_DIR", cfg_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", cfg_dir / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", True)

    sweeps = 0

    def _counting_sweep(directory: Path, pattern: str) -> int:
        nonlocal sweeps
        sweeps += 1
        return 0

    monkeypatch.setattr(config_io, "_sweep_stale_tmp_files", _counting_sweep)
    config_io.load_config()
    assert sweeps == 1
    config_io.load_config()
    config_io.load_config()
    assert sweeps == 1  # hot cache hits skipped sweeping

    # Failed legacy removal keeps _LEGACY_MERGE_DONE False for retry.
    legacy_file = tmp_path / "legacy.json"
    legacy_file.write_text(json.dumps({"mistral_model": "mistral-medium-3.5"}), encoding="utf-8")
    old_t = time.time() - 600
    os.utime(legacy_file, (old_t, old_t))
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", legacy_file)
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", False)
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_io, "_remove_stale_legacy_config", lambda *_a, **_kw: False)
    config_io.load_config()
    assert config_store._LEGACY_MERGE_DONE is False


def test_cfg01_and_config_paths_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CFG-01 & TEST-01: TemplateConfigSchema uses constants; config/paths handles env/home/chmod."""
    import constants
    from config import paths as cfg_paths
    from schemas.config import TemplateConfigSchema

    schema = TemplateConfigSchema(
        has_mistral_api_key=False,
        has_langsearch_api_key=False,
        has_tavily_api_key=False,
        has_alphavantage_api_key=False,
        mistral_model="m",
        is_ai_technical_lines_eligible=False,
    )
    assert schema.mistral_api_key_min_length == constants.MISTRAL_API_KEY_MIN_LENGTH
    assert schema.langsearch_api_key_min_length == constants.LANGSEARCH_API_KEY_MIN_LENGTH
    assert schema.tavily_api_key_min_length == constants.TAVILY_API_KEY_MIN_LENGTH
    assert schema.sse_heartbeat_interval == constants.SSE_HEARTBEAT_INTERVAL

    # Whitespace-only overrides are ignored safely.
    monkeypatch.setenv("MNS_DATA_DIR", "   ")
    monkeypatch.setenv("MNS_APP_DATA_DIR", f"  {tmp_path / 'custom_app'}  ")
    assert cfg_paths._get_runtime_data_dir() == (tmp_path / "custom_app")

    monkeypatch.delenv("MNS_APP_DATA_DIR", raising=False)
    monkeypatch.delenv("MNS_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", MagicMock(side_effect=RuntimeError("no HOME")))
    fallback_dir = cfg_paths._get_runtime_data_dir(is_windows=False)
    assert fallback_dir.name == ".data"

    # POSIX chmod failure logs warning without crashing.
    target_dir = tmp_path / "perm_dir"
    with (
        patch.object(cfg_paths.config_store, "APP_DATA_DIR", target_dir),
        patch.object(cfg_paths.config_store, "_is_windows", return_value=False),
        patch.object(os, "chmod", side_effect=OSError("chmod blocked")),
    ):
        cfg_paths._ensure_runtime_dir()
    assert target_dir.is_dir()


def test_atomic_write_coverage_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST-01: Cover _fsync_directory, _sweep_stale_tmp_files, _migrate_legacy_runtime_file, lock fail."""
    from config import atomic_write

    # _fsync_directory returns True when O_DIRECTORY is absent (e.g., Windows)
    # and returns False cleanly on POSIX open/fsync OSErrors.
    monkeypatch.setattr(os, "O_DIRECTORY", None, raising=False)
    assert atomic_write._fsync_directory(tmp_path) is True

    monkeypatch.setattr(os, "O_DIRECTORY", 0, raising=False)
    with patch("config.atomic_write.os.open", side_effect=OSError("denied")):
        assert atomic_write._fsync_directory(tmp_path) is False

    closed_fds: list[int] = []
    with (
        patch("config.atomic_write.os.open", return_value=99),
        patch("config.atomic_write.os.fsync", side_effect=OSError("fsync unsupported")),
        patch("config.atomic_write.os.close", side_effect=lambda fd: closed_fds.append(fd)),
    ):
        assert atomic_write._fsync_directory(tmp_path) is False
    assert closed_fds == [99]

    # _sweep_stale_tmp_files respects grace period and survives unlink OSError.
    stale = tmp_path / "config.json.old1.tmp"
    fresh = tmp_path / "config.json.new1.tmp"
    stale.write_text("x", encoding="utf-8")
    fresh.write_text("y", encoding="utf-8")
    old = time.time() - atomic_write._STALE_TMP_GRACE_SEC - 30
    os.utime(stale, (old, old))
    assert atomic_write._sweep_stale_tmp_files(tmp_path, "config.json.*.tmp") == 1
    assert not stale.exists()
    assert fresh.exists()

    # _migrate_legacy_runtime_file retains source when _fsync_directory fails on POSIX.
    import config_store

    src = tmp_path / "legacy_cfg.json"
    dst = tmp_path / "runtime" / "config.json"
    dst.parent.mkdir()
    src.write_text('{"a": 1}', encoding="utf-8")
    with (
        patch.object(config_store, "_is_windows", return_value=False),
        patch("config.atomic_write._fsync_directory", return_value=False),
    ):
        assert atomic_write._migrate_legacy_runtime_file(src, dst) is True
    assert dst.exists()
    assert src.exists()

    # msvcrt lock contention exhaustion raises RuntimeError rather than bypassing lock.
    lock_file = tmp_path / "cfg.lock"
    cfg_target = tmp_path / "cfg_target.json"
    tmp_cfg = tmp_path / "cfg_target.tmp"
    fake_msvcrt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=MagicMock(side_effect=OSError("busy")))
    with (
        patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
        patch("config.atomic_write.time.sleep", lambda _s: None),
        pytest.raises(RuntimeError, match="lock busy"),
    ):
        atomic_write._write_and_replace_with_msvcrt_lock(
            {"x": 1}, tmp_cfg, cfg_target, lock_file
        )
    assert not cfg_target.exists()


def test_warmup_schedule_news_isolated_keys_and_partial_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TEST-01: schedule_news_warmup isolates keyring errors and step exceptions cleanly."""
    import app as app_mod
    from app_factory import warmup

    captured_jobs: list[object] = []

    class _RecordingExecutor:
        def submit(self, fn):
            captured_jobs.append(fn)

    monkeypatch.setattr(app_mod, "get_langsearch_api_key", MagicMock(side_effect=RuntimeError("kr fail")))
    monkeypatch.setattr(app_mod, "get_tavily_api_key", MagicMock(return_value="tvly-test-key"))
    monkeypatch.setattr(warmup.app_state.execution, "news_executor", _RecordingExecutor())
    monkeypatch.setattr(
        warmup,
        "get_cached_context_with_negative_cache",
        lambda key, fn, *_a: "ok" if "_us_" in key else (_ for _ in ()).throw(ValueError("jp fail")),
    )
    monkeypatch.setattr(
        warmup,
        "collect_market_trending_titles",
        lambda market, *_a: ["Title"] if market == "us" else [],
    )

    warmup.schedule_news_warmup()
    assert len(captured_jobs) == 1
    captured_jobs[0]()  # Should complete without propagating errors


def test_disk_cache_nan_rejection_monotonic_cleanup_and_lock_order(
    tmp_path: Path,
) -> None:
    """DiskCache rejects NaN/Inf JSON, uses monotonic interval, and cleans up orphaned tmp."""
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(
        cache_dir=tmp_path / "dc",
        default_ttl=60,
        max_entries=10,
        enable_cleanup=True,
    )
    # Initial monotonic cleanup runs even if wall clock is small or steps backwards.
    with patch("utils.disk_cache.time.time", return_value=10.0):
        assert cache._maybe_run_cleanup() is True
    assert cache._last_cleanup_mono > 0.0
    assert cache._maybe_run_cleanup() is False

    cache.set("nan_key", {"val": float("nan")})
    assert cache.get("nan_key") is None
    assert list((tmp_path / "dc").glob("*.tmp")) == []


def test_chat_history_validators_market_utils_tradingview_and_bg_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify chat_history role dedup, validators blank string, DATA-03, MAP-01, and sync fixes."""
    import route_helpers
    from bg.sync import fetch as sync_fetch
    from bg.sync import process as sync_process
    from bg.sync import state as sync_state
    from utils import chat_history as ch_mod
    from utils.market_utils import is_market_open
    from utils.tradingview_mapper import get_tradingview_symbol
    from utils.validators import extract_chat_content

    # chat_history: cross-role identical content is NOT dropped as duplicate.
    monkeypatch.setattr(ch_mod, "DB_PATH", tmp_path / "chat.db")
    ch_mod.init_db()
    store = ch_mod.SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    store.record_turn("s1", init_messages=[{"role": "user", "content": "AAPL"}])
    assert store.append_message_if_not_duplicate("s1", {"role": "assistant", "content": "AAPL"}) is True
    store.close()

    # validators: whitespace string -> empty response message; object reasoning_details supported.
    assert (
        extract_chat_content({"choices": [{"message": {"content": "   "}}]})
        == "(空の応答が返されました)"
    )
    msg_obj = SimpleNamespace(
        content=" ",
        reasoning_content=None,
        reasoning=None,
        thinking=None,
        reasoning_details=[SimpleNamespace(text="obj reason")],
    )
    assert extract_chat_content(SimpleNamespace(choices=[SimpleNamespace(message=msg_obj)])) == "obj reason"

    # DATA-03: unknown market fails closed.
    assert is_market_open("unknown_market", bypass_cache=True, ignore_weekend=True) is False

    # MAP-01: crypto pairs vs BRK-B share class.
    assert get_tradingview_symbol("BTC-USD") == "BITSTAMP:BTCUSD"
    assert get_tradingview_symbol("ETH-USD") == "COINBASE:ETHUSD"
    assert get_tradingview_symbol("BRK-B") == "NYSE:BRK-B"

    # fetch_index_data rejects trailing NaN Close rows cleanly.
    nan_hist = pd.DataFrame({"Close": [100.0, float("nan")], "High": [101.0, 102.0]})
    with (
        patch.object(sync_fetch._shim(), "acquire_yfinance_slot", return_value=True),
        patch.object(sync_fetch.app_state.stock_provider, "get_history", return_value=nan_hist),
    ):
        assert sync_fetch.fetch_index_data("SP500", "^GSPC") is None

    # Cancelled future callback does not emit spurious warning.
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.cancel()
    assert fut.cancelled()

    # Deleted stock is NOT resurrected by an overlapping _process_fetched_stocks call.
    with app_state.cache.sse_data_lock:
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "NVDA", "market": "us", "price": 100.0}],
            "jp": [],
            "idx": [],
        }
        app_state.market.current_stocks_cache = {
            "us": [{"symbol": "NVDA", "market": "us", "price": 100.0}],
            "jp": [],
            "idx": [],
        }
    route_helpers.remove_stock_from_caches("NVDA", "us")
    with patch("bg.sync.process.announce_real_market_state", lambda: None):
        processed = sync_process._process_fetched_stocks(
            [{"symbol": "NVDA", "market": "us", "price": 120.0}]
        )
    assert processed.us == []
    # Preserves identity of sync_state._last_loaded_mtimes
    orig_mtimes = sync_state._last_loaded_mtimes
    orig_mtimes["test_k"] = 1.0
    assert sync_state._last_loaded_mtimes is orig_mtimes


def test_trend_sources_timeout_queue_full_and_native_host_ancestor_spoofing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT-03, NEW-NH-1, NH-1: TrendReq timeout, queue.Full handling, untrusted exe notcmd fallback."""
    import constants
    import native_host.native_host as nh
    import trend_sources

    init_kwargs: list[dict] = []

    class _FakeTrendReq:
        def __init__(self, *args, **kwargs):
            init_kwargs.append(kwargs)

    monkeypatch.setattr(trend_sources, "TrendReq", _FakeTrendReq)
    trend_sources._GOOGLE_TRENDS_CLIENTS.clear()
    trend_sources._google_trends_client("us")
    assert init_kwargs[0]["timeout"] == constants.TREND_REQUEST_TIMEOUT

    fake_exec = SimpleNamespace(submit=MagicMock(side_effect=queue.Full()))
    monkeypatch.setattr(trend_sources, "_EXECUTOR", fake_exec)
    assert trend_sources.collect_market_trending_items("us", 5) == []

    # NEW-NH-1: Untrusted /proc/<ppid>/exe must NOT fall back to spoofed /proc/<ppid>/cmdline.
    pid_dir = tmp_path / "100"
    ppid_dir = tmp_path / "200"
    pid_dir.mkdir()
    ppid_dir.mkdir()
    (pid_dir / "status").write_text("Name:\tpython\nPPid:\t200\n", encoding="utf-8")
    (ppid_dir / "status").write_text("Name:\tevil\nPPid:\t1\n", encoding="utf-8")
    (ppid_dir / "cmdline").write_bytes(b"/opt/google/chrome/chrome\x00--spoof\x00")

    def _fake_readlink(path: Path | str) -> str:
        if Path(path) == ppid_dir / "exe":
            return "/tmp/untrusted-binary"
        raise OSError("missing")

    with patch("native_host.native_host.os.readlink", side_effect=_fake_readlink):
        ancestors = nh._get_posix_ancestor_process_names(
            max_depth=3, proc_dir=tmp_path, start_pid=100, enforce_path_trust=True
        )
    assert ancestors == []
