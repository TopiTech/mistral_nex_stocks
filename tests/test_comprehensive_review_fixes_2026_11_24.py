"""Regression, edge-case, and concurrency tests for the 2026-11-24 full-repo review."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import time
import types
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_support import run_node_script

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. MarketState.removed_watchlist_symbols concurrency
# ---------------------------------------------------------------------------
def test_market_state_removed_watchlist_thread_safety() -> None:
    from market_state import MarketDataState

    state = MarketDataState()
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def _writer(idx: int) -> None:
        try:
            barrier.wait(timeout=5)
            for step in range(200):
                sym = f"72{idx}{step % 10}.T"
                state.mark_watchlist_removed(sym, "jp")
                if step % 2 == 0:
                    state.clear_watchlist_removed(sym, "jp")
        except BaseException as exc:  # pragma: no cover - captured for assertion
            errors.append(exc)

    def _reader() -> None:
        try:
            barrier.wait(timeout=5)
            for step in range(400):
                state.is_watchlist_removed(f"720{step % 10}", "jp")
                state.is_watchlist_removed("AAPL", "us")
        except BaseException as exc:  # pragma: no cover - captured for assertion
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(3)]
    threads.extend(threading.Thread(target=_reader) for _ in range(3))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors


# ---------------------------------------------------------------------------
# 2. AIState._lookup_or_evict_locked infinite-spin & exception guards
# ---------------------------------------------------------------------------
def test_ai_state_lookup_or_evict_breaks_on_failing_popitem() -> None:
    from collections import deque

    from cachetools import LRUCache

    from ai_state import AIState

    class _BrokenPopPool(LRUCache):
        def popitem(self):
            raise RuntimeError("corrupted internal node")

    pool: _BrokenPopPool[str, object] = _BrokenPopPool(maxsize=1)
    pool["k1"] = object()
    retired: deque[tuple[float, object]] = deque()

    res = AIState._lookup_or_evict_locked(
        pool, retired, "missing-key", default_maxsize=1, label="TestBrokenPool"
    )
    assert res is None


def test_ai_state_lookup_or_evict_continues_when_close_probe_fails() -> None:
    from collections import deque

    from cachetools import LRUCache

    from ai_state import AIState

    class _UnreadableClient:
        @property
        def _closed(self) -> bool:
            raise RuntimeError("client state unavailable")

    pool: LRUCache[str, object] = LRUCache(maxsize=2)
    pool["old1"] = _UnreadableClient()
    pool["old2"] = object()
    retired: deque[tuple[float, object]] = deque()

    res = AIState._lookup_or_evict_locked(
        pool, retired, "new-key", default_maxsize=2, label="TestUnreadable"
    )
    assert res is None
    assert len(pool) == 1


# ---------------------------------------------------------------------------
# 3. YFinanceSessionManager closed-session retry skips duplicate post-processing
# ---------------------------------------------------------------------------
def test_closed_session_retry_runs_handle_block_and_timestamp_once() -> None:
    from session_manager import YFinanceSessionManager

    YFinanceSessionManager._reset_for_testing()
    mgr = YFinanceSessionManager()
    block_calls: list[int] = []
    timestamp_sessions: list[int] = []

    class _FakeResp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _ClosingOnceSession:
        def __init__(self, fail_closed: bool) -> None:
            self.fail_closed = fail_closed
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}
            self.closed = False

        def request(self, *_args, **_kwargs):
            if self.fail_closed:
                raise RuntimeError("Session is closed")
            return _FakeResp(429)

        def close(self) -> None:
            self.closed = True

    s_dead = _ClosingOnceSession(fail_closed=True)
    s_fresh = _ClosingOnceSession(fail_closed=False)
    created = [s_dead, s_fresh]

    with (
        patch.object(mgr, "_is_shutting_down", return_value=False),
        patch.object(mgr, "_compute_wait", return_value=0.0),
        patch.object(
            mgr,
            "_handle_block",
            side_effect=lambda code, _resp: block_calls.append(code),
        ),
        patch.object(
            mgr,
            "_update_session_timestamp",
            side_effect=lambda s: timestamp_sessions.append(id(s)),
        ),
    ):
        # Swap underlying session creator so first get_session builds s_dead wrapper,
        # and retry builds s_fresh wrapper.
        with (
            patch("session_manager.CURL_CFFI_AVAILABLE", True),
            patch(
                "session_manager.curl_requests",
                SimpleNamespace(Session=lambda **_kw: created.pop(0)),
            ),
        ):
            first = mgr.get_session()
            resp = first.request("GET", "https://example.test")

    assert resp.status_code == 429
    assert block_calls == [429]
    assert timestamp_sessions == [id(s_fresh)]


# ---------------------------------------------------------------------------
# 4. check_admin_token UTF-8 / non-ASCII / surrogate safety
# ---------------------------------------------------------------------------
def test_check_admin_token_non_ascii_and_surrogate_safe() -> None:
    from utils.networking import check_admin_token

    with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": "secret-ascii-token"}, clear=False):
        assert check_admin_token("非ASCIIトークン") is False
        assert check_admin_token("\ud800") is False
        assert check_admin_token(None) is False

    with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": "秘密トークン123"}, clear=False):
        assert check_admin_token("秘密トークン123") is True
        assert check_admin_token("秘密トークン999") is False


# ---------------------------------------------------------------------------
# 5. Windows lock cleanup always closes FD even if os.lseek fails
# ---------------------------------------------------------------------------
def test_disk_cache_process_lock_closes_fd_when_unlock_lseek_fails(tmp_path: Path) -> None:
    from utils import disk_cache as dc_mod

    cache = dc_mod.StockDiskCache(cache_dir=tmp_path / "dc", max_entries=8, default_ttl=60)
    fake_fd = 4242
    closed_fds: list[int] = []
    lseek_calls = 0

    def _mock_lseek(fd: int, pos: int, how: int) -> int:
        nonlocal lseek_calls
        lseek_calls += 1
        # Fail on the unlock lseek inside finally (3rd+ lseek call)
        if lseek_calls >= 2:
            raise OSError("bad file descriptor during unlock lseek")
        return 0

    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_NBLCK = 2  # type: ignore[attr-defined]
    fake_msvcrt.LK_UNLCK = 0  # type: ignore[attr-defined]
    fake_msvcrt.locking = MagicMock()  # type: ignore[attr-defined]

    with (
        patch.object(dc_mod.os, "name", "nt"),
        patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
        patch.object(dc_mod.os, "open", return_value=fake_fd),
        patch.object(dc_mod.os, "fstat", return_value=SimpleNamespace(st_size=1)),
        patch.object(dc_mod.os, "lseek", side_effect=_mock_lseek),
        patch.object(dc_mod.os, "close", side_effect=lambda fd: closed_fds.append(fd)),
    ):
        with cache._process_lock():
            pass

    assert closed_fds == [fake_fd]


def test_start_backend_startup_lock_closes_fd_when_unlock_lseek_fails(tmp_path: Path) -> None:
    from native_host import start_backend as sb_mod

    fake_fd = 5151
    closed_fds: list[int] = []
    lseek_calls = 0

    def _mock_lseek(fd: int, pos: int, how: int) -> int:
        nonlocal lseek_calls
        lseek_calls += 1
        if lseek_calls >= 2:
            raise OSError("bad file descriptor during unlock lseek")
        return 0

    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_LOCK = 1  # type: ignore[attr-defined]
    fake_msvcrt.LK_UNLCK = 0  # type: ignore[attr-defined]
    fake_msvcrt.locking = MagicMock()  # type: ignore[attr-defined]

    with (
        patch.object(sb_mod, "STARTUP_LOCK_FILE", tmp_path / ".backend.start.lock"),
        patch.object(sb_mod.os, "name", "nt"),
        patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
        patch.object(sb_mod.os, "open", return_value=fake_fd),
        patch.object(sb_mod.os, "fstat", return_value=SimpleNamespace(st_size=1)),
        patch.object(sb_mod.os, "lseek", side_effect=_mock_lseek),
        patch.object(sb_mod.os, "close", side_effect=lambda fd: closed_fds.append(fd)),
    ):
        with sb_mod._startup_lock():
            pass

    assert closed_fds == [fake_fd]


# ---------------------------------------------------------------------------
# 6. Restrictive rotating log handlers pre-create with 0o600 on POSIX
# ---------------------------------------------------------------------------
def _assert_posix_precreate_0600(handler_cls: type[logging.handlers.RotatingFileHandler]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "test_restrict.log"
        observed_events: list[str] = []
        real_open = os.open
        real_close = os.close
        pre_fd = 98765

        def _spy_os_open(path, flags, mode=0o777, *args, **kwargs):
            if Path(path) == log_path and (mode & 0o777) == 0o600:
                observed_events.append(f"os.open:{oct(mode & 0o777)}")
                return pre_fd
            return real_open(path, flags, mode, *args, **kwargs)

        def _spy_os_close(fd: int) -> None:
            if fd == pre_fd:
                observed_events.append("os.close")
                return None
            return real_close(fd)

        def _spy_chmod(path, mode, *args, **kwargs):
            observed_events.append(f"os.chmod:{oct(int(mode) & 0o777)}")

        fake_os = SimpleNamespace(
            name="posix",
            O_WRONLY=getattr(os, "O_WRONLY", 1),
            O_CREAT=getattr(os, "O_CREAT", 0o100),
            O_EXCL=getattr(os, "O_EXCL", 0o200),
            open=_spy_os_open,
            close=_spy_os_close,
            chmod=_spy_chmod,
        )
        # Patch os on the module owning handler_cls
        mod = sys.modules[handler_cls.__module__]
        handler = None
        with patch.object(mod, "os", fake_os):
            handler = handler_cls(str(log_path), maxBytes=1024, backupCount=1, encoding="utf-8")
            handler.close()

        assert observed_events[:3] == ["os.open:0o600", "os.close", "os.chmod:0o600"]


def test_restrictive_rotating_log_handlers_precreate_0600() -> None:
    import logging_config
    import native_host.native_host as nh_mod
    from native_host import start_backend

    _assert_posix_precreate_0600(logging_config._RestrictiveRotatingFileHandler)
    _assert_posix_precreate_0600(start_backend._RestrictiveRotatingFileHandler)
    _assert_posix_precreate_0600(nh_mod._RestrictiveRotatingFileHandler)


# ---------------------------------------------------------------------------
# 7. Stale sync recovery via schedule_sync_all_stocks_now triggers lock rotation
# ---------------------------------------------------------------------------
def test_schedule_sync_stale_recovery_forces_takeover_and_rotates_wedged_lock() -> None:
    import app_bg
    from app_state import app_state
    from bg.sync import control as sync_control
    from bg.sync import state as sync_state

    wedged_lock = threading.Lock()
    wedged_lock.acquire()
    fetch_calls: list[int] = []

    orig_schedule = app_bg.schedule_sync_all_stocks_now
    orig_bg_lock = app_bg._sync_execution_lock
    orig_state_lock = sync_state._sync_execution_lock

    @sync_state._register_app_bg_delegate
    def _real_schedule(force: bool = False) -> bool:
        return sync_control.schedule_sync_all_stocks_now(
            force=force, facade_globals=app_bg.__dict__
        )

    try:
        app_bg.schedule_sync_all_stocks_now = _real_schedule
        app_bg._sync_execution_lock = wedged_lock
        sync_state._sync_execution_lock = wedged_lock

        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        app_bg._sync_start_time = time.monotonic() - (app_bg.SYNC_STALE_TIMEOUT_SEC + 30.0)
        sync_state._set_sync_start_time(app_bg._sync_start_time)

        with (
            patch.object(app_bg, "SYNC_STALE_LOCK_WAIT_SEC", 0.02),
            patch.object(sync_control, "SYNC_STALE_LOCK_WAIT_SEC", 0.02),
            patch.object(app_bg, "_run_scheduled_sync_job", lambda: None),
            patch.object(sync_control, "_run_scheduled_sync_job", lambda: None),
            patch.object(app_bg, "fetch_index_data", return_value=None),
            patch.object(app_bg, "_update_indices_data", return_value=None),
            patch("routes.stocks.common._fetch_heatmap_cached", return_value=None),
            patch.object(
                app_bg,
                "fetch_stocks_batch",
                side_effect=lambda items, **_kw: fetch_calls.append(len(items)) or [],
            ),
        ):
            scheduled = app_bg.schedule_sync_all_stocks_now(force=True)
            assert scheduled is True
            with app_state.market.is_syncing_lock:
                assert app_state.market.is_syncing is False

            # Old worker still holds wedged_lock: direct sync_all_stocks_now MUST
            # detect pending stale takeover and rotate the lock instead of skipping.
            app_bg.sync_all_stocks_now(force_fetch=True)

            rotated_lock = app_bg._sync_execution_lock
            assert rotated_lock is not wedged_lock
            assert sync_state._sync_execution_lock is rotated_lock

            # Subsequent sync immediately acquires rotated_lock and proceeds.
            app_bg.sync_all_stocks_now(force_fetch=True)

        assert len(fetch_calls) >= 1
    finally:
        app_bg.schedule_sync_all_stocks_now = orig_schedule
        app_bg._sync_execution_lock = orig_bg_lock
        sync_state._sync_execution_lock = orig_state_lock
        sync_state._set_stale_takeover_pending(False)
        if wedged_lock.locked():
            wedged_lock.release()
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False
        app_bg._sync_start_time = 0.0
        sync_state._set_sync_start_time(0.0)
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
            app_state.market.sync_pending = False
            app_state.market.sync_forced = False


# ---------------------------------------------------------------------------
# 8. Null / malformed JSON payloads in trend_sources and fallback_provider
# ---------------------------------------------------------------------------
def test_trend_sources_and_fallback_provider_tolerate_null_containers() -> None:
    import trend_sources
    from services.fallback_provider import AlphaVantageProvider, YahooWebScraperProvider

    with patch.object(
        trend_sources,
        "_request_json_retry_on_429",
        return_value={"data": None},
    ):
        assert trend_sources.collect_reddit_hot_items("us", limit_per_subreddit=2) == []
        assert trend_sources.collect_reddit_search_items(["NVDA"], "us", limit_per_query=2) == []

    with patch.object(
        trend_sources,
        "_request_json_retry_on_429",
        return_value={"data": {"children": [None, {"data": None}, {"data": {"title": None}}]}},
    ):
        assert trend_sources.collect_reddit_hot_items("us", limit_per_subreddit=5) == []

    with patch.object(
        trend_sources,
        "_request_json",
        side_effect=[{"query": None}, {"query": {"search": None}}, {"query": {"search": [None]}}],
    ):
        assert trend_sources.collect_wikipedia_search_items(["AAPL", "MSFT", "TSLA"], "us") == []

    # Fallback provider: null context/pageProps falls through cleanly to marker parsing
    provider = YahooWebScraperProvider()
    marker_html = (
        '<script>root.App.main = {"context": null, "props": {"pageProps": null}}; (function(){})();</script>'
        '<fin-streamer data-testid="qsp-price" data-value="189.50">189.50</fin-streamer>'
    )
    mock_resp = MagicMock(status_code=200, text=marker_html, content=marker_html.encode("utf-8"))
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    with (
        patch("services.fallback_provider._is_scraper_blocked", return_value=False),
        patch.object(provider, "_get_client", return_value=(mock_client, True)),
    ):
        res = provider.get_latest_quote("AAPL")

    assert res is not None
    assert res["symbol"] == "AAPL"
    assert res["regularMarketPrice"] == 189.50

    av_provider = AlphaVantageProvider()
    av_resp = MagicMock(status_code=200)
    av_resp.json.return_value = {"Global Quote": None}
    with (
        patch("services.fallback_provider.get_alphavantage_api_key", return_value="TESTKEY"),
        patch("requests.get", return_value=av_resp),
    ):
        assert av_provider.get_latest_quote("AAPL") is None


# ---------------------------------------------------------------------------
# 9. Yahoo JP comma-formatted quote strings
# ---------------------------------------------------------------------------
def test_yahoo_extract_quote_field_preserves_comma_formatted_strings() -> None:
    from services.realtime.scrapers import YahooJPRealtimeScraper

    html = '{"quote":{"price":"2,500.5","priceChange":"+1,250","volume":98765,"empty":""}}'
    assert YahooJPRealtimeScraper._extract_quote_field(html, "price") == "2,500.5"
    assert YahooJPRealtimeScraper._extract_quote_field(html, "priceChange") == "+1,250"
    assert YahooJPRealtimeScraper._extract_quote_field(html, "volume") == "98765"
    assert YahooJPRealtimeScraper._extract_quote_field(html, "empty") is None


# ---------------------------------------------------------------------------
# 10. api_heatmap releases inflight claim when submitted Future is cancelled
# ---------------------------------------------------------------------------
def test_api_heatmap_cancelled_future_clears_inflight_state(client) -> None:
    from app_state import app_state
    from routes.stocks import views as views_mod

    cache_key = "heatmap_us"
    cancelled_future: Future[object] = Future()
    cancelled_future.cancel()

    with app_state.heatmap_fetch_lock:
        app_state.heatmap_fetch_inflight.discard(cache_key)
        app_state.heatmap_fetch_start_times.pop(cache_key, None)

    try:
        with (
            patch.object(views_mod, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(views_mod, "_get_cached_value_if_present", return_value=(False, None)),
            patch.object(app_state.payload_disk_cache, "get", return_value=None),
            patch.object(
                app_state.execution.data_executor,
                "submit",
                return_value=cancelled_future,
            ),
        ):
            resp = client.get("/api/heatmap?market=us")

        assert resp.status_code == 200
        with app_state.heatmap_fetch_lock:
            assert cache_key not in app_state.heatmap_fetch_inflight
            assert cache_key not in app_state.heatmap_fetch_start_times
    finally:
        with app_state.heatmap_fetch_lock:
            app_state.heatmap_fetch_inflight.discard(cache_key)
            app_state.heatmap_fetch_start_times.pop(cache_key, None)


# ---------------------------------------------------------------------------
# 11. Frontend lifecycle, AbortError recovery, and market-targeted deltas
# ---------------------------------------------------------------------------
def test_setup_js_restores_buttons_on_abort_and_pageshow() -> None:
    script = r"""
const fs = require("fs");
const assert = require("assert");
const vm = require("vm");

const makeEl = (id, init = {}) => ({
  id,
  disabled: false,
  hidden: false,
  textContent: init.textContent || "",
  value: init.value || "",
  className: "",
  dataset: {},
  style: {},
  classList: { toggle() {}, add() {}, remove() {} },
  addEventListener() {},
  focus() {},
  setAttribute() {},
  querySelector() { return null; },
});

const elements = new Map([
  ["previewBtn", makeEl("previewBtn", { textContent: "プレビュー" })],
  ["saveBtn", makeEl("saveBtn", { textContent: "保存して開始" })],
  ["apiKey", makeEl("apiKey", { value: "valid-mistral-key-1234567890" })],
  ["langsearchApiKey", makeEl("langsearchApiKey", { value: "" })],
  ["tavilyApiKey", makeEl("tavilyApiKey", { value: "" })],
  ["errorMsg", makeEl("errorMsg")],
]);

const listeners = new Map();
const abortErr = new Error("Aborted");
abortErr.name = "AbortError";

const ctx = {
  console,
  setTimeout,
  clearTimeout,
  AbortController: globalThis.AbortController,
  APP_CONFIG: { api_mode: "chat", ai_provider: "mistral" },
  window: {
    location: { href: "http://127.0.0.1:5000/setup" },
    addEventListener(type, fn) {
      listeners.set(type, fn);
    },
  },
  document: {
    getElementById: (id) => elements.get(id) || null,
    querySelector: (sel) => {
      if (sel.includes("aiProvider")) return { value: "mistral" };
      if (sel.includes("apiMode")) return { value: "chat" };
      return null;
    },
    querySelectorAll: () => [],
    addEventListener() {},
  },
  csrfFetch: () => Promise.reject(abortErr),
  storeCredentials: () => Promise.reject(abortErr),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/setup.js", "utf8"), ctx, { filename: "setup.js" });

(async () => {
  await ctx.startPreviewMode();
  assert.strictEqual(elements.get("previewBtn").disabled, false);

  await ctx.saveKey();
  assert.strictEqual(elements.get("saveBtn").disabled, false);
  assert.strictEqual(elements.get("saveBtn").textContent, "保存して開始");

  elements.get("previewBtn").disabled = true;
  elements.get("saveBtn").disabled = true;
  elements.get("saveBtn").textContent = "保存中...";
  listeners.get("pageshow")({ persisted: true });
  assert.strictEqual(elements.get("previewBtn").disabled, false);
  assert.strictEqual(elements.get("saveBtn").disabled, false);
  assert.strictEqual(elements.get("saveBtn").textContent, "保存して開始");
  process.stdout.write("setup_ok");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
"""
    res = run_node_script(script, context="setup.js AbortError and pageshow")
    assert res.stdout.strip() == "setup_ok"


def test_pagehide_lifecycle_hooks_registered_on_chart_ai_portfolio_and_index_main() -> None:
    script = r"""
const fs = require("fs");
const assert = require("assert");
const vm = require("vm");

// 1. ai_portfolio.js registers both beforeunload & pagehide
{
  const listeners = new Map();
  let aborted = 0;
  class FakeAC {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; aborted += 1; }
  }
  const ctx = {
    console,
    setTimeout,
    clearTimeout,
    AbortController: FakeAC,
    document: { getElementById: () => null, addEventListener() {}, querySelectorAll: () => [] },
    window: { addEventListener: (t, fn) => listeners.set(t, fn), navigator: {} },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("static/js/ai_portfolio.js", "utf8"), ctx, { filename: "ai_portfolio.js" });
  assert.strictEqual(typeof listeners.get("beforeunload"), "function");
  assert.strictEqual(typeof listeners.get("pagehide"), "function");
  listeners.get("pagehide")();
  assert.ok(aborted >= 0);
}

// 2. chart.js registers both beforeunload & pagehide
{
  const listeners = new Map();
  const saved = new Map();
  const ctx = {
    console,
    setTimeout,
    clearTimeout,
    localStorage: {
      getItem: (k) => saved.get(k) ?? null,
      setItem: (k, v) => saved.set(k, v),
    },
    document: { getElementById: () => null, addEventListener() {} },
    window: { addEventListener: (t, fn) => listeners.set(t, fn) },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("static/js/chart.js", "utf8"), ctx, { filename: "chart.js" });
  assert.strictEqual(typeof listeners.get("beforeunload"), "function");
  assert.strictEqual(typeof listeners.get("pagehide"), "function");
  listeners.get("pagehide")();
}

// 3. index_main.js registers both beforeunload & pagehide and bumps fetch generation on pagehide
{
  const listeners = new Map();
  let aborted = false;
  class FakeAC {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; aborted = true; }
  }
  const ctx = {
    console,
    setTimeout,
    clearTimeout,
    AbortController: FakeAC,
    DOM: { get: () => null },
    document: { getElementById: () => null, addEventListener() {}, querySelectorAll: () => [], hidden: false },
    window: { addEventListener: (t, fn) => listeners.set(t, fn), Notification: null },
    localStorage: { getItem: () => null, setItem() {} },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("static/js/index_main.js", "utf8"), ctx, { filename: "index_main.js" });
  assert.strictEqual(typeof listeners.get("beforeunload"), "function");
  assert.strictEqual(typeof listeners.get("pagehide"), "function");
  vm.runInContext("fetchInitialStocksAbortController = new AbortController();", ctx);
  listeners.get("pagehide")();
  assert.strictEqual(aborted, true);
}

process.stdout.write("pagehide_ok");
"""
    res = run_node_script(script, context="pagehide lifecycle parity")
    assert res.stdout.strip() == "pagehide_ok"


def test_realtime_client_prefers_explicit_market_to_avoid_cross_market_collision() -> None:
    script = r"""
const fs = require("fs");
const assert = require("assert");
const vm = require("vm");

const win = {
  state: {
    stocks: {
      us: [{ symbol: "TEST", market: "us", price: 100, change: 1, change_percent: 1.0, previous_close: 99, pts_price: null }],
      jp: [],
      idx: [{ symbol: "TEST", market: "idx", price: 2000, change: 10, change_percent: 0.5, previous_close: 1990, pts_price: null }],
    },
  },
  addEventListener() {},
  requestAnimationFrame: () => 1,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  updateExistingCard: () => {},
};
const ctx = {
  console,
  document: {
    readyState: "complete",
    hidden: true,
    addEventListener() {},
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  },
  window: win,
  setTimeout,
  clearTimeout,
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/realtime_client.js", "utf8"), ctx, { filename: "realtime_client.js" });

const rc = win.realtimeClient;
rc._applyDeltasNow({ TEST: { price: 105, market: "us" } });
assert.strictEqual(win.state.stocks.us[0].price, 105);
assert.strictEqual(win.state.stocks.idx[0].price, 2000);

rc._applyPtsDeltasNow({ TEST: { price: 106.5, market: "us" } });
assert.strictEqual(win.state.stocks.us[0].pts_price, 106.5);
assert.strictEqual(win.state.stocks.idx[0].pts_price, null);

process.stdout.write("market_scope_ok");
"""
    res = run_node_script(script, context="realtime_client explicit market scoping")
    assert res.stdout.strip() == "market_scope_ok"
