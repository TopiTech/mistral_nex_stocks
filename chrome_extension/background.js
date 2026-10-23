// --- Security Utilities ---
/**
 * ルートパスをサニタイズ
 * @param {string} route - サニタイズ対象のルートパス
 * @returns {string} サニタイズされたルートパス
 */
const ALLOWED_ROUTES = new Set([
  "/main",
  "/screener",
  "/heatmap",
  "/setup",
  "/settings",
  "/experimental/orbit",
  "/",
]);

function sanitizeRoute(route) {
  if (!route || typeof route !== "string") {
    return "/";
  }
  const trimmed = route.trim();
  const normalized = trimmed.startsWith("/") ? trimmed : "/" + trimmed;
  return ALLOWED_ROUTES.has(normalized) ? normalized : "/";
}

const HOST_NAME = "com.mistral_nex_stocks.host";
const DEFAULT_BACKEND_PORT = 5000;
const ADD_STOCK_TIMEOUT_MS = 10000;
const BADGE_FETCH_TIMEOUT_MS = 10000;
const HEALTH_TIMEOUT_MS = 3000;
const SHUTDOWN_TIMEOUT_MS = 5000;
let mnsShutdownToken = null;
let mnsExtensionToken = null;
let backendPort = DEFAULT_BACKEND_PORT;

function normalizeBackendPort(value) {
  const port = Number(value);
  if (Number.isInteger(port) && port > 0 && port <= 65535) {
    return port;
  }
  return DEFAULT_BACKEND_PORT;
}

/**
 * fetch with a bounded wait for the backend to answer. A stalled backend must
 * never leave the service worker waiting forever (badge updates hold a lock,
 * and add/stop flows hold the popup's button state). The timer is cleared as
 * soon as the response arrives so streaming bodies are not cut off.
 */
function fetchWithTimeout(url, options = {}, timeoutMs = ADD_STOCK_TIMEOUT_MS) {
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const callerSignal = options.signal;
  const abortFromCaller = () => controller.abort();
  if (callerSignal) {
    if (callerSignal.aborted) controller.abort();
    else
      callerSignal.addEventListener("abort", abortFromCaller, { once: true });
  }
  const cleanup = () => {
    clearTimeout(timeoutId);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  };
  return fetch(url, { ...options, signal: controller.signal })
    .catch((error) => {
      if (timedOut) {
        const timeoutError = new Error("リクエストがタイムアウトしました");
        timeoutError.name = "TimeoutError";
        throw timeoutError;
      }
      throw error;
    })
    .finally(cleanup);
}

function buildBackendUrls(port = backendPort) {
  const normalized = normalizeBackendPort(port);
  return [`http://127.0.0.1:${normalized}`, `http://localhost:${normalized}`];
}

let BACKEND_URLS = buildBackendUrls();

// The shutdown token is single-use and minted per shutdown flow, so it
// is kept only in memory: persisting it in chrome.storage.session would
// widen its exposure surface without benefit (only this worker uses it).
function setMnsShutdownToken(value) {
  mnsShutdownToken = value;
}

function getMnsSessionStorage() {
  return chrome.storage && chrome.storage.session
    ? chrome.storage.session
    : null;
}

function setMnsExtensionToken(value) {
  mnsExtensionToken = value;
  // Use session storage when available; older browsers keep the token in memory.
  const sessionStorage = getMnsSessionStorage();
  if (!sessionStorage || typeof sessionStorage.set !== "function") return;
  try {
    const pending = sessionStorage.set({ mnsExtensionToken: value });
    if (pending && typeof pending.catch === "function") {
      pending.catch((error) =>
        console.warn(
          "Failed to persist extension token in session storage:",
          error,
        ),
      );
    }
  } catch (error) {
    console.warn(
      "Failed to persist extension token in session storage:",
      error,
    );
  }
}

function setBackendPort(value) {
  backendPort = normalizeBackendPort(value);
  BACKEND_URLS = buildBackendUrls(backendPort);
  chrome.storage.local.set({ backendPort: backendPort });
}

// Load persisted state from storage
chrome.storage.local.get(["backendPort"], (items) => {
  if (items.backendPort) {
    backendPort = normalizeBackendPort(items.backendPort);
    BACKEND_URLS = buildBackendUrls(backendPort);
  }
});
// Remove shutdown tokens written by older versions to persistent storage.
chrome.storage.local.remove("mnsShutdownToken");

let mnsSessionStorage = null;
try {
  mnsSessionStorage = getMnsSessionStorage();
} catch (error) {
  // Some older Chromium-compatible browsers expose storage.session but throw
  // when the API is accessed.  The token can still be kept in memory.
  console.warn("Failed to access storage.session:", error);
}
if (mnsSessionStorage && typeof mnsSessionStorage.get === "function") {
  try {
    const pending = mnsSessionStorage.get(["mnsExtensionToken"], (items) => {
      // A token acquired while this read was pending is newer than the copy
      // this callback started with, so never overwrite it.
      if (items?.mnsExtensionToken && !mnsExtensionToken) {
        mnsExtensionToken = items.mnsExtensionToken;
      }
    });
    if (pending && typeof pending.catch === "function") {
      pending.catch((error) =>
        console.warn("Failed to load token from storage.session:", error),
      );
    }
  } catch (error) {
    // Treat session storage as an optional cache; native token retrieval below
    // remains the source of truth.
    console.warn("Failed to load token from storage.session:", error);
  }
}

let mnsExtensionTokenInflight = null;

async function getOrFetchExtensionToken(forceRefresh = false) {
  if (forceRefresh) {
    // The backend rejected the cached value, so drop both copies and ask the
    // native host again (it owns the token's rotation).
    mnsExtensionToken = null;
    mnsExtensionTokenInflight = null;
    try {
      const sessionStorage = getMnsSessionStorage();
      if (sessionStorage && typeof sessionStorage.remove === "function") {
        await sessionStorage.remove("mnsExtensionToken");
      }
    } catch (e) {
      console.warn("Failed to clear stale extension token:", e);
    }
  }
  if (mnsExtensionToken) return mnsExtensionToken;

  // The native host only returns a few secrets per window: concurrent callers
  // share one acquisition, and getContext/startBackend never read this token.
  if (mnsExtensionTokenInflight) return mnsExtensionTokenInflight;

  const acquisition = (async () => {
    // After a forced refresh the persisted copy is known to be rejected (or
    // could not be removed), so only the native host can provide a valid one.
    if (!forceRefresh) {
      try {
        const sessionStorage = getMnsSessionStorage();
        if (sessionStorage && typeof sessionStorage.get === "function") {
          const items = await sessionStorage.get("mnsExtensionToken");
          if (items && items.mnsExtensionToken) {
            mnsExtensionToken = items.mnsExtensionToken;
            return mnsExtensionToken;
          }
        }
      } catch (e) {
        console.warn("Failed to get token from storage.session:", e);
      }
    }
    try {
      const res = await sendNativeMessage({
        action: "get_extension_api_token",
      });
      if (res && res.ok && res.token) {
        setMnsExtensionToken(res.token);
        return res.token;
      }
    } catch (e) {
      console.warn("Failed to query extension api token via native host:", e);
    }
    return "";
  })();

  mnsExtensionTokenInflight = acquisition;
  try {
    return await acquisition;
  } finally {
    if (mnsExtensionTokenInflight === acquisition) {
      mnsExtensionTokenInflight = null;
    }
  }
}

/**
 * POST a stock through the extension-authenticated endpoint.
 * A 401/403 can mean that the backend rotated its age-limited token after the
 * service worker cached the previous value. Refresh once, then return the
 * final response without retrying indefinitely.
 */
async function addStockViaExtension(base, symbol, market) {
  let forceRefresh = false;
  for (let attempt = 0; attempt < 2; attempt++) {
    const token = await getOrFetchExtensionToken(forceRefresh);
    const res = await fetchWithTimeout(
      `${base}/api/stocks/add_ext`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-MNS-Extension-Request": "true",
          Authorization: `Bearer ${token || ""}`,
        },
        body: JSON.stringify({ symbol, market }),
      },
      ADD_STOCK_TIMEOUT_MS,
    );
    const data = await res.json().catch(() => ({}));
    if ((res.status === 401 || res.status === 403) && !forceRefresh) {
      forceRefresh = true;
      continue;
    }
    return { res, data };
  }
  throw new Error("Extension stock request retry limit reached");
}

async function refreshBackendPort() {
  try {
    const response = await sendNativeMessage({ action: "get_backend_port" });
    if (response && response.ok) {
      setBackendPort(response.port);
    }
  } catch (e) {
    console.warn("Failed to query backend port:", e);
    BACKEND_URLS = buildBackendUrls(backendPort);
  }
  return backendPort;
}

async function checkHealth(retries = 1, timeoutMs = HEALTH_TIMEOUT_MS) {
  let lastError = null;
  const attempts = [];

  for (let attempt = 0; attempt <= retries; attempt++) {
    const port = await refreshBackendPort();
    const urls = buildBackendUrls(port);
    for (const base of urls) {
      try {
        const res = await fetchWithTimeout(
          `${base}/api/health`,
          { method: "GET", cache: "no-store" },
          timeoutMs,
        );
        if (!res.ok) {
          throw new Error(`HTTP status ${res.status}`);
        }
        const data = await res.json();
        if (
          data?.ok !== true ||
          data?.app !== "Mistral NeX Stocks" ||
          data?.ready !== true
        ) {
          throw new Error("Unexpected backend health response");
        }
        return { ok: true, base, data };
      } catch (e) {
        console.debug("checkHealth attempt failed:", e);
        lastError = e;
        attempts.push({ base, error: e.message || String(e) });
      }
    }
    if (attempt < retries) {
      // 指数バックオフ + ジッター (0.5~1.5倍の揺らぎ)
      const baseDelay = 1000 * Math.pow(2, attempt);
      const jitter = 0.5 + Math.random() * 1.0;
      const delay = Math.min(baseDelay * jitter, 15000);
      console.debug(
        `checkHealth retry ${attempt + 1}/${retries} in ${Math.round(delay)}ms`,
      );
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  return {
    ok: false,
    error: lastError
      ? lastError.message || String(lastError)
      : "No connection attempts succeeded",
    attempts,
  };
}

function detectBrowserName() {
  const ua = navigator.userAgent || "";
  if (ua.includes("Edg/")) return "Microsoft Edge";
  if (ua.includes("Chrome/")) return "Google Chrome";
  return "Chromium Browser";
}

async function sendNativeMessage(message, retries = 3) {
  const safeMessage = { ...(message || {}), extensionId: chrome.runtime.id };

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const response = await new Promise((resolve, reject) => {
        chrome.runtime.sendNativeMessage(HOST_NAME, safeMessage, (res) => {
          const err = chrome.runtime.lastError;
          if (err) {
            reject(
              new Error(
                err.message ||
                  "Error when communicating with the native messaging host",
              ),
            );
          } else {
            resolve(res || { ok: false, error: "No response" });
          }
        });
      });

      if (
        response &&
        response.ok === false &&
        response.error === "Rate limit exceeded"
      ) {
        if (attempt < retries) {
          const delay = 500 * Math.pow(1.5, attempt) + Math.random() * 200;
          console.warn(
            `Native host rate limited, retrying in ${Math.round(delay)}ms...`,
          );
          await new Promise((r) => setTimeout(r, delay));
          continue;
        }
      }
      return response;
    } catch (err) {
      if (attempt < retries) {
        const delay = 500 * Math.pow(1.5, attempt) + Math.random() * 200;
        console.warn(
          `Native messaging error, retrying in ${Math.round(delay)}ms...`,
        );
        await new Promise((r) => setTimeout(r, delay));
        continue;
      }
      console.error("Native messaging error:", err.message || err);
      throw err;
    }
  }
}

async function openRoute(route) {
  const health = await checkHealth();
  const base = health.ok ? health.base : BACKEND_URLS[0];
  const sanitizedRoute = sanitizeRoute(route);
  await chrome.tabs.create({ url: `${base}${sanitizedRoute}` });
  return { ok: true, base, route: sanitizedRoute };
}

const DEFAULT_BADGE_COLOR = "#4d8fff";
const DEFAULT_BADGE_DURATION = 2500;

function setBadgeMessage(
  text,
  color = DEFAULT_BADGE_COLOR,
  durationMs = DEFAULT_BADGE_DURATION,
) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  setTimeout(() => {
    chrome.action.setBadgeText({ text: "" });
  }, durationMs);
}

// ------------------------------------------------------------------
// Side Panel Support
// ------------------------------------------------------------------
if (chrome.sidePanel?.setPanelBehavior) {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((error) => console.error(error));
} else if (chrome.action?.onClicked && chrome.sidePanel?.open) {
  chrome.action.onClicked.addListener((tab) => {
    if (tab.windowId) {
      chrome.sidePanel.open({ windowId: tab.windowId }).catch(console.error);
    }
  });
}

// ------------------------------------------------------------------
// Context Menus
// ------------------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create(
      {
        id: "open-side-panel",
        title: "Mistral NeX: サイドパネルを開く",
        contexts: ["all"],
      },
      () => {
        if (chrome.runtime.lastError) {
          console.error(
            "Failed to create context menu open-side-panel:",
            chrome.runtime.lastError.message,
          );
        }
      },
    );
    chrome.contextMenus.create(
      {
        id: "add-us-stock",
        title: "Mistral NeX: 米国株に追加 '%s'",
        contexts: ["selection"],
      },
      () => {
        if (chrome.runtime.lastError) {
          console.error(
            "Failed to create context menu add-us-stock:",
            chrome.runtime.lastError.message,
          );
        }
      },
    );
    chrome.contextMenus.create(
      {
        id: "add-jp-stock",
        title: "Mistral NeX: 日本株に追加 '%s'",
        contexts: ["selection"],
      },
      () => {
        if (chrome.runtime.lastError) {
          console.error(
            "Failed to create context menu add-jp-stock:",
            chrome.runtime.lastError.message,
          );
        }
      },
    );
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "open-side-panel") {
    const isRestrictedUrl = (url) => {
      if (!url || typeof url !== "string") return false;
      const lower = url.toLowerCase();
      return (
        lower.startsWith("chrome://") ||
        lower.startsWith("edge://") ||
        lower.startsWith("about:") ||
        lower.startsWith("chrome-extension://") ||
        lower.startsWith("devtools://")
      );
    };

    if (tab && tab.url && isRestrictedUrl(tab.url)) {
      console.debug("open-side-panel skipped on restricted page:", tab.url);
      return;
    }

    if (tab && typeof tab.windowId === "number") {
      chrome.sidePanel
        .open({ windowId: tab.windowId })
        .catch((error) =>
          console.debug("Side panel open on window failed:", error),
        );
    } else {
      chrome.windows.getCurrent((win) => {
        if (win && typeof win.id === "number") {
          chrome.sidePanel
            .open({ windowId: win.id })
            .catch((error) =>
              console.debug("Side panel open on current window failed:", error),
            );
        }
      });
    }
    return;
  }
  const symbol = (info.selectionText || "").trim();
  if (!symbol) return;

  // Client-side validation before hitting the backend: stock symbols are
  // 1-15 chars of ASCII alphanumerics plus '.', '-', '^', '='.
  const SYMBOL_RE = /^[A-Za-z0-9.\-^=]{1,15}$/;
  if (!SYMBOL_RE.test(symbol)) {
    console.warn("Rejected invalid stock symbol from context menu");
    setBadgeMessage("NG", "#ff7d7d");
    return;
  }

  const market = info.menuItemId === "add-jp-stock" ? "jp" : "us";
  let health = await checkHealth();
  if (!health.ok) {
    console.info("Backend not running. Attempting auto-start...");
    try {
      await sendNativeMessage({ action: "start_backend" });
      await new Promise((r) => setTimeout(r, 2000));
      health = await checkHealth();
    } catch (e) {
      console.warn("Auto-start failed:", e);
    }
  }

  if (!health.ok) {
    console.error("Backend not running after auto-start. Cannot add stock.");
    setBadgeMessage("NG", "#ff7d7d");
    return;
  }

  try {
    const { res, data } = await addStockViaExtension(
      health.base,
      symbol,
      market,
    );
    if (!res.ok || data?.ok === false) {
      console.error(
        "Add stock API failed:",
        data?.error || `HTTP ${res.status}`,
      );
      setBadgeMessage("NG", "#ff7d7d");
      return;
    }
    setBadgeMessage("OK", "#7dffb0");
  } catch (e) {
    console.error("Add stock failed:", e);
    setBadgeMessage("NG", "#ff7d7d");
  }
});

// ------------------------------------------------------------------
// Badge Updates (Nikkei 225)
// ------------------------------------------------------------------
let isUpdatingBadge = false;

async function updateBadge() {
  if (isUpdatingBadge) {
    console.debug("updateBadge is already in progress, skipping...");
    return;
  }
  isUpdatingBadge = true;
  try {
    const health = await checkHealth();
    if (!health.ok) {
      chrome.action.setBadgeText({ text: "" });
      return;
    }

    try {
      const res = await fetchWithTimeout(
        `${health.base}/api/indices`,
        { method: "GET", cache: "no-store" },
        BADGE_FETCH_TIMEOUT_MS,
      );
      if (!res.ok) return;
      const data = await res.json();
      const n225 = data["N225"];
      if (n225 && n225.percent !== null && n225.percent !== undefined) {
        const pctValue = parseFloat(n225.percent);
        if (!Number.isNaN(pctValue)) {
          const text =
            (pctValue >= 0 ? "+" : "") + Math.round(pctValue).toString() + "%";
          const color = pctValue >= 0 ? "#7dffb0" : "#ff7d7d";

          chrome.action.setBadgeText({ text: text });
          chrome.action.setBadgeBackgroundColor({ color: color });
        } else {
          chrome.action.setBadgeText({ text: "" });
        }
      } else {
        chrome.action.setBadgeText({ text: "" });
      }
    } catch (e) {
      console.debug("Badge update failed:", e);
    }
  } finally {
    isUpdatingBadge = false;
  }
}

// MV3 Alarm to keep background active for polling
if (chrome.alarms) {
  chrome.alarms.get("badgeUpdate", (alarm) => {
    if (!alarm) {
      chrome.alarms.create("badgeUpdate", { periodInMinutes: 3 }); // 個人利用向けに最適化
    }
  });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "badgeUpdate") {
      updateBadge();
    }
  });
}

// Initial update - wrap in try-catch to prevent SW registration failure
try {
  updateBadge();
} catch (e) {
  console.error("Initial badge update failed:", e);
}

// ------------------------------------------------------------------
// Message Listeners
// ------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  let responded = false;
  const safeSendResponse = (data) => {
    if (!responded) {
      responded = true;
      try {
        sendResponse(data);
      } catch (e) {
        console.warn("sendResponse failed:", e);
      }
    }
  };

  if (sender?.id !== chrome.runtime.id) {
    safeSendResponse({ ok: false, error: "Unauthorized sender" });
    return true;
  }
  if (!message || !message.action) {
    safeSendResponse({ ok: false, error: "No action provided" });
    return true;
  }

  (async () => {
    try {
      if (message.action === "health") {
        const retries =
          typeof message.retries === "number" ? message.retries : 1;
        const timeoutMs =
          typeof message.timeoutMs === "number"
            ? message.timeoutMs
            : HEALTH_TIMEOUT_MS;
        return safeSendResponse(await checkHealth(retries, timeoutMs));
      }
      if (message.action === "getContext") {
        await refreshBackendPort();
        const health = await checkHealth();
        // Do not read either token here: the popup only needs diagnostics,
        // and both secrets are acquired lazily by the actions that use them.
        return safeSendResponse({
          ok: true,
          hostName: HOST_NAME,
          extensionId: chrome.runtime.id,
          browserName: detectBrowserName(),
          backendUrls: BACKEND_URLS,
          backendPort,
          health,
        });
      }
      if (message.action === "startBackend") {
        const res = await sendNativeMessage({
          action: "start_backend",
        });
        if (res && res.port) {
          setBackendPort(res.port);
        }
        // Token acquisition is demand-driven (addStockViaExtension /
        // stopBackend) so startup cannot exhaust the native host's shared
        // 3-requests-per-30-seconds secret budget.
        // Start badge update soon after backend starts
        setTimeout(updateBadge, 2000);
        return safeSendResponse(res);
      }
      if (message.action === "stopBackend") {
        const health = await checkHealth();
        if (!health.ok) {
          return safeSendResponse({
            ok: false,
            error: "バックエンドは既に停止しています(未接続)",
          });
        }

        if (!mnsShutdownToken) {
          try {
            const tokenRes = await sendNativeMessage({
              action: "get_shutdown_token",
            });
            if (tokenRes && tokenRes.ok) {
              setMnsShutdownToken(tokenRes.token);
            }
          } catch (e) {
            console.warn("Failed to query shutdown token before shutdown:", e);
          }
        }

        try {
          const res = await fetchWithTimeout(
            `${health.base}/api/shutdown`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-MNS-Shutdown-Token": mnsShutdownToken || "",
              },
              body: JSON.stringify({ confirm: true }),
              cache: "no-store",
            },
            SHUTDOWN_TIMEOUT_MS,
          );
          const data = await res.json().catch(() => ({}));
          if (!res.ok || data?.ok === false) {
            const rawError = String(data?.error || `HTTP ${res.status}`);
            if (
              rawError.includes("shutdown token is not configured") ||
              rawError.includes("invalid or missing shutdown token")
            ) {
              return safeSendResponse({
                ok: false,
                error:
                  "古いバックエンドが起動しているか、シャットダウントークンが無効です。バックエンドを再起動し、拡張機能を再読み込みしてください。",
              });
            }
            return safeSendResponse({ ok: false, error: rawError });
          }
          chrome.action.setBadgeText({ text: "" });
          setMnsShutdownToken(null);
          return safeSendResponse({ ok: true });
        } catch (_e) {
          // The fetch failed, which may occur if Flask closed the connection during
          // shutdown. Verify if the backend is down before treating as success.
          const checkAfterError = await checkHealth();
          if (!checkAfterError.ok) {
            chrome.action.setBadgeText({ text: "" });
            setMnsShutdownToken(null);
            return safeSendResponse({ ok: true, stopped: true });
          }
          return safeSendResponse({
            ok: false,
            error:
              _e?.message || "シャットダウンリクエストの送信に失敗しました",
          });
        }
      }
      if (message.action === "addDetectedStock") {
        const symbol = String(message.symbol || "").trim();
        const market = message.market === "jp" ? "jp" : "us";
        if (!symbol) {
          return safeSendResponse({
            ok: false,
            error: "銘柄シンボルが指定されていません",
          });
        }
        const SYMBOL_RE = /^[A-Za-z0-9.\-^=]{1,15}$/;
        if (!SYMBOL_RE.test(symbol)) {
          return safeSendResponse({
            ok: false,
            error: "銘柄シンボルの形式が不正です",
          });
        }
        const health = await checkHealth();
        if (!health.ok) {
          return safeSendResponse({
            ok: false,
            error: "バックエンドに接続できません",
          });
        }
        try {
          const { res, data } = await addStockViaExtension(
            health.base,
            symbol,
            market,
          );
          if (!res.ok || data?.ok === false) {
            return safeSendResponse({
              ok: false,
              error: data?.error || `HTTP ${res.status}`,
            });
          }
          return safeSendResponse({ ok: true });
        } catch (e) {
          return safeSendResponse({
            ok: false,
            error: e.message || String(e),
          });
        }
      }
      if (message.action === "openMain") {
        return safeSendResponse(await openRoute("/main"));
      }
      if (message.action === "openSetup") {
        return safeSendResponse(await openRoute("/setup"));
      }
      if (message.action === "openSettings") {
        return safeSendResponse(await openRoute("/settings"));
      }
      if (message.action === "openScreener") {
        return safeSendResponse(await openRoute("/screener"));
      }
      if (message.action === "openHeatmap") {
        return safeSendResponse(await openRoute("/heatmap"));
      }

      return safeSendResponse({ ok: false, error: "Unknown action" });
    } catch (e) {
      return safeSendResponse({ ok: false, error: e.message || String(e) });
    }
  })();

  return true;
});
