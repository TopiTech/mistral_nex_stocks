"""Regression and edge-case tests for all 2026-11-16 code review fixes (CR-01..CR-15)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import types
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node(script: str, expected_marker: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to run frontend VM tests"
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert result.stdout.strip().endswith(expected_marker), (
        f"Expected {expected_marker!r}, got STDOUT={result.stdout!r} STDERR={result.stderr!r}"
    )


def _node_prelude() -> str:
    return r"""
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let __finished = false;
process.on("beforeExit", () => {
  if (!__finished) {
    console.error("Node exited before async test completed");
    process.exit(1);
  }
});

function readRel(relPath) {
  return fs.readFileSync(path.join(process.cwd(), relPath), "utf8");
}

function extractNamedBlock(src, marker) {
  const idx = src.indexOf(marker);
  if (idx < 0) throw new Error("Marker not found: " + marker);
  const start = marker.startsWith("\n") ? idx + 1 : idx;

  function skipString(pos, quote) {
    pos++;
    while (pos < src.length) {
      if (src[pos] === "\\") { pos += 2; continue; }
      if (src[pos] === quote) return pos + 1;
      pos++;
    }
    return src.length;
  }

  function skipRegex(pos) {
    pos++;
    let inClass = false;
    while (pos < src.length) {
      const c = src[pos];
      if (c === "\\") { pos += 2; continue; }
      if (c === "[") { inClass = true; pos++; continue; }
      if (c === "]" && inClass) { inClass = false; pos++; continue; }
      if (c === "/" && !inClass) {
        pos++;
        while (pos < src.length && /[dgimsuvy]/.test(src[pos])) pos++;
        return pos;
      }
      if (c === "\n") return pos;
      pos++;
    }
    return src.length;
  }

  function canStartRegex(prevChar, prevWord) {
    if (!prevChar) return true;
    if ("=([,:;!?&|^~+-*%<>{}".includes(prevChar)) return true;
    return /^(?:return|throw|case|typeof|void|delete|in|of|await|yield|else|do)$/.test(prevWord);
  }

  function scanFrom(pos, mode) {
    let depth = 0;
    let bodyStarted = false;
    let prevChar = "";
    let prevWord = "";
    const tmplStack = [];

    while (pos < src.length) {
      const ch = src[pos];
      const next = src[pos + 1] || "";

      if (tmplStack.length > 0 && tmplStack[tmplStack.length - 1] === 0) {
        if (ch === "\\") { pos += 2; continue; }
        if (ch === "`") { tmplStack.pop(); prevChar = "`"; prevWord = ""; pos++; continue; }
        if (ch === "$" && next === "{") {
          tmplStack.push(1);
          prevChar = "{";
          prevWord = "";
          pos += 2;
          continue;
        }
        pos++;
        continue;
      }

      if (ch === "/" && next === "/") {
        const nl = src.indexOf("\n", pos + 2);
        pos = nl < 0 ? src.length : nl + 1;
        continue;
      }
      if (ch === "/" && next === "*") {
        const endC = src.indexOf("*/", pos + 2);
        pos = endC < 0 ? src.length : endC + 2;
        continue;
      }
      if (ch === "'" || ch === '"') {
        pos = skipString(pos, ch);
        prevChar = ch;
        prevWord = "";
        continue;
      }
      if (ch === "`") {
        tmplStack.push(0);
        pos++;
        continue;
      }
      if (ch === "/" && canStartRegex(prevChar, prevWord)) {
        pos = skipRegex(pos);
        prevChar = "/";
        prevWord = "";
        continue;
      }
      if (/[a-zA-Z0-9_$]/.test(ch)) {
        let wEnd = pos + 1;
        while (wEnd < src.length && /[a-zA-Z0-9_$]/.test(src[wEnd])) wEnd++;
        prevWord = src.slice(pos, wEnd);
        prevChar = src[wEnd - 1];
        pos = wEnd;
        continue;
      }
      if (/\s/.test(ch)) {
        pos++;
        continue;
      }
      if (tmplStack.length > 0 && tmplStack[tmplStack.length - 1] > 0) {
        if (ch === "{") tmplStack[tmplStack.length - 1]++;
        else if (ch === "}") {
          tmplStack[tmplStack.length - 1]--;
          if (tmplStack[tmplStack.length - 1] === 0) tmplStack.pop();
        }
        prevChar = ch;
        prevWord = "";
        pos++;
        continue;
      }
      if (mode === "parens") {
        if (ch === "(") depth++;
        else if (ch === ")") {
          depth--;
          if (depth === 0) return pos + 1;
        }
        prevChar = ch;
        prevWord = "";
        pos++;
        continue;
      }
      if (ch === "{") {
        bodyStarted = true;
        depth++;
      } else if (ch === "}" && bodyStarted) {
        depth--;
        if (depth === 0) return pos + 1;
      }
      prevChar = ch;
      prevWord = "";
      pos++;
    }
    throw new Error("Unclosed block for: " + marker);
  }

  let bodyStartPos;
  if (marker.endsWith("{")) {
    bodyStartPos = idx + marker.length - 1;
  } else {
    const openParen = src.indexOf("(", start);
    if (openParen < 0) throw new Error("Paren not found for: " + marker);
    bodyStartPos = scanFrom(openParen, "parens");
  }
  const endPos = scanFrom(bodyStartPos, "block");
  return src.slice(start, endPos);
}
"""


# ---------------------------------------------------------------------------
# CR-01: Python SanitizedJsonFormatter & JS Logger._sanitize
# ---------------------------------------------------------------------------


def test_cr01_python_json_formatter_sanitizes_before_serialization() -> None:
    from logging_config import SanitizedJsonFormatter

    formatter = SanitizedJsonFormatter("%(asctime)s %(levelname)s %(message)s")
    cyclic: dict[str, object] = {"note": 'token="inner secret"'}
    cyclic["self"] = cyclic

    record = logging.LogRecord(
        name="cr01.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg='request failed with api_key="sk-secret with spaces"',
        args=(),
        exc_info=None,
    )
    record.headers = {"Authorization": 'Bearer "quoted-secret-123"', "X-Safe": "ok"}
    record.nested = [{"password": 'p"w'}, cyclic]
    record.has_api_key = True
    record.request_token = "req-tok-keep"

    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert "sk-secret" not in formatted
    assert "quoted-secret-123" not in formatted
    assert "inner secret" not in formatted
    assert parsed["headers"]["Authorization"] == "[REDACTED]"
    assert parsed["headers"]["X-Safe"] == "ok"
    assert parsed["nested"][0]["password"] == "[REDACTED]"
    assert parsed["nested"][1]["self"] == "[circular]"
    assert parsed["has_api_key"] is True
    assert parsed["request_token"] == "req-tok-keep"


def test_cr01_js_logger_sanitizes_structured_objects_and_cycles() -> None:
    script = (
        _node_prelude()
        + r"""
(() => {
  const utilsSrc = readRel("static/js/utils.js");
  const classBlock = extractNamedBlock(utilsSrc, "class Logger {");
  const ctx = {
    localStorage: { getItem: () => null },
    WeakSet, BigInt, Error, Date, RegExp, Object, Array, String,
    console: { debug() {}, info() {}, warn() {}, error() {} },
  };
  vm.runInNewContext(classBlock + "; this.Logger = Logger;", ctx);
  const logger = new ctx.Logger("CR01");
  const cyclic = { api_key: 'sk-"quoted space"', safe: "ok" };
  cyclic.self = cyclic;
  const err = new Error("Authorization: Bearer super-secret-jwt");
  const out = logger._sanitize([
    {
      headers: { Authorization: "Bearer hdr-secret", "X-MNS-Shutdown-Token": "sd-1" },
      request_token: "req-99",
      has_api_key: true,
      nested: [cyclic],
    },
    err,
    BigInt(42),
  ]);
  assert.strictEqual(typeof out[0], "object");
  assert.strictEqual(out[0].headers.Authorization, "[REDACTED]");
  assert.strictEqual(out[0].headers["X-MNS-Shutdown-Token"], "[REDACTED]");
  assert.strictEqual(out[0].request_token, "req-99");
  assert.strictEqual(out[0].has_api_key, true);
  assert.strictEqual(out[0].nested[0].api_key, "[REDACTED]");
  assert.strictEqual(out[0].nested[0].safe, "ok");
  assert.strictEqual(out[0].nested[0].self, "[Circular]");
  assert(!String(out[1].message).includes("super-secret-jwt"));
  assert.strictEqual(out[2], "42");
  __finished = true;
  console.log("CR01 JS PASS");
})();
"""
    )
    _run_node(script, "CR01 JS PASS")


# ---------------------------------------------------------------------------
# CR-02: Mixed DST datetime index & is_disk_cache_degraded edge cases
# ---------------------------------------------------------------------------


def test_cr02_mixed_dst_offsets_load_and_normalize_cleanly() -> None:
    from io import StringIO

    from services.stock.provider import YFinanceProvider
    from utils.normalization import coerce_datetime_index, normalize_history_frame

    # Pandas 3.x read_json(orient="split") preserves mixed offset ISO index as str
    raw_df = pd.DataFrame(
        {
            "Open": [100.0, 102.0],
            "High": [105.0, 106.0],
            "Low": [99.0, 101.0],
            "Close": [103.0, 104.0],
            "Volume": [1000, 1200],
        },
        index=pd.Index(["2025-03-07T09:30:00-05:00", "2025-03-10T09:30:00-04:00"]),
    )
    payload_json = raw_df.to_json(orient="split")
    roundtrip = pd.read_json(StringIO(payload_json), orient="split")

    normalized = normalize_history_frame(roundtrip.copy(), "AAPL")
    assert isinstance(normalized.index, pd.DatetimeIndex)
    assert len(normalized) == 2

    naive_idx = coerce_datetime_index(pd.Index(["2025-03-07", "2025-03-08"]))
    assert isinstance(naive_idx, pd.DatetimeIndex)
    assert naive_idx.tz is None

    with patch("app_state.app_state") as mock_state:
        mock_state.stock_disk_cache.get.return_value = {
            "type": "dataframe",
            "json_data": payload_json,
        }
        loaded = YFinanceProvider._load_history_from_disk_cache("hist_disk_AAPL_1y")
    assert loaded is not None
    assert isinstance(loaded.index, pd.DatetimeIndex)
    assert len(loaded) == 2


def test_cr02_is_disk_cache_degraded_boundary_and_thread_safety() -> None:
    from utils import disk_cache

    orig_ts = disk_cache._last_lock_timeout_ts
    try:
        # Unmarked (0.0) must NEVER report degraded even on a small clock
        disk_cache._last_lock_timeout_ts = 0.0
        with patch.object(disk_cache.time, "time", return_value=2.5):
            assert disk_cache.is_disk_cache_degraded(within_sec=30.0) is False

        # Marked active within window
        with patch.object(disk_cache.time, "time", return_value=100.0):
            disk_cache._note_lock_timeout()
        with patch.object(disk_cache.time, "time", return_value=110.0):
            assert disk_cache.is_disk_cache_degraded(within_sec=30.0) is True
        # Expired
        with patch.object(disk_cache.time, "time", return_value=135.0):
            assert disk_cache.is_disk_cache_degraded(within_sec=30.0) is False
        # Minor clock skew (-0.5s) tolerated; large jump (-10.0s) rejected
        with patch.object(disk_cache.time, "time", return_value=99.5):
            assert disk_cache.is_disk_cache_degraded(within_sec=30.0) is True
        with patch.object(disk_cache.time, "time", return_value=90.0):
            assert disk_cache.is_disk_cache_degraded(within_sec=30.0) is False

        # Bool / NaN / non-positive within_sec safely rejected
        assert disk_cache.is_disk_cache_degraded(within_sec=True) is False
        assert disk_cache.is_disk_cache_degraded(within_sec=float("nan")) is False
        assert disk_cache.is_disk_cache_degraded(within_sec=0.0) is False

        # Concurrent stress without errors
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _step in range(50):
                    disk_cache._note_lock_timeout()
                    disk_cache.is_disk_cache_degraded(30.0)
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
    finally:
        with disk_cache._degraded_state_lock:
            disk_cache._last_lock_timeout_ts = orig_ts


# ---------------------------------------------------------------------------
# CR-03: /api/ai-portfolio/save preserves top-level theme/name
# ---------------------------------------------------------------------------


def test_cr03_save_ai_portfolio_propagates_top_level_theme_and_name() -> None:
    from pydantic import ValidationError

    from app import app
    from schemas.ai_portfolio import AIPortfolioSaveRequest

    blank_name_req = AIPortfolioSaveRequest(
        name="   ",
        portfolio={
            "items": [{"symbol": "AAPL", "market": "us", "weight_pct": 100.0}],
        },
    )
    assert blank_name_req.name is None

    with pytest.raises(ValidationError):
        AIPortfolioSaveRequest(
            theme="   ",
            portfolio={
                "items": [{"symbol": "AAPL", "market": "us", "weight_pct": 100.0}],
            },
        )

    has_csrf = "WTF_CSRF_ENABLED" in app.config
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    saved_args: dict[str, object] = {}

    def _capture_save(portfolio_dict: dict[str, object], portfolio_id: str | None = None):
        saved_args["portfolio"] = portfolio_dict
        return {**portfolio_dict, "id": portfolio_id or "custom-cr03"}

    try:
        with (
            patch("utils.networking.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.stocks.ai_portfolio.save_custom_ai_portfolio", side_effect=_capture_save),
        ):
            res = app.test_client().post(
                "/api/ai-portfolio/save",
                json={
                    "name": "  Semiconductor Core  ",
                    "theme": "  AI Chips  ",
                    "portfolio": {
                        "items": [
                            {"symbol": "NVDA", "market": "us", "weight_pct": 100.0},
                        ]
                    },
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert res.status_code == 200
        body = res.get_json()
        assert body["ok"] is True
        saved = saved_args["portfolio"]
        assert isinstance(saved, dict)
        assert saved["title"] == "Semiconductor Core"
        assert saved["theme"] == "AI Chips"
    finally:
        if has_csrf:
            app.config["WTF_CSRF_ENABLED"] = orig_csrf
        else:
            app.config.pop("WTF_CSRF_ENABLED", None)


# ---------------------------------------------------------------------------
# CR-04: Deduplicated helpers contract
# ---------------------------------------------------------------------------


def test_cr04_shared_helpers_are_single_source_of_truth() -> None:
    import route_helpers
    from routes import api_analysis
    from routes.stocks import ai_portfolio
    from services.search import langsearch, tavily
    from utils.http_utils import extract_external_error_metadata

    assert langsearch._external_error_metadata is extract_external_error_metadata
    assert tavily._external_error_metadata is extract_external_error_metadata
    assert ai_portfolio._safe_provider_error_details is route_helpers.safe_provider_error_details
    assert api_analysis._safe_provider_error_details is route_helpers.safe_provider_error_details

    class FakeHTTPError(Exception):
        status_code = 429

    assert extract_external_error_metadata(None) == ("UnknownError", "unknown")
    err_with_status = FakeHTTPError("rate limited")
    assert extract_external_error_metadata(err_with_status) == ("FakeHTTPError", "429")

    filtered = route_helpers.safe_provider_error_details(
        {
            "retry_after": True,
            "prompt": "TOP SECRET USER PROMPT",
            "provider_error": {
                "status_code": 503,
                "code": "rate_limit",
                "message": "See https://internal.example.com?key=secret failed",
            },
        }
    )
    assert "retry_after" not in filtered
    assert "prompt" not in filtered
    assert filtered["provider_error"]["status_code"] == 503
    assert "internal.example.com" not in filtered["provider_error"]["message"]


# ---------------------------------------------------------------------------
# CR-05: Scraper/engine lifecycle lock synchronization
# ---------------------------------------------------------------------------


def test_cr05_scraper_is_worker_current_uses_lifecycle_lock_without_data_lock() -> None:
    from services.realtime.engine import RealtimeMarketEngine
    from services.realtime.scrapers import YahooJPRealtimeScraper
    from services.realtime.tv_client import TradingViewWSClient

    scraper = YahooJPRealtimeScraper()
    with scraper._lifecycle_lock:
        scraper.running = True
        scraper._epoch = 3
        assert scraper._is_worker_current_locked(3) is True
        assert scraper._is_worker_current_locked(2) is False

    # Holding scraper.lock must NOT block _is_worker_current
    entered = threading.Event()
    result_box: list[bool] = []

    def _check_current() -> None:
        entered.set()
        result_box.append(scraper._is_worker_current(3))

    with scraper.lock:
        t = threading.Thread(target=_check_current, daemon=True)
        t.start()
        t.join(timeout=1.5)
    assert entered.is_set()
    assert result_box == [True]

    # object.__new__ engine mock compatibility + real instance lock safety
    bare_engine = object.__new__(RealtimeMarketEngine)
    bare_engine.running = True
    bare_engine.pts_thread = None
    bare_tv = object.__new__(TradingViewWSClient)
    bare_tv.thread = None
    bare_scraper = object.__new__(YahooJPRealtimeScraper)
    bare_scraper.thread = None
    bare_engine.tv_client = bare_tv
    bare_engine.yahoojp_scraper = bare_scraper
    assert bare_engine.worker_threads() == []
    assert set(bare_engine.missing_worker_names()) == {
        "TradingViewWSWorker",
        "YahooJPScraperWorker",
        "JPPTSWorker",
    }

    real_engine = RealtimeMarketEngine()
    assert real_engine.worker_threads() == []

    # Holding _lifecycle_op_lock (as stop()/restart() do across waits) must NOT
    # block _is_pts_worker_current() on _lifecycle_lock.
    with real_engine._lifecycle_lock:
        real_engine.running = True
        real_engine._pts_epoch = 5
    check_box: list[bool] = []
    with real_engine._lifecycle_op_lock:
        t_pts = threading.Thread(
            target=lambda: check_box.append(real_engine._is_pts_worker_current(5)),
            daemon=True,
        )
        t_pts.start()
        t_pts.join(timeout=1.5)
    assert check_box == [True]


# ---------------------------------------------------------------------------
# CR-06: SSE Mode 2 initial snapshot does not double-emit immediately
# ---------------------------------------------------------------------------


def test_cr06_mode2_initial_snapshot_skips_immediate_duplicate_snapshot() -> None:
    import queue as queue_mod
    from contextlib import contextmanager

    from app import app
    from routes.stocks import stream as stream_mod

    snapshot_calls = 0

    def _count_snapshot(*_a, **_kw):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return {"AAPL": {"symbol": "AAPL", "price": 200.0}}

    stop_q: queue_mod.Queue[object] = queue_mod.Queue()
    stop_q.put(None)  # exit immediately after first iteration (if loop enters)

    @contextmanager
    def _stop_listener_ctx(*_a, **_kw):
        yield stop_q

    @contextmanager
    def _client_ctx():
        yield "cid-cr06"

    stream_mod.sse_event_log.clear()
    try:
        with (
            patch.object(stream_mod, "require_sse_auth", return_value=(True, None)),
            patch.object(
                stream_mod.app_state.sse_announcer_mode2,
                "listener_context",
                side_effect=_stop_listener_ctx,
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "client_context",
                side_effect=_client_ctx,
            ),
            patch.object(
                stream_mod,
                "_initial_snapshot_for_mode",
                return_value='{"stream_event":"init"}',
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "get_market_deltas",
                return_value={},
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "get_pts_deltas",
                return_value={},
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "get_market_snapshot",
                side_effect=_count_snapshot,
            ),
        ):
            res = app.test_client().get(
                "/api/stocks/stream?mode=2",
                headers={"Origin": "http://localhost:5000"},
            )
            assert res.status_code == 200
            body = res.get_data(as_text=True)
            assert '{"stream_event":"init"}' in body

        assert snapshot_calls == 0

        ping_q: queue_mod.Queue[object] = queue_mod.Queue()
        ping_q.put(": keepalive\n\n")
        ping_q.put(None)
        snapshot_calls = 0

        @contextmanager
        def _ping_listener_ctx(*_a, **_kw):
            yield ping_q

        def _dequeue_and_stop(*_a, **_kw):
            ping_q.get_nowait()  # second iteration hits None and breaks
            return {}

        with (
            patch.object(stream_mod, "require_sse_auth", return_value=(True, None)),
            patch.object(
                stream_mod.app_state.sse_announcer_mode2,
                "listener_context",
                side_effect=_ping_listener_ctx,
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "client_context",
                side_effect=_client_ctx,
            ),
            patch.object(
                stream_mod,
                "_initial_snapshot_for_mode",
                return_value='{"stream_event":"init"}',
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "get_market_deltas",
                side_effect=_dequeue_and_stop,
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "get_pts_deltas",
                return_value={},
            ),
            patch.object(
                stream_mod.realtime_market_engine,
                "get_market_snapshot",
                side_effect=_count_snapshot,
            ),
            patch.object(stream_mod.realtime_market_engine, "wait_for_updates"),
        ):
            res2 = app.test_client().get(
                "/api/stocks/stream?mode=2",
                headers={"Origin": "http://localhost:5000"},
            )
            assert res2.status_code == 200
            _ = res2.get_data(as_text=True)
        assert snapshot_calls == 0
    finally:
        stream_mod.sse_event_log.clear()


# ---------------------------------------------------------------------------
# CR-07: Estimated FX keeps stale_warning across repeated copy-to-my calls
# ---------------------------------------------------------------------------


def test_cr07_copy_to_my_keeps_stale_warning_across_consecutive_estimated_calls() -> None:
    from app import app
    from app_state import app_state

    has_csrf = "WTF_CSRF_ENABLED" in app.config
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    orig_rate, orig_ts = app_state.market.get_usdjpy_rate_snapshot()
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        items = [{"symbol": "NVDA", "market": "us", "weight_pct": 100.0, "target_price": 150.0}]
        app_state.market.update_usdjpy_rate(150.0, ts=0.0)
        with (
            patch("utils.networking.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.stocks.ai_portfolio.save_user_stocks"),
            patch("routes.stocks.ai_portfolio.schedule_sync_all_stocks_now"),
            patch("routes.stocks.common._announce_watchlist_state"),
            patch("routes.stocks.common._sync_realtime_symbol"),
            patch("routes.stocks.ai_portfolio.get_current_usdjpy_rate", return_value=(150.0, True)),
        ):
            client = app.test_client()
            res1 = client.post("/api/ai-portfolio/copy-to-my", json={"items": items})
            res2 = client.post("/api/ai-portfolio/copy-to-my", json={"items": items})
            assert res1.status_code == 200
            assert res2.status_code == 200
            assert "stale_warning" in res1.get_json()
            assert "stale_warning" in res2.get_json()
            _, after_ts = app_state.market.get_usdjpy_rate_snapshot()
            assert after_ts == 0.0
    finally:
        app_state.market.update_usdjpy_rate(orig_rate, ts=orig_ts)
        if has_csrf:
            app.config["WTF_CSRF_ENABLED"] = orig_csrf
        else:
            app.config.pop("WTF_CSRF_ENABLED", None)


# ---------------------------------------------------------------------------
# CR-08 & CR-09: Extension fetchWithTimeout / popup stop timeout / start_backend CONN_LISTEN
# ---------------------------------------------------------------------------


def test_cr08_extension_fetch_with_timeout_cleanup_and_timeout_validation() -> None:
    script = (
        _node_prelude()
        + r"""
(async () => {
  for (const rel of ["chrome_extension/background.js", "chrome_extension/popup.js"]) {
    const src = readRel(rel);
    const fnBlock = extractNamedBlock(src, "function fetchWithTimeout(");
    let clearedCount = 0;
    let bodyCanceled = 0;
    let lastSignal = null;
    const origClear = clearTimeout;
    const ctx = {
      AbortController,
      setTimeout,
      clearTimeout: (id) => { clearedCount += 1; return origClear(id); },
      Number,
      Object,
      Promise,
      console,
      fetch: async (_url, opts) => {
        lastSignal = opts.signal;
        return {
          ok: false,
          status: 503,
          body: {
            async cancel() { bodyCanceled += 1; },
          },
          json: async () => ({}),
        };
      },
    };
    vm.runInNewContext(fnBlock + "; this.fetchWithTimeout = fetchWithTimeout;", ctx);

    // Non-finite / non-positive timeoutMs must not immediately abort (uses default timeout)
    for (const badTimeout of [NaN, 0, -5]) {
      const res = await ctx.fetchWithTimeout("http://127.0.0.1:5000/api/health", {}, badTimeout);
      assert.strictEqual(lastSignal.aborted, false, rel + ": invalid timeoutMs (" + badTimeout + ") must fall back cleanly");
      await res.body.cancel();
    }
    assert.strictEqual(bodyCanceled, 3, rel + ": body.cancel wrapper runs");
    assert(clearedCount >= 3, rel + ": timer cleared on body.cancel");
  }
  const bgSrc = readRel("chrome_extension/background.js");
  const popSrc = readRel("chrome_extension/popup.js");
  assert(bgSrc.includes("res?._mnsCleanupTimeout?.()"));
  assert(popSrc.includes("res?._mnsCleanupTimeout?.()"));
  assert(popSrc.includes("const stopped = await waitForBackendStopped();"));
  assert(popSrc.includes("if (!stopped)"));
  __finished = true;
  console.log("CR08 PASS");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    _run_node(script, "CR08 PASS")


def test_cr09_start_backend_verification_ignores_non_listen_sockets(tmp_path: Path) -> None:
    import native_host.start_backend as sb

    pid_file = tmp_path / ".backend.pid"
    pid_file.write_text("4242", encoding="utf-8")

    foreign_time_wait = types.SimpleNamespace(
        pid=9999,
        status="TIME_WAIT",
        laddr=types.SimpleNamespace(port=5000),
    )
    own_listen = types.SimpleNamespace(
        pid=4242,
        status="LISTEN",
        laddr=types.SimpleNamespace(port=5000),
    )
    own_established = types.SimpleNamespace(
        pid=4242,
        status="ESTABLISHED",
        laddr=types.SimpleNamespace(port=5000),
    )
    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        patch.object(sb, "_pid_is_backend_process", return_value=True),
        patch.object(sb.psutil, "net_connections", return_value=[foreign_time_wait, own_listen]),
    ):
        assert sb.get_backend_verification(5000) is True

    with (
        patch.object(sb, "PID_FILE", pid_file),
        patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        patch.object(sb, "_pid_is_backend_process", return_value=True),
        patch.object(sb.psutil, "net_connections", return_value=[own_established]),
    ):
        assert sb.get_backend_verification(5000) is False


# ---------------------------------------------------------------------------
# CR-10..CR-15: CSS var, Drawers, 3D Heatmap, Abortable sleeps, LRU historyMap, Volume
# ---------------------------------------------------------------------------


def test_cr10_heatmap_css_uses_defined_accent_red_variable() -> None:
    heatmap_css = (ROOT_DIR / "static" / "css" / "heatmap.css").read_text(encoding="utf-8")
    colors_css = (ROOT_DIR / "static" / "css" / "colors.css").read_text(encoding="utf-8")
    assert "--acc-red" not in heatmap_css
    assert "var(--accent-red)" in heatmap_css
    assert "--accent-red:" in colors_css


def test_cr11_drawer_switch_preserves_new_trigger_and_ai_drawer_trigger() -> None:
    script = (
        _node_prelude()
        + r"""
(() => {
  const uiSrc = readRel("static/js/ui.js");
  const openBlock = extractNamedBlock(uiSrc, "function openStockDetailDrawer(");
  const closeBlock = extractNamedBlock(uiSrc, "function closeStockDetailDrawer(");
  const aiOpenBlock = extractNamedBlock(uiSrc, "function openAiDrawer(");

  function makeNode(tag, id = "") {
    const node = {
      tagName: tag.toUpperCase(),
      id,
      className: "",
      classList: {
        _s: new Set(),
        add(c) { this._s.add(c); },
        remove(c) { this._s.delete(c); },
        contains(c) { return this._s.has(c); },
      },
      dataset: {},
      style: {},
      attrs: {},
      children: [],
      parentNode: null,
      isConnected: true,
      focusCalls: 0,
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] ?? null; },
      removeAttribute(k) { delete this.attrs[k]; },
      appendChild(c) { c.parentNode = this; this.children.push(c); },
      append(...items) { items.forEach((c) => { if (c && typeof c === "object") this.appendChild(c); }); },
      replaceChildren(...items) { this.children = items; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      remove() {},
      contains(el) { return this === el || this.children.includes(el); },
      focus() { this.focusCalls += 1; ctx.document.activeElement = this; },
    };
    return node;
  }

  class FakeHTMLElement {}
  const btnA = makeNode("button", "btnA");
  Object.setPrototypeOf(btnA, FakeHTMLElement.prototype);
  const btnB = makeNode("button", "btnB");
  Object.setPrototypeOf(btnB, FakeHTMLElement.prototype);
  const wrapA = makeNode("div"); wrapA.dataset.stockKey = "us:AAPL";
  const wrapB = makeNode("div"); wrapB.dataset.stockKey = "us:MSFT";

  const overlay = makeNode("div", "stock-detail-drawer-overlay");
  const drawerEl = makeNode("div", "stock-detail-drawer");
  const aiOverlay = makeNode("div", "ai-drawer-overlay");
  aiOverlay.classList.add("hidden");
  const aiInput = makeNode("input", "aiDrawerInput");
  Object.setPrototypeOf(aiInput, FakeHTMLElement.prototype);
  aiOverlay.appendChild(aiInput);

  const byId = {
    "stock-detail-drawer-overlay": overlay,
    "stock-detail-drawer": drawerEl,
    "drawerTabChartContent": makeNode("div"),
    "drawerTabAiContent": makeNode("div"),
    "ai-drawer-overlay": aiOverlay,
    "ai-drawer-symbol": makeNode("span"),
    "ai-drawer-name": makeNode("span"),
    "ai-drawer-chat-messages": makeNode("div"),
    "aiDrawerInput": aiInput,
    "sendAiDrawerBtn": makeNode("button"),
  };

  const ctx = {
    HTMLElement: FakeHTMLElement,
    AbortController,
    setTimeout,
    clearTimeout,
    currentDrawerActiveWrapper: null,
    stockDetailDrawerTrigger: null,
    stockDetailDrawerRefreshTimer: null,
    aiDrawerTrigger: null,
    aiDrawerSendGeneration: 0,
    aiDrawerAbortController: null,
    currentDrawerSymbol: "",
    currentDrawerName: "",
    currentDrawerMarket: "us",
    document: {
      activeElement: btnA,
      getElementById: (id) => byId[id] || null,
      createElement: (t) => makeNode(t),
      createTextNode: (t) => ({ textContent: t }),
      querySelector: () => null,
      querySelectorAll: () => [],
    },
    createEl: (t, c, txt) => {
      const el = makeNode(t);
      if (c) el.className = c;
      if (txt != null) el.textContent = txt;
      return el;
    },
    getLatestStockForDrawer: (s) => s,
    renderStockDetailDrawerHeader() {},
    switchDrawerTab() {},
    resetDetailTabsToDefault() {},
    makeStockKey: (m, s) => `${m}:${s}`,
    getChartPref: (_k, _p, d) => d,
    refreshStockChart() {},
    ensureStockDetails() {},
    lockBodyScroll() {},
    unlockBodyScroll() {},
    renderAiTechnicalLinesSummary() {},
  };
  vm.runInNewContext(
    openBlock + "\n" + closeBlock + "\n" + aiOpenBlock +
    "\nthis.openStockDetailDrawer = openStockDetailDrawer;" +
    "\nthis.closeStockDetailDrawer = closeStockDetailDrawer;" +
    "\nthis.openAiDrawer = openAiDrawer;",
    ctx,
  );

  // Open wrapper A from btnA
  ctx.document.activeElement = btnA;
  ctx.openStockDetailDrawer({ symbol: "AAPL", market: "us" }, wrapA);
  assert.strictEqual(ctx.stockDetailDrawerTrigger, btnA);

  // Switch straight to wrapper B from btnB -> btnA must NOT receive focus
  ctx.document.activeElement = btnB;
  const btnAFocusBefore = btnA.focusCalls;
  ctx.openStockDetailDrawer({ symbol: "MSFT", market: "us" }, wrapB);
  assert.strictEqual(btnA.focusCalls, btnAFocusBefore, "btnA must not steal focus during switch");
  assert.strictEqual(ctx.stockDetailDrawerTrigger, btnB, "trigger must update to btnB");

  ctx.closeStockDetailDrawer();
  assert.strictEqual(btnB.focusCalls, 1, "closing wrapper B returns focus to btnB");

  // AI drawer: opening captures external trigger; reopening while already open keeps it
  ctx.document.activeElement = btnA;
  ctx.openAiDrawer("AAPL", "Apple", "us");
  assert.strictEqual(ctx.aiDrawerTrigger, btnA);
  ctx.document.activeElement = aiInput;
  ctx.openAiDrawer("NVDA", "NVIDIA", "us");
  assert.strictEqual(ctx.aiDrawerTrigger, btnA, "already-open AI drawer preserves external opener");
  __finished = true;
  console.log("CR11 PASS");
})();
"""
    )
    _run_node(script, "CR11 PASS")


def test_cr12_heatmap_3d_hover_and_keyboard_highlight_cleanup() -> None:
    script = (
        _node_prelude()
        + r"""
(() => {
  const src = readRel("static/js/heatmap.js");
  function makeMesh(symbol, origHex) {
    return {
      userData: { stock: { symbol, market: "us", name: symbol, change_percent: 1.5 } },
      material: {
        emissive: {
          _h: origHex,
          getHex() { return this._h; },
          setHex(v) { this._h = v; },
        },
      },
    };
  }
  const mesh0 = makeMesh("AAPL", 0x111111);
  const mesh1 = makeMesh("MSFT", 0x222222);
  const listeners = {};
  const domEl = {
    style: {},
    attributes: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    setAttribute(k, v) { this.attributes[k] = v; },
    addEventListener(evt, fn) { (listeners[evt] ||= []).push(fn); },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
  };
  const tooltip = {
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
    style: {},
    innerHTML: "",
    textContent: "",
    append() {},
    replaceChildren() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 120, height: 40 }),
  };
  const canvas2d = {
    width: 800,
    height: 600,
    clientWidth: 800,
    clientHeight: 600,
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    parentElement: { clientWidth: 800, clientHeight: 600, appendChild() {}, replaceChildren() {} },
    setAttribute() {},
    addEventListener() {},
    querySelectorAll: () => [],
    appendChild() {},
    replaceChildren() {},
    getContext: () => ({
      clearRect() {},
      fillRect() {},
      strokeRect() {},
      fillText() {},
      measureText: () => ({ width: 10 }),
      beginPath() {},
      rect() {},
      clip() {},
      save() {},
      restore() {},
    }),
  };
  const canvas3d = {
    clientWidth: 800,
    clientHeight: 600,
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelectorAll: () => [],
    replaceChildren() {},
    appendChild() {},
  };
  const domReadyCallbacks = [];
  const ctx = {
    AbortController,
    window: { location: { href: "" }, addEventListener() {}, innerWidth: 1280, innerHeight: 800 },
    innerWidth: 1280,
    innerHeight: 800,
    localStorage: { getItem: () => null, setItem() {} },
    requestAnimationFrame() { return 0; },
    cancelAnimationFrame() {},
    setTimeout,
    clearTimeout,
    apiFetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, stocks: [] }) }),
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, stocks: [] }) }),
    document: {
      readyState: "complete",
      addEventListener(evt, fn) { if (evt === "DOMContentLoaded") domReadyCallbacks.push(fn); },
      getElementById(id) {
        if (id === "heatmap-canvas") return canvas2d;
        if (id === "heatmap-3d-canvas") return canvas3d;
        if (id === "heatmap-tooltip") return tooltip;
        return null;
      },
      querySelectorAll: () => [],
      createElement: () => ({
        style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        setAttribute() {},
        append() {},
        appendChild() {},
        replaceChildren() {},
        remove() {},
        addEventListener() {},
        querySelectorAll: () => [],
      }),
    },
    THREE: {
      Scene: function () { this.add = () => {}; },
      Color: function () {},
      FogExp2: function () {},
      PerspectiveCamera: function () { this.position = { set() {} }; },
      WebGLRenderer: function () { this.domElement = domEl; this.shadowMap = {}; this.setSize = () => {}; this.setPixelRatio = () => {}; this.render = () => {}; },
      OrbitControls: function () { this.update = () => {}; },
      AmbientLight: function () {},
      DirectionalLight: function () { this.position = { set() {} }; },
      GridHelper: function () { this.position = { y: 0 }; },
      Raycaster: function () { this.setFromCamera = () => {}; this.intersectObjects = () => []; },
      Vector2: function () {},
    },
  };
  ctx.window.window = ctx.window;
  const endMarker = "});";
  const closeIdx = src.lastIndexOf(endMarker);
  assert(closeIdx > 0, "heatmap.js must end with DOMContentLoaded closing brace");
  const patchedSrc =
    src.slice(0, closeIdx) +
    "\nwindow.__cr12Expose = { init3DScene, getThreeState: () => state.three, setMode3d: () => { state.viewMode = '3d'; } };\n" +
    src.slice(closeIdx);
  vm.createContext(ctx);
  vm.runInContext(patchedSrc, ctx);
  assert(domReadyCallbacks.length === 1, "DOMContentLoaded handler must register");
  domReadyCallbacks[0]();
  const expose = ctx.window.__cr12Expose;
  expose.setMode3d();
  assert.strictEqual(expose.init3DScene(), true);
  const threeState = expose.getThreeState();
  threeState.stockMeshes = [mesh0, mesh1];
  threeState.keyboardIndex = -1;

  listeners.keydown[0]({ key: "ArrowRight", isComposing: false, keyCode: 39, preventDefault() {} });
  assert.strictEqual(threeState.hoveredMesh, mesh0);
  assert.strictEqual(mesh0.material.emissive.getHex(), 0x00f2fe);

  listeners.keydown[0]({ key: "ArrowRight", isComposing: false, keyCode: 39, preventDefault() {} });
  assert.strictEqual(threeState.hoveredMesh, mesh1);
  assert.strictEqual(mesh0.material.emissive.getHex(), 0x111111, "previous mesh emissive restored");
  assert.strictEqual(mesh1.material.emissive.getHex(), 0x00f2fe, "newly selected mesh highlighted");

  listeners.mouseleave[0]();
  assert.strictEqual(threeState.hoveredMesh, null, "mouseleave must clear hoveredMesh");
  assert.strictEqual(mesh1.material.emissive.getHex(), 0x222222, "mouseleave must restore emissive");

  listeners.keydown[0]({ key: "ArrowLeft", isComposing: false, keyCode: 37, preventDefault() {} });
  assert.strictEqual(threeState.hoveredMesh, mesh0);
  listeners.blur[0]();
  assert.strictEqual(threeState.hoveredMesh, null, "blur must clear hoveredMesh");
  assert.strictEqual(mesh0.material.emissive.getHex(), 0x111111, "blur must restore emissive");
  __finished = true;
  console.log("CR12 PASS");
})();
"""
    )
    _run_node(script, "CR12 PASS")


def test_cr13_abortable_retry_waits_cancel_promptly() -> None:
    script = (
        _node_prelude()
        + r"""
(async () => {
  const chartSrc = readRel("static/js/chart.js");
  const uiSrc = readRel("static/js/ui.js");
  const fetchBlock = extractNamedBlock(chartSrc, "async function fetchStockHistoryPayload(");
  const detailsBlock = extractNamedBlock(uiSrc, "async function ensureStockDetails(");

  // 1. chart.js fetchStockHistoryPayload abort cancels 1500ms wait immediately
  const chartCtx = {
    AbortController,
    setTimeout,
    clearTimeout,
    CONSTANTS: { TIMEOUT: { STOCK_HISTORY: 10000, STOCK_HISTORY_RETRY: 5000 } },
    apiFetch: async () => ({ data: { fetching: true, history: [] } }),
    normalizeHistoryData: (h) => h,
    logger: { warn() {}, info() {} },
  };
  vm.runInNewContext(fetchBlock + "; this.fetchStockHistoryPayload = fetchStockHistoryPayload;", chartCtx);
  const ac1 = new AbortController();
  const started1 = Date.now();
  setTimeout(() => ac1.abort(), 30);
  let err1 = null;
  try {
    await chartCtx.fetchStockHistoryPayload("AAPL", "us", "3mo", "auto", ac1.signal);
  } catch (e) {
    err1 = e;
  }
  assert(err1 && err1.name === "AbortError", "Expected AbortError, got " + err1);
  assert(Date.now() - started1 < 400, "chart sleep must wake on abort quickly");

  // 2. ui.js ensureStockDetails abort cancels 700ms poll sleep immediately
  const wrapper = {
    dataset: { stockKey: "us:AAPL", symbol: "AAPL", market: "us" },
    querySelector: () => null,
  };
  const detailsCtx = {
    AbortController,
    URL,
    URLSearchParams,
    window: { location: { origin: "http://127.0.0.1:5000" } },
    document: {
      querySelector: () => null,
      querySelectorAll: () => [],
      getElementById: () => null,
      createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {}, addEventListener() {} }),
    },
    currentDrawerActiveWrapper: null,
    setTimeout,
    clearTimeout,
    MAX_DETAILS_POLLS: 5,
    stockDetailsCache: new Map(),
    stockDetailsCacheAt: new Map(),
    isStockDetailsCacheFresh: () => false,
    DETAIL_CACHE_TTL_MS: 60000,
    makeStockKey: (m, s) => `${m}:${s}`,
    logger: { info() {}, warn() {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, fetching: true }) }),
    renderDetailExtras() {},
    _enforceStockDetailsCacheLimit() {},
  };
  vm.runInNewContext(detailsBlock + "; this.ensureStockDetails = ensureStockDetails;", detailsCtx);
  const started2 = Date.now();
  setTimeout(() => wrapper.__detailsReqController?.abort(), 30);
  await detailsCtx.ensureStockDetails(wrapper);
  assert(Date.now() - started2 < 350, "ensureStockDetails poll wait must wake on abort quickly");

  __finished = true;
  console.log("CR13 PASS");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    _run_node(script, "CR13 PASS")


def test_cr14_temporal_controller_bounds_shared_history_map() -> None:
    script = (
        _node_prelude()
        + r"""
(async () => {
  const temporalSrc = readRel("static/js/experimental/temporal-controller.js");
  const windowObj = { addEventListener() {}, ObservatoryDataAdapter: { normalizeHistory: (h) => h } };
  const docObj = { addEventListener() {}, getElementById: () => null };
  const sandbox = {
    window: windowObj,
    document: docObj,
    AbortController,
    setTimeout,
    clearTimeout,
    Map,
    Set,
    console: { log() {}, warn() {}, error() {} },
    apiFetch: async (url) => ({
      ok: true,
      status: 200,
      json: async () => ({ history: [{ date: "2026-01-01", close: 100, url }] }),
    }),
  };
  sandbox.window.apiFetch = sandbox.apiFetch;
  sandbox.global = sandbox.window;
  vm.createContext(sandbox);
  vm.runInContext(temporalSrc, sandbox);
  const TemporalController = sandbox.window.TemporalController;

  const stateObj = {
    state: {
      selectedSymbol: "S0",
      timeGranularity: "3mo",
      timeCursor: 0,
      market: "us",
      stocks: new Map(),
    },
    data: { historyMap: new Map(), activeHistory: [] },
    subscribe: () => () => {},
  };
  const tc = new TemporalController(stateObj, {});

  for (let i = 0; i < 80; i++) {
    const sym = `S${i}`;
    stateObj.state.selectedSymbol = sym;
    stateObj.state.stocks.set(sym, { symbol: sym, market: "us" });
    tc.activeHistoryKey = `${sym}:3mo`;
    await tc.fetchHistoryForSymbol(sym, "3mo", 0);
  }
  // Cache-hit touch on oldest S0 refreshes its LRU position
  stateObj.state.selectedSymbol = "S0";
  tc.activeHistoryKey = "S0:3mo";
  await tc.fetchHistoryForSymbol("S0", "3mo", 0);

  for (let i = 80; i < 95; i++) {
    const sym = `S${i}`;
    stateObj.state.selectedSymbol = sym;
    stateObj.state.stocks.set(sym, { symbol: sym, market: "us" });
    tc.activeHistoryKey = `${sym}:3mo`;
    await tc.fetchHistoryForSymbol(sym, "3mo", 0);
  }
  assert.strictEqual(tc.historyCache.size, 80, "historyCache must cap at 80 entries");
  assert.strictEqual(stateObj.data.historyMap.size, 80, "historyMap must cap alongside historyCache");
  assert(stateObj.data.historyMap.has("S0:3mo"), "recently accessed S0 entry must survive LRU eviction");
  assert(!stateObj.data.historyMap.has("S1:3mo"), "least recently used S1 entry must be evicted from historyMap");
  assert(stateObj.data.historyMap.has("S94:3mo"), "latest entry must remain in historyMap");

  __finished = true;
  console.log("CR14 PASS");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    _run_node(script, "CR14 PASS")


def test_cr15_realtime_client_updates_positive_volume_and_preserves_on_empty_deltas() -> None:
    script = (
        _node_prelude()
        + r"""
(() => {
  const rtSrc = readRel("static/js/realtime_client.js");
  const stockObj = { symbol: "AAPL", market: "us", price: 100, previous_close: 99, volume: 1000 };
  let lastUpdatedCardStock = null;
  const wrapper = {
    isConnected: true,
    dataset: { stockKey: "us:AAPL" },
    __stockData: { ...stockObj },
    querySelector: () => null,
  };
  const sandbox = {
    requestAnimationFrame: (fn) => { fn(); return 1; },
    cancelAnimationFrame() {},
    setTimeout,
    clearTimeout,
    console: { log() {}, warn() {}, error() {}, debug() {} },
    Object, Array, String, Number, Boolean, Math, Set, Map, Error,
    window: {
      state: { stocks: { us: [stockObj], jp: [], idx: [] } },
      updateExistingCard(_w, stock) {
        wrapper.__stockData = { ...stock };
        lastUpdatedCardStock = { ...stock };
      },
      requestAnimationFrame: (fn) => { fn(); return 1; },
      cancelAnimationFrame() {},
    },
    document: {
      readyState: "complete",
      hidden: false,
      addEventListener() {},
      querySelectorAll: (sel) => (sel.includes(".compact-pts") ? [] : [wrapper]),
    },
  };
  sandbox.window.window = sandbox.window;
  vm.createContext(sandbox);
  vm.runInContext(rtSrc, sandbox);

  const client = sandbox.window.realtimeClient;
  client._applyDeltasNow({ AAPL: { price: 102, volume: 5500 } });
  assert.strictEqual(stockObj.volume, 5500, "state.stocks volume must advance to 5500");
  assert.strictEqual(lastUpdatedCardStock.volume, 5500, "card __stockData volume must update to 5500");

  // Subsequent scraper/partial delta omitting or zeroing volume keeps 5500
  client._applyDeltasNow({ AAPL: { price: 103, volume: null } });
  assert.strictEqual(lastUpdatedCardStock.volume, 5500, "null delta volume must keep 5500");

  client._applyDeltasNow({ AAPL: { price: 104, volume: true } });
  assert.strictEqual(lastUpdatedCardStock.volume, 5500, "boolean volume must not become 1");

  __finished = true;
  console.log("CR15 PASS");
})();
"""
    )
    _run_node(script, "CR15 PASS")
