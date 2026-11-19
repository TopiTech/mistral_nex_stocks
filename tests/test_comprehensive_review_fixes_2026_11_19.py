"""Regression and edge-case tests for the 2026-11-19 comprehensive review fixes."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import trend_sources
from app import app
from app_state import app_state
from bg.sse_interpolator import bg_interpolate_loop
from route_helpers import (
    _is_polling_token_inflight,
    ensure_stock_placeholder_in_caches,
)
from routes import api_analysis as ra
from routes.stocks import ai_portfolio as ai_pf_routes
from services import ai_portfolio_service as ai_pf_svc
from services.fallback_provider import MinkabuProvider
from services.market_data_service import _build_market_row
from services.openai_service import _normalize_openai_temperature
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.scrapers import YahooJPRealtimeScraper
from session_manager import YFinanceSessionManager
from utils.http_utils import decode_http_response_text

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend VM regression tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


# ---------------------------------------------------------------------------
# Group 1: Network decoders, timeouts, temperature validation, trend sessions
# ---------------------------------------------------------------------------


def test_decode_http_response_text_avoids_iso8859_1_mojibake_and_handles_meta():
    utf8_html = "<html><body>日経平均株価チャート</body></html>".encode()
    resp_utf8 = SimpleNamespace(
        headers={"Content-Type": "text/html"},
        encoding="ISO-8859-1",
        apparent_encoding="utf-8",
        content=utf8_html,
        text=utf8_html.decode("latin-1"),
    )
    assert "日経平均株価チャート" in decode_http_response_text(resp_utf8)

    sjis_html = (
        '<html><head><meta charset="Shift_JIS"></head>'
        "<body>現在値 2,500円</body></html>"
    ).encode("cp932")
    resp_sjis = SimpleNamespace(
        headers={"Content-Type": "text/html"},
        encoding="ISO-8859-1",
        apparent_encoding="SHIFT_JIS",
        content=sjis_html,
        text=sjis_html.decode("latin-1"),
    )
    assert "現在値 2,500円" in decode_http_response_text(resp_sjis)

    # Legacy mock without raw bytes still falls back cleanly to .text
    legacy_mock = SimpleNamespace(headers={}, content=None, text="fallback text")
    assert decode_http_response_text(legacy_mock) == "fallback text"


def test_minkabu_and_kabutan_decode_japanese_utf8_when_requests_defaults_latin1():
    minkabu_html = (
        "<html><body><div>現在値</div><div>2,500円</div></body></html>"
    ).encode()
    minkabu_resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "text/html"},
        encoding="ISO-8859-1",
        content=minkabu_html,
        text=minkabu_html.decode("latin-1"),
        close=lambda: None,
    )
    provider = MinkabuProvider()
    fake_client = MagicMock()
    fake_client.get.return_value = minkabu_resp
    with (
        patch("services.fallback_provider._is_scraper_blocked", return_value=False),
        patch.object(provider, "_get_client", return_value=(fake_client, True)),
    ):
        quote = provider.get_latest_quote("7203.T")
    assert quote is not None
    assert quote["regularMarketPrice"] == 2500.0

    kabutan_html = (
        '<html><body><div class="si_i1_2"><span class="kabuka">3,000</span></div>'
        '<dl class="si_i1_dl1"><dd>▲25.5</dd><dd>▼0.84%</dd></dl>'
        "</body></html>"
    ).encode()
    kabutan_resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "text/html"},
        encoding="ISO-8859-1",
        content=kabutan_html,
        text=kabutan_html.decode("latin-1"),
        close=lambda: None,
    )
    scraper = YahooJPRealtimeScraper()
    mock_session = MagicMock()
    mock_session.get.return_value = kabutan_resp
    with (
        patch("services.realtime.scrapers._is_scraper_blocked", return_value=False),
        patch.object(scraper, "_get_session", return_value=mock_session),
    ):
        k_quote = scraper._fetch_kabutan_symbol("7203.T")
    assert k_quote is not None
    assert k_quote["price"] == 3000.0
    assert k_quote["change"] == -25.5
    assert k_quote["change_percent"] == -0.84


def test_trend_sources_rss_passes_raw_bytes_and_isolates_thread_sessions():
    xml_bytes = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<rss><channel><item><title>半導体株が上昇</title></item></channel></rss>"
    ).encode()
    resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/xml"},
        encoding="ISO-8859-1",
        content=xml_bytes,
        text=xml_bytes.decode("latin-1"),
        raise_for_status=lambda: None,
        close=lambda: None,
    )
    fake_sess = MagicMock()
    fake_sess.get.return_value = resp
    seen_parse_args: list[Any] = []

    def _capture_parse(arg: Any) -> SimpleNamespace:
        seen_parse_args.append(arg)
        return SimpleNamespace(entries=[SimpleNamespace(title="半導体株が上昇")])

    with (
        patch("trend_sources._get_trend_session", return_value=fake_sess),
        patch("trend_sources.feedparser.parse", side_effect=_capture_parse),
    ):
        feed = trend_sources._fetch_rss_feed("https://example.com/rss")

    assert seen_parse_args == [xml_bytes]
    assert feed is not None
    assert feed.entries[0].title == "半導体株が上昇"

    # Distinct requests.Session per thread + clean cleanup & recreation
    trend_sources._cleanup_trend_session()
    thread_sessions: dict[int, int] = {}
    barrier = threading.Barrier(2)

    def _worker(idx: int) -> None:
        s = trend_sources._get_trend_session()
        thread_sessions[idx] = id(s)
        barrier.wait(timeout=5)

    t1 = threading.Thread(target=_worker, args=(1,))
    t2 = threading.Thread(target=_worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(set(thread_sessions.values())) == 2
    # Keep a strong reference to the old session: CPython may reuse the id()
    # of a freed object, so id comparison after cleanup is not reliable.
    old_session = trend_sources._get_trend_session()
    trend_sources._cleanup_trend_session()
    new_session = trend_sources._get_trend_session()
    assert new_session is not old_session
    trend_sources._cleanup_trend_session()


def test_session_manager_timeout_clamping_rejects_invalid_and_bool_values():
    mgr = YFinanceSessionManager()
    recorded_timeouts: list[Any] = []

    class _FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.cookies = SimpleNamespace(clear=lambda: None)

        def request(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            recorded_timeouts.append(kwargs.get("timeout"))
            return SimpleNamespace(status_code=200)

        def mount(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    fake_sess = _FakeSession()
    with (
        patch("session_manager.CURL_CFFI_AVAILABLE", False),
        patch("requests.Session", return_value=fake_sess),
    ):
        sess = mgr._create_session("Mozilla/5.0", 0)

    for bad_timeout in (float("nan"), float("inf"), -2.0, 0.0, True, "bad"):
        sess.request("GET", "https://example.com", timeout=bad_timeout)
    sess.request("GET", "https://example.com", timeout=45.0)
    sess.request("GET", "https://example.com", timeout=(float("nan"), 90.0))
    sess.request("GET", "https://example.com", timeout=(3.5, -1.0))

    assert recorded_timeouts == [
        15.0,
        15.0,
        15.0,
        15.0,
        15.0,
        15.0,
        15.0,
        (15.0, 15.0),
        (3.5, 15.0),
    ]

    # Concurrent 401/429 block tracking stays exception-free and lock-safe
    errors: list[Exception] = []
    fake_resp = SimpleNamespace(url="https://example.com", headers={})

    def _spam_block() -> None:
        try:
            for code in (401, 429, 401, 403):
                mgr._handle_block(code, fake_resp)
        except Exception as exc:  # pragma: no cover - assertion checks empty
            errors.append(exc)

    with patch.object(mgr, "_rotate_user_agent"):
        threads = [threading.Thread(target=_spam_block) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    assert errors == []
    mgr.reset_consecutive_401_count()
    mgr.clear_rate_limit("yfinance")


def test_openai_temperature_normalization_rejects_bool_and_nonfinite():
    assert _normalize_openai_temperature(None) is None
    assert _normalize_openai_temperature(0.0) == 0.0
    assert _normalize_openai_temperature(-0.5) == 0.0
    assert _normalize_openai_temperature(3.2) == 2.0
    for bad in (True, False, float("nan"), float("inf"), float("-inf"), "invalid"):
        with pytest.raises(ValueError):
            _normalize_openai_temperature(bad)


# ---------------------------------------------------------------------------
# Group 2: Copy-on-write caches, strict portfolio load, volume, engine purge
# ---------------------------------------------------------------------------


def test_ensure_stock_placeholder_in_caches_copy_on_write_and_interpolator_abort():
    orig_item = {"symbol": "AAPL", "name": "Apple", "market": "us", "price": 100.0, "change": 0.0}
    orig_list = [orig_item]
    orig_target = app_state.market.target_stocks_cache
    orig_current = app_state.market.current_stocks_cache
    orig_indices = app_state.market.current_indices_cache
    orig_shutdown = app_state.execution.shutdown_event
    try:
        app_state.market.target_stocks_cache = {"us": [dict(orig_item)], "jp": [], "idx": []}
        app_state.market.current_stocks_cache = {"us": orig_list, "jp": [], "idx": []}

        ensure_stock_placeholder_in_caches("NVDA", "Nvidia", "us")
        assert app_state.market.current_stocks_cache["us"] is not orig_list
        assert len(orig_list) == 1
        assert len(app_state.market.current_stocks_cache["us"]) == 2

        # Updating AAPL produces a new dict copy rather than mutating orig_item
        prev_list = app_state.market.current_stocks_cache["us"]
        ensure_stock_placeholder_in_caches("AAPL", "Apple", "us", shares=5, avg_price=95.0)
        assert app_state.market.current_stocks_cache["us"] is not prev_list
        assert "shares" not in orig_item
        assert app_state.market.current_stocks_cache["us"][0]["shares"] == 5

        # bg_interpolate_loop must abort write-back when target_stocks_cache list changes
        current_marker = [{"symbol": "AAPL", "price": 100.0, "change": 0.0}]
        app_state.market.current_stocks_cache = {"us": current_marker, "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 110.0, "change": 10.0}],
            "jp": [],
            "idx": [],
        }
        stop_evt = threading.Event()
        app_state.execution.shutdown_event = stop_evt
        swapped_target = [{"symbol": "MSFT", "price": 200.0, "change": 1.0}]

        def _swap_during_compute(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            app_state.market.target_stocks_cache["us"] = swapped_target
            stop_evt.set()
            return [{"symbol": "AAPL", "price": 105.0, "change": 5.0}]

        with (
            patch.object(app_state.sse_announcer_mode1, "listener_count", return_value=1),
            patch(
                "bg.sse_interpolator._interpolate_and_fluctuate_market",
                side_effect=_swap_during_compute,
            ),
            patch.object(stop_evt, "wait", return_value=False),
        ):
            bg_interpolate_loop()

        assert app_state.market.current_stocks_cache["us"] is current_marker
        assert app_state.market.target_stocks_cache["us"] is swapped_target
    finally:
        app_state.market.target_stocks_cache = orig_target
        app_state.market.current_stocks_cache = orig_current
        app_state.market.current_indices_cache = orig_indices
        app_state.execution.shutdown_event = orig_shutdown


def test_ai_portfolio_corrupted_storage_fails_before_external_calls():
    with (
        patch.object(
            ai_pf_svc,
            "_load_saved_ai_portfolios_strict",
            side_effect=ai_pf_svc.PortfolioStorageError("corrupted json", status_code=500),
        ),
        patch.object(ai_pf_svc, "collect_symbol_research_context") as mock_search,
    ):
        with pytest.raises(ai_pf_svc.PortfolioStorageError):
            ai_pf_svc.generate_ai_portfolio_by_theme("AI", api_key="test-key")
    mock_search.assert_not_called()


def test_build_market_row_extracts_regular_market_volume_fallback():
    row = _build_market_row(
        "AAPL",
        "us",
        {"price": 150.0, "volume": None, "regularMarketVolume": 1234567},
        fallback_name="AAPL",
    )
    assert row["volume"] == 1234567.0


def test_realtime_engine_unregister_purges_all_jp_token_variants():
    engine = RealtimeMarketEngine()
    engine.yahoojp_scraper._start_background = lambda: None  # type: ignore[method-assign]
    tok_t = object()
    tok_bare = object()
    with engine.store_lock:
        engine._registration_tokens[("jp", "7203.T")] = tok_t
        engine._registration_tokens[("jp", "7203")] = tok_bare
    engine.unregister_symbol("7203.T", "jp")
    with engine.store_lock:
        assert ("jp", "7203.T") not in engine._registration_tokens
        assert ("jp", "7203") not in engine._registration_tokens


# ---------------------------------------------------------------------------
# Group 3: Async chat persistence and AI portfolio token conflict/lifecycle
# ---------------------------------------------------------------------------


def test_async_chat_persists_assistant_reply_even_without_second_poll():
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    orig_executor = app_state.execution.executor
    real_executor = ThreadPoolExecutor(max_workers=2)
    block_event = threading.Event()
    symbol = "AAPLASYNC"
    token = "async-persist-token-001"
    conv_scope = "conv-async-persist-01"
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app_state.execution.executor = real_executor
    with app_state.ai.chat_history_lock:
        app_state.ai.chat_history.clear()

    try:
        with (
            patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch("routes.api_analysis.CHAT_PREPARE_WAIT_SEC", 0.01),
            patch(
                "routes.api_analysis._call_mistral_chat_with_retry",
                side_effect=lambda *a, **k: (
                    block_event.wait(timeout=5),
                    "Async persisted reply",
                )[1],
            ),
            app.test_client() as client,
        ):
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = conv_scope
            chat_key = f"{conv_scope}:us:{symbol}"
            resp = client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": symbol,
                    "message": "Persist even if client drops?",
                    "request_token": token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert resp.status_code == 200
            assert json.loads(resp.data).get("fetching") is True
            block_event.set()
            real_executor.shutdown(wait=True)

            # Without issuing a second poll, history already holds the assistant reply
            with app_state.ai.chat_history_lock:
                msgs = list(app_state.ai.chat_history[chat_key])
            assistant_contents = [m["content"] for m in msgs if m["role"] == "assistant"]
            assert "Async persisted reply" in assistant_contents

            # Polling afterwards returns the reply without duplicating history
            poll_resp = client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": symbol,
                    "message": "Persist even if client drops?",
                    "request_token": token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert poll_resp.status_code == 200
            with app_state.ai.chat_history_lock:
                msgs_after = list(app_state.ai.chat_history[chat_key])
            assert (
                len([m for m in msgs_after if m["content"] == "Async persisted reply"])
                == 1
            )
    finally:
        block_event.set()
        real_executor.shutdown(wait=False)
        app_state.execution.executor = orig_executor
        with ra.chat_fetch_lock:
            ra.chat_fetch_inflight.clear()
            ra.chat_result_cache.clear()
            ra.chat_result_fingerprints.clear()
        if orig_testing is None:
            app.config.pop("TESTING", None)
        else:
            app.config["TESTING"] = orig_testing
        if orig_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_ai_portfolio_token_conflict_409_and_rebalance_delivered_flag():
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    token = "conflict-token-0123456789"
    scope = "scope-conflict-012345"
    remote = {"REMOTE_ADDR": "192.168.10.55"}

    try:
        with (
            patch(
                "routes.stocks.ai_portfolio.require_trusted_or_admin",
                return_value=(True, None),
            ),
            patch("routes.stocks.ai_portfolio.extract_api_key", return_value="test-key-32"),
            app.test_client() as client,
        ):
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = scope

            with ai_pf_routes.ai_portfolio_fetch_lock:
                ai_pf_routes.ai_portfolio_result_cache[f"generate:{scope}:AI"] = (
                    time.time(),
                    {"id": "p1", "theme": "AI", "items": []},
                    None,
                )
                ai_pf_routes.ai_portfolio_result_cache[f"rebalance:{scope}:AI"] = (
                    time.time(),
                    {"id": "p1", "theme": "AI", "items": []},
                    None,
                )

            # First generate binds token -> ("generate", scope, "AI")
            r1 = client.post(
                "/api/ai-portfolio/generate",
                json={"theme": "AI", "request_token": token},
                environ_base=remote,
            )
            assert r1.status_code == 200

            # Reusing same token for different theme -> 409
            r_diff_theme = client.post(
                "/api/ai-portfolio/generate",
                json={"theme": "Semiconductors", "request_token": token},
                environ_base=remote,
            )
            assert r_diff_theme.status_code == 409

            # Reusing same token on /rebalance -> 409
            r_diff_op = client.post(
                "/api/ai-portfolio/rebalance",
                json={"theme": "AI", "request_token": token},
                environ_base=remote,
            )
            assert r_diff_op.status_code == 409

            # Rebalance delivery marks its token delivered so it is no longer inflight
            reb_token = "rebalance-token-0123456789"
            r_reb = client.post(
                "/api/ai-portfolio/rebalance",
                json={"theme": "AI", "request_token": reb_token},
                environ_base=remote,
            )
            assert r_reb.status_code == 200
            assert _is_polling_token_inflight(reb_token) is False
    finally:
        with ai_pf_routes.ai_portfolio_fetch_lock:
            ai_pf_routes.ai_portfolio_fetch_inflight.clear()
            ai_pf_routes.ai_portfolio_result_cache.clear()
            ai_pf_routes.ai_portfolio_token_records.clear()
        if orig_testing is None:
            app.config.pop("TESTING", None)
        else:
            app.config["TESTING"] = orig_testing
        if orig_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = orig_csrf


# ---------------------------------------------------------------------------
# Group 4: Frontend JS VM regression tests (Bugs 9..15)
# ---------------------------------------------------------------------------


def test_frontend_ui_sparkline_min_points_and_fullscreen_stock_key_guard():
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

function makeEl(tag = "div", cls = "", text = "") {
  const classes = new Set(String(cls || "").split(/\s+/).filter(Boolean));
  const el = {
    tagName: String(tag).toUpperCase(),
    id: "",
    className: cls || "",
    textContent: text || "",
    disabled: false,
    children: [],
    style: {},
    dataset: {},
    parentNode: null,
    _queryMap: {},
    _attrs: {},
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      if (String(child.className).includes("ai-tech-loading-overlay")) {
        this._overlay = child;
      }
      return child;
    },
    append(child) { return this.appendChild(child); },
    remove() {
      const p = this.parentNode;
      if (!p) return;
      const i = p.children.indexOf(this);
      if (i >= 0) p.children.splice(i, 1);
      if (p._overlay === this) p._overlay = null;
    },
    querySelector(sel) {
      if (String(sel).includes("ai-tech-loading-overlay")) return this._overlay || null;
      return this._queryMap[sel] || null;
    },
    querySelectorAll(sel) { return this._queryMap[sel] || []; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k] ?? null; },
    removeAttribute(k) { delete this._attrs[k]; },
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      contains: (c) => classes.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
    },
  };
  return el;
}

const fsModal = makeEl("div", "chart-fullscreen-modal"); // open!
fsModal.dataset.stockKey = "us:MSFT";
const fsBtn = makeEl("button", "ai-tech-lines-btn", "✨ AIテクニカル描画");
fsBtn.id = "fs-ai-tech-lines-btn";
const fsCanvasWrapper = makeEl("div", "fs-chart-canvas-wrapper");
fsCanvasWrapper.parentNode = { insertBefore() {} };
const fsCanvas = makeEl("canvas", "fs-chart-canvas");

const doc = {
  readyState: "loading",
  activeElement: null,
  addEventListener() {},
  getElementById(id) {
    if (id === "chart-fullscreen-modal") return fsModal;
    if (id === "fs-ai-tech-lines-btn") return fsBtn;
    if (id === "fs-chart-canvas-wrapper") return fsCanvasWrapper;
    if (id === "fs-chart-canvas") return fsCanvas;
    return null;
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
};

const drawChartCalls = [];
const reqs = [];
const context = {
  console,
  document: doc,
  window: {
    APP_CONFIG: { is_ai_technical_lines_eligible: true, ai_provider: "mistral" },
    devicePixelRatio: 1,
    addEventListener() {},
    removeEventListener() {},
  },
  state: { subscribe() {} },
  logger: { error() {}, warn() {}, info() {} },
  showToast() {},
  getChartPref: (_k, _n, fb) => fb,
  getFreshPrefetchedHistory: () => ({ formattedData: [1, 2], ohlcData: [{ x: 1, c: 1 }, { x: 2, c: 2 }] }),
  refreshStockChart: async () => {},
  drawChart: (...args) => drawChartCalls.push(args),
  getStockByKey: () => null,
  csrfFetch: () => new Promise((resolve) => reqs.push(resolve)),
  createEl: (tag, cls, txt) => makeEl(tag, cls, txt),
  AbortController,
  Map, Set, Math, Number, Array, Object, JSON, Date, Promise, Error,
  setTimeout, clearTimeout, setImmediate,
  requestAnimationFrame: () => 1,
  cancelAnimationFrame: () => {},
};
context.globalThis = context;
context.self = context;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "ui.js"), "utf8") +
  "\n;this.drawSparkline = drawSparkline; this.triggerAiTechnicalLines = triggerAiTechnicalLines; this.hasValidSparklineData = hasValidSparklineData;",
  context,
);

const visCalls = [];
context.setSparklineVisibility = (_w, v) => visCalls.push(v);
context.destroyChart = () => {};
context.getSparklineSignature = () => null;
const sparkCanvas = makeEl("canvas", "spark-canvas");
const sparkWrapper = makeEl("div", "stock-wrapper");
sparkWrapper.dataset.stockKey = "us:AAPL";
sparkWrapper._queryMap[".spark-canvas"] = sparkCanvas;

assert.strictEqual(context.hasValidSparklineData([{ price: 100 }]), false);
assert.strictEqual(context.hasValidSparklineData([{ price: 100 }, { price: null }]), false);
assert.strictEqual(context.hasValidSparklineData([{ price: 100 }, { c: 101 }]), true);

// Single-point drawSparkline hides sparkline wrapper instead of leaving blank box
context.drawSparkline(sparkWrapper, [{ price: 100 }]);
assert.strictEqual(visCalls[visCalls.length - 1], false);

(async () => {
  const aaplBtn = makeEl("button", "ai-tech-lines-btn", "✨ AIテクニカル描画");
  const aaplContainer = makeEl("div", "chart-container");
  aaplContainer.parentNode = { insertBefore() {} };
  const aaplWrapper = makeEl("div", "stock-wrapper");
  aaplWrapper.dataset.stockKey = "us:AAPL";
  aaplWrapper.__stockData = { symbol: "AAPL", market: "us" };
  aaplWrapper._queryMap[".ai-tech-lines-btn"] = [aaplBtn];
  aaplWrapper._queryMap[".chart-container"] = aaplContainer;

  // Trigger AAPL while fullscreen modal is showing MSFT -> MSFT modal must stay untouched
  const p = context.triggerAiTechnicalLines(aaplWrapper);
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(aaplBtn.disabled, true);
  assert.strictEqual(fsBtn.disabled, false);
  assert.strictEqual(Boolean(fsCanvasWrapper._overlay), false);

  reqs[0]({ ok: true, status: 200, json: async () => ({ ok: true, summary: "", lines: [] }) });
  await p;
  assert.strictEqual(aaplBtn.disabled, false);
  assert.strictEqual(drawChartCalls.length, 0);
  process.stdout.write("ok");
})();
"""
    assert _run_node(script) == "ok"


def test_frontend_realtime_api_chart_tv_heatmap_ai_dive_regressions():
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

// 1) realtime_client.js triggers debouncedRenderPortfolio on #tab-portfolio delta
let activeTabId = "tab-portfolio";
let portfolioRenders = 0;
const rtWindow = {
  state: { stocks: { us: [{ symbol: "AAPL", market: "us", price: 100, change: 0 }], jp: [], idx: [] } },
  debouncedRenderPortfolio: () => { portfolioRenders++; },
  updateExistingCard: () => {},
  addEventListener() {},
  removeEventListener() {},
};
const rtCtx = {
  window: rtWindow,
  self: rtWindow,
  globalThis: rtWindow,
  document: {
    readyState: "complete",
    addEventListener() {},
    querySelector: (sel) => (sel === ".tab.active" ? { id: activeTabId } : null),
    querySelectorAll: () => [],
  },
  console, Math, Number, Object, Array, Set, Map, Date, setTimeout, clearTimeout,
};
vm.createContext(rtCtx);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "realtime_client.js"), "utf8"),
  rtCtx,
);
rtWindow.realtimeClient._applyDeltasNow({ AAPL: { price: 105 } });
assert.strictEqual(portfolioRenders, 1);
activeTabId = "tab-stocks";
rtWindow.realtimeClient._applyDeltasNow({ AAPL: { price: 106 } });
assert.strictEqual(portfolioRenders, 1);

// 2) api.js loadNews treats TimeoutError like AbortError (not fatal error)
const apiSrc = fs.readFileSync(path.join(process.cwd(), "static", "js", "api.js"), "utf8");
function extractNamedBlock(src, marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(`marker not found: ${marker}`);
  const braceStart = src.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < src.length; i++) {
    const ch = src[i];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unclosed block: ${marker}`);
}
const loadNewsBlock = extractNamedBlock(apiSrc, "async function loadNews(forceRefresh = false)");
function makeNewsEl(cls = "") {
  const el = {
    className: cls,
    textContent: "",
    style: {},
    children: [],
    parentNode: null,
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    replaceChildren() { this.children = []; },
    classList: { add() {}, remove() {}, contains: () => false },
  };
  return el;
}
const newsMetaStats = makeNewsEl();
const usBox = makeNewsEl();
const jpBox = makeNewsEl();
const trendsBox = makeNewsEl();
const newsToasts = [];
const newsCtx = {
  AbortController,
  Math,
  Date,
  JSON,
  Error,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (fn) => fn(),
  HAS_MISTRAL_API_KEY: true,
  state: { isLoadingNews: false },
  DOM: {
    get: (id) => ({
      "news-us": usBox,
      "news-jp": jpBox,
      "news-trends": trendsBox,
      "newsRefreshBtn": makeNewsEl(),
      "news-meta-stats": newsMetaStats,
    }[id] || null),
  },
  document: { createElement: () => makeNewsEl() },
  setButtonLoading() {},
  resetButton() {},
  createRequestToken: () => "tok-news-timeout",
  getNewsCooldownRemaining: () => 0,
  $logger: { error() {}, warn() {}, info() {} },
  showToast: (m, c) => newsToasts.push({ m, c }),
  CONSTANTS: { TIMEOUT: { NEWS_REQUEST: 10000 } },
  apiFetch: async () => {
    const err = new Error("Request timed out");
    err.name = "TimeoutError";
    throw err;
  },
};
vm.createContext(newsCtx);
vm.runInContext(loadNewsBlock + "; this.loadNews = loadNews;", newsCtx);

// 3) chart.js parses ISO string d.x timestamps and uses stock/wrapper currency
let capturedChartCfg = null;
let createdChartInstance = null;
function ChartMock(_ctx, cfg) {
  capturedChartCfg = cfg;
  createdChartInstance = { update() {}, destroy() {}, canvas: {} };
  return createdChartInstance;
}
ChartMock.register = () => {};
const chartCtx = {
  Chart: ChartMock,
  chartInstances: new Map(),
  getChartPref: (_k, _p, fb) => fb,
  getDatasetHiddenStateByLabel: () => null,
  getChartAnimationEnabled: () => false,
  _safeDestroyChart() {},
  showChartError() { throw new Error("unexpected showChartError"); },
  chartErrorHosts: () => [],
  NO_HISTORY_MSG: "no history",
  CHART_TOOLTIP_DEFAULTS: {},
  Number, Math, Date, Array, Object, Set, Map,
};
const chartCanvas = { getContext: () => ({}) };
const idxWrapper = {
  dataset: { stockKey: "idx:^N225", market: "idx" },
  __stockData: { symbol: "^N225", market: "idx", currency: "JPY" },
  querySelector: (sel) => (sel === ".chart-canvas" ? chartCanvas : null),
  querySelectorAll: () => [],
};
vm.createContext(chartCtx);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "chart.js"), "utf8"),
  chartCtx,
);
const sampleIsoBars = [
  { x: "2026-10-01T00:00:00Z", o: 38000, h: 38500, l: 37900, c: 38400 },
  { x: "2026-10-02", o: 38400, h: 38700, l: 38200, c: 38600 },
];
chartCtx.drawChart(idxWrapper, sampleIsoBars, sampleIsoBars, {});
assert(createdChartInstance !== null, "chart must be created for ISO d.x timestamps");
assert.strictEqual(createdChartInstance.$currency, "JPY");
assert(capturedChartCfg.data.datasets[0].data[0].x > 0);

// 4) tradingview_manager.js omits US exchange switches for FX:/TVC:/CAPITALCOM:/BITSTAMP:
function makeSimpleEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    id: "",
    className: "",
    textContent: "",
    style: {},
    children: [],
    isConnected: true,
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute() {},
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren() { this.children = []; },
    querySelectorAll: () => [],
  };
}
const tvContainer = makeSimpleEl("div");
const tvWindow = { TradingView: { widget: function () {} }, addEventListener() {}, removeEventListener() {} };
const tvCtx = {
  window: tvWindow,
  document: {
    body: { classList: { contains: () => false } },
    getElementById: (id) => (id === "tv-box" ? tvContainer : null),
    createElement: (t) => makeSimpleEl(t),
  },
  console, Set, String, Number, Math, Array, Object, Promise, setTimeout, clearTimeout,
};
vm.createContext(tvCtx);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "tradingview_manager.js"), "utf8"),
  tvCtx,
);
tvWindow.TradingViewManager.renderAdvancedChart("tv-box", "FX:USDJPY");
assert.strictEqual(tvContainer.children[0].children[1].children.length, 0);
tvContainer.replaceChildren();
tvWindow.TradingViewManager.renderAdvancedChart("tv-box", "AAPL");
assert.strictEqual(tvContainer.children[0].children[1].children.length, 3);

// 5) heatmap.js clears .heatmap-3d-error when switching back to 2D
const heatmapSrc = fs.readFileSync(path.join(process.cwd(), "static", "js", "heatmap.js"), "utf8");
function makeHmEl(id = "") {
  const listeners = {};
  const classes = new Set();
  const el = {
    id,
    className: "",
    textContent: "",
    value: "",
    style: { setProperty() {} },
    children: [],
    parentNode: null,
    dataset: {},
    setAttribute() {},
    getBoundingClientRect: () => ({ width: 800, height: 600 }),
    addEventListener(ev, fn) { (listeners[ev] ||= []).push(fn); },
    trigger(ev) { (listeners[ev] || []).forEach((fn) => fn({ key: "Enter" })); },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    replaceChildren() { this.children = []; },
    remove() {
      if (!this.parentNode) return;
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
      this.parentNode = null;
    },
    querySelector(sel) {
      if (sel === ".heatmap-3d-error") {
        return this.children.find((c) => c.className === "heatmap-3d-error") || null;
      }
      return null;
    },
    querySelectorAll: () => [],
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      contains: (c) => classes.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
    },
  };
  return el;
}
const hmDocListeners = {};
const hmToggleBar = makeHmEl("view-mode-toggle-container");
const hmView2d = makeHmEl("view-2d");
hmView2d.classList.add("active");
const hmView3d = makeHmEl("view-3d");
const hmIds = {
  "view-mode-toggle-container": hmToggleBar,
  "view-2d": hmView2d,
  "view-3d": hmView3d,
  "heatmap-canvas": makeHmEl("heatmap-canvas"),
  "heatmap-3d-canvas": makeHmEl("heatmap-3d-canvas"),
  "heatmap-3d-controls": makeHmEl("heatmap-3d-controls"),
};
const hmCreated = [];
const hmDoc = {
  addEventListener(ev, fn) { (hmDocListeners[ev] ||= []).push(fn); },
  getElementById: (id) => hmIds[id] || makeHmEl(id),
  createElement: () => {
    const e = makeHmEl();
    hmCreated.push(e);
    return e;
  },
  querySelectorAll(sel) {
    if (sel === ".heatmap-3d-error") {
      return hmToggleBar.children.filter((c) => c.className === "heatmap-3d-error");
    }
    return [];
  },
  body: hmToggleBar,
};
const hmCtx = {
  document: hmDoc,
  window: { innerWidth: 1280, innerHeight: 800, addEventListener() {}, removeEventListener() {} },
  toFiniteNumber: (v, d = 0) => (Number.isFinite(Number(v)) ? Number(v) : d),
  apiFetch: async () => ({
    data: {
      stocks: [{ symbol: "AAPL", price: 150, market_cap: 1e12, change_percent: 1.0, sector: "Tech" }],
    },
  }),
  console, AbortController, Map, Set, Array, Object, Number, Math, Date,
  setTimeout, clearTimeout,
  fetch: async () => ({ ok: true, json: async () => ({ stocks: [] }) }),
};
vm.createContext(hmCtx);
vm.runInContext(heatmapSrc, hmCtx);
(hmDocListeners.DOMContentLoaded || []).forEach((fn) => fn());
hmView3d.trigger("click");
assert.strictEqual(hmToggleBar.children.filter((c) => c.className === "heatmap-3d-error").length, 1);
hmView2d.trigger("click");
assert.strictEqual(hmToggleBar.children.filter((c) => c.className === "heatmap-3d-error").length, 0);

// 6) ai-dive-controller.js preserves initial start label when first run errors
const aiDiveSrc = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "experimental", "ai-dive-controller.js"),
  "utf8",
);
const startBtn = {
  disabled: false,
  textContent: "✨ AI深層分析を開始",
  addEventListener() {},
};
const tier4 = makeSimpleEl("div");
let callCount = 0;
const diveGlobals = {
  AbortController,
  createRequestToken: () => "tok-dive",
  apiFetch: async () => {
    callCount++;
    if (callCount === 1) throw new Error("network down");
    return {
      ok: true,
      status: 200,
      json: async () => ({ recommendation: "BUY", summary: "ok" }),
    };
  },
};
const diveDoc = {
  readyState: "complete",
  createElement: (t) => makeSimpleEl(t),
  createTextNode: (t) => ({ nodeType: 3, textContent: t }),
  getElementById: () => null,
  addEventListener() {},
};
const diveCtx = {
  window: diveGlobals,
  globalThis: diveGlobals,
  document: diveDoc,
  console, AbortController, Map, Set, Number, String, JSON, Error,
};
diveGlobals.AiDiveController = undefined;
vm.createContext(diveCtx);
vm.runInContext(aiDiveSrc, diveCtx);
const Controller = diveGlobals.AiDiveController;
assert(typeof Controller === "function");

// Stub instance invoking bind/run behavior directly
const fakeState = {
  state: { aiDiveOpen: true, aiDiveSymbol: "AAPL", stocks: new Map([["AAPL", { symbol: "AAPL", market: "us", price: 100 }]]) },
  subscribe: () => () => {},
};
const ctrl = new Controller(fakeState, { tier4Container: tier4, startAiBtn: startBtn });
ctrl.renderAiAnalysisResult = () => {};

(async () => {
  await newsCtx.loadNews();
  assert.strictEqual(newsToasts.length, 0);
  assert.strictEqual(newsMetaStats.children.length, 1);
  assert(newsMetaStats.children[0].textContent.includes("タイムアウト"));

  await ctrl.runDeepAiAnalysis();
  assert.strictEqual(startBtn.textContent, "✨ AI深層分析を開始");
  await ctrl.runDeepAiAnalysis();
  assert.strictEqual(startBtn.textContent, "🔄 AI分析を再実行");
  ctrl.resetTier4Ai();
  assert.strictEqual(ctrl._hasAnalysisResult, false);
  process.stdout.write("ok");
})();
"""
    assert _run_node(script) == "ok"
