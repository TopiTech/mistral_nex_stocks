"""Unit 4 regression tests for frontend JS/TS and CSS fixes (2026-11-14)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> dict[str, object]:
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=15,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node exited {proc.returncode}: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout.strip())


def test_realtime_hidden_tab_flushes_pending_raf_deltas_before_applying_new() -> None:
    """[U4-01] Pre-hide rAF delta batch flushes before hidden-tab synchronous apply."""
    res = _run_node(
        r"""
        const fs = require("node:fs");
        const vm = require("node:vm");
        const src = fs.readFileSync("static/js/realtime_client.js", "utf8");
        const queuedCallbacks = new Map();
        let nextFrameId = 1;
        const docHidden = { hidden: false };
        const fakeStock = { symbol: "AAPL", price: 100 };
        const fakePriceEl = {
          textContent: "",
          dataset: {},
          classList: { remove() {}, add() {} },
        };
        const fakeWrapper = {
          isConnected: true,
          __stockData: fakeStock,
          querySelector: (sel) => (sel === ".compact-price" ? fakePriceEl : null),
        };
        const raf = (cb) => {
          const id = nextFrameId++;
          queuedCallbacks.set(id, cb);
          return id;
        };
        const caf = (id) => {
          queuedCallbacks.delete(id);
        };
        const sandbox = {
          requestAnimationFrame: raf,
          cancelAnimationFrame: caf,
          setTimeout,
          clearTimeout,
          console,
          window: {
            state: { stocks: { us: [fakeStock], jp: [], idx: [] } },
            formatPrice: (p) => "$" + p.toFixed(2),
            requestAnimationFrame: raf,
            cancelAnimationFrame: caf,
          },
          document: {
            readyState: "complete",
            get hidden() { return docHidden.hidden; },
            addEventListener() {},
            querySelectorAll: (sel) => (sel.includes(".compact-pts") ? [] : [fakeWrapper]),
          },
          Object, Array, String, Number, Boolean, Math, Set, Map, Error,
        };
        sandbox.window.window = sandbox.window;
        vm.createContext(sandbox);
        vm.runInContext(src, sandbox);
        const applier = sandbox.window.realtimeClient;
        // 1. Visible tab schedules rAF delta (price 101)
        applier.handleDeltas({ AAPL: { price: 101, previous_close: 100 } });
        // 2. Tab hides before rAF fires; hidden delta (price 105) arrives
        docHidden.hidden = true;
        applier.handleDeltas({ AAPL: { price: 105, previous_close: 100 } });
        // 3. No stale rAF should remain queued when tab becomes visible again
        const remainingCallbacks = queuedCallbacks.size;
        console.log(JSON.stringify({
          finalPrice: fakeStock.price,
          remainingCallbacks,
        }));
        """
    )
    assert res["finalPrice"] == 105
    assert res["remainingCallbacks"] == 0


def test_api_client_resume_sse_tears_down_existing_stream_before_ticket_fetch() -> None:
    """[U4-01] APIClient._resumeSSE calls _teardownSSE before async urlProvider resolves."""
    res = _run_node(
        r"""
        const fs = require("node:fs");
        const vm = require("node:vm");
        const src = fs.readFileSync("static/js/api_client.js", "utf8");
        const events = [];
        function FakeEventSource(url) {
          this.url = url;
          this.readyState = 1;
          events.push("new:" + url);
        }
        FakeEventSource.prototype.close = function() {
          this.readyState = 2;
          events.push("close:" + this.url);
        };
        FakeEventSource.prototype.addEventListener = function() {};
        FakeEventSource.prototype.removeEventListener = function() {};
        FakeEventSource.OPEN = 1;
        FakeEventSource.CONNECTING = 0;

        const sandbox = {
          window: { addEventListener() {}, removeEventListener() {} },
          document: { hidden: false, addEventListener() {}, removeEventListener() {} },
          EventSource: FakeEventSource,
          console, setTimeout, clearTimeout, setInterval() { return 1; }, clearInterval() {},
          Date, Promise, WeakMap, Map, Set, Math, Number, String, Boolean, Error,
        };
        sandbox.window.window = sandbox.window;
        vm.createContext(sandbox);
        vm.runInContext(src, sandbox);
        const client = new sandbox.window.APIClient();
        client.currentEventSource = new FakeEventSource("/api/stocks/stream?ticket=old");
        client._lastSSEParams = {
          url: "/api/stocks/stream",
          onMessage: () => {},
          onError: () => {},
          options: {
            urlProvider: () => {
              events.push("resolve-url");
              return "/api/stocks/stream?ticket=new";
            },
          },
        };
        client._resumeSSE(true);
        Promise.resolve().then(() => {
          console.log(JSON.stringify({ events }));
        });
        """
    )
    events = res["events"]
    assert isinstance(events, list)
    assert events.index("close:/api/stocks/stream?ticket=old") < events.index("resolve-url")


def test_orbit_renderer_resolves_timeline_view_stock_when_scrubbed() -> None:
    """[U4-02] OrbitRenderer interpolates historical price & changePercent when timeCursor > 0."""
    res = _run_node(
        r"""
        const fs = require("node:fs");
        const vm = require("node:vm");
        const adapterSrc = fs.readFileSync("static/js/experimental/data-adapter.js", "utf8");
        const rendererSrc = fs.readFileSync("static/js/experimental/orbit-renderer.js", "utf8");
        const sandbox = {
          window: {},
          document: { hidden: false, addEventListener() {}, removeEventListener() {} },
          performance: { now: () => 0 },
          requestAnimationFrame: () => 1,
          cancelAnimationFrame: () => {},
          addEventListener() {},
          removeEventListener() {},
          innerWidth: 800,
          innerHeight: 600,
          devicePixelRatio: 1,
          console, Math, Number, String, Boolean, Array, Object, Map, Set,
        };
        sandbox.window = sandbox;
        vm.createContext(sandbox);
        vm.runInContext(adapterSrc, sandbox);
        vm.runInContext(rendererSrc, sandbox);
        const canvas = {
          getContext: () => ({}),
          getBoundingClientRect: () => ({ width: 800, height: 600 }),
        };
        const renderer = new sandbox.OrbitRenderer(canvas, {});
        const stock = { symbol: "AAPL", price: 200, changePercent: 5.0 };
        const history = [
          { close: 100, dateStr: "2026-11-01", timestamp: 1 },
          { close: 120, dateStr: "2026-11-02", timestamp: 2 },
          { close: 200, dateStr: "2026-11-03", timestamp: 3 },
        ];
        const stateData = {
          selectedSymbol: "AAPL",
          timeGranularity: "3mo",
          timeCursor: 1,
          historyMap: new Map([["AAPL:3mo", history]]),
          activeHistory: history,
        };
        const scrubbed = renderer._resolveTimelineViewStock(stock, stateData);
        console.log(JSON.stringify({ price: scrubbed.price, changePercent: scrubbed.changePercent }));
        """
    )
    assert res["price"] == 120
    assert res["changePercent"] == 20


def test_css_index_removes_duplicate_flash_keyframes_and_supports_theme_color_jp() -> None:
    """[U4-03] static/css/index.css removes duplicate flash-green/red and maps JP pulse colors."""
    css = (ROOT / "static" / "css" / "index.css").read_text(encoding="utf-8")
    colors_css = (ROOT / "static" / "css" / "colors.css").read_text(encoding="utf-8")

    assert "@keyframes flash-green" not in css
    assert "@keyframes flash-red" not in css
    assert "@keyframes flash-green" in colors_css
    assert "@keyframes flash-red" in colors_css
    assert "body.theme-color-jp .price-pulse-up" in css
    assert "body.theme-color-jp .price-pulse-down" in css
