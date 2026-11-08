"""Regression: a hidden tab must still pause SSE after a reconnect.

``APIClient._teardownSSE()`` clears the deferred visibility-pause timer. A
transport error (or heartbeat timeout) while the tab is hidden therefore used to
cancel the pause permanently: ``visibilitychange`` does not fire again until the
tab becomes visible, so the reconnected ``EventSource`` kept streaming in a
background tab — the resource use the deferred pause exists to prevent.

The fix re-arms the pause whenever a source is opened, a reconnect is scheduled,
or a URL resolution is in flight while ``document.hidden`` is true. This test
loads the real generated ``api_client.js`` and exercises:

* the hidden-tab reconnect path (pause must be re-armed, and firing it must
  close the source and cancel the reconnect),
* an open that starts while the tab is already hidden,
* the normal visible path (no pause is armed) and resume-on-visible.
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
const documentHandlers = {};
const instances = [];

function FakeEventSource(url) {
  this.url = url;
  this.closed = false;
  this.listeners = {};
  instances.push(this);
}
FakeEventSource.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};
FakeEventSource.prototype.removeEventListener = function () {};
FakeEventSource.prototype.close = function () {
  this.closed = true;
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
    addEventListener(type, fn) {
      documentHandlers[type] = fn;
    },
    removeEventListener() {},
  },
  window: { addEventListener() {}, removeEventListener() {} },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "api_client.js" });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function findVisibilityTimer(client) {
  const id = client._visibilityTimeout;
  if (!id) return null;
  const timer = timers.get(id);
  if (!timer) return null;
  // The deferred pause uses the configured visibilityTimeout (30s default);
  // reconnect backoff is far shorter, so this also proves the right timer.
  return timer.ms === client.visibilityTimeout ? timer : null;
}

(async () => {
  const client = new context.window.APIClient("/api");

  // 1. Visible open must not arm a pause.
  const first = client.openSSE("/api/stocks/stream", () => {}, () => {}, {});
  assert(first !== null, "visible open must create an EventSource");
  assert(!client._visibilityTimeout, "visible open must not arm a pause");

  // 2. Hide the tab, then hit a transport error while still hidden.
  context.document.hidden = true;
  documentHandlers.visibilitychange();
  assert(findVisibilityTimer(client), "hiding must arm the deferred pause");

  first.onerror(new Error("transport dropped"));
  assert(
    !!client.ssePendingReconnectTimeout,
    "a transport error must schedule a reconnect",
  );
  assert(
    first.closed,
    "the errored EventSource must be closed before reconnecting",
  );
  const rearmed = findVisibilityTimer(client);
  assert(
    rearmed,
    "reconnect while hidden must re-arm the deferred visibility pause",
  );

  // 3. Firing the pause must pause the client and cancel the reconnect.
  rearmed.fn();
  assert(client.isVisibilityPaused === true, "the client must be paused");
  assert(
    client.currentEventSource === null,
    "pausing must leave no open EventSource",
  );
  assert(
    client.ssePendingReconnectTimeout === null,
    "pausing must cancel the pending reconnect",
  );
  assert(
    instances.every((instance) => instance.closed),
    "every EventSource must be closed while paused",
  );

  // 4. Becoming visible resumes a single fresh connection.
  context.document.hidden = false;
  documentHandlers.visibilitychange();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert(client.isVisibilityPaused === false, "visible resume must unpause");
  assert(
    instances.length === 2,
    `resume must open exactly one new EventSource, got ${instances.length}`,
  );
  assert(
    !instances[1].closed,
    "the resumed EventSource must stay open",
  );
  client.closeSSE();

  // 5. A connection that starts while the tab is already hidden must pause too.
  const hiddenClient = new context.window.APIClient("/api");
  context.document.hidden = true;
  hiddenClient.openSSE("/api/stocks/stream", () => {}, () => {}, {});
  assert(
    findVisibilityTimer(hiddenClient),
    "an open while hidden must arm the deferred pause",
  );
  hiddenClient.destroy();

  process.stdout.write("ok");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""


def test_hidden_tab_pause_survives_reconnect_and_resumes() -> None:
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
