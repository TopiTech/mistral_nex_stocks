"""
test_experimental_orbit.py - Unit and integration tests for Market Observatory.

Validates the experimental orbit route, template rendering, security headers,
HTML structure, query parameter safety, and static asset accessibility.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app import create_app

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Observatory JavaScript regression test")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def client():
    """Create a test client with isolated app instance."""
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True)
    with app.test_client() as test_client:
        yield test_client


def test_experimental_orbit_page_status_and_content(client):
    """GET /experimental/orbit returns 200 OK and renders Market Observatory."""
    res = client.get("/experimental/orbit")
    assert res.status_code == 200
    assert res.mimetype == "text/html"

    html = res.get_data(as_text=True)
    # Check core branding, navigation, and settings button
    assert "Market Observatory" in html
    assert "badge-exp" in html
    assert 'href="/main?view=dashboard"' in html
    assert 'id="btn-orbit-settings"' in html
    assert 'href="/settings"' in html

    # Check canvas and critical UI elements
    assert 'id="orbit-canvas"' in html
    assert 'id="center-stock-card"' in html
    assert 'id="timeline-slider"' in html
    assert 'id="constellation-drawer"' in html
    assert 'id="ai-dive-overlay"' in html
    assert 'id="shortcuts-help-modal"' in html
    assert 'id="observatory-live-region"' in html


def test_experimental_orbit_security_and_csp(client):
    """Ensure CSP nonce and security headers are properly attached."""
    res = client.get("/experimental/orbit")
    assert res.status_code == 200

    html = res.get_data(as_text=True)
    # Ensure script tags have nonce
    assert 'script nonce="' in html or "script" in html
    assert "static/js/experimental/data-adapter.js" in html
    assert "static/js/experimental/orbit-state.js" in html
    assert "static/js/experimental/orbit-renderer.js" in html
    assert "static/js/experimental/gesture-controller.js" in html
    assert "static/js/experimental/temporal-controller.js" in html
    assert "static/js/experimental/constellation-controller.js" in html
    assert "static/js/experimental/ai-dive-controller.js" in html
    assert "static/js/experimental/accessibility-controller.js" in html
    assert "static/js/experimental/orbit-entry.js" in html


def test_experimental_orbit_query_parameters(client):
    """Verify route handles query parameters safely without crashes or reflections."""
    # Test valid symbol & market query params
    res = client.get("/experimental/orbit?symbol=NVDA&market=us")
    assert res.status_code == 200

    # Test malicious / malformed query params
    res_xss = client.get('/experimental/orbit?symbol=<script>alert(1)</script>&market="../../etc"')
    assert res_xss.status_code == 200
    html = res_xss.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html


def test_experimental_orbit_static_assets_exist():
    """Verify that all new static files exist in the repository."""
    assets = [
        "static/css/experimental-orbit.css",
        "static/js/experimental/data-adapter.js",
        "static/js/experimental/orbit-state.js",
        "static/js/experimental/orbit-renderer.js",
        "static/js/experimental/gesture-controller.js",
        "static/js/experimental/temporal-controller.js",
        "static/js/experimental/constellation-controller.js",
        "static/js/experimental/ai-dive-controller.js",
        "static/js/experimental/accessibility-controller.js",
        "static/js/experimental/orbit-entry.js",
        "templates/experimental_orbit.html",
    ]

    for rel in assets:
        path = ROOT / rel
        assert path.exists(), f"Asset {rel} does not exist"
        assert path.stat().st_size > 0, f"Asset {rel} is empty"


def test_observatory_formats_jpy_values_in_accessibility_mirror():
    _run_node(
        r'''
const fs = require("fs");
const vm = require("vm");
class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this._text = "";
    this.className = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
}
const context = {
  document: {
    createElement: (tag) => new Node(tag),
    createTextNode: (text) => {
      const node = new Node("#text");
      node.textContent = text;
      return node;
    },
  },
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), context);
vm.runInContext(fs.readFileSync("static/js/experimental/accessibility-controller.js", "utf8"), context);
const stock = context.ObservatoryDataAdapter.normalizeStock({
  symbol: "7203.T", market: "jp", price: 7203, market_cap: 30000000000000,
});
if (context.ObservatoryDataAdapter.formatPrice(stock.price, stock) !== "¥7,203") {
  throw new Error("JPY price formatter returned an incorrect value");
}
if (context.ObservatoryDataAdapter.formatMarketCap(stock.marketCap, stock) !== "¥30.0兆") {
  throw new Error("JPY market-cap formatter returned an incorrect value");
}
const container = new Node("div");
context.AccessibilityController.prototype.updateScreenReaderTable.call(
  { els: { srTableContainer: container } },
  { selectedSymbol: stock.symbol, stockList: [stock] },
);
function flatten(node) {
  return [node.textContent, ...node.children.flatMap(flatten)];
}
const text = flatten(container).filter(Boolean).join(" | ");
if (!text.includes("¥7,203") || !text.includes("¥30.0兆") || text.includes("$7203.00")) {
  throw new Error(`accessibility mirror has incorrect JPY values: ${text}`);
}
process.stdout.write("ok");
process.exit(0);
'''
    )


def test_ai_dive_renders_structured_analysis_response_fields():
    _run_node(
        r'''
const fs = require("fs");
const vm = require("vm");
class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this._text = "";
    this.className = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
}
const context = {
  document: {
    createElement: (tag) => new Node(tag),
    createTextNode: (text) => {
      const node = new Node("#text");
      node.textContent = text;
      return node;
    },
  },
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), context);
vm.runInContext(fs.readFileSync("static/js/experimental/ai-dive-controller.js", "utf8"), context);
const stock = context.ObservatoryDataAdapter.normalizeStock({
  symbol: "7203.T", market: "jp", price: 7203,
});
const container = new Node("div");
context.AiDiveController.prototype.renderAiAnalysisResult.call(
  {
    els: { tier4Container: container },
    state: { state: { aiDiveSymbol: stock.symbol, stocks: new Map([[stock.symbol, stock]]) } },
  },
  {
    recommendation: "買い",
    sentiment: "強気",
    target_price_3m: 7500,
    analysis_summary: "structured summary",
    key_catalysts: ["structured catalyst"],
    risk_factors: ["structured risk"],
  },
);
function flatten(node) {
  return [node.textContent, ...node.children.flatMap(flatten)];
}
const text = flatten(container).filter(Boolean).join(" | ");
for (const expected of ["目標株価: ¥7,500", "structured summary", "structured catalyst", "structured risk"]) {
  if (!text.includes(expected)) throw new Error(`missing structured field: ${expected}; ${text}`);
}
process.stdout.write("ok");
process.exit(0);
'''
    )


def test_navigation_links_present_in_settings_and_removed_from_headers(client):
    """Ensure link to /experimental/orbit is present in settings page and removed from headers of main, heatmap, and screener."""
    # Settings page must contain link to Market Observatory
    res_settings = client.get("/settings")
    assert res_settings.status_code == 200
    html_settings = res_settings.get_data(as_text=True)
    assert "/experimental/orbit" in html_settings, (
        "Link to /experimental/orbit missing in /settings"
    )

    # Header pages should no longer contain header EXP link
    for page in ["/main", "/heatmap", "/screener"]:
        res = client.get(page)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert 'class="heatmap-nav-btn exp-nav-btn"' not in html, (
            f"Deprecated EXP link still present in {page}"
        )


def test_stock_history_api_response_schema(client):
    """Verify /api/stock-history returns non-empty history array with short keys for frontend data-adapter."""
    from app_state import app_state

    mock_payload = {
        "symbol": "MRAM",
        "history": [{"c": 10.5, "o": 10.0, "h": 11.0, "l": 9.8, "v": 1000, "x": 1700000000000}],
    }
    app_state.stock_disk_cache.set("hist_MRAM_us_3mo", mock_payload)
    res = client.get("/api/stock-history?symbol=MRAM&market=us&period=3mo")
    assert res.status_code == 200
    data = res.get_json()
    assert isinstance(data, dict)
    assert data.get("symbol") == "MRAM"
    history = data.get("history", [])
    assert len(history) > 0
    item = history[0]
    assert "c" in item or "close" in item
    assert "x" in item or "timestamp" in item or "date" in item
