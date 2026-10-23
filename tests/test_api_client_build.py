"""
Tests for API client build verification and frontend real-time / stream fixes.

Verifies:
1. TASK-09: api_client.ts / api_client.js uses this.resolveURL("/csrf-token")
   and frontend build verification script passes.
2. TASK-10: static/js/api.js buildStreamUrl aborts stream connection (returns null)
   on SSE ticket fetch failure or invalid ticket.
3. TASK-11: static/js/realtime_client.js does not check cached.ptsElements.length === 0
   in isStale check, preventing DOM cache invalidation for non-PTS stocks.
4. TASK-12: static/js/realtime_client.js and static/js/tradingview_manager.js
   include "idx" market in stock lookup and delta updates.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_api_client_build_matches_generated() -> None:
    """Ensure compiled static/js/api_client.js exactly matches TypeScript output."""
    res = subprocess.run(
        ["node", "scripts/verify_generated_frontend.mjs"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"verify_generated_frontend failed:\n{res.stdout}\n{res.stderr}"
    assert "matches the TypeScript output" in res.stdout


def test_api_client_csrf_uses_resolve_url() -> None:
    """Ensure CSRF token refresh uses this.resolveURL('/csrf-token') instead of hardcoded '/api/csrf-token'."""
    ts_file = ROOT_DIR / "static" / "js" / "api_client.ts"
    js_file = ROOT_DIR / "static" / "js" / "api_client.js"

    ts_content = ts_file.read_text(encoding="utf-8")
    js_content = js_file.read_text(encoding="utf-8")

    assert 'fetch(this.resolveURL("/csrf-token")' in ts_content
    assert 'fetch("/api/csrf-token"' not in ts_content

    assert 'fetch(this.resolveURL("/csrf-token")' in js_content
    assert 'fetch("/api/csrf-token"' not in js_content


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
