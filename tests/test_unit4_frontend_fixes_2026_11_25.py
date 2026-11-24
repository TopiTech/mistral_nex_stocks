"""Regression and contract tests for Unit 4 frontend JS/UI fixes (2026-11-25)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

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
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"node failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_fe_01_api_pageshow_ignores_non_dashboard_pages() -> None:
    """FE-01: pageshow handler in api.js must not call dashboard loops on non-dashboard pages."""
    js = (ROOT / "static/js/api.js").read_text(encoding="utf-8")
    idx = js.index('window.addEventListener("pageshow"')
    block = js[idx : idx + 600]
    assert 'typeof state === "undefined"' in block
    assert 'getElementById("us-stocks")' in block


def test_fe_02_ai_portfolio_clears_loading_on_pagehide_and_pageshow() -> None:
    """FE-02: pagehide and pageshow handlers in ai_portfolio.js must reset stuck loading flags."""
    js = (ROOT / "static/js/ai_portfolio.js").read_text(encoding="utf-8")
    idx = js.index("cleanupAiPortfolioResources = () =>")
    block = js[idx:]
    assert "savedAiPortfoliosLoading = false;" in block
    assert "showLoadingState(false);" in block
    assert "renderSavedAiPortfolios();" in block
    assert 'window.addEventListener("pageshow"' in block


def test_fe_03_escape_exits_connect_mode_when_canvas_or_button_focused() -> None:
    """FE-03: AccessibilityController must allow Escape to cancel Connect Mode when canvas is focused."""
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync("static/js/experimental/accessibility-controller.js", "utf8");
const canvasEl = { closest: (sel) => (sel.includes("canvas") ? canvasEl : null), tagName: "CANVAS" };
const ctx = {
  window: { addEventListener: () => {}, removeEventListener: () => {} },
  document: { activeElement: canvasEl, addEventListener: () => {} },
  console,
};
vm.createContext(ctx);
vm.runInContext(code, ctx);
let cleared = false;
let toggled = null;
const fakeState = {
  state: { isConnectMode: true, connectedSymbols: ["AAPL"], aiDiveOpen: false },
  subscribe() { return () => {}; },
  clearConnectedSymbols() { cleared = true; this.state.connectedSymbols = []; },
  toggleConnectMode(v) { toggled = v; this.state.isConnectMode = v; },
};
const ctrl = new ctx.window.AccessibilityController(fakeState, { searchModal: null, helpModal: null });
let prevented = false;
ctrl.handleKeydown({ key: "Escape", preventDefault() { prevented = true; } });
console.log(JSON.stringify({ cleared, toggled, prevented }));
"""
    out = _run_node(script)
    assert out == {"cleared": True, "toggled": False, "prevented": True}


def test_fe_04_realtime_client_does_not_mutate_shared_tick_across_markets() -> None:
    """FE-04: Multi-market loop in realtime_client.js must compute per-item change without leaking across items."""
    import re

    js = (ROOT / "static/js/realtime_client.js").read_text(encoding="utf-8")
    idx = js.index("_forEachMatchingStateStock(")
    end_idx = js.index("const effectiveMarket =", idx)
    loop_body = js[idx:end_idx]
    assert not re.search(r"\bdata\.(?:change|change_percent|previous_close)\s*=(?!=)", loop_body)
    assert "let itemPrevClose = incomingPrevClose;" in loop_body


def test_fe_05_index_main_uses_lowercase_window_state() -> None:
    """FE-05: index_main.js must reference window.state?.exchangeRate, never window.State."""
    js = (ROOT / "static/js/index_main.js").read_text(encoding="utf-8")
    assert "window.State" not in js
    assert "window.state?.exchangeRate" in js
