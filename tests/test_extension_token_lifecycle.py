"""Regression tests for the extension token lifecycle (review item K-07).

The native host allows only ``NATIVE_HOST_TOKEN_ACTION_MAX`` secret-returning
actions per ``NATIVE_HOST_TOKEN_ACTION_WINDOW`` seconds, shared by
``get_shutdown_token`` and ``get_extension_api_token``.  The service worker
must therefore acquire the extension token once, cache it (memory +
``storage.session``) and refetch only after a genuine 401/403.

These tests execute the real ``chrome_extension/background.js`` in a Node VM.
``chrome.runtime.sendNativeMessage`` is backed by a mock that enforces the
budget read from ``native_host/native_host.py``; ``fetch`` is backed by a mock
that answers ``/api/stocks/add_ext`` only for the current Bearer token, just
like the Flask route's ``compare_digest`` check.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const scenario = process.argv[2];
const tokenBudget = Number(process.env.MNS_TEST_TOKEN_BUDGET);
const tokenWindowMs = Number(process.env.MNS_TEST_TOKEN_WINDOW_MS);
if (!scenario || !Number.isFinite(tokenBudget) || !Number.isFinite(tokenWindowMs)) {
  throw new Error("scenario and token budget env vars are required");
}

const BACKEND_PORT = 5000;
const FRESH_TOKEN = "fresh-token";
const STALE_TOKEN = "stale-token";
const SHUTDOWN_TOKEN = "shutdown-token";

const stats = {
  extTokenRequests: 0,
  shutdownTokenRequests: 0,
  tokenRateLimited: 0,
  authFailures: 0,
  addRequests: 0,
  addOk: 0,
  shutdownOk: 0,
};

const state = {
  backendHealthy: true,
  issuedToken:
    scenario === "persistent-failure" ? "rejected-token" : FRESH_TOKEN,
  session:
    scenario === "expired" || scenario === "no-session-remove"
      ? { mnsExtensionToken: STALE_TOKEN }
      : {},
  sessionRemoveFails: scenario === "no-session-remove",
};

// Mirrors native_host._check_token_action_rate_limit: one sliding window and
// one budget shared by every secret-returning action.
const tokenTimestamps = [];
function tokenActionAllowed() {
  const now = Date.now();
  while (tokenTimestamps.length > 0 && tokenTimestamps[0] <= now - tokenWindowMs) {
    tokenTimestamps.shift();
  }
  if (tokenTimestamps.length >= tokenBudget) {
    stats.tokenRateLimited += 1;
    return false;
  }
  tokenTimestamps.push(now);
  return true;
}

function nativeHost(message, callback) {
  const reply = (payload) => setImmediate(() => callback(payload));
  if (message.action === "get_backend_port") {
    return reply({ ok: true, port: BACKEND_PORT });
  }
  if (message.action === "start_backend") {
    state.backendHealthy = true;
    return reply({ ok: true, port: BACKEND_PORT });
  }
  if (message.action === "get_extension_api_token") {
    stats.extTokenRequests += 1;
    if (!tokenActionAllowed()) {
      return reply({ ok: false, error: "Token action rate limit exceeded" });
    }
    if (!state.backendHealthy) {
      return reply({ ok: false, error: "Backend is not running" });
    }
    return reply({ ok: true, token: state.issuedToken });
  }
  if (message.action === "get_shutdown_token") {
    stats.shutdownTokenRequests += 1;
    if (!tokenActionAllowed()) {
      return reply({ ok: false, error: "Token action rate limit exceeded" });
    }
    if (!state.backendHealthy) {
      return reply({ ok: false, error: "Backend is not running" });
    }
    return reply({ ok: true, token: SHUTDOWN_TOKEN });
  }
  return reply({ ok: false, error: "Unknown or disallowed action" });
}

function makeResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

async function fetchMock(input, init = {}) {
  const url = new URL(String(input));
  const headers = init.headers || {};
  if (url.pathname === "/api/health") {
    return makeResponse(200, {
      ok: true,
      app: "Mistral NeX Stocks",
      ready: true,
      model: "test",
    });
  }
  if (url.pathname === "/api/indices") {
    return makeResponse(200, { N225: { percent: 1 } });
  }
  if (url.pathname === "/api/stocks/add_ext") {
    stats.addRequests += 1;
    if (headers.Authorization !== `Bearer ${FRESH_TOKEN}`) {
      stats.authFailures += 1;
      return makeResponse(403, {
        ok: false,
        error: "invalid or missing extension token",
      });
    }
    stats.addOk += 1;
    return makeResponse(200, { ok: true });
  }
  if (url.pathname === "/api/shutdown") {
    if (headers["X-MNS-Shutdown-Token"] !== SHUTDOWN_TOKEN) {
      return makeResponse(403, {
        ok: false,
        error: "invalid or missing shutdown token",
      });
    }
    state.backendHealthy = false;
    stats.shutdownOk += 1;
    return makeResponse(200, { ok: true });
  }
  return makeResponse(404, { ok: false, error: "not found" });
}

const messageListeners = [];
const sessionStorage = {
  get(keys, callback) {
    const list = Array.isArray(keys) ? keys : [keys];
    const result = {};
    for (const key of list) {
      if (key in state.session) result[key] = state.session[key];
    }
    if (typeof callback === "function") {
      setImmediate(() => callback(result));
      return undefined;
    }
    return Promise.resolve(result);
  },
  set(items, callback) {
    Object.assign(state.session, items);
    if (typeof callback === "function") {
      setImmediate(callback);
      return undefined;
    }
    return Promise.resolve();
  },
  remove(keys, callback) {
    if (state.sessionRemoveFails) {
      throw new Error("storage.session.remove unavailable");
    }
    const list = Array.isArray(keys) ? keys : [keys];
    for (const key of list) delete state.session[key];
    if (typeof callback === "function") {
      setImmediate(callback);
      return undefined;
    }
    return Promise.resolve();
  },
};

const chromeMock = {
  storage: {
    local: {
      get(_keys, callback) {
        if (typeof callback === "function") setImmediate(() => callback({}));
        return Promise.resolve({});
      },
      set(_items, callback) {
        if (typeof callback === "function") setImmediate(callback);
        return Promise.resolve();
      },
      remove(_keys, callback) {
        if (typeof callback === "function") setImmediate(callback);
        return Promise.resolve();
      },
    },
    session: sessionStorage,
  },
  runtime: {
    id: "a".repeat(32),
    sendNativeMessage(_host, message, callback) {
      nativeHost(message, callback);
    },
    onMessage: {
      addListener(listener) {
        messageListeners.push(listener);
      },
    },
    onInstalled: { addListener() {} },
  },
  action: {
    setBadgeText() {},
    setBadgeBackgroundColor() {},
  },
  contextMenus: {
    onClicked: { addListener() {} },
    removeAll() {},
  },
};

const context = {
  chrome: chromeMock,
  console,
  fetch: fetchMock,
  navigator: { userAgent: "Chrome/120" },
  setTimeout,
  clearTimeout,
  setImmediate,
  AbortController,
  URL,
};
vm.createContext(context);
vm.runInContext(
  fs.readFileSync("chrome_extension/background.js", "utf8"),
  context,
  { filename: "chrome_extension/background.js" },
);

function dispatch(message) {
  const listener = messageListeners[0];
  if (!listener) throw new Error("background.js registered no message listener");
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`no response for ${message.action}`)),
      5000,
    );
    const returned = listener(
      message,
      { id: chromeMock.runtime.id },
      (response) => {
        clearTimeout(timer);
        resolve(response);
      },
    );
    if (returned !== true) {
      clearTimeout(timer);
      reject(new Error("message listener closed the response channel"));
    }
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  // Let the top-level storage.session hydration callback run.
  await sleep(30);
  let result;
  if (scenario === "happy") {
    const contextReply = await dispatch({ action: "getContext" });
    const started = await dispatch({ action: "startBackend" });
    const added = await dispatch({
      action: "addDetectedStock",
      symbol: "AAPL",
      market: "us",
    });
    const stopped = await dispatch({ action: "stopBackend" });
    result = { context: contextReply, started, added, stopped };
  } else if (scenario === "no-eager") {
    const contextReply = await dispatch({ action: "getContext" });
    const started = await dispatch({ action: "startBackend" });
    result = { context: contextReply, started };
  } else if (
    ["expired", "no-session-remove", "persistent-failure"].includes(scenario)
  ) {
    const added = await dispatch({
      action: "addDetectedStock",
      symbol: "AAPL",
      market: "us",
    });
    result = { added };
  } else if (scenario === "concurrent") {
    const added = await Promise.all([
      dispatch({ action: "addDetectedStock", symbol: "AAPL", market: "us" }),
      dispatch({ action: "addDetectedStock", symbol: "MSFT", market: "us" }),
    ]);
    result = { added };
  } else {
    throw new Error(`unknown scenario: ${scenario}`);
  }

  const output = JSON.stringify({
    scenario,
    result,
    stats: { ...stats, session: { ...state.session } },
  });
  process.stdout.write(output + "\n", () => process.exit(0));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


def _native_token_budget() -> tuple[int, float]:
    """Read the native host's actual budget so this test cannot silently drift."""
    source = (ROOT / "native_host" / "native_host.py").read_text(encoding="utf-8")
    max_match = re.search(
        r'_safe_int_env\(\s*"NATIVE_HOST_TOKEN_ACTION_MAX"\s*,\s*(\d+)', source
    )
    window_match = re.search(
        r'"NATIVE_HOST_TOKEN_ACTION_WINDOW"\s*,\s*([\d.]+)', source
    )
    assert max_match, "NATIVE_HOST_TOKEN_ACTION_MAX default not found"
    assert window_match, "NATIVE_HOST_TOKEN_ACTION_WINDOW default not found"
    return int(max_match.group(1)), float(window_match.group(1))


def _run_scenario(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the extension runtime regression test")
    max_actions, window_seconds = _native_token_budget()
    env = {
        **os.environ,
        "MNS_TEST_TOKEN_BUDGET": str(max_actions),
        "MNS_TEST_TOKEN_WINDOW_MS": str(int(window_seconds * 1000)),
    }
    result = subprocess.run(
        [node, "-", scenario],
        cwd=ROOT,
        input=_HARNESS,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stderr
    return json.loads(lines[-1])


def test_one_round_of_extension_requests_stays_within_native_token_budget():
    """getContext -> startBackend -> add -> stop must not exceed 3 token reads."""
    outcome = _run_scenario("happy")

    assert outcome["result"]["context"]["ok"] is True
    assert outcome["result"]["started"]["ok"] is True
    assert outcome["result"]["added"]["ok"] is True, outcome["result"]["added"]
    assert outcome["result"]["stopped"]["ok"] is True

    stats = outcome["stats"]
    assert stats["authFailures"] == 0, "add_ext rejected the extension token"
    assert stats["tokenRateLimited"] == 0, "native host token budget was exhausted"
    assert stats["extTokenRequests"] == 1
    assert stats["shutdownTokenRequests"] == 1
    assert stats["addOk"] == 1
    assert stats["shutdownOk"] == 1


def test_context_and_start_backend_do_not_spend_the_token_budget():
    """Neither diagnostics nor startup may eagerly read secrets."""
    outcome = _run_scenario("no-eager")

    assert outcome["result"]["context"]["ok"] is True
    assert outcome["result"]["started"]["ok"] is True
    assert outcome["stats"]["extTokenRequests"] == 0
    assert outcome["stats"]["shutdownTokenRequests"] == 0


def test_expired_cached_token_is_refetched_exactly_once():
    """A stale session token yields one 403, one refetch, and one successful retry."""
    outcome = _run_scenario("expired")

    assert outcome["result"]["added"]["ok"] is True
    stats = outcome["stats"]
    assert stats["authFailures"] == 1
    assert stats["extTokenRequests"] == 1
    assert stats["tokenRateLimited"] == 0
    assert stats["session"]["mnsExtensionToken"] == "fresh-token"


def test_refresh_ignores_an_unremovable_stale_session_copy():
    """Older browsers without storage.session.remove must still recover."""
    outcome = _run_scenario("no-session-remove")

    assert outcome["result"]["added"]["ok"] is True
    stats = outcome["stats"]
    assert stats["authFailures"] == 1
    assert stats["extTokenRequests"] == 1
    assert stats["tokenRateLimited"] == 0
    assert stats["session"]["mnsExtensionToken"] == "fresh-token"


def test_token_that_stays_rejected_stops_after_one_refresh_and_one_retry():
    """A failed refresh must not turn into a retry storm (or a second refresh)."""
    outcome = _run_scenario("persistent-failure")

    assert outcome["result"]["added"]["ok"] is False
    stats = outcome["stats"]
    assert stats["addRequests"] == 2
    assert stats["authFailures"] == 2
    assert stats["extTokenRequests"] == 2
    assert stats["tokenRateLimited"] == 0


def test_concurrent_adds_share_a_single_token_acquisition():
    """Two simultaneous callers must join the same in-flight acquisition."""
    outcome = _run_scenario("concurrent")

    assert [reply["ok"] for reply in outcome["result"]["added"]] == [True, True]
    stats = outcome["stats"]
    assert stats["extTokenRequests"] == 1
    assert stats["authFailures"] == 0
    assert stats["tokenRateLimited"] == 0
