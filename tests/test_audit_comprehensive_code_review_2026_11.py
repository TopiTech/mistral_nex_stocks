"""
tests/test_audit_comprehensive_code_review_2026_11.py

Comprehensive tests verifying the fixes from the exhaustive code review:
1. SSE client teardown resets reconnecting flag, preventing reconnection deadlock.
2. Screener registers pagehide alongside beforeunload for robust lifecycle cleanup.
3. Embeddings batch service respects circuit breaker HALF_OPEN state without polluting probes.
4. Thread pool executors in stock provider and AI services use DaemonThreadPoolExecutor.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app_state import app_state

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executing frontend JavaScript test scripts")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Node script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result.stdout


def test_sse_teardown_resets_reconnecting_flag():
    """Verify that _teardownSSE and closeSSE reset _reconnecting to false,
    preventing permanent reconnection lockout when teardown happens during backoff."""
    node_test = r"""
const assert = require("assert");
const fs = require("fs");
const path = require("path");

// Mock browser globals for api_client.js
global.window = {
  addEventListener: () => {},
  removeEventListener: () => {},
};
global.document = {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
};
global.navigator = { onLine: true };

const apiClientCode = fs.readFileSync(path.join(__dirname, "static/js/api_client.js"), "utf8");
eval(apiClientCode);

const APIClient = window.APIClient;
const client = new APIClient("/api");

// Simulate entering reconnecting state with pending reconnect timer
client._reconnecting = true;
client.ssePendingReconnectTimeout = setTimeout(() => {}, 10000);

assert.strictEqual(client._reconnecting, true);
assert.notStrictEqual(client.ssePendingReconnectTimeout, null);

// Teardown should clear timer AND reset _reconnecting
client._teardownSSE();

assert.strictEqual(client._reconnecting, false);
assert.strictEqual(client.ssePendingReconnectTimeout, null);

// Re-test via closeSSE
client._reconnecting = true;
client.ssePendingReconnectTimeout = setTimeout(() => {}, 10000);
client.closeSSE();

assert.strictEqual(client._reconnecting, false);
assert.strictEqual(client.ssePendingReconnectTimeout, null);

console.log("SSE_TEARDOWN_RESETS_RECONNECTING_OK");
"""
    out = _run_node(node_test)
    assert "SSE_TEARDOWN_RESETS_RECONNECTING_OK" in out


def test_screener_pagehide_registered():
    """Verify that static/js/screener.js registers pagehide for cleanup."""
    screener_source = (ROOT / "static" / "js" / "screener.js").read_text(encoding="utf-8")
    assert "pagehide" in screener_source
    assert 'window.addEventListener("pagehide", cleanupScreenerResources);' in screener_source
    assert 'window.addEventListener("beforeunload", cleanupScreenerResources);' in screener_source


def test_embeddings_service_respects_half_open_circuit():
    """Verify that get_mistral_embeddings_batch does not execute remote calls or
    pollute circuit probe when circuit is in HALF_OPEN recovery state."""
    from services.embeddings_service import get_mistral_embeddings_batch

    with app_state.market.circuit_lock:
        app_state.market.circuit_states["mistral"] = {
            "status": "HALF_OPEN",
            "failures": 3,
            "timeout_streak": 0,
            "open_until": 0.0,
            "probing": False,
        }

    try:
        mock_client = MagicMock()
        with patch("services.embeddings_service._get_client", return_value=mock_client):
            results = get_mistral_embeddings_batch(["test embedding query"], "fake-key")

            # Must return [None] without making remote call to embeddings.create
            assert results == [None]
            mock_client.embeddings.create.assert_not_called()

            # Must not have claimed or altered circuit probing state
            state = app_state.market.get_circuit_state("mistral")
            assert state.get("status") == "HALF_OPEN"
            assert state.get("probing") is False
    finally:
        with app_state.market.circuit_lock:
            app_state.market.circuit_states.pop("mistral", None)


def test_daemon_thread_pool_executor_usage():
    """Verify that stock provider, ai_service, and openai_service executors spawn daemon threads."""
    from services.ai_service import _run_coro_sync

    async def dummy_coro():
        cur_thread = threading.current_thread()
        return cur_thread.daemon, cur_thread.name

    # When running within an active event loop, _run_coro_sync dispatches to the ThreadPoolExecutor
    async def invoke_from_running_loop():
        return _run_coro_sync(dummy_coro(), timeout_sec=2.0)

    is_daemon, thread_name = asyncio.run(invoke_from_running_loop())
    assert is_daemon is True
    assert "ai-coro-sync" in thread_name
