"""Regression tests for Unit 7 fixes (D6B-01..D6B-10 and D7-01..D7-09)."""

from __future__ import annotations

import contextlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend/extension JS tests")
    proc = subprocess.run(
        [node, "-e", script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"Node script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_unit7_js_pages_scripts_and_orbit_regressions() -> None:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const path = require("path");

// D6B-01 & D6B-02: ai_portfolio.js custom pill cancels request & saveAiPortfolio checks freshness
const aiPortfolioSrc = fs.readFileSync("static/js/ai_portfolio.js", "utf8");
assert(
  /presetKey === "custom"\)\s*\{\s*cancelAiPortfolioRequest\(\);\s*showLoadingState\(false\);/m.test(aiPortfolioSrc),
  "custom preset click must cancel in-flight preset load and hide loading state",
);
assert(
  aiPortfolioSrc.includes("const startGeneration = aiPortfolioRequestGeneration;") &&
    aiPortfolioSrc.includes("if (stillSamePortfolio)"),
  "saveAiPortfolio must not overwrite currentAiPortfolio after user switched portfolio",
);

// D6B-03 & D6B-04: settings.js agent_id clearing and stale GET /api/credentials generation/abort guard
const settingsSrc = fs.readFileSync("static/js/settings.js", "utf8");
assert(settingsSrc.includes("let credentialsAbortController = null;"));
assert(settingsSrc.includes("let credentialsStateGeneration = 0;"));
assert(settingsSrc.includes("if (nextAgentId || prevAgentId) {\n            payload.mistral_agent_id = nextAgentId;") ||
  settingsSrc.includes("payload.mistral_agent_id = nextAgentId;"));
assert(settingsSrc.includes("if (agentIdInput) agentIdInput.value = data.agent_id || \"\";"));
assert(!settingsSrc.includes('document.getElementById("agent-id-settings-input")?.value?.trim() ||\n          _currentCredentialsData.agent_id'));

// D6B-05: setup.js shared setupSubmitInFlight guard disables both #saveBtn and #previewBtn
const setupSrc = fs.readFileSync("static/js/setup.js", "utf8");
assert(setupSrc.includes("let setupSubmitInFlight = false;"));
assert(setupSrc.includes("function setSetupSubmitBusy(isBusy)"));
assert(setupSrc.includes("if (setupSubmitInFlight) return;"));

// D6B-06 & D6B-07: heatmap.css JP legend & dedicated full-width heatmap-3d-error banner
const heatmapCss = fs.readFileSync("static/css/heatmap.css", "utf8");
assert(heatmapCss.includes("body.theme-color-jp .legend-bar"));
const heatmapHtml = fs.readFileSync("templates/heatmap.html", "utf8");
assert(heatmapHtml.includes('id="heatmap-3d-error"'));
const heatmapJs = fs.readFileSync("static/js/heatmap.js", "utf8");
let bannerText = "";
let bannerHidden = true;
const heatmapErrEl = {
  className: "heatmap-3d-error hidden",
  set textContent(v) { bannerText = v; },
  get textContent() { return bannerText; },
  classList: {
    contains: (c) => c === "heatmap-3d-error",
    add(c) { if (c === "hidden") bannerHidden = true; },
    remove(c) { if (c === "hidden") bannerHidden = false; },
  },
};
const switchStart = heatmapJs.indexOf("function _getDedicated3DErrorEl(");
const switchEnd = heatmapJs.indexOf("if (!state.three.isInit)", switchStart);
const heatmapSnippet = heatmapJs.slice(switchStart, switchEnd) + "}\n}";
const hmCtx = {
  document: {
    getElementById: (id) => (id === "heatmap-3d-error" ? heatmapErrEl : null),
    querySelectorAll: () => [],
  },
  els: { canvas3d: {} },
  state: { viewMode: "2d", three: { isInit: false } },
  THREE: undefined,
};
vm.runInNewContext(heatmapSnippet + "; this.switchViewMode = switchViewMode; this.clear3DErrorBanner = clear3DErrorBanner;", hmCtx);
hmCtx.switchViewMode("3d");
assert(!bannerHidden && bannerText.includes("3D"), "3D error banner should become visible with message");
hmCtx.switchViewMode("2d");
assert(bannerHidden && bannerText === "", "Switching back to 2d must clear and hide 3D error banner");

// D6B-08: setup.html & settings.js noopener noreferrer
const setupHtml = fs.readFileSync("templates/setup.html", "utf8");
assert(!/target="_blank"[^>]*rel="noopener"(?!\s+noreferrer)/.test(setupHtml));
assert(settingsSrc.includes('consoleLink.rel = "noopener noreferrer"'));

// D7-01 & D7-07: orbit-entry.js pagehide (no beforeunload teardown) and Space / slash canvas shortcuts
const orbitEntrySrc = fs.readFileSync("static/js/experimental/orbit-entry.js", "utf8");
assert(!orbitEntrySrc.includes('addEventListener("beforeunload"'), "orbit-entry.js must not attach beforeunload teardown");
assert(orbitEntrySrc.includes('window.addEventListener("pagehide"'));
assert(orbitEntrySrc.includes("const unsubCenterCard = state.subscribe"));
assert(orbitEntrySrc.includes('case " ":') && orbitEntrySrc.includes("state.togglePause()"));
assert(orbitEntrySrc.includes('case "/":') && orbitEntrySrc.includes("accessibilityController.openSearchModal()"));

// D7-04 & D7-05: orbit-renderer repulsion paused/reducedMotion guard, clamped spacing, active symbol filter
const rendererSrc = fs.readFileSync("static/js/experimental/orbit-renderer.js", "utf8");
assert(rendererSrc.includes("if (!stateData.paused && !stateData.reducedMotion)"));
assert(rendererSrc.includes("(Math.PI * 1.6) / list.length"));
assert(rendererSrc.includes("if (activeStocks && !activeStocks.has(sym)) continue;"));
const constellSrc = fs.readFileSync("static/js/experimental/constellation-controller.js", "utf8");
assert(constellSrc.includes("this.els.aiCompareBtn.disabled = activeStockCount < 2;"));
assert(constellSrc.includes("if (!symbols || stockObjs.length < 2)"));

// D7-08: data-adapter cursorIndex === 0 uses open vs close when distinct
const adapterSrc = fs.readFileSync("static/js/experimental/data-adapter.js", "utf8");
const adapterSandbox = { window: {} };
adapterSandbox.window = adapterSandbox;
vm.runInNewContext(adapterSrc, adapterSandbox);
const interp = adapterSandbox.ObservatoryDataAdapter.interpolateAtHistory(
  { symbol: "AAPL", price: 110, change: 10, changePercent: 10, volume: 50 },
  [{ close: 105, open: 100, volume: 10, dateStr: "2026-01-01", timestamp: 1 }],
  0,
);
assert.strictEqual(interp.change, 5);
assert.strictEqual(interp.changePercent, 5);

// D7-09: temporal-controller stops timelapse on wheel scrub
const temporalSrc = fs.readFileSync("static/js/experimental/temporal-controller.js", "utf8");
assert(/Normal Wheel: Scrub time cursor[\s\S]{0,120}this\.stopTimelapse\(\)/.test(temporalSrc));

// D7-06: chrome_extension/content.js filters prose stopwords & 1-letter words unless ticker-contextualized
const extSrc = fs.readFileSync("chrome_extension/content.js", "utf8");
const extListeners = [];
const extNodes = [
  { nodeValue: "A quick cat NOW and IT can work.", parentElement: { tagName: "P", isContentEditable: false, closest: () => null } },
  { nodeValue: "$A and Blue chips BRK.B, V, A, T, F, MSFT", parentElement: { tagName: "P", isContentEditable: false, closest: () => null } },
];
let wIdx = -1;
const extCtx = {
  window: { location: { href: "https://example.com" } },
  document: {
    title: "News",
    body: {},
    createTreeWalker() {
      wIdx = -1;
      return { nextNode: () => (++wIdx < extNodes.length ? extNodes[wIdx] : null) };
    },
  },
  NodeFilter: { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1, FILTER_SKIP: 3 },
  chrome: { runtime: { id: "ext-1", onMessage: { addListener: (fn) => extListeners.push(fn) } } },
  console, Set, Map, RegExp, String, Array, Math,
};
vm.runInNewContext(extSrc, extCtx);
let extReply = null;
extListeners[0]({ action: "detectTickers" }, { id: "ext-1" }, (r) => { extReply = r; });
const syms = new Set(extReply.tickers.map((t) => t.symbol));
assert(!syms.has("NOW") && !syms.has("CAT"), "prose stopwords NOW/CAT should not be flagged as tickers");
assert(syms.has("A") && syms.has("MSFT") && syms.has("BRK.B"), "explicit/list tickers should still be detected");
"""
    _run_node(script)


def test_unit7_start_backend_rejects_unrelated_pid_before_healthy_check(tmp_path: Path) -> None:
    """D7-03: Unrelated recycled PID must be rejected before trusting healthy/port."""
    import native_host.start_backend as sb

    pid_file = tmp_path / "backend.pid"
    pid_file.write_text("42424", encoding="utf-8")

    called = {"spawned": False}

    class _FakeProc:
        pid = 55555
        returncode = None

        @staticmethod
        def poll() -> int | None:
            return None

    def _fake_popen(*_args: object, **_kwargs: object) -> _FakeProc:
        called["spawned"] = True
        return _FakeProc()

    with (
        mock.patch.object(sb, "PID_FILE", pid_file),
        mock.patch.object(sb, "_LEGACY_PID_FILE", tmp_path / "legacy.pid"),
        mock.patch.object(sb, "get_backend_port", return_value=5000),
        mock.patch.object(sb, "is_backend_healthy_once", return_value=True),
        mock.patch.object(sb, "wait_for_backend_ready", return_value=True),
        mock.patch.object(sb, "is_port_in_use", return_value=False),
        mock.patch.object(sb, "is_running", return_value=True),
        mock.patch.object(sb, "_pid_is_backend_process", return_value=False),
        mock.patch.object(sb, "_startup_lock", side_effect=contextlib.nullcontext),
        mock.patch.object(sb.subprocess, "Popen", side_effect=_fake_popen),
    ):
        res = sb.start()

    assert called["spawned"] is True
    assert res["ok"] is True
    assert res["pid"] == 55555
    assert pid_file.read_text(encoding="utf-8").strip() == "55555"


def test_unit7_pre_command_guard_pipe_redirections_and_attached_flags() -> None:
    """D7-02: Piped rg, shell redirections, and attached short flags are handled cleanly."""
    guard_path = ROOT_DIR / ".agents" / "scripts" / "pre_command_guard.py"
    spec = importlib.util.spec_from_file_location("pre_command_guard", guard_path)
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    sys.modules["pre_command_guard"] = guard
    spec.loader.exec_module(guard)

    # Piped rg intentionally reads from stdin; do NOT append trailing "."
    assert guard.process_command("cat notes.txt | rg foo") == "cat notes.txt | rg foo"
    # Shell redirection target is NOT a search path, so "." must still be appended
    rewritten = guard.process_command("rg foo > out.txt")
    assert rewritten.endswith(" .")
    assert '"!.venv/**"' in rewritten
    # Attached -e flag must count 0 path args and append "."
    rewritten_attached = guard.process_command("rg -efoo")
    assert rewritten_attached.endswith(" .")
    # Attached -g flag must NOT inject duplicate --glob
    rewritten_glob = guard.process_command("rg -g*.py foo")
    assert "--glob" not in rewritten_glob and rewritten_glob.endswith(" .")
