"""Regression coverage for the 2026-10-29 frontend data-flow fixes.

These tests run the real JavaScript files through Node's vm, matching the
project's existing frontend test layer (pytest + subprocess). They pin two
behaviours that static source assertions cannot prove:

* DATA-3: a populated stale history payload is rendered even while the server
  also reports ``fetching: true``.
* CONC-3: repeated malformed realtime frames use the APIClient backoff
  scheduler and eventually stop reconnecting with a visible terminal toast.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend data-flow coverage")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return result.stdout


def test_history_fetch_uses_payload_before_fetching_status() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "chart.js"),
  "utf8",
);

const history = [
  { x: 1793145600000, o: 100, h: 101, l: 99, c: 100.5, v: 1000 },
];
let calls = 0;
let mode = "stale";
const context = {
  console,
  AbortController,
  Promise,
  Error,
  TypeError,
  setTimeout: (fn, ms) => {
    if (ms < 5000) fn();
    return 0;
  },
  clearTimeout: () => {},
  apiFetch: async () => {
    calls += 1;
    if (mode === "retry" && calls === 1) throw new TypeError("network");
    return { data: { history, stale: true, fetching: true } };
  },
  CONSTANTS: {
    TIMEOUT: { STOCK_HISTORY: 30000, STOCK_HISTORY_RETRY: 30000 },
  },
  normalizeHistoryData: (rows) => ({ formattedData: rows, ohlcData: rows }),
  logger: { warn() {}, info() {}, debug() {}, error() {} },
  DOM: { get: () => null },
  document: { getElementById: () => null, querySelector: () => null },
  window: { addEventListener() {} },
  getStockByKey: () => null,
  getChartPref: () => "1d",
};
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: "chart.js" });

(async () => {
  const first = await vm.runInContext(
    'fetchStockHistoryPayload("AAPL", "us", "3mo", "1d", null)',
    context,
  );
  if (calls !== 1) throw new Error(`expected one request, got ${calls}`);
  if (!first || first.formattedData.length !== 1) {
    throw new Error(`cached history was discarded: ${JSON.stringify(first)}`);
  }
  if (first.formattedData[0].c !== 100.5) {
    throw new Error(`wrong close: ${JSON.stringify(first.formattedData[0])}`);
  }

  // The TypeError retry branch has its own response-application site; it must
  // use the same payload-first rule.
  calls = 0;
  mode = "retry";
  const second = await vm.runInContext(
    'fetchStockHistoryPayload("AAPL", "us", "3mo", "1d", null)',
    context,
  );
  if (calls !== 2) throw new Error(`expected two retry requests, got ${calls}`);
  if (!second || second.formattedData.length !== 1) {
    throw new Error(`retry discarded cached history: ${JSON.stringify(second)}`);
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""

    assert _run_node(script) == "ok"


def test_internal_history_timeout_is_not_reported_as_user_abort() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "chart.js"),
  "utf8",
);

const timers = [];
let nextTimerId = 1;
const chartErrors = [];

const context = {
  console,
  AbortController,
  Promise,
  Error,
  TypeError,
  Date,
  Math,
  Number,
  Array,
  Object,
  JSON,
  setTimeout: (fn, ms) => {
    const id = nextTimerId++;
    timers.push({ id, fn, ms, cleared: false });
    return id;
  },
  clearTimeout: (id) => {
    const timer = timers.find((entry) => entry.id === id);
    if (timer) timer.cleared = true;
  },
  apiFetch: (url, options) =>
    new Promise((resolve, reject) => {
      const signal = options && options.signal;
      const fail = () => {
        const err = new Error("aborted");
        err.name = "AbortError";
        reject(err);
      };
      if (!signal) {
        reject(new Error("missing signal"));
        return;
      }
      if (signal.aborted) {
        fail();
        return;
      }
      signal.addEventListener("abort", fail, { once: true });
    }),
  CONSTANTS: {
    TIMEOUT: { STOCK_HISTORY: 30000, STOCK_HISTORY_RETRY: 30000 },
    PREFETCH: { CACHE_TTL_MS: 60000, PERIOD: "3mo", MAX_ITEMS: 10 },
  },
  historyPrefetchCache: new Map(),
  getHistoryPrefetchKey: (stockKey, period, interval) =>
    `${stockKey}:${period}:${interval}`,
  normalizeHistoryData: (rows) => ({ formattedData: rows, ohlcData: rows }),
  logger: { warn() {}, info() {}, debug() {}, error() {} },
  DOM: { get: () => null },
  document: { getElementById: () => null, querySelector: () => null },
  window: { addEventListener() {} },
  getStockByKey: () => ({ symbol: "AAPL", market: "us" }),
  getChartPref: (key, name, fallback) => fallback,
  clearChartError: () => {},
  showChartError: (wrapper, message, kind) => {
    chartErrors.push({ message, kind });
  },
  __fireTimeout: () => {
    for (const timer of timers) {
      if (!timer.cleared && timer.ms >= 30000) {
        timer.cleared = true;
        timer.fn();
      }
    }
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "chart.js" });

(async () => {
  // Internal timeout: the request is aborted by the chart's own 30s timer.
  const pending = vm.runInContext(
    'fetchStockHistoryPayload("AAPL", "us", "3mo", "1d", null)',
    context,
  );
  context.__fireTimeout();
  let timeoutErr = null;
  try {
    await pending;
  } catch (err) {
    timeoutErr = err;
  }
  if (!timeoutErr || timeoutErr.name !== "TimeoutError") {
    throw new Error(`expected TimeoutError, got ${timeoutErr && timeoutErr.name}`);
  }

  // A caller cancellation must stay an AbortError (silent in the UI).
  const caller = new AbortController();
  caller.abort();
  context.__callerSignal = caller.signal;
  let abortErr = null;
  try {
    await vm.runInContext(
      'fetchStockHistoryPayload("AAPL", "us", "3mo", "1d", __callerSignal)',
      context,
    );
  } catch (err) {
    abortErr = err;
  }
  if (!abortErr || abortErr.name !== "AbortError") {
    throw new Error(`expected AbortError, got ${abortErr && abortErr.name}`);
  }

  // The real refresh path must surface the timeout instead of leaving a blank chart.
  const wrapper = {
    dataset: { stockKey: "us:AAPL" },
    querySelector: () => null,
    _chartAbortController: null,
  };
  const refresh = vm.runInContext(
    'refreshStockChart(__wrapper, "3mo", "1d")',
    (context.__wrapper = wrapper, context),
  );
  context.__fireTimeout();
  await refresh;
  if (chartErrors.length !== 1) {
    throw new Error(`expected one chart error, got ${JSON.stringify(chartErrors)}`);
  }
  if (!chartErrors[0].message.includes("タイムアウト")) {
    throw new Error(`wrong chart error: ${JSON.stringify(chartErrors[0])}`);
  }
  if (chartErrors[0].kind !== "error") {
    throw new Error(`wrong chart error kind: ${chartErrors[0].kind}`);
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""

    assert _run_node(script) == "ok"


def test_realtime_parse_errors_back_off_then_stop() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "api.js"),
  "utf8",
);

const reconnectAttempts = [];
const toasts = [];
let openCalls = 0;
let fallbackIntervals = 0;

class FakeAPIClient {
  constructor() {
    this.lastEventId = 0;
    this.sseReconnectAttempt = 0;
    this.listeners = {};
    this.currentEventSource = null;
  }
  closeSSE() {
    this.currentEventSource = null;
  }
  resetHeartbeat() {}
  reconnectWithBackoff(_onError, attempt) {
    reconnectAttempts.push(attempt);
  }
  openSSE(_url, _onMessage, _onError, options) {
    openCalls += 1;
    const listeners = {};
    const es = {
      addEventListener(type, listener) {
        listeners[type] = listener;
      },
      close() {},
    };
    this.listeners = listeners;
    this.currentEventSource = es;
    options.onReconnect?.(es);
    return es;
  }
}

const context = {
  console,
  APIClient: FakeAPIClient,
  AbortController,
  Promise,
  JSON,
  Number,
  Math,
  Date,
  Error,
  TypeError,
  Object,
  Array,
  setTimeout,
  clearTimeout,
  setInterval: () => {
    fallbackIntervals += 1;
    return 1;
  },
  clearInterval: () => {},
  logger: { debug() {}, info() {}, warn() {}, error() {} },
  document: {
    hidden: false,
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  localStorage: { getItem: () => "2", setItem: () => {} },
  state: { isStreaming: true, stocks: { us: [], jp: [], idx: [] } },
  DOM: { get: () => null },
  pollingManager: { clearInterval: () => {}, setInterval: () => {} },
  updateSseModeSelectorUI: () => {},
  renderSkeletons: () => {},
  fetchInitialStocks: () => {},
  showToast: (message, color) => toasts.push({ message, color }),
  csrfFetch: async () => ({
    ok: true,
    json: async () => ({ ok: true }),
  }),
};
context.globalThis = context;
context.window = {
  addEventListener: () => {},
  removeEventListener: () => {},
};

vm.createContext(context);
vm.runInContext(source, context, { filename: "api.js" });

(async () => {
  vm.runInContext("connectSSE(2)", context);
  await new Promise((resolve) => setTimeout(resolve, 0));

  const client = vm.runInContext("sseApiClient", context);
  const malformed = () =>
    client.listeners.realtime_update({ data: '{"bad":' });

  for (let cycle = 0; cycle < 3; cycle += 1) {
    malformed();
    malformed();
    malformed();
  }
  if (openCalls !== 1) {
    throw new Error(`malformed frames opened immediately: ${openCalls} opens`);
  }
  if (JSON.stringify(reconnectAttempts) !== "[1,2,3]") {
    throw new Error(
      `expected backoff attempts [1,2,3], got ${JSON.stringify(reconnectAttempts)}`,
    );
  }
  if (toasts.length !== 0) throw new Error("terminal toast fired too early");

  malformed();
  malformed();
  malformed();

  if (toasts.length !== 1) throw new Error("terminal toast was not surfaced");
  if (!toasts[0].message.includes("リアルタイム配信を停止")) {
    throw new Error(`unexpected terminal toast: ${JSON.stringify(toasts[0])}`);
  }
  if (context.state.isStreaming !== false) {
    throw new Error("state.isStreaming was not cleared");
  }
  if (fallbackIntervals === 0) throw new Error("fallback polling was not started");

  malformed();
  malformed();
  malformed();
  if (toasts.length !== 1) {
    throw new Error("terminal state emitted a duplicate toast after stopping");
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""

    assert _run_node(script) == "ok"


def test_reconnect_with_backoff_uses_existing_delay_policy() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "api_client.js"),
  "utf8",
);

const timers = [];
let nextTimerId = 1;
class FakeEventSource {
  constructor() {
    this.listeners = {};
  }
  addEventListener(type, listener) {
    this.listeners[type] = listener;
  }
  removeEventListener() {}
  close() {}
}
const window = {
  addEventListener: () => {},
  removeEventListener: () => {},
  APIClient: null,
  APIError: null,
};
const context = {
  console,
  window,
  document: { hidden: false, addEventListener: () => {} },
  EventSource: FakeEventSource,
  DOMException,
  setTimeout: (fn, ms) => {
    const id = nextTimerId++;
    timers.push({ id, fn, ms });
    return id;
  },
  clearTimeout: () => {},
  setInterval: () => 1,
  clearInterval: () => {},
  Date,
  Math,
  Number,
  Error,
  TypeError,
  Object,
  Array,
  Promise,
  JSON,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "api_client.js" });

const client = new context.window.APIClient("/api");
client.openSSE("/stocks/stream?mode=2", () => {}, () => {}, {
  autoReconnect: true,
  maxReconnectAttempts: 7,
});
client.reconnectWithBackoff(() => {}, 2);

if (timers.length !== 1) {
  throw new Error(`expected one scheduled reconnect, got ${timers.length}`);
}
const delay = timers[0].ms;
if (!(delay >= 2000 && delay <= 6000)) {
  throw new Error(`attempt 2 delay ${delay} is outside the jittered 4s window`);
}
if (client.sseReconnectAttempt !== 2) {
  throw new Error(`expected reconnect attempt 2, got ${client.sseReconnectAttempt}`);
}
process.stdout.write("ok");
"""

    assert _run_node(script).strip().endswith("ok")


def test_realtime_previous_price_state_dies_with_the_card() -> None:
    """CONC-4: no global symbol->price registry; flash state belongs to the card."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "realtime_client.js"),
  "utf8",
);

const wrappers = [];
function makeClassList() {
  const classes = new Set();
  return {
    add: (...names) => names.forEach((name) => classes.add(name)),
    remove: (...names) => names.forEach((name) => classes.delete(name)),
    contains: (name) => classes.has(name),
  };
}
function makeWrapper(symbol) {
  const priceEl = { textContent: "", classList: makeClassList(), __flashTimer: null };
  const wrapper = {
    dataset: { symbol },
    __stockData: { symbol, price: 0 },
    isConnected: true,
    querySelector(selector) {
      return selector === ".compact-price" ? priceEl : null;
    },
    priceEl,
  };
  wrappers.push(wrapper);
  return wrapper;
}

const documentStub = {
  listeners: {},
  addEventListener(type, listener) {
    this.listeners[type] = listener;
  },
  querySelectorAll(selector) {
    if (selector.includes(".compact-pts")) return [];
    const symbols = Array.from(selector.matchAll(/data-symbol="([^"]+)"/g)).map(
      (match) => match[1],
    );
    return wrappers.filter((wrapper) => symbols.includes(wrapper.dataset.symbol));
  },
};

const context = {
  console,
  document: documentStub,
  CSS: { escape: (value) => String(value) },
  setTimeout: () => 1,
  clearTimeout: () => {},
  Number,
  Object,
  Array,
  String,
  JSON,
  Date,
  Math,
  Error,
};
context.window = context;
context.globalThis = context;
context.requestAnimationFrame = (callback) => callback();
context.state = { stocks: { us: [], jp: [], idx: [] } };

vm.createContext(context);
vm.runInContext(source, context, { filename: "realtime_client.js" });
documentStub.listeners.DOMContentLoaded();
const client = context.window.realtimeClient;

if (!client) throw new Error("RealtimeStockClient was not initialised");
if ("priceStore" in client) {
  throw new Error("a symbol-keyed priceStore registry still exists");
}

const wrapper = makeWrapper("AAPL");
client.handleDeltas({ AAPL: { price: 100 } });
const priceEl = wrapper.priceEl;
if (
  priceEl.classList.contains("flash-up") ||
  priceEl.classList.contains("flash-down")
) {
  throw new Error("the first delta must not flash without a previous price");
}
client.handleDeltas({ AAPL: { price: 101 } });
if (!priceEl.classList.contains("flash-up")) {
  throw new Error("an existing card did not flash up on a price increase");
}
client.handleDeltas({ AAPL: { price: 99 } });
if (!priceEl.classList.contains("flash-down")) {
  throw new Error("an existing card did not flash down on a price decrease");
}

// Remove the card, then re-add one for the same symbol. The new card has never
// rendered a price, so it must not flash against the removed card's last price.
wrapper.isConnected = false;
wrappers.length = 0;
const fresh = makeWrapper("AAPL");
client.handleDeltas({ AAPL: { price: 102 } });
if (
  fresh.priceEl.classList.contains("flash-up") ||
  fresh.priceEl.classList.contains("flash-down")
) {
  throw new Error("a re-added card flashed against a price from a removed card");
}
if (!fresh.priceEl.textContent.includes("102")) {
  throw new Error(`the re-added card did not render: ${fresh.priceEl.textContent}`);
}

// Symbols without a card must not leave state behind on the client.
const before = Object.keys(client).sort().join(",");
for (let i = 0; i < 500; i += 1) {
  client.handleDeltas({ ["SYM" + i]: { price: i + 1 } });
}
const after = Object.keys(client).sort().join(",");
if (before !== after) {
  throw new Error(`the client accumulated symbol state: ${after}`);
}
process.stdout.write("ok");
"""

    assert _run_node(script) == "ok"


def test_settings_reset_surfaces_the_server_reason() -> None:
    """DATA-5 (UI): reset must show details.reason, not only HTTP status."""
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(process.cwd(), "static", "js", "settings.js"),
  "utf8",
);

const toasts = [];
const responses = [];
const context = {
  console,
  Promise,
  JSON,
  Error,
  Object,
  Array,
  String,
  Number,
  Math,
  Date,
  document: {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  csrfFetch: async () => responses.shift(),
  showToast: (message, color) => toasts.push({ message, color }),
  logger: { error() {}, warn() {}, info() {}, debug() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  loadStocks: () => {},
  setTimeout: () => 0,
  clearTimeout: () => {},
  confirm: () => true,
};
context.globalThis = context;
context.window = {};
vm.createContext(context);
vm.runInContext(source, context, { filename: "settings.js" });

const jsonResponse = (status, statusText, body) => ({
  ok: false,
  status,
  statusText,
  text: async () => JSON.stringify(body),
});

(async () => {
  responses.push(
    jsonResponse(409, "Conflict", {
      ok: false,
      error: "初期化できません",
      details: { reason: "カスタム銘柄を保存できませんでした" },
    }),
  );
  await vm.runInContext("resetAllStocks()", context);
  if (toasts.length !== 1) throw new Error("reset did not report an error");
  if (!toasts[0].message.includes("カスタム銘柄を保存できませんでした")) {
    throw new Error(`reset lost the reason: ${toasts[0].message}`);
  }
  if (toasts[0].message.includes("HTTP 409")) {
    throw new Error(`reset fell back to the bare status: ${toasts[0].message}`);
  }

  // The delete path uses the same parser; keep its behavior pinned too.
  toasts.length = 0;
  responses.push(
    jsonResponse(400, "Bad Request", {
      ok: false,
      error: "削除できません",
      details: { reason: "その銘柄は削除できません" },
    }),
  );
  await vm.runInContext('executeDeleteStock("us", "AAPL")', context);
  if (toasts.length !== 1) throw new Error("delete did not report an error");
  if (!toasts[0].message.includes("その銘柄は削除できません")) {
    throw new Error(`delete lost the reason: ${toasts[0].message}`);
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""

    assert _run_node(script) == "ok"
