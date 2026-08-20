// --- Security Utilities ---
function setSafeText(element, text) {
  if (!element) return;
  element.textContent = String(text || "");
}

const $ = (id) => document.getElementById(id);
const healthPill = $("healthPill");
const healthMeta = $("healthMeta");
const browserPill = $("browserPill");
const diagBox = $("diagBox");

const DEFAULT_BACKEND_PORT = 5000;

function normalizeBackendPort(value) {
  const port = Number(value);
  if (Number.isInteger(port) && port > 0 && port <= 65535) {
    return port;
  }
  return DEFAULT_BACKEND_PORT;
}

// Fallback base used before/without a successful health check. background.js
// persists the resolved port, so a custom backend port (MNS_BACKEND_PORT)
// still works here even when health has not been confirmed yet.
function fallbackBackendBase() {
  return `http://127.0.0.1:${normalizeBackendPort(backendPort)}`;
}

let backendPort = DEFAULT_BACKEND_PORT;
let currentBackendBase = fallbackBackendBase();

chrome.storage.local.get(["backendPort"], (items) => {
  if (items.backendPort) {
    backendPort = normalizeBackendPort(items.backendPort);
    currentBackendBase = fallbackBackendBase();
  }
});

async function send(action, payload) {
  const message = { action, ...(payload || {}) };
  return chrome.runtime.sendMessage(message);
}

let stockPollInterval = null;
let stockPollActive = false;
let stockPollGeneration = 0;
let allStocksData = null;

// Tab Switching
function initTabSwitching() {
  const buttons = Array.from(document.querySelectorAll(".tab-btn"));
  const selectTab = (btn, moveFocus = false) => {
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
      b.tabIndex = -1;
    });
    document.querySelectorAll(".tab-content").forEach((c) => {
      c.classList.add("hidden");
      c.hidden = true;
      c.setAttribute("aria-hidden", "true");
    });

    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    btn.tabIndex = 0;
    if (moveFocus) btn.focus();
    const targetTab = btn.dataset.tab;
    const contentEl = $(`tab-content-${targetTab}`);
    if (contentEl) {
      contentEl.classList.remove("hidden");
      contentEl.hidden = false;
      contentEl.setAttribute("aria-hidden", "false");
    }
    if (targetTab === "detector") {
      loadDetectedTickers().catch((e) =>
        console.error("Detector tab load error:", e),
      );
    }
  };
  buttons.forEach((btn) => {
    btn.tabIndex = btn.getAttribute("aria-selected") === "true" ? 0 : -1;
    btn.addEventListener("click", () => selectTab(btn));
    btn.addEventListener("keydown", (event) => {
      const index = buttons.indexOf(btn);
      let nextIndex = index;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft")
        nextIndex = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = buttons.length - 1;
      else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectTab(btn);
        return;
      } else return;
      event.preventDefault();
      selectTab(buttons[nextIndex], true);
    });
  });
}

function renderStockItem(symbol, name, price, changePercent, market = "") {
  const container = document.createElement("div");
  container.className = "stock-item";
  container.setAttribute("data-symbol", symbol);
  container.setAttribute("role", "button");
  container.setAttribute("tabindex", "0");
  container.setAttribute(
    "aria-label",
    `${symbol}${name ? ` ${name}` : ""} の詳細を開く`,
  );

  let changeClass = "neutral";
  let changeSign = "";
  const val = parseFloat(changePercent);
  if (!isNaN(val)) {
    if (val > 0) {
      changeClass = "plus";
      changeSign = "+";
    } else if (val < 0) {
      changeClass = "minus";
    }
  }
  const pctStr = Number.isFinite(val)
    ? `${changeSign}${val.toFixed(2)}%`
    : "--%";
  const priceStr =
    price !== null && price !== undefined && price !== "--"
      ? typeof price === "number"
        ? price.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })
        : price
      : "--";

  const infoDiv = document.createElement("div");
  infoDiv.className = "stock-info";

  const symbolSpan = document.createElement("span");
  symbolSpan.className = "stock-symbol";
  symbolSpan.textContent = symbol;

  const nameSpan = document.createElement("span");
  nameSpan.className = "stock-name";
  nameSpan.textContent = name || "";

  infoDiv.appendChild(symbolSpan);
  infoDiv.appendChild(nameSpan);

  const valuesDiv = document.createElement("div");
  valuesDiv.className = "stock-values";

  const priceSpan = document.createElement("span");
  priceSpan.className = "stock-price";
  priceSpan.textContent = priceStr;

  const changeSpan = document.createElement("span");
  changeSpan.className = `stock-change ${changeClass}`;
  changeSpan.textContent = pctStr;

  valuesDiv.appendChild(priceSpan);
  valuesDiv.appendChild(changeSpan);

  container.appendChild(infoDiv);
  container.appendChild(valuesDiv);

  // Click on stock item -> open main app
  const openStock = () => {
    const base = currentBackendBase || fallbackBackendBase();
    const marketQuery = market ? `&market=${encodeURIComponent(market)}` : "";
    const url = `${base}/main?q=${encodeURIComponent(symbol)}${marketQuery}`;
    chrome.tabs.create({ url });
  };
  container.addEventListener("click", openStock);
  container.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openStock();
    }
  });

  return container;
}

function filterAndRenderStocks() {
  if (!allStocksData) return;
  const filterText = ($("extStockFilter")?.value || "").toLowerCase().trim();
  const container = $("stockListContainer");
  if (!container) return;

  const fragment = document.createDocumentFragment();

  // Render Indices
  if (allStocksData.indices && Object.keys(allStocksData.indices).length > 0) {
    const indicesMapping = {
      N225: { name: "日経平均", symbol: "^N225" },
      DJI: { name: "NYダウ", symbol: "^DJI" },
      USDJPY: { name: "米ドル/円", symbol: "USDJPY=X" },
      SP500: { name: "S&P 500", symbol: "^GSPC" },
      NASDAQ: { name: "NASDAQ", symbol: "^IXIC" },
    };

    const matchingIndices = [];
    for (const key of ["N225", "DJI", "USDJPY", "SP500", "NASDAQ"]) {
      const item = allStocksData.indices[key];
      if (item) {
        const mapped = indicesMapping[key] || { name: key, symbol: key };
        const name = mapped.name;
        if (
          !filterText ||
          key.toLowerCase().includes(filterText) ||
          name.toLowerCase().includes(filterText)
        ) {
          matchingIndices.push({ key, name, symbol: mapped.symbol, item });
        }
      }
    }

    if (matchingIndices.length > 0) {
      const title = document.createElement("div");
      title.className = "section-title";
      title.textContent = "主要指数";
      fragment.appendChild(title);

      for (const { name, symbol, item } of matchingIndices) {
        const pct = item.percent ?? item.change_percent;
        fragment.appendChild(
          renderStockItem(symbol, name, item.price, pct, "idx"),
        );
      }
    }
  }

  // Render Stocks
  const usStocks = allStocksData.stocks?.us || [];
  const jpStocks = allStocksData.stocks?.jp || [];
  const idxStocks = allStocksData.stocks?.idx || [];
  const allList = [
    ...usStocks.map((stock) => ({ ...stock, market: "us" })),
    ...jpStocks.map((stock) => ({ ...stock, market: "jp" })),
    ...idxStocks.map((stock) => ({ ...stock, market: "idx" })),
  ];

  const matchingStocks = allList.filter((s) => {
    if (!filterText) return true;
    const sym = String(s.symbol || "").toLowerCase();
    const nm = String(s.name || "").toLowerCase();
    return sym.includes(filterText) || nm.includes(filterText);
  });

  if (matchingStocks.length > 0) {
    const title = document.createElement("div");
    title.className = "section-title";
    title.textContent = "登録銘柄";
    fragment.appendChild(title);

    for (const s of matchingStocks) {
      const pct = s.change_percent ?? s.percent;
      fragment.appendChild(
        renderStockItem(s.symbol, s.name, s.price, pct, s.market),
      );
    }
  }

  container.textContent = "";
  if (fragment.childNodes.length > 0) {
    container.appendChild(fragment);
  } else {
    const emptyDiv = document.createElement("div");
    emptyDiv.className = "meta text-center p-14";
    emptyDiv.textContent = filterText
      ? "該当する銘柄が見つかりません"
      : "表示可能なデータがありません";
    container.appendChild(emptyDiv);
  }
}

async function fetchAndRenderStocks(base) {
  try {
    const res = await fetch(`${base}/api/stocks`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    allStocksData = data;
    const stockContainer = $("stockListContainer");
    stockContainer?.classList.remove("stale");

    filterAndRenderStocks();

    const now = new Date();
    const timeStr = now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    setSafeText($("stockRefreshTime"), timeStr);
  } catch (err) {
    const stockContainer = $("stockListContainer");
    if (stockContainer) {
      stockContainer.classList.add("stale");
      stockContainer.setAttribute(
        "aria-label",
        "更新に失敗しました。表示中の株価は古い可能性があります。",
      );
    }
    setSafeText(
      $("stockRefreshTime"),
      "更新失敗 — 表示中のデータは古い可能性があります",
    );
    if (stockPollActive) {
      if (
        err instanceof TypeError ||
        err?.name === "TypeError" ||
        String(err?.message || "").includes("Failed to fetch")
      ) {
        console.warn(
          "Backend unavailable during stock fetch:",
          err.message || err,
        );
      } else {
        console.error("Failed to fetch/render stocks:", err);
      }
    }
  }
}

function startStockPolling(base) {
  if (stockPollInterval) clearTimeout(stockPollInterval);
  stockPollActive = true;
  const generation = ++stockPollGeneration;

  async function poll() {
    await fetchAndRenderStocks(base);
    if (stockPollActive && generation === stockPollGeneration) {
      stockPollInterval = setTimeout(poll, 5000);
    }
  }

  poll();
}

function stopStockPolling() {
  stockPollActive = false;
  stockPollGeneration += 1;
  if (stockPollInterval) {
    clearTimeout(stockPollInterval);
    stockPollInterval = null;
  }
}

function setHealth(health) {
  if (health?.ok) {
    currentBackendBase = health.base || fallbackBackendBase();
    setSafeText(healthPill, "起動済み");
    healthPill.className = "pill ok";
    setSafeText(
      healthMeta,
      `${health.base} / model=${health.data?.model || "-"}`,
    );
    $("startBtn").classList.add("hidden");
    $("stopBtn").classList.remove("hidden");
    startStockPolling(health.base);
  } else {
    setSafeText(healthPill, "未起動");
    healthPill.className = "pill ng";
    setSafeText(healthMeta, "バックエンドに接続できません");
    $("startBtn").classList.remove("hidden");
    $("stopBtn").classList.add("hidden");
    stopStockPolling();
  }
}

function maskExtensionId(extensionId) {
  if (!extensionId) return "";
  const text = String(extensionId);
  if (text.length <= 8) return "*".repeat(text.length);
  return `${text.slice(0, 4)}...${text.slice(-4)}`;
}

function buildDiagnostics(ctx) {
  return [
    `browser      : ${ctx.browserName}`,
    `extensionId  : ${maskExtensionId(ctx.extensionId)}`,
    `hostName     : ${ctx.hostName}`,
    `backendUrls  : ${ctx.backendUrls.join(", ")}`,
    `backendAlive : ${ctx.health?.ok ? "yes" : "no"}`,
    ctx.health?.ok ? `backendBase  : ${ctx.health.base}` : "",
    ctx.health?.ok ? `model        : ${ctx.health.data?.model || ""}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

async function refresh() {
  const ctx = await send("getContext");
  if (!ctx?.ok) throw new Error(ctx?.error || "状態取得に失敗しました");
  setSafeText(browserPill, ctx.browserName);
  setHealth(ctx.health);
  setSafeText(diagBox, buildDiagnostics(ctx));
  return ctx;
}

async function withBusy(btn, fn) {
  const prev = btn.textContent;
  btn.disabled = true;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function handleUIError(error) {
  const message =
    error && error.message ? error.message : String(error || "不明なエラー");
  healthPill.textContent = "エラー";
  healthPill.className = "pill ng";
  healthMeta.textContent = message;
}

function bindAsyncButton(id, handler) {
  const el = $(id);
  if (!el) return;
  el.addEventListener("click", () => {
    Promise.resolve(handler()).catch((e) => {
      console.error(e);
      handleUIError(e);
    });
  });
}

async function openAppPage(path = "/") {
  const ctx = await send("getContext");
  const base = ctx?.health?.ok ? ctx.health.base : fallbackBackendBase();
  chrome.tabs.create({ url: `${base}${path}` });
}

async function waitForBackendReady(maxWaitMs = 20000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    const health = await send("health");
    if (health?.ok) {
      return await send("getContext");
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("バックエンド起動の待機がタイムアウトしました");
}

document.addEventListener("DOMContentLoaded", () => {
  initTabSwitching();

  $("extStockFilter")?.addEventListener("input", filterAndRenderStocks);

  bindAsyncButton("refreshBtn", () => withBusy($("refreshBtn"), refresh));

  bindAsyncButton("startBtn", () =>
    withBusy($("startBtn"), async () => {
      const res = await send("startBackend");
      if (!res?.ok) throw new Error(res?.error || "起動に失敗しました");
      await waitForBackendReady();
      await refresh();
    }),
  );

  bindAsyncButton("stopBtn", () =>
    withBusy($("stopBtn"), async () => {
      stopStockPolling();
      const res = await send("stopBackend");
      if (!res?.ok) throw new Error(res?.error || "停止に失敗しました");
      await new Promise((r) => setTimeout(r, 1000));
      await refresh();
    }),
  );

  $("openMainBtn")?.addEventListener("click", () => openAppPage("/main"));
  $("openScreenerBtn")?.addEventListener("click", () =>
    openAppPage("/screener"),
  );
  $("openSetupBtn")?.addEventListener("click", () => openAppPage("/setup"));
  $("openSettingsBtn")?.addEventListener("click", () =>
    openAppPage("/settings"),
  );

  $("rescanDetectorBtn")?.addEventListener("click", () => {
    loadDetectedTickers().catch((e) => console.error("Rescan failed:", e));
  });

  $("copyDiagBtn")?.addEventListener("click", async () => {
    const text = $("diagBox")?.textContent || "";
    if (text) {
      await navigator.clipboard.writeText(text);
      const btn = $("copyDiagBtn");
      const old = btn.textContent;
      btn.textContent = "コピー完了!";
      setTimeout(() => (btn.textContent = old), 1500);
    }
  });

  refresh().catch((err) => {
    console.error("Initial refresh failed:", err);
    handleUIError(err);
  });
});

/**
 * Helper to determine if a URL can have content scripts injected.
 * Chrome restricts content script injection on extension internal pages, browser settings, webstore, etc.
 */
function isInjectableUrl(url) {
  if (!url || typeof url !== "string") return true;
  const lower = url.toLowerCase();
  if (
    lower.startsWith("chrome://") ||
    lower.startsWith("chrome-extension://") ||
    lower.startsWith("edge://") ||
    lower.startsWith("about:") ||
    lower.startsWith("view-source:") ||
    lower.startsWith("devtools://") ||
    lower.startsWith("data:") ||
    lower.startsWith("javascript:") ||
    lower.includes("chrome.google.com/webstore") ||
    lower.includes("chromewebstore.google.com")
  ) {
    return false;
  }
  return true;
}

// Ticker Auto-Detection logic for Active Web Page
async function loadDetectedTickers() {
  const container = $("detectedListContainer");
  const titleEl = $("detectorPageTitle");
  if (!container) return;

  container.textContent = "";
  const loadingDiv = document.createElement("div");
  loadingDiv.className = "detector-loading";
  loadingDiv.textContent = "ページ上のティッカーを検出中...";
  container.appendChild(loadingDiv);
  if (titleEl) setSafeText(titleEl, "アクティブページを解析中...");

  try {
    const [tab] = await chrome.tabs.query({
      active: true,
      currentWindow: true,
    });
    if (!tab || !tab.id) {
      container.textContent = "";
      const emptyDiv = document.createElement("div");
      emptyDiv.className = "detector-empty";
      emptyDiv.textContent = "アクティブなタブが見つかりません。";
      container.appendChild(emptyDiv);
      return;
    }

    const tabUrl = tab.url || tab.pendingUrl;
    if (titleEl)
      setSafeText(titleEl, tab.title || tabUrl || "アクティブページ");

    // Check if the current tab URL is eligible for script injection/messaging
    if (tabUrl && !isInjectableUrl(tabUrl)) {
      container.textContent = "";
      const emptyDiv = document.createElement("div");
      emptyDiv.className = "detector-empty";
      emptyDiv.textContent =
        "このページ（ブラウザ特殊ページ・拡張機能ページ等）ではティッカー検出がサポートされていません。";
      container.appendChild(emptyDiv);
      return;
    }

    let response;
    try {
      response = await chrome.tabs.sendMessage(tab.id, {
        action: "detectTickers",
      });
    } catch (_err) {
      // Content script may not be injected yet (e.g. page loaded before extension installed/updated)
      if (chrome.scripting) {
        try {
          await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            files: ["content.js"],
          });
          response = await chrome.tabs.sendMessage(tab.id, {
            action: "detectTickers",
          });
        } catch (e2) {
          console.debug("Script injection skipped or failed:", e2);
        }
      }
    }

    if (
      !response ||
      !response.ok ||
      !Array.isArray(response.tickers) ||
      response.tickers.length === 0
    ) {
      container.textContent = "";
      const noTickersDiv = document.createElement("div");
      noTickersDiv.className = "detector-empty";
      noTickersDiv.textContent =
        "このWebページ上に検出可能な銘柄ティッカーは見つかりませんでした。";
      container.appendChild(noTickersDiv);
      return;
    }

    container.textContent = "";
    const list = response.tickers;

    for (const item of list) {
      const card = document.createElement("div");
      card.className = "detected-card";

      const header = document.createElement("div");
      header.className = "detected-card-header";

      const symBox = document.createElement("div");
      symBox.className = "detected-sym-box";

      const symbolSpan = document.createElement("span");
      symbolSpan.className = "detected-symbol";
      symbolSpan.textContent = item.symbol;

      const mktSpan = document.createElement("span");
      mktSpan.className = `detected-mkt-badge ${item.market}`;
      mktSpan.textContent = item.market === "jp" ? "JP" : "US";

      const countSpan = document.createElement("span");
      countSpan.className = "detected-count-badge";
      countSpan.textContent = `${item.count}件検出`;

      symBox.appendChild(symbolSpan);
      symBox.appendChild(mktSpan);
      symBox.appendChild(countSpan);

      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "mini-add-btn";
      addBtn.textContent = "➕ 追加";
      addBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        addBtn.disabled = true;
        addBtn.textContent = "追加中...";
        try {
          const response = await send("addDetectedStock", {
            symbol: item.symbol,
            market: item.market,
          });
          if (response?.ok) {
            addBtn.textContent = "✓ 追加済";
            addBtn.className = "mini-add-btn success";
          } else {
            addBtn.textContent = "失敗";
            addBtn.disabled = false;
          }
        } catch (_e) {
          addBtn.textContent = "エラー";
          addBtn.disabled = false;
        }
      });

      header.appendChild(symBox);
      header.appendChild(addBtn);
      card.appendChild(header);

      if (item.snippet) {
        const snippetEl = document.createElement("div");
        snippetEl.className = "detected-snippet";
        snippetEl.textContent = `"... ${item.snippet} ..."`;
        card.appendChild(snippetEl);
      }

      container.appendChild(card);
    }
  } catch (err) {
    console.error("loadDetectedTickers error:", err);
    const errorDiv = document.createElement("div");
    errorDiv.className = "detector-empty";
    errorDiv.textContent = `検出エラー: ${err.message || String(err)}`;
    container.textContent = "";
    container.appendChild(errorDiv);
  }
}
