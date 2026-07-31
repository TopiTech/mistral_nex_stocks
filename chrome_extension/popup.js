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

let currentBackendBase = "http://127.0.0.1:5000";

async function send(action) {
  return chrome.runtime.sendMessage({ action });
}

let stockPollInterval = null;
let stockPollActive = false;
let allStocksData = null;

// Tab Switching
function initTabSwitching() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      document.querySelectorAll(".tab-content").forEach((c) => c.classList.add("hidden"));

      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
      const targetTab = btn.dataset.tab;
      const contentEl = $(`tab-content-${targetTab}`);
      if (contentEl) contentEl.classList.remove("hidden");
    });
  });
}

function renderStockItem(symbol, name, price, changePercent) {
  const container = document.createElement("div");
  container.className = "stock-item";
  container.setAttribute("data-symbol", symbol);

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
  container.addEventListener("click", () => {
    const url = currentBackendBase || "http://127.0.0.1:5000/";
    chrome.tabs.create({ url });
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
      N225: "日経平均",
      DJI: "NYダウ",
      USDJPY: "米ドル/円",
      SP500: "S&P 500",
      NASDAQ: "NASDAQ",
    };

    const matchingIndices = [];
    for (const key of ["N225", "DJI", "USDJPY", "SP500", "NASDAQ"]) {
      const item = allStocksData.indices[key];
      if (item) {
        const name = indicesMapping[key] || key;
        if (!filterText || key.toLowerCase().includes(filterText) || name.toLowerCase().includes(filterText)) {
          matchingIndices.push({ key, name, item });
        }
      }
    }

    if (matchingIndices.length > 0) {
      const title = document.createElement("div");
      title.className = "section-title";
      title.textContent = "主要指数";
      fragment.appendChild(title);

      for (const { key, name, item } of matchingIndices) {
        const pct = item.percent ?? item.change_percent;
        fragment.appendChild(renderStockItem(key, name, item.price, pct));
      }
    }
  }

  // Render Stocks
  const usStocks = allStocksData.stocks?.us || [];
  const jpStocks = allStocksData.stocks?.jp || [];
  const allList = [...usStocks, ...jpStocks];

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
      fragment.appendChild(renderStockItem(s.symbol, s.name, s.price, pct));
    }
  }

  container.textContent = "";
  if (fragment.childNodes.length > 0) {
    container.appendChild(fragment);
  } else {
    const emptyDiv = document.createElement("div");
    emptyDiv.className = "meta";
    emptyDiv.style.textAlign = "center";
    emptyDiv.style.padding = "14px";
    emptyDiv.textContent = filterText ? "該当する銘柄が見つかりません" : "表示可能なデータがありません";
    container.appendChild(emptyDiv);
  }
}

async function fetchAndRenderStocks(base) {
  try {
    const res = await fetch(`${base}/api/stocks`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    allStocksData = data;

    filterAndRenderStocks();

    const now = new Date();
    const timeStr = now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    setSafeText($("stockRefreshTime"), timeStr);
  } catch (err) {
    if (stockPollActive) {
      console.error("Failed to fetch/render stocks:", err);
    }
  }
}

function startStockPolling(base) {
  if (stockPollInterval) clearTimeout(stockPollInterval);
  stockPollActive = true;

  async function poll() {
    await fetchAndRenderStocks(base);
    if (stockPollActive) {
      stockPollInterval = setTimeout(poll, 5000);
    }
  }

  poll();
}

function stopStockPolling() {
  stockPollActive = false;
  if (stockPollInterval) {
    clearTimeout(stockPollInterval);
    stockPollInterval = null;
  }
}

function setHealth(health) {
  if (health?.ok) {
    currentBackendBase = health.base || "http://127.0.0.1:5000";
    setSafeText(healthPill, "起動済み");
    healthPill.className = "pill ok";
    setSafeText(
      healthMeta,
      `${health.base} / model=${health.data?.model || "-"}`,
    );
    $("startBtn").style.display = "none";
    $("stopBtn").style.display = "block";
    startStockPolling(health.base);
  } else {
    setSafeText(healthPill, "未起動");
    healthPill.className = "pill ng";
    setSafeText(healthMeta, "バックエンドに接続できません");
    $("startBtn").style.display = "block";
    $("stopBtn").style.display = "none";
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
  const base = ctx?.health?.ok ? ctx.health.base : "http://127.0.0.1:5000";
  chrome.tabs.create({ url: `${base}${path}` });
}

document.addEventListener("DOMContentLoaded", () => {
  initTabSwitching();

  $("extStockFilter")?.addEventListener("input", filterAndRenderStocks);

  bindAsyncButton("refreshBtn", () => withBusy($("refreshBtn"), refresh));

  bindAsyncButton("startBtn", () =>
    withBusy($("startBtn"), async () => {
      await send("startBackend");
      await new Promise((r) => setTimeout(r, 1000));
      await refresh();
    }),
  );

  bindAsyncButton("stopBtn", () =>
    withBusy($("stopBtn"), async () => {
      await send("stopBackend");
      await new Promise((r) => setTimeout(r, 1000));
      await refresh();
    }),
  );

  $("openMainBtn")?.addEventListener("click", () => openAppPage("/"));
  $("openSetupBtn")?.addEventListener("click", () => openAppPage("/setup"));
  $("openSettingsBtn")?.addEventListener("click", () => openAppPage("/settings"));

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
