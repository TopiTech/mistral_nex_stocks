"""Regression tests for hung fetch waits (review item K-08 and siblings).

Every browser/extension fetch that waits on the local backend must be bounded:

* ``chrome_extension/background.js`` - add-stock, health, badge and shutdown.
* ``chrome_extension/popup.js`` - the 5-second stock poll.
* ``static/js/utils.js`` - the shared ``csrfFetch`` used by the whole frontend.

The Node harnesses replace ``fetch`` with a promise that never settles unless
its AbortSignal fires. The fixed code must reject/return within the timeout and
surface the failure; the old code leaves the promise pending forever, so the
harness watchdog fails the test instead of hanging pytest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_BACKGROUND_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("chrome_extension/background.js", "utf8");

const realSetTimeout = setTimeout;
const timers = new Map();
let timerId = 0;
function fakeSetTimeout(fn, delay, ...args) {
  const id = ++timerId;
  timers.set(id, { fn, delay, args });
  return id;
}
function fakeClearTimeout(id) {
  timers.delete(id);
}
function runPendingTimers() {
  for (const [id, timer] of [...timers]) {
    timers.delete(id);
    timer.fn(...timer.args);
  }
}

const stats = { addRequests: 0, addSignals: 0, aborted: 0 };
const EXTENSION_ID = "a".repeat(32);

function abortError() {
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

function nativeHost(message, callback) {
  setImmediate(() => {
    if (message.action === "get_extension_api_token") {
      callback({ ok: true, token: "test-extension-token" });
    } else {
      callback({ ok: true, port: 5000 });
    }
  });
}

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function fetchMock(url, options = {}) {
  const target = String(url);
  if (target.endsWith("/api/health")) {
    return Promise.resolve(
      response(200, {
        ok: true,
        app: "Mistral NeX Stocks",
        ready: true,
        model: "test",
      }),
    );
  }
  if (target.endsWith("/api/stocks/add_ext")) {
    stats.addRequests += 1;
    return new Promise((resolve, reject) => {
      const signal = options.signal;
      if (!signal) return; // Old code: pending forever.
      stats.addSignals += 1;
      if (signal.aborted) {
        stats.aborted += 1;
        reject(abortError());
        return;
      }
      signal.addEventListener(
        "abort",
        () => {
          stats.aborted += 1;
          reject(abortError());
        },
        { once: true },
      );
    });
  }
  return Promise.resolve(response(404, { ok: false }));
}

let listener = null;
const context = {
  chrome: {
    storage: {
      local: {
        get(_keys, callback) {
          if (typeof callback === "function") callback({});
        },
        set() {},
        remove() {},
      },
      session: {
        get() {
          return Promise.resolve({});
        },
        set() {
          return Promise.resolve();
        },
        remove() {
          return Promise.resolve();
        },
        getItem() {
          return null;
        },
      },
    },
    runtime: {
      id: EXTENSION_ID,
      sendNativeMessage(_host, message, callback) {
        nativeHost(message, callback);
      },
      onMessage: {
        addListener(fn) {
          listener = fn;
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
  },
  console,
  fetch: fetchMock,
  navigator: { userAgent: "Chrome/120" },
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  setImmediate,
  AbortController,
  URL,
};
vm.createContext(context);
vm.runInContext(source, context, { filename: "chrome_extension/background.js" });

function dispatch(message) {
  return new Promise((resolve, reject) => {
    const watchdog = realSetTimeout(
      () => reject(new Error("message handler never responded")),
      1500,
    );
    listener(message, { id: EXTENSION_ID }, (reply) => {
      clearTimeout(watchdog);
      resolve(reply);
    });
  });
}

function waitFor(predicate, timeoutMs = 500) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    function poll() {
      if (predicate()) return resolve();
      if (Date.now() - started > timeoutMs) {
        return reject(new Error("condition was not reached"));
      }
      realSetTimeout(poll, 5);
    }
    poll();
  });
}

(async () => {
  const replyPromise = dispatch({
    action: "addDetectedStock",
    symbol: "AAPL",
    market: "us",
  });
  await waitFor(() => stats.addRequests === 1);
  if (stats.addSignals !== 1) {
    throw new Error("add-stock fetch did not receive an abort signal");
  }
  runPendingTimers();
  const reply = await replyPromise;
  if (reply.ok !== false) {
    throw new Error(`expected failure reply, got ${JSON.stringify(reply)}`);
  }
  if (!/タイムアウト/.test(String(reply.error || ""))) {
    throw new Error(`timeout was not reported: ${JSON.stringify(reply)}`);
  }
  if (stats.aborted !== 1) {
    throw new Error(`expected one abort, got ${stats.aborted}`);
  }
  process.stdout.write(JSON.stringify(stats));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""

_POPUP_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("chrome_extension/popup.js", "utf8");

const realSetTimeout = setTimeout;
const timers = new Map();
let timerId = 0;
function fakeSetTimeout(fn, delay, ...args) {
  const id = ++timerId;
  timers.set(id, { delay, fn, args });
  return id;
}
function fakeClearTimeout(id) {
  timers.delete(id);
}
function runPendingTimers() {
  for (const [id, timer] of [...timers]) {
    timers.delete(id);
    timer.fn(...timer.args);
  }
}

const stats = { requests: 0, signals: 0, aborted: 0 };
const classes = new Set();
const stockListContainer = {
  classList: {
    add(name) {
      classes.add(name);
    },
    remove(name) {
      classes.delete(name);
    },
    contains(name) {
      return classes.has(name);
    },
  },
  setAttribute() {},
  appendChild() {},
  textContent: "",
};
const stockRefreshTime = { textContent: "" };

function abortError() {
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

const context = {
  chrome: {
    storage: {
      local: {
        get(_keys, callback) {
          if (typeof callback === "function") callback({});
        },
      },
    },
    runtime: { sendMessage: async () => ({ ok: false }) },
  },
  console,
  navigator: { clipboard: { writeText: async () => {} } },
  document: {
    getElementById(id) {
      if (id === "stockListContainer") return stockListContainer;
      if (id === "stockRefreshTime") return stockRefreshTime;
      return null;
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
    createElement() {
      return { className: "", textContent: "", appendChild() {} };
    },
  },
  fetch(url, options = {}) {
    stats.requests += 1;
    return new Promise((resolve, reject) => {
      const signal = options.signal;
      if (!signal) return; // Old code: pending forever.
      stats.signals += 1;
      if (signal.aborted) {
        stats.aborted += 1;
        reject(abortError());
        return;
      }
      signal.addEventListener(
        "abort",
        () => {
          stats.aborted += 1;
          reject(abortError());
        },
        { once: true },
      );
    });
  },
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  AbortController,
  addEventListener() {},
  removeEventListener() {},
};
context.window = context;
context.global = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "chrome_extension/popup.js" });

function waitFor(predicate, timeoutMs = 500) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    function poll() {
      if (predicate()) return resolve();
      if (Date.now() - started > timeoutMs) {
        return reject(new Error("condition was not reached"));
      }
      realSetTimeout(poll, 5);
    }
    poll();
  });
}

(async () => {
  const renderPromise = context.fetchAndRenderStocks("http://127.0.0.1:5000");
  await waitFor(() => stats.requests === 1);
  if (stats.signals !== 1) {
    throw new Error("stock poll fetch did not receive an abort signal");
  }
  runPendingTimers();
  await Promise.race([
    renderPromise,
    new Promise((_, reject) =>
      realSetTimeout(
        () => reject(new Error("stock poll never completed")),
        1500,
      ),
    ),
  ]);
  if (stats.aborted !== 1) {
    throw new Error(`expected one abort, got ${stats.aborted}`);
  }
  if (!classes.has("stale")) {
    throw new Error("timed-out poll did not mark the list stale");
  }
  if (!String(stockRefreshTime.textContent).includes("更新失敗")) {
    throw new Error(
      `timed-out poll did not report failure: ${stockRefreshTime.textContent}`,
    );
  }
  process.stdout.write(JSON.stringify(stats));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""

_CSRF_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("static/js/utils.js", "utf8");
const start = source.indexOf("const _CSRF_SAFE_METHODS");
const end = source.indexOf("// #region Global Error Handler", start);
if (start < 0 || end < 0) throw new Error("CSRF utility section not found");

process.on("unhandledRejection", (error) => {
  console.error("unhandled rejection:", error);
  process.exit(1);
});

function abortError() {
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

const fastSignals = [];
let csrfToken = "initial-token";
let refreshStarted = false;
const context = {
  document: {
    querySelector(selector) {
      if (!String(selector).includes("csrf-token")) return null;
      return {
        getAttribute: () => csrfToken,
        setAttribute: (_name, value) => {
          csrfToken = String(value);
        },
      };
    },
  },
  Headers,
  AbortController,
  setTimeout,
  clearTimeout,
  showToast() {},
  fetch(url, options = {}) {
    const target = String(url);
    if (target.includes("/csrf-token")) {
      refreshStarted = true;
      return new Promise((resolve) => {
        setTimeout(
          () =>
            resolve({
              ok: true,
              status: 200,
              headers: { get: () => "application/json" },
              json: async () => ({ csrf_token: "fresh-token" }),
            }),
          500,
        );
      });
    }
    if (target.includes("/api/fast")) {
      fastSignals.push(options.signal);
      return new Promise((resolve) => {
        setTimeout(
          () =>
            resolve({
              ok: true,
              status: 200,
              headers: { get: () => "application/json" },
            }),
          5,
        );
      });
    }
    if (target.includes("/api/retry") && !refreshStarted) {
      return Promise.resolve({
        ok: false,
        status: 400,
        url: "/api/retry",
        headers: {
          get: (name) =>
            String(name).toLowerCase() === "content-type"
              ? "application/json"
              : "",
        },
        clone: () => ({
          json: async () => ({
            details: { reason: "The CSRF token has expired" },
          }),
        }),
      });
    }
    return new Promise((resolve, reject) => {
      const signal = options.signal;
      if (!signal) return;
      if (signal.aborted) {
        reject(abortError());
        return;
      }
      signal.addEventListener("abort", () => reject(abortError()), {
        once: true,
      });
    });
  },
};
vm.createContext(context);
const apiSource = fs.readFileSync("static/js/api.js", "utf8");
const apiEnd = apiSource.indexOf("// #region SSE & Real-time Integration");
if (apiEnd < 0) throw new Error("API error-handler section not found");
vm.runInContext(
  `${source.slice(start, end)}\n${apiSource.slice(0, apiEnd)}\nglobalThis.__csrfFetch = csrfFetch;\nglobalThis.__apiFetch = apiFetch;`,
  context,
);

function withWatchdog(promise, timeoutMs = 1500) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("request never settled")), timeoutMs),
    ),
  ]);
}

(async () => {
  const started = Date.now();
  let timeoutError = null;
  try {
    await withWatchdog(
      context.__csrfFetch("/api/slow", { method: "GET", timeoutMs: 40 }),
    );
  } catch (error) {
    timeoutError = error;
  }
  if (!timeoutError || timeoutError.name !== "TimeoutError") {
    throw new Error(
      `expected TimeoutError, got ${timeoutError && timeoutError.name}`,
    );
  }
  if (Date.now() - started > 1000) {
    throw new Error("timeout took far longer than requested");
  }

  const caller = new AbortController();
  const pending = context.__csrfFetch("/api/cancelled", {
    method: "GET",
    signal: caller.signal,
    timeoutMs: 60000,
  });
  caller.abort();
  let abortErrorSeen = null;
  try {
    await withWatchdog(pending);
  } catch (error) {
    abortErrorSeen = error;
  }
  if (!abortErrorSeen || abortErrorSeen.name !== "AbortError") {
    throw new Error(
      `caller cancellation changed into ${abortErrorSeen && abortErrorSeen.name}`,
    );
  }

  const fastResponse = await withWatchdog(
    context.__csrfFetch("/api/fast", { method: "GET", timeoutMs: 30 }),
  );
  await new Promise((resolve) => setTimeout(resolve, 60));
  const fastSignal = fastSignals[0];
  if (!fastResponse.ok || !fastSignal || fastSignal.aborted) {
    throw new Error("the timer aborted a response that had already arrived");
  }

  // A caller abort while the CSRF retry is waiting for a refreshed token must
  // settle immediately as AbortError, not wait for the refresh timeout and
  // then be reclassified as a timeout.
  const retryCaller = new AbortController();
  const retryStartedAt = Date.now();
  const retryPromise = context.__apiFetch("/api/retry", {
    method: "POST",
    signal: retryCaller.signal,
    timeoutMs: 60000,
  });
  setTimeout(() => retryCaller.abort(), 20);
  let retryError = null;
  try {
    await withWatchdog(retryPromise);
  } catch (error) {
    retryError = error;
  }
  const retryElapsedMs = Date.now() - retryStartedAt;
  if (!retryError || retryError.name !== "AbortError") {
    throw new Error(
      `retry cancellation changed into ${retryError && retryError.name}`,
    );
  }
  if (retryError.type !== "aborted") {
    throw new Error(`retry cancellation type is ${retryError.type}`);
  }
  if (!refreshStarted) {
    throw new Error("retry path never requested a CSRF refresh");
  }
  if (retryElapsedMs > 300) {
    throw new Error(`retry abort waited ${retryElapsedMs}ms for the refresh`);
  }

  process.stdout.write(
    JSON.stringify({
      timeoutName: timeoutError.name,
      abortName: abortErrorSeen.name,
      fastAborted: fastSignal.aborted,
      retryName: retryError.name,
      retryType: retryError.type,
      retryElapsedMs,
    }),
  );
  process.exit(0);
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


def _run(node_script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the fetch-timeout regression test")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=node_script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stderr
    return json.loads(lines[-1])


def test_add_stock_fetch_times_out_and_reports_the_failure():
    """A backend that accepts the connection but never answers must not hang."""
    stats = _run(_BACKGROUND_HARNESS)
    assert stats == {"addRequests": 1, "addSignals": 1, "aborted": 1}


def test_popup_stock_poll_times_out_and_marks_the_list_stale():
    """The poll chain must not stall forever on a stalled /api/stocks."""
    stats = _run(_POPUP_HARNESS)
    assert stats == {"requests": 1, "signals": 1, "aborted": 1}


def test_csrf_fetch_has_a_timeout_and_keeps_caller_cancellation_distinct():
    """csrfFetch must reject a hung request as TimeoutError, not hang or AbortError."""
    result = _run(_CSRF_HARNESS)
    assert result["timeoutName"] == "TimeoutError"
    assert result["abortName"] == "AbortError"
    assert result["fastAborted"] is False
    assert result["retryName"] == "AbortError"
    assert result["retryType"] == "aborted"
    assert result["retryElapsedMs"] <= 300
