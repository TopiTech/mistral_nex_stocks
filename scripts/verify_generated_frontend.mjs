import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import vm from "node:vm";

const compilerPath = join(
  process.cwd(),
  "node_modules",
  "typescript",
  "bin",
  "tsc",
);
const prettierPath = join(
  process.cwd(),
  "node_modules",
  "prettier",
  "bin",
  "prettier.cjs",
);

if (!existsSync(compilerPath)) {
  console.error(
    `TypeScript compiler not found at ${compilerPath}. Run 'npm ci' first.`,
  );
  process.exit(1);
}
if (!existsSync(prettierPath)) {
  console.error(`Prettier not found at ${prettierPath}. Run 'npm ci' first.`);
  process.exit(1);
}

const tempDirectory = mkdtempSync(join(tmpdir(), "mns-frontend-"));
const generatedPath = join(tempDirectory, "api_client.js");
const trackedPath = "static/js/api_client.js";

try {
  execFileSync(
    process.execPath,
    [compilerPath, "-p", "tsconfig.json", "--outFile", generatedPath],
    {
      stdio: "inherit",
    },
  );
  execFileSync(process.execPath, [prettierPath, "--write", generatedPath], {
    stdio: "inherit",
  });
  const generated = readFileSync(generatedPath);
  const tracked = readFileSync(trackedPath);
  if (!generated.equals(tracked)) {
    console.error(
      `${trackedPath} is out of date; run npm run compile and review the diff.`,
    );
    process.exitCode = 1;
  } else {
    console.log(`${trackedPath} matches the TypeScript output.`);
  }
  verifySseReconnectGuard();
} finally {
  rmSync(tempDirectory, { recursive: true, force: true });
}

/**
 * Behavioural check for the SSE reconnect guard in the tracked api_client.js.
 *
 * ``_teardownSSE()`` clears the pending-reconnect timer *and* the
 * ``_reconnecting`` flag that suppresses re-entrant reconnects. Because
 * ``_handleReconnect()`` installs a replacement timer immediately after that
 * teardown, it has to re-assert the flag; otherwise a second trigger during the
 * wait (up to 60s under the slow-retry policy) tears the pending timer down and
 * restarts the backoff from scratch instead of being skipped.
 *
 * The shipped script is loaded in a sandbox with deterministic fake timers, so
 * the check never touches the network or a real clock.
 */
function verifySseReconnectGuard() {
  const noop = () => {};
  let nextTimerId = 1;
  const timers = [];
  const schedule = (kind) => (callback, delay) => {
    const id = nextTimerId++;
    timers.push({ id, kind, delay, callback });
    return id;
  };
  const cancel = (id) => {
    const index = timers.findIndex((timer) => timer.id === id);
    if (index !== -1) timers.splice(index, 1);
  };

  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.listeners = new Map();
      this.closed = false;
    }
    addEventListener(type, handler) {
      this.listeners.set(type, handler);
    }
    removeEventListener(type) {
      this.listeners.delete(type);
    }
    close() {
      this.closed = true;
      this.readyState = 2;
    }
  }

  const sandbox = {
    window: { addEventListener: noop, removeEventListener: noop },
    document: {
      hidden: false,
      addEventListener: noop,
      removeEventListener: noop,
    },
    EventSource: FakeEventSource,
    console: { debug: noop, info: noop, warn: noop, error: noop },
    setTimeout: schedule("timeout"),
    clearTimeout: cancel,
    setInterval: schedule("interval"),
    clearInterval: cancel,
  };
  vm.runInNewContext(readFileSync(trackedPath, "utf8"), sandbox, {
    filename: trackedPath,
  });

  const APIClient = sandbox.window.APIClient;
  if (typeof APIClient !== "function") {
    throw new Error(`${trackedPath} did not publish window.APIClient`);
  }

  const client = new APIClient("/api", {
    sseHeartbeatTimeout: 60000,
    sseReconnectBaseDelay: 1000,
    sseReconnectMaxDelay: 1000,
    watchdogInterval: 60000,
    visibilityTimeout: 60000,
  });
  const onError = noop;
  client.openSSE("https://127.0.0.1/api/stocks/stream", noop, onError, {});
  if (!client.currentEventSource) {
    throw new Error("openSSE() created no EventSource for the initial connect");
  }

  client.reconnectWithBackoff(onError);
  const firstAttempt = client.sseReconnectAttempt;
  const firstPending = client.ssePendingReconnectTimeout;
  if (firstAttempt !== 1 || firstPending === null) {
    throw new Error(
      `first reconnect was not scheduled (attempt=${firstAttempt}, pending=${String(firstPending)})`,
    );
  }

  client.reconnectWithBackoff(onError);
  if (client.sseReconnectAttempt !== firstAttempt) {
    throw new Error(
      `re-entrant reconnect advanced the backoff (${firstAttempt} -> ${client.sseReconnectAttempt})`,
    );
  }
  if (client.ssePendingReconnectTimeout !== firstPending) {
    throw new Error(
      "re-entrant reconnect replaced the pending reconnect timer",
    );
  }

  client.destroy();
  console.log(
    "api_client.js SSE reconnect guard suppresses re-entrant scheduling.",
  );
}
