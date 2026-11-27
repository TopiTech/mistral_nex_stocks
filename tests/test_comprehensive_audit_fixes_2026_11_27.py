"""Regression and edge-case tests for the 2026-11-27 comprehensive audit fixes."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run(
        [node, "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, (
        f"node failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# Unit 1: Schemas, Validators, Envelopes, MarketState, SessionManager, Manifest
# ---------------------------------------------------------------------------


def test_screener_schema_handles_oversized_int_without_overflow() -> None:
    """ScreenerQueryRequest converts int >= 10**309 to ValidationError instead of OverflowError."""
    from pydantic import ValidationError

    from route_helpers import _parse_screener_request
    from schemas.stocks import ScreenerQueryRequest

    huge = 10**400
    with pytest.raises(ValidationError) as exc_info:
        ScreenerQueryRequest(min_price=huge)
    assert "must_be_finite" in str(exc_info.value)

    from app import app

    with app.app_context():
        parsed, err = _parse_screener_request({"min_price": huge})
    assert parsed is None
    assert err is not None
    resp, status = err
    assert status == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "min_price" in body.get("details", {}).get("fields", [])


def test_error_envelopes_drop_non_finite_floats_from_json() -> None:
    """_sanitize_provider_error_scalar and error_response must never emit NaN/Infinity JSON."""
    from app import app
    from error_codes import ErrorCode
    from utils.stock_payload import error_response
    from utils.validators import _sanitize_provider_error_scalar

    assert _sanitize_provider_error_scalar(float("nan")) is None
    assert _sanitize_provider_error_scalar(float("inf")) is None
    assert _sanitize_provider_error_scalar(float("-inf")) is None
    assert _sanitize_provider_error_scalar(True) is True
    assert _sanitize_provider_error_scalar(429) == 429

    def _reject_nonfinite(constant: str) -> None:
        raise ValueError(f"Non-standard JSON constant emitted: {constant}")

    with app.app_context():
        resp, status = error_response(
            ErrorCode.API_SERVICE_ERROR,
            status_code=502,
            details={"retry_after": float("nan"), "nested": [float("inf"), 1.5]},
            extra_fields={"bad_float": float("-inf")},
        )
    assert status == 502
    raw = resp.get_data(as_text=True)
    parsed = json.loads(raw, parse_constant=_reject_nonfinite)
    assert parsed["details"]["retry_after"] is None
    assert parsed["details"]["nested"] == [None, 1.5]
    assert parsed["bad_float"] is None


def test_market_state_circuit_resilient_to_non_finite_timestamps() -> None:
    """MarketDataState circuit helpers reject NaN/Inf/huge-int timestamps without crashing or wedging."""
    from market_state import CircuitState, MarketDataState

    ms = MarketDataState()
    svc = "mistral"
    with ms.circuit_lock:
        ms.circuit_states[svc] = CircuitState(
            status="OPEN",
            timeout_streak=3,
            open_until=float("nan"),
            probing=False,
        )
    assert ms.peek_circuit_open(svc) is False
    assert ms.is_circuit_open(svc) is False

    # Now in HALF_OPEN with corrupt probing_since values
    with ms.circuit_lock:
        ms.circuit_states[svc]["status"] = "HALF_OPEN"
        ms.circuit_states[svc]["probing"] = True
        for corrupt_ts in (float("inf"), float("nan"), 10**400, True):
            ms.circuit_states[svc]["probing_since"] = corrupt_ts
            assert ms.try_claim_circuit_probe(svc) is False


def test_session_manager_reclaims_captured_epoch_snapshot() -> None:
    """mark_rate_limited and _rotate_user_agent pass the locked epoch snapshot, not unlocked self._session_epoch."""
    from session_manager import YFinanceSessionManager

    mgr = YFinanceSessionManager()
    seen_epochs: list[int] = []

    def _spy_reclaim(epoch_arg: int) -> None:
        seen_epochs.append(epoch_arg)
        # Simulate another thread mutating _session_epoch concurrently
        with mgr._lock:
            mgr._session_epoch = 9999

    try:
        with (
            patch.object(mgr, "_reclaim_epoch_sessions", side_effect=_spy_reclaim),
            patch("session_manager.reset_yfinance_auth"),
        ):
            with mgr._lock:
                start_epoch = mgr._session_epoch
            mgr.mark_rate_limited("yfinance", duration=10)
            assert seen_epochs == [start_epoch + 1]

            mgr._rotate_user_agent()
            # Captured epoch 9999 + 1 = 10000 inside lock cleanly
            assert seen_epochs[-1] == 10000
    finally:
        mgr.close_all()


def test_conftest_manifest_origin_matches_validator_contract() -> None:
    """conftest ensure_manifest_exists generates a valid trailing-slash chrome-extension origin."""
    conftest_src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert '"chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/"' in conftest_src


# ---------------------------------------------------------------------------
# Unit 2: OpenAI 3xx redirect blocking & realtime engine finite integer guard
# ---------------------------------------------------------------------------


def test_openai_service_blocks_3xx_redirects_and_tags_provider() -> None:
    """verify/chat/stream/embeddings reject 3xx redirects without parsing body or advancing circuit failure."""
    from app_state import app_state
    from market_state import CircuitState
    from services import openai_service as os_mod

    redirect_resp = SimpleNamespace(
        status_code=302,
        text="Redirecting",
        headers={"Location": "http://169.254.169.254/latest"},
        json=MagicMock(side_effect=AssertionError("3xx response body must not be parsed as JSON")),
    )
    redirect_client = MagicMock()
    redirect_client.get.return_value = redirect_resp
    redirect_client.post.return_value = redirect_resp
    redirect_client.__enter__.return_value = redirect_client
    redirect_client.__exit__.return_value = False

    with (
        patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
        patch("services.openai_service._create_httpx_client", return_value=redirect_client),
    ):
        ver = os_mod.verify_openai_connection("https://api.example.test/v1", "sk-real-key-12345")
    assert ver["ok"] is False
    assert ver["valid"] is False
    assert "302" in ver["error"]

    # OpenRouter /auth/key redirect must also be rejected cleanly
    models_ok = SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gpt-4o-mini"}]})
    openrouter_client = MagicMock()
    openrouter_client.get.side_effect = [models_ok, redirect_resp]
    openrouter_client.__enter__.return_value = openrouter_client
    openrouter_client.__exit__.return_value = False
    with (
        patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
        patch("services.openai_service._create_httpx_client", return_value=openrouter_client),
    ):
        or_ver = os_mod.verify_openai_connection("https://openrouter.ai/api/v1", "sk-or-v1-test")
    assert or_ver["ok"] is False
    assert or_ver["valid"] is False
    assert "302" in or_ver["error"]

    # Chat non-stream + stream + embeddings reject 302 cleanly and release HALF_OPEN probe
    try:
        with app_state.market.circuit_lock:
            app_state.market.circuit_states["openai_compatible"] = CircuitState(
                status="HALF_OPEN",
                timeout_streak=1,
                open_until=0.0,
                probing=False,
            )
        with (
            patch("services.openai_service.is_preview_api_key", return_value=False),
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
            patch("services.openai_service.get_openai_base_url", return_value="https://api.example.test/v1"),
            patch("services.openai_service.get_openai_model", return_value="gpt-4o-mini"),
            patch("services.openai_service._get_openai_client", return_value=redirect_client),
            patch("services.openai_service._close_call_owned_openai_client"),
        ):
            chat_res = os_mod.call_openai_chat(
                "sk-real-key-12345",
                [{"role": "user", "content": "hello"}],
                use_cache=False,
            )
            assert chat_res["error"]["status_code"] == 302
            assert chat_res["error"]["provider"] == "openai_compatible"
            assert app_state.market.circuit_states["openai_compatible"]["probing"] is False

            # Stream redirect test
            stream_ctx = MagicMock()
            stream_ctx.__enter__.return_value = SimpleNamespace(
                status_code=307,
                text="",
                headers={"Location": "http://169.254.169.254/"},
                iter_lines=MagicMock(side_effect=AssertionError("must not iterate 3xx redirect body")),
            )
            stream_ctx.__exit__.return_value = False
            redirect_client.stream.return_value = stream_ctx

            events = list(
                os_mod.stream_openai_chat("sk-real-key-12345", [{"role": "user", "content": "hi"}])
            )
            assert len(events) == 1
            assert events[0]["type"] == "error"
            assert events[0]["status_code"] == 307
            assert app_state.market.circuit_states["openai_compatible"]["probing"] is False

            # Embeddings redirect aborts remaining batches cleanly after first 302
            redirect_client.post.reset_mock()
            embs = os_mod.get_openai_embeddings_batch(
                ["text1", "text2"], "sk-real-key-12345", batch_size=1
            )
            assert embs == [None, None]
            assert redirect_client.post.call_count == 1
            assert app_state.market.circuit_states["openai_compatible"]["probing"] is False
    finally:
        with app_state.market.circuit_lock:
            app_state.market.circuit_states["openai_compatible"] = CircuitState()


def test_realtime_engine_handles_oversized_int_payload_without_overflow() -> None:
    """RealtimeMarketEngine ticker & PTS ingestion must not raise OverflowError on huge integers."""
    from services.realtime.engine import RealtimeMarketEngine

    engine = RealtimeMarketEngine()
    huge = 10**400
    with (
        patch("services.realtime.engine._get_yfinance_previous_close", return_value=100.0),
        patch("services.realtime.engine._get_yfinance_volume", return_value=1000.0),
    ):
        engine._handle_producer_update(
            {"symbol": "AAPL", "price": huge, "change": huge, "change_percent": huge}
        )
        engine._handle_pts_update(
            {"symbol": "7203.T", "price": huge, "change": huge, "change_percent": huge}
        )
    with engine.store_lock:
        assert "AAPL" in engine.market_store
        assert "7203.T" in engine.pts_store


# ---------------------------------------------------------------------------
# Unit 3: Frontend JS & CSS regressions
# ---------------------------------------------------------------------------


def test_open_modal_focus_trap_includes_anchor_links() -> None:
    """utils.js openModal includes a[href] links in Tab and Shift+Tab focus cycling."""
    script = r"""
const fs = require("fs");
const vm = require("vm");
const utilsSrc = fs.readFileSync("static/js/utils.js", "utf8");
const focused = [];
const mkEl = (name, tag) => ({
  tagName: tag,
  offsetParent: {},
  hasAttribute: () => false,
  closest: () => null,
  getBoundingClientRect: () => ({ width: 80, height: 24 }),
  focus() { focused.push(name); },
});
const linkEl = mkEl("link", "A");
const btnEl = mkEl("btn", "BUTTON");
const selectorsSeen = [];
const listeners = {};
const modalEl = {
  classList: { contains: () => false, remove() {}, add() {} },
  style: {},
  hasAttribute: () => true,
  setAttribute() {},
  removeAttribute() {},
  contains: (el) => el === linkEl || el === btnEl,
  querySelectorAll(sel) {
    selectorsSeen.push(sel);
    return [linkEl, btnEl];
  },
  addEventListener(type, fn) { listeners[type] = fn; },
  removeEventListener() {},
  focus() {},
};
const ctx = {
  document: {
    getElementById: (id) => (id === "m1" ? modalEl : null),
    activeElement: null,
    body: { style: {}, _modalOpenCount: 0 },
    addEventListener() {},
    removeEventListener() {},
    createElement: () => ({ style: {} }),
  },
  window: {
    addEventListener() {},
    removeEventListener() {},
    getComputedStyle: () => ({ display: "block", visibility: "visible" }),
    location: { protocol: "https:" },
  },
  location: { protocol: "https:" },
  localStorage: { getItem: () => null, setItem() {} },
  navigator: {},
  console,
  setTimeout,
  clearTimeout,
};
ctx.window.document = ctx.document;
vm.createContext(ctx);
vm.runInContext(utilsSrc, ctx);
ctx.openModal("m1");
ctx.document.activeElement = btnEl;
listeners.keydown({ key: "Tab", shiftKey: false, preventDefault() {} });
console.log(JSON.stringify({ selector: selectorsSeen[0] || "", focused }));
"""
    out = _run_node(script)
    assert "a[href]" in out["selector"]
    assert out["focused"] == ["link", "link"]


def test_chart_ai_technical_lines_clamps_label_badge_inside_chart_area() -> None:
    """chart.js aiTechnicalLinesPlugin clamps badgeX >= xScale.left+4 and badgeY inside yScale."""
    script = r"""
const fs = require("fs");
const vm = require("vm");
const chartSrc = fs.readFileSync("static/js/chart.js", "utf8");
const idx = chartSrc.indexOf("const aiTechnicalLinesPlugin =");
const endIdx = chartSrc.indexOf('if (typeof Chart !== "undefined")', idx);
const pluginSnippet = chartSrc.slice(idx, endIdx);
const rects = [];
const fakeCtx = {
  save() {},
  restore() {},
  beginPath() {},
  moveTo() {},
  lineTo() {},
  stroke() {},
  fill() {},
  setLineDash() {},
  measureText: () => ({ width: 80 }),
  roundRect(x, y, w, h) { rects.push({ x, y, w, h }); },
  fillText() {},
};
const fakeChart = {
  ctx: fakeCtx,
  $aiTechnicalLines: [{
    type: "support",
    start_price: 5,
    end_price: 5,
    end_date: "2026-01-02",
    color: "#00ff88",
  }],
  scales: {
    x: { left: 50, right: 350, getPixelForValue: () => 50 },
    y: { top: 10, bottom: 200, getPixelForValue: () => 2 },
  },
};
const ctx = {
  sanitizeHexColor: (c, f) => c || f,
};
vm.createContext(ctx);
vm.runInContext(pluginSnippet + "\nglobalThis.__p = aiTechnicalLinesPlugin;", ctx);
ctx.__p.afterDatasetsDraw(fakeChart);
console.log(JSON.stringify({ rects }));
"""
    out = _run_node(script)
    assert len(out["rects"]) == 1
    rect = out["rects"][0]
    assert math.isclose(rect["x"], 54.0)  # xScale.left (50) + 4, never off-left!
    assert rect["y"] >= 12.0  # yScale.top (10) + 2, never off-top!


def test_tradingview_manager_recovers_when_script_loaded_without_widget() -> None:
    """tradingview_manager replaces already-loaded tv-script-js and surfaces error when widget is missing."""
    script = r"""
const fs = require("fs");
const vm = require("vm");
const tvSrc = fs.readFileSync("static/js/tradingview_manager.js", "utf8");
const appended = [];
let removedStale = false;
const headTags = [];
const staleLoadedScript = {
  id: "tv-script-js",
  dataset: { loaded: "true" },
  parentNode: true,
  remove() {
    removedStale = true;
    const idx = headTags.indexOf(staleLoadedScript);
    if (idx >= 0) headTags.splice(idx, 1);
  },
  addEventListener() { throw new Error("should not attach load listener to already-loaded script"); },
};
headTags.push(staleLoadedScript);
const fakeContainer = {
  id: "tv-container",
  innerHTML: "",
  children: [],
  style: {},
  isConnected: true,
  querySelector: () => null,
  replaceChildren() { this.children = []; appended.length = 0; },
  appendChild(el) { appended.push(el); this.children.push(el); },
  get textContent() { return ""; },
  set textContent(v) { if (v === "") { this.children = []; appended.length = 0; } },
};
const doc = {
  getElementById(id) {
    if (id === "tv-script-js") return headTags.find((el) => el.id === "tv-script-js") || null;
    if (id === "tv-container") return fakeContainer;
    if (id === "tv-container-inner") return { style: {} };
    return null;
  },
  querySelector: () => null,
  createElement(tag) {
    return {
      tagName: tag.toUpperCase(),
      id: "",
      className: "",
      style: {},
      textContent: "",
      dataset: {},
      setAttribute() {},
      appendChild() {},
      remove() {
        const idx = headTags.indexOf(this);
        if (idx >= 0) headTags.splice(idx, 1);
      },
    };
  },
  body: { classList: { contains: () => false } },
  head: { appendChild(el) { headTags.push(el); } },
};
const win = {
  document: doc,
  TradingView: {},
  addEventListener() {},
  removeEventListener() {},
};
const ctx = { window: win, document: doc, console };
vm.createContext(ctx);
vm.runInContext(tvSrc, ctx);
// 1. Stale already-loaded script without widget is evicted and replaced cleanly.
win.TradingViewManager.renderAdvancedChart("tv-container", "AAPL", "us", "dark");
const freshScript = headTags.find((el) => el.id === "tv-script-js");
// 2. If fresh onload fires without window.TradingView.widget, handleLoaded triggers error recovery.
freshScript.onload();
console.log(JSON.stringify({
  removedStale,
  replacedWithFresh: Boolean(freshScript && freshScript !== staleLoadedScript),
  freshRemainingAfterNoWidgetLoad: headTags.length,
  errorClasses: appended.map((el) => el.className),
  errorText: appended.map((el) => el.textContent),
}));
"""
    out = _run_node(script)
    assert out["removedStale"] is True
    assert out["replacedWithFresh"] is True
    assert out["freshRemainingAfterNoWidgetLoad"] == 0
    assert "tv-load-error" in out["errorClasses"]
    assert any("チャートの読み込みに失敗" in t for t in out["errorText"])


def test_experimental_orbit_css_includes_standard_background_clip() -> None:
    """experimental-orbit.css pairs background-clip: text with -webkit-background-clip: text."""
    css = (ROOT / "static/css/experimental-orbit.css").read_text(encoding="utf-8")
    assert "-webkit-background-clip: text;\n  background-clip: text;" in css
