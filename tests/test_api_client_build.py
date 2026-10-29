"""
Tests for API client build verification and frontend real-time / stream fixes.

Verifies:
1. The compiled api_client.js matches TypeScript and exposes only the SSE surface.
2. TASK-10: static/js/api.js buildStreamUrl aborts stream connection (returns null)
   on SSE ticket fetch failure or invalid ticket.
3. TASK-11: static/js/realtime_client.js does not check cached.ptsElements.length === 0
   in isStale check, preventing DOM cache invalidation for non-PTS stocks.
4. TASK-12: static/js/realtime_client.js and static/js/tradingview_manager.js
   include "idx" market in stock lookup and delta updates.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_api_client_build_matches_generated() -> None:
    """Ensure compiled static/js/api_client.js exactly matches TypeScript output."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to verify generated frontend build")

    tsc_path = ROOT_DIR / "node_modules" / "typescript" / "bin" / "tsc"
    prettier_path = ROOT_DIR / "node_modules" / "prettier" / "bin" / "prettier.cjs"
    if not tsc_path.exists() or not prettier_path.exists():
        pytest.skip(
            "Frontend build dependencies (node_modules/typescript, prettier) are not installed. "
            "Run 'npm ci' to enable frontend build verification."
        )

    res = subprocess.run(
        [node, "scripts/verify_generated_frontend.mjs"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"verify_generated_frontend failed:\n{res.stdout}\n{res.stderr}"
    assert "matches the TypeScript output" in res.stdout


def test_api_client_exposes_only_the_sse_surface() -> None:
    """The compiled client is an SSE client; REST transport has one owner (api.js).

    ``APIClient`` used to expose a second, unused fetch/retry/CSRF stack. This
    loads the real generated artifact and asserts the dead transport is gone
    while the live SSE surface and error class remain.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to verify the generated API client")

    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "api_client.js"),
  "utf8",
);
const context = {
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  EventSource: function EventSource() {},
  document: {
    hidden: false,
    addEventListener() {},
    removeEventListener() {},
  },
  window: { addEventListener() {}, removeEventListener() {} },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "api_client.js" });

const client = new context.window.APIClient("/api");
if (typeof client.openSSE !== "function") throw new Error("openSSE missing");
if (typeof client.resolveURL !== "function") throw new Error("resolveURL missing");
if (typeof context.window.APIError !== "function") throw new Error("APIError missing");
if (client.timeout !== undefined) throw new Error("dead timeout config still exposed");
for (const name of ["request", "get", "post", "put", "delete"]) {
  if (client[name] !== undefined) {
    throw new Error(`dead REST method still exposed: ${name}`);
  }
}
process.stdout.write("ok");
"""
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout == "ok"


def test_api_build_stream_url_aborts_on_ticket_failure() -> None:
    """Ensure buildStreamUrl returns null when ticket fetch fails or returns invalid ticket."""
    api_js = ROOT_DIR / "static" / "js" / "api.js"
    content = api_js.read_text(encoding="utf-8")

    # Ensure buildStreamUrl handles ticket response failure and invalid data
    assert "Failed to obtain SSE ticket: HTTP" in content
    assert "Ticket endpoint returned an invalid response" in content

    # Check that return null is invoked on ticket failure
    ticket_section = content[content.find("const buildStreamUrl = async () =>"):content.find("const openSseWithTicket = async () =>")]
    assert "if (!ticketResponse.ok)" in ticket_section
    assert "if (!ticketData?.ok)" in ticket_section
    assert "return null;" in ticket_section


def test_realtime_client_dom_cache_and_idx_support() -> None:
    """Ensure RealtimeStockClient DOM cache does not invalidate for non-PTS stocks and supports idx."""
    realtime_js = ROOT_DIR / "static" / "js" / "realtime_client.js"
    content = realtime_js.read_text(encoding="utf-8")

    # TASK-11: cached.ptsElements.length === 0 must NOT be in isStale
    assert "cached.ptsElements.length === 0" not in content
    assert "cached.wrappers.length === 0" in content

    # TASK-12: scanning array must include idx
    assert '["us", "jp", "idx"].forEach' in content


def test_tradingview_manager_idx_market_support() -> None:
    """Ensure tradingview_manager.js checks ['us', 'jp', 'idx'] in symbol resolution and fallback."""
    tv_js = ROOT_DIR / "static" / "js" / "tradingview_manager.js"
    content = tv_js.read_text(encoding="utf-8")

    assert '["us", "jp", "idx"]' in content
    assert 'const markets = ["us", "jp", "idx"];' in content
