"""Regression: the first SSE open must resolve its ticket through APIClient.

`connectSSE` used to await ``buildStreamUrl()`` itself and silently ``return``
when the ticket POST failed. That bypassed ``APIClient``'s onError/backoff
handling, so a single transient ticket failure left ``state.isStreaming`` true
with no EventSource and no fallback polling until a manual reload/mode switch.

The fix routes the initial open (empty URL) through the same
resolve -> open -> onError/reconnect path that reconnects use. This test loads
the real generated ``api_client.js`` and exercises both the failure and the
success path of that resolution.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent

_NODE_SCRIPT = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "api_client.js"),
  "utf8",
);

const timers = new Map();
let nextTimerId = 1;
const opened = [];
let providerCalls = 0;
let providerUrl = null;

function FakeEventSource(url) {
  this.url = url;
  this.listeners = {};
  this.readyState = 0;
}
FakeEventSource.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};
FakeEventSource.prototype.removeEventListener = function () {};
FakeEventSource.prototype.close = function () {
  this.readyState = 2;
};

const context = {
  console,
  setTimeout: (fn, ms) => {
    const id = nextTimerId++;
    timers.set(id, { fn, ms });
    return id;
  },
  clearTimeout: (id) => {
    timers.delete(id);
  },
  setInterval: () => 1,
  clearInterval: () => {},
  EventSource: FakeEventSource,
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
const errors = [];
let reconnectCallbacks = 0;

const immediate = client.openSSE(
  "",
  () => {},
  (error) => errors.push(String((error && error.message) || error)),
  {
    autoReconnect: true,
    maxReconnectAttempts: 3,
    urlProvider: async () => {
      providerCalls += 1;
      return providerUrl;
    },
    onReconnect: (eventSource) => {
      reconnectCallbacks += 1;
      opened.push(eventSource.url);
    },
  },
);

(async () => {
  if (immediate !== null) {
    throw new Error("initial open must be asynchronous when the URL is minted");
  }

  // First resolution fails (ticket endpoint error mapped to a null URL).
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  if (providerCalls !== 1) {
    throw new Error(`expected one provider call, got ${providerCalls}`);
  }
  if (errors.length !== 1) {
    throw new Error(`expected one onError, got ${JSON.stringify(errors)}`);
  }
  if (opened.length !== 0) {
    throw new Error("no EventSource may be created without a resolved URL");
  }

  // A retry must be scheduled; run it with a now-healthy provider.
  const retryId = client.ssePendingReconnectTimeout;
  if (!retryId || !timers.has(retryId)) {
    throw new Error("initial ticket failure did not schedule a retry");
  }
  providerUrl = "/api/stocks/stream?mode=2";
  timers.get(retryId).fn();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  if (providerCalls !== 2) {
    throw new Error(`retry did not re-resolve the URL: ${providerCalls}`);
  }
  if (opened.length !== 1 || !opened[0].endsWith("mode=2")) {
    throw new Error(`retry did not open the resolved stream: ${opened}`);
  }
  if (reconnectCallbacks !== 1) {
    throw new Error("onReconnect was not invoked for the resolved connection");
  }

  // A late failure from an abandoned connection must not create an
  // EventSource: closeSSE() clears the stored params while resolution is in
  // flight.
  let lateProviderCalls = 0;
  let resolveLate;
  client.openSSE(
    "",
    () => {},
    () => {},
    {
      autoReconnect: true,
      urlProvider: () =>
        new Promise((resolve) => {
          lateProviderCalls += 1;
          resolveLate = resolve;
        }),
    },
  );
  await Promise.resolve();
  const before = opened.length;
  client.closeSSE();
  resolveLate("/api/stocks/stream?mode=1");
  await Promise.resolve();
  await Promise.resolve();
  if (lateProviderCalls !== 1 || opened.length !== before) {
    throw new Error("closed connection opened a stale EventSource");
  }

  process.stdout.write("ok");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""


def test_initial_sse_open_resolves_and_retries_through_api_client() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the generated SSE client")

    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=_NODE_SCRIPT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.strip().endswith("ok")
