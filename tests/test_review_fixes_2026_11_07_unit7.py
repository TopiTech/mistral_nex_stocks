"""Regression coverage for the 2026-11-07 full-project review (frontend unit).

Covers the frontend review findings L12-L30 plus the APIClient SSE-client
fixes (L7/L11/L13) from the same review. Behaviour that Node's ``vm`` can
execute is tested that way (chart.js, realtime_client.js, state.js,
temporal-controller, gesture-controller); page-level wiring that requires
the full DOM is pinned with source-contract assertions, matching the
project's existing frontend regression layer (test_frontend_review_fixes.py).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend review-fix coverage")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# L19 — chart.js network-error retry recognises apiFetch-wrapped errors
# ---------------------------------------------------------------------------


def test_chart_history_retry_recognises_wrapped_network_error() -> None:
    """apiFetch wraps raw TypeErrors in plain Errors carrying name/type
    strings; the retry branch must catch both shapes (L19)."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "chart.js"),
  "utf8",
);

const history = [
  { x: 1793145600000, o: 100, h: 101, l: 99, c: 100.5, v: 1000 },
];
let calls = 0;
let failNext = true;

const context = {
  console,
  AbortController,
  Promise,
  Error,
  TypeError,
  setTimeout: (fn, ms) => {
    if (ms < 5000) fn();
    return 0;
  },
  clearTimeout: () => {},
  apiFetch: async () => {
    calls += 1;
    if (failNext) {
      failNext = false;
      // Exact apiFetch wrap contract (api.js classifyAPIError): plain Error
      // carrying the original error's name plus a classified `type`.
      const err = new Error("ネットワークエラーが発生しました");
      err.name = "TypeError";
      err.type = "network";
      throw err;
    }
    return { data: { history, stale: true, fetching: true } };
  },
  CONSTANTS: {
    TIMEOUT: { STOCK_HISTORY: 30000, STOCK_HISTORY_RETRY: 30000 },
  },
  normalizeHistoryData: (rows) => ({ formattedData: rows, ohlcData: rows }),
  logger: { warn() {}, info() {}, debug() {}, error() {} },
  DOM: { get: () => null },
  document: { getElementById: () => null, querySelector: () => null },
  window: { addEventListener() {} },
  getStockByKey: () => null,
  getChartPref: () => "1d",
};
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: "chart.js" });

(async () => {
  const first = await vm.runInContext(
    'fetchStockHistoryPayload("AAPL", "us", "3mo", "1d", null)',
    context,
  );
  if (calls !== 2) throw new Error(`wrapped error not retried, calls=${calls}`);
  if (!first || first.formattedData.length !== 1) {
    throw new Error(`retry did not apply history: ${JSON.stringify(first)}`);
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    assert _run_node(script) == "ok"


# ---------------------------------------------------------------------------
# L20 — drawChart carries legend-hidden datasets across destroy/recreate
# ---------------------------------------------------------------------------


def test_chart_legend_hidden_state_roundtrip_and_wiring() -> None:
    source = _read("static/js/chart.js")
    assert "getDatasetHiddenStateByLabel(previousChart)" in source
    assert "applyDatasetHiddenStateByLabel(chart, hiddenByLabel);" in source
    # Legend state must be read BEFORE the destroy boundary...
    capture_idx = source.index("getDatasetHiddenStateByLabel(previousChart)")
    destroy_idx = source.index("_safeDestroyChart(canvas);", capture_idx)
    assert capture_idx < destroy_idx
    # ...and re-applied AFTER the new Chart was built.
    apply_idx = source.index("applyDatasetHiddenStateByLabel(chart, hiddenByLabel);")
    assert apply_idx > destroy_idx
    assert 'chart.update("none")' in source[apply_idx : apply_idx + 200]

    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "chart.js"),
  "utf8",
);
const visibilitySets = [];
const chart = {
  data: { datasets: [{ label: "ローソク足" }, { label: "出来高" }, { noLabel: true }] },
  isDatasetVisible: (i) => i !== 0,
  setDatasetVisibility: (i, v) => visibilitySets.push([i, v]),
  update: () => {},
};
const context = {
  console, AbortController, Promise, Error, TypeError,
  setTimeout: () => 0, clearTimeout: () => {},
  DOM: { get: () => null },
  document: { getElementById: () => null, querySelector: () => null },
  window: { addEventListener() {} },
  logger: { warn() {}, info() {}, debug() {}, error() {} },
  chart,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "chart.js" });

const hidden = vm.runInContext("getDatasetHiddenStateByLabel(chart)", context);
// (Realm note: the Map lives inside the vm realm, so instanceof would
// cross-check realms — assert the Map contract instead.)
if (typeof hidden.get !== "function" || typeof hidden.size !== "number") {
  throw new Error("expected Map");
}
if (hidden.get("ローソク足") !== true) throw new Error("hidden dataset not recorded");
if (hidden.get("出来高") !== false) throw new Error("visible dataset recorded hidden");
if (hidden.size !== 2) throw new Error(`unlabeled dataset leaked into state: ${hidden.size}`);

context.hidden = hidden;
vm.runInContext("applyDatasetHiddenStateByLabel(chart, hidden)", context);
if (chart.data.datasets[0].hidden !== true) throw new Error("hidden not applied");
if (chart.data.datasets[1].hidden !== false) throw new Error("visible not applied");
if (visibilitySets.length !== 2) throw new Error("setDatasetVisibility not called per dataset");
process.stdout.write("ok");
"""
    assert _run_node(script) == "ok"


# ---------------------------------------------------------------------------
# L14 — hidden tabs bypass rAF and apply deltas synchronously
# ---------------------------------------------------------------------------


def test_realtime_hidden_tab_applies_deltas_without_raf() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "realtime_client.js"),
  "utf8",
);

let rafCalls = 0;
const ptsEl = { textContent: "", hidden: false, isConnected: true, closest: () => null };
const document = {
  readyState: "complete",
  addEventListener() {},
  querySelectorAll(sel) {
    return sel.includes(".compact-pts") ? [ptsEl] : [];
  },
  getElementById: () => null,
  querySelector: () => null,
  hidden: false,
};
const window = {
  addEventListener() {},
  requestAnimationFrame() { rafCalls += 1; },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
};
const context = { console, document, window, setTimeout: () => 0, clearTimeout: () => {} };
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: "realtime_client.js" });

const W = window;
if (!W.realtimeClient) throw new Error("realtimeClient not initialised");

// Visible tab: the apply must be scheduled through rAF, not synchronous.
W.realtimeClient.handlePtsDeltas({ AAPL: { price: 12345.67 } });
if (rafCalls !== 1) throw new Error(`visible path bypassed rAF: ${rafCalls}`);
if (ptsEl.textContent !== "") throw new Error("visible path applied synchronously");

// Hidden tab: rAF callbacks never fire — deltas must apply synchronously.
document.hidden = true;
ptsEl.textContent = "";
W.realtimeClient.handlePtsDeltas({ AAPL: { price: 12345.67 } });
if (rafCalls !== 1) throw new Error(`hidden path scheduled rAF: ${rafCalls}`);
if (!ptsEl.textContent.startsWith("PTS 12345")) {
  throw new Error(`hidden path did not apply delta: ${JSON.stringify(ptsEl.textContent)}`);
}
if (ptsEl.hidden !== false) throw new Error("pts element stayed hidden");

// Same contract for the main realtime delta path.
rafCalls = 0;
W.realtimeClient.handleDeltas({ AAPL: { price: 12345.67 } });
if (rafCalls !== 0) throw new Error(`hidden delta path scheduled rAF: ${rafCalls}`);

document.hidden = false;
W.realtimeClient.handleDeltas({ AAPL: { price: 12345.68 } });
if (rafCalls !== 1) throw new Error(`visible delta path bypassed rAF: ${rafCalls}`);
process.stdout.write("ok");
"""
    assert _run_node(script) == "ok"


# ---------------------------------------------------------------------------
# L25 — bounded caches
# ---------------------------------------------------------------------------


def test_chart_pref_cache_is_bounded_and_flushes_dirty_evictions() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "chart.js"),
  "utf8",
);

const storage = {};
const context = {
  console, AbortController, Promise, Error, TypeError,
  setTimeout: () => 0, clearTimeout: () => {},
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
    removeItem: (k) => { delete storage[k]; },
  },
  DOM: { get: () => null },
  document: { getElementById: () => null, querySelector: () => null },
  window: { addEventListener() {} },
  logger: { warn() {}, info() {}, debug() {}, error() {} },
  getStockByKey: () => null,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "chart.js" });

// 200 stocks x 12 prefs = 2400 entries is the intended ceiling; insert well
// beyond it to exercise eviction while writes are still pending (dirty keys).
vm.runInContext(`
  for (let i = 0; i < 2500; i++) {
    setChartPref("S" + i, "type", "line");
  }
`, context);

const size = vm.runInContext("_chartPrefCache.size", context);
if (size > 2400) throw new Error(`pref cache exceeded cap: ${size}`);
if (!("chart_type_S0" in storage)) {
  throw new Error("evicted dirty key was not flushed to localStorage");
}
// The evicted (oldest) key must be gone from memory but retrievable back
// from localStorage (lazy reload) without data loss.
const reloaded = vm.runInContext('getChartPref("S0", "type", "candlestick")', context);
if (reloaded !== "line") throw new Error(`lost persisted pref: ${reloaded}`);
process.stdout.write("ok");
"""
    assert _run_node(script) == "ok"


def test_stock_details_cache_is_capped_at_fifty_entries() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "state.js"),
  "utf8",
);

const document = {
  addEventListener() {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  documentElement: {},
  readyState: "complete",
  hidden: false,
};
const window = {
  addEventListener() {},
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  matchMedia: () => ({ matches: false }),
  setTimeout: () => 0, clearTimeout: () => {},
};
window.window = window;
const context = {
  console,
  document,
  window,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  APP_CONFIG: {},
  DEFAULT_SYMBOLS: { us: [], jp: [], idx: [] },
  navigator: { userAgent: "node-test" },
};
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: "state.js" });

vm.runInContext(`
  for (let i = 0; i < 51; i++) stockDetailsCache.set("K" + i, { n: i });
  _enforceStockDetailsCacheLimit();
`, context);

const size = vm.runInContext("stockDetailsCache.size", context);
if (size !== 50) throw new Error(`expected cap at 50, got ${size}`);
const hasOldest = vm.runInContext('stockDetailsCache.has("K0")', context);
if (hasOldest) throw new Error("FIFO eviction did not remove the oldest entry");
const hasNewest = vm.runInContext('stockDetailsCache.has("K50")', context);
if (!hasNewest) throw new Error("newest entry was evicted");
process.stdout.write("ok");
"""
    assert _run_node(script) == "ok"


def test_temporal_history_cache_is_bounded_and_protects_active_key() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "experimental", "temporal-controller.js"),
  "utf8",
);

const state = {
  state: {
    selectedSymbol: "ZZZ",
    timeGranularity: "3mo",
    timeCursor: 0,
    market: "us",
    stocks: new Map(),
  },
  subscribe() {},
  setTimeCursor() {},
  setTimeGranularity() {},
};
const els = {
  timeSlider: null,
  timeDisplay: null,
  timeBadge: null,
  timePlayBtn: null,
  timeNowBtn: null,
  granularityGroup: null,
  canvas: null,
};
const window = {};
window.apiFetch = async () => ({
  data: { history: [{ date: "2026-11-01", price: 1 }, { date: "2026-11-02", price: 2 }] },
});
const context = {
  console, window, AbortController, Promise, Error,
  setTimeout: () => 0, clearTimeout: () => {},
  document: { getElementById: () => null },
};
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: "temporal-controller.js" });

(async () => {
  const TemporalController = window.TemporalController;
  const ctl = new TemporalController(state, els);
  // GRANULARITY_PERIODS.length (5) x 16 == 80 is the FIFO cap. Each fetch must
  // be the ACTIVE context (selectedSymbol/timeGranularity match) so the
  // response is applied and cached like a real user navigation.
  for (let i = 0; i < 85; i++) {
    const sym = "SYM" + i;
    state.state.selectedSymbol = sym;
    state.state.timeGranularity = "1d";
    await ctl.fetchHistoryForSymbol(sym, "1d");
  }
  if (ctl.historyCache.size > 80) {
    throw new Error(`history cache exceeded cap: ${ctl.historyCache.size}`);
  }
  if (ctl.historyCache.has("SYM0:1d")) {
    throw new Error("oldest entries were not evicted");
  }
  if (!ctl.historyCache.has("SYM84:1d")) {
    throw new Error("newest entry missing");
  }
  // The displayed history can never be evicted: inserts only happen on a
  // symbol switch (which reassigns activeHistoryKey first), so eviction
  // always removes an entry that is no longer on screen. Verify the last
  // inserted entry (the currently displayed one) survives a full cycle.
  state.state.selectedSymbol = "PIN";
  state.state.timeGranularity = "1d";
  await ctl.fetchHistoryForSymbol("PIN", "1d");
  if (!ctl.historyCache.has("PIN:1d")) {
    throw new Error("displayed history was evicted");
  }
  if (ctl.historyCache.size > 80) {
    throw new Error(`cache grew past cap: ${ctl.historyCache.size}`);
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    assert _run_node(script) == "ok"


def test_orbit_node_render_data_prunes_stale_symbols() -> None:
    source = _read("static/js/experimental/orbit-renderer.js")
    prune_idx = source.index(
        "updateAndRenderNodes(ctx, assignedStocks, stateData, dt, now)"
    )
    prune_block = source[prune_idx : prune_idx + 1800]
    # The prune pass must run at the top of the frame, before anti-collision.
    assert "nodeRenderData.delete(symbol)" in prune_block
    # Hovered, dragged and connected nodes must survive the prune.
    assert "stateData.hoveredSymbol" in prune_block
    assert "stateData.draggedSymbol" in prune_block
    assert "stateData.connectedSymbols" in prune_block


def test_ui_dead_portfolio_chart_cache_removed() -> None:
    assert "portfolioChartCache" not in _read("static/js/ui.js")


# ---------------------------------------------------------------------------
# L28 — gesture-controller single-pointer ownership
# ---------------------------------------------------------------------------


def test_gesture_second_pointer_is_ignored_until_first_releases() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "experimental", "gesture-controller.js"),
  "utf8",
);

const selected = [];
const dragged = [];
const nodeA = { symbol: "AAA" };
const nodeB = { symbol: "BBB" };
const canvas = {
  style: {},
  addEventListener() {},
  removeEventListener() {},
  getBoundingClientRect: () => ({ left: 0, top: 0 }),
};
const state = {
  state: {},
  setSelectedSymbol: (s) => selected.push(s),
  toggleConnectedSymbol: (s) => selected.push("conn:" + s),
  openAiDive() {},
  setDraggedSymbol: (s, pos, over) => dragged.push([s, over]),
  setHoveredSymbol() {},
};
const renderer = {
  centerX: 0,
  centerY: 0,
  hitTest: (x) => (x < 15 ? nodeA : nodeB),
  triggerShockwave() {},
};
const window = { addEventListener() {}, removeEventListener() {} };
const context = { console, window, setTimeout: () => 0, clearTimeout: () => {} };
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: "gesture-controller.js" });

const GestureController = window.GestureController;
const ctl = new GestureController(canvas, state, renderer);

// Pointer 1 owns the gesture from its pointerdown.
ctl.handlePointerDown({ button: 0, pointerType: "touch", pointerId: 1, clientX: 10, clientY: 10 });
if (ctl.pointerDownTarget !== nodeA) throw new Error("pointer 1 did not capture target");

// A second pointerdown while owned: must be ignored entirely.
ctl.handlePointerDown({ button: 0, pointerType: "touch", pointerId: 2, clientX: 20, clientY: 10 });
if (ctl.pointerDownTarget !== nodeA) throw new Error("second pointerdown overwrote the target");

// Its pointermove must not corrupt the threshold/position tracking.
ctl.handlePointerMove({ pointerId: 2, clientX: 400, clientY: 400, target: canvas });
if (ctl.isDragging) throw new Error("second pointer movement started a drag");

// And its pointerup must not release the gesture or select the second node.
ctl.handlePointerUp({ pointerId: 2 });
if (selected.length !== 0) throw new Error(`second pointer up acted: ${JSON.stringify(selected)}`);
if (ctl.pointerDownTarget !== nodeA) throw new Error("second pointer up released the gesture");

// The owning pointer's up completes the click on the ORIGINAL node.
ctl.handlePointerUp({ pointerId: 1 });
if (selected.length !== 1 || selected[0] !== "AAA") {
  throw new Error(`owning pointer did not select original node: ${JSON.stringify(selected)}`);
}
if (ctl.pointerDownTarget !== null) throw new Error("gesture state not released");

// Window blur (event without pointerId) must still abandon an active gesture.
ctl.handlePointerDown({ button: 0, pointerType: "touch", pointerId: 3, clientX: 10, clientY: 10 });
ctl.handlePointerCancel({ });
if (ctl.pointerDownTarget !== null) throw new Error("window blur did not cancel gesture");
process.stdout.write("ok");
"""
    assert _run_node(script) == "ok"


# ---------------------------------------------------------------------------
# L12 — SSE heartbeat contract between server interval and client watchdog
# ---------------------------------------------------------------------------


def test_get_api_credential_state_exposes_sse_heartbeat_interval() -> None:
    import config_store
    import constants
    from credential_manager import get_api_credential_state

    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(config_store, "CONFIG_FILE", Path(tmp) / "config.json"):
            state = get_api_credential_state()
    assert state["sse_heartbeat_interval"] == constants.SSE_HEARTBEAT_INTERVAL

    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(config_store, "CONFIG_FILE", Path(tmp) / "config.json"),
        patch.object(constants, "SSE_HEARTBEAT_INTERVAL", 90),
    ):
        state = get_api_credential_state()
    assert state["sse_heartbeat_interval"] == 90


def test_template_config_schema_accepts_sse_heartbeat_interval() -> None:
    from schemas.config import TemplateConfigSchema

    validated = TemplateConfigSchema.model_validate(
        {
            "has_mistral_api_key": True,
            "has_langsearch_api_key": False,
            "has_tavily_api_key": False,
            "has_alphavantage_api_key": False,
            "mistral_model": "mistral-medium-latest",
            "is_ai_technical_lines_eligible": True,
            "sse_heartbeat_interval": 90,
        }
    )
    assert validated.sse_heartbeat_interval == 90
    # Default keeps the constants.py default (fallback render path).
    fallback = TemplateConfigSchema(
        has_mistral_api_key=False,
        has_langsearch_api_key=False,
        has_tavily_api_key=False,
        has_alphavantage_api_key=False,
        mistral_model="",
        is_ai_technical_lines_eligible=False,
    )
    assert fallback.sse_heartbeat_interval == 15


def test_api_js_sse_heartbeat_timeout_follows_server_config() -> None:
    source = _read("static/js/api.js")
    assert "window.APP_CONFIG?.sse_heartbeat_interval" in source
    assert "Math.max(45000" in source
    gen_start = source.index('new APIClient("/api", {')
    gen_block = source[gen_start : gen_start + 120]
    assert "sseHeartbeatTimeout: sseHeartbeatTimeoutMs" in gen_block
    # Without a config the documented 45s default must remain intact.
    timeout_block = source[
        source.index("const sseHeartbeatTimeoutMs") : source.index(
            'new APIClient("/api", {'
        )
    ]
    assert "45000" in timeout_block


# ---------------------------------------------------------------------------
# L16/L17 — drawer fallback contract and unfiltered restore
# ---------------------------------------------------------------------------


def test_drawer_detail_fetch_uses_drawer_fallback_and_visible_banner_host() -> None:
    source = _read("static/js/ui.js")
    ensure_fn = source[
        source.index("async function ensureStockDetails") : source.index(
            "// #region Portfolio Logic"
        )
    ]
    assert "const findDetailEl = (sel) =" in ensure_fn
    assert "document.querySelector(`#stock-detail-drawer ${sel}`)" in ensure_fn
    assert 'findDetailEl(".detail-sector")' in ensure_fn
    # The failure banner must prefer the visible drawer tab when open.
    assert 'document.getElementById("drawerTabChartContent")' in ensure_fn
    assert "bannerHost.appendChild(banner);" in ensure_fn
    # Retry button must restart the same request flow.
    assert "ensureStockDetails(wrapper);" in ensure_fn


def test_drawer_close_restores_only_owned_elements() -> None:
    source = _read("static/js/ui.js")
    close_fn = source[
        source.index("function closeStockDetailDrawer") : source.index(
            "if (typeof state !== \"undefined\" && state?.subscribe)"
        )
    ]
    # No unfiltered restore of transient children may remain.
    assert "Array.from(chartContent.children).forEach" not in close_fn
    assert "Array.from(aiContent.children).forEach" not in close_fn
    owned = [
        ".detail-info",
        ".detail-actions",
        ".chart-controls",
        ".chart-container:not(.pnl-chart-container)",
        ".pnl-chart-container",
        ".analyze-btn",
        ".ai-section",
        ".chat-toggle-btn",
        ".chat-section",
    ]
    for selector in owned:
        assert selector in close_fn
    assert "appendScoped(chartContent, ownedChartSelectors)" in close_fn


def test_drawer_summary_renders_drawer_first_when_active() -> None:
    source = _read("static/js/ui.js")
    fn_start = source.index("function renderAiTechnicalLinesSummary")
    fn_end = source.index("function openFullscreenChart", fn_start)
    render_fn = source[fn_start:fn_end]
    assert "if (isDrawerActive) {" in render_fn
    drawer_first = render_fn.index(
        'drawerChartContent?.querySelector(".ai-tech-lines-summary")'
    )
    wrapper_first = render_fn.index(
        'wrapper.querySelector(".ai-tech-lines-summary")'
    )
    assert drawer_first < wrapper_first
    # A stale inline summary found while the drawer is active must be moved
    # into the drawer, not re-rendered in the hidden panel forever.
    assert 'summaryBox.closest("#stock-detail-drawer")' in render_fn


# ---------------------------------------------------------------------------
# Source contracts for the remaining frontend findings
# ---------------------------------------------------------------------------


def test_tradingview_script_load_failure_is_surfaced_and_retried() -> None:
    source = _read("static/js/tradingview_manager.js")
    assert "tvScript.onerror = () => {" in source
    onerror_idx = source.index("tvScript.onerror = () => {")
    onerror_block = source[onerror_idx : onerror_idx + 700]
    assert ".tv-load-error" in onerror_block
    assert "stale.remove()" in onerror_block


def test_heatmap_3d_failure_uses_visible_channel() -> None:
    source = _read("static/js/heatmap.js")
    assert 'document.getElementById("view-mode-toggle-container")' in source
    assert ".heatmap-3d-error" in source


def test_ai_portfolio_null_change_uses_muted_colour() -> None:
    source = _read("static/js/ai_portfolio.js")
    change_idx = source.index('"var(--color-muted, inherit)"')
    change_block = source[change_idx - 400 : change_idx + 250]
    assert "changePct === null" in change_block
    assert '"var(--color-positive)"' in change_block
    assert '"var(--color-negative)"' in change_block


def test_ai_portfolio_pill_tabs_sync_on_all_paths() -> None:
    """Every aria-pressed pill write must pair with a tabindex write (L23)."""
    import re

    source = _read("static/js/ai_portfolio.js")
    assert (
        source.count('pill.setAttribute("aria-pressed", String(isActive));') >= 3
    )
    assert (
        source.count('pill.setAttribute("tabindex", isActive ? "0" : "-1");') >= 3
    )
    fn_boundary = re.compile(r"\n\s*(?:async )?function ")
    for marker in (
        "function selectSavedAiPortfolio",
        "async function deleteSavedAiPortfolio",
    ):
        fn_start = source.index(marker)
        match = fn_boundary.search(source, fn_start + len(marker))
        assert match is not None, f"no function boundary after {marker}"
        fn_block = source[fn_start : match.start()]
        assert 'setAttribute("aria-pressed"' in fn_block
        assert 'setAttribute("tabindex"' in fn_block


def test_settings_delete_has_timer_clear_and_inflight_dedupe() -> None:
    source = _read("static/js/settings.js")
    confirm_fn = source[
        source.index("function attachInlineDeleteConfirm") : source.index(
            "/**\n * Parse a settings-mutation response"
        )
    ]
    assert "const autoRestoreTimer = setTimeout(" in confirm_fn
    assert "clearTimeout(autoRestoreTimer);" in confirm_fn
    assert "deletingStockKeys = new Set()" in source
    delete_fn = source[
        source.index("async function executeDeleteStock") : source.index(
            "async function resetAllStocks"
        )
    ]
    assert "if (deletingStockKeys.has(key)) return;" in delete_fn
    assert "deletingStockKeys.delete(key);" in delete_fn


def test_index_main_resets_skeleton_timestamp_on_fetch_error() -> None:
    source = _read("static/js/index_main.js")
    catch_start = source.index('logger.warn("Init fetch err:", e);')
    catch_block = source[catch_start : catch_start + 400]
    assert "sseState.skeletonShownAt = 0;" in catch_block


def test_api_js_eof_without_done_falls_back_to_polling() -> None:
    source = _read("static/js/api.js")
    assert "let sawDoneFrame = false;" in source
    assert "sawDoneFrame = true;" in source
    assert "isStreamFallback = true" in source
    assert "ストリームが中断されました。再試行してください" in source


def test_api_client_consumes_server_error_frames() -> None:
    source = _read("static/js/api_client.ts")
    listener = 'eventSource.addEventListener("error", (event: Event) => {'
    assert listener in source
    error_idx = source.index(listener, source.index("openSSE("))
    error_block = source[error_idx : error_idx + 900]
    assert "maybeMessage.data" in error_block
    assert "onError(new Error(message));" in error_block


def test_api_client_serialises_open_chains_by_token() -> None:
    source = _read("static/js/api_client.ts")
    assert "private _openChainToken: object | null = null;" in source
    assert "const chainToken = {};" in source
    assert "this._openChainToken = chainToken;" in source
    # Only the newest chain may clear the connecting flag.
    finally_idx = source.index("if (this._openChainToken === chainToken) {")
    finally_block = source[finally_idx : finally_idx + 120]
    assert "this.isConnecting = false;" in finally_block
    # _onlineHandler must not re-enter while a chain is resolving.
    online_idx = source.index("this._onlineHandler = () => {")
    online_block = source[online_idx : online_idx + 600]
    assert "!this.isConnecting" in online_block


def test_orbit_keyboard_shortcuts_match_canvas_aria_promise() -> None:
    source = _read("static/js/experimental/orbit-entry.js")
    keydown_start = source.index('canvas.addEventListener("keydown", (e) => {')
    keydown_end = source.index("// 4. Initialize Temporal Controller", keydown_start)
    keydown_block = source[keydown_start:keydown_end]
    for key in ("d", "c", "m", "h", "?"):
        assert f'case "{key}":' in keydown_block
    assert "state.openAiDive(current);" in keydown_block
    assert "state.toggleConnectMode();" in keydown_block
    assert "state.toggleReducedMotion();" in keydown_block
    assert "accessibilityController.openHelpModal();" in keydown_block
    # Modifier combos must stay with the global shortcuts.
    assert "if (e.ctrlKey || e.metaKey || e.altKey) return;" in keydown_block


def test_orbit_observatory_refresh_channel_exists() -> None:
    source = _read("static/js/experimental/orbit-entry.js")
    assert "const OBSERVATORY_REFRESH_INTERVAL_MS = 60000;" in source
    refresh_block = source[
        source.index("startObservatoryRefresh();") : source.index(
            "// Cleanup on unload"
        )
    ]
    # Prettier may wrap long addEventListener calls; match whitespace-tolerantly.
    assert re.search(
        r'document\.addEventListener\(\s*"visibilitychange",\s*observatoryVisibilityRefresh',
        refresh_block,
    )
    assert re.search(
        r"observatoryRefreshTimer\s*=\s*window\.setInterval\(",
        refresh_block,
    )
    # Refresh loads run silent and reuse the generation/abort guards.
    silent_idx = source.index("loadObservatoryData(0, null, { silent: true });")
    assert "if (document.hidden) return;" in source[silent_idx - 200 : silent_idx]
    # Cleanup must remove both the interval and the document listener.
    cleanup_block = source[
        source.index("// Cleanup on unload") : source.index(
            "});", source.index("// Cleanup on unload")
        )
    ]
    assert "window.clearInterval(observatoryRefreshTimer);" in cleanup_block
    assert "target.removeEventListener(type, fn);" in cleanup_block
    # Silent loads must not flicker the loading overlay or nuke the view.
    load_fn = source[
        source.index("async function loadObservatoryData") : source.index(
            "function setLoading"
        )
    ]
    assert "if (!silent) setLoading(true);" in load_fn
    assert "if (!silent) {" in load_fn


def test_popup_excludes_file_urls_and_classifies_injection_failure() -> None:
    source = _read("chrome_extension/popup.js")
    injectable_fn = source[
        source.index("function isInjectableUrl") : source.index(
            "let detectorGeneration"
        )
    ]
    assert 'lower.startsWith("file://")' in injectable_fn
    assert "injectionFailed" in source
    assert "const INJECTION_FAILED_MSG =" in source
    assert "permDiv.textContent = INJECTION_FAILED_MSG;" in source
    # The silent failure (response never obtained) must NOT fall through to
    # the "no tickers detected" data claim.
    assert (
        'if (injectionFailed) {\n      container.textContent = "";' in source
    )


def test_popup_no_deprecated_unload_listener() -> None:
    source = _read("chrome_extension/popup.js")
    assert 'addEventListener("unload"' not in source
    assert 'addEventListener("pagehide"' in source
