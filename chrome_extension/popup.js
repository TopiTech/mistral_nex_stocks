// --- Security Utilities ---
/**
 * 安全なテキストコンテンツを設定
 * textContentは自動的にHTMLエスケープされるため、
 * 追加のサニタイズは不要
 * @param {HTMLElement} element - 対象の要素
 * @param {string} text - 設定するテキスト
 */
function setSafeText(element, text) {
  if (!element) return;
  element.textContent = String(text || "");
}

const $ = (id) => document.getElementById(id);
const healthPill = $("healthPill");
const healthMeta = $("healthMeta");
const browserPill = $("browserPill");
const diagBox = $("diagBox");

async function send(action) {
  return chrome.runtime.sendMessage({ action });
}

let stockPollInterval = null;

function renderStockItem(symbol, name, price, changePercent) {
  const container = document.createElement("div");
  container.className = "stock-item";

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
  const pctStr =
    changePercent !== null && changePercent !== undefined
      ? `${changeSign}${val.toFixed(2)}%`
      : "--%";
  const priceStr =
    price !== null && price !== undefined && price !== "--"
      ? typeof price === "number"
        ? price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
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

  return container;
}

async function fetchAndRenderStocks(base) {
  try {
    const res = await fetch(`${base}/api/stocks`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const container = $("stockListContainer");
    const card = $("stockPricesCard");
    if (!container || !card) return;

    const fragment = document.createDocumentFragment();

    // Render Indices
    if (data.indices && Object.keys(data.indices).length > 0) {
      const title = document.createElement("div");
      title.className = "section-title";
      title.textContent = "主要指数";
      fragment.appendChild(title);

      const indicesMapping = {
        N225: "日経平均",
        DJI: "NYダウ",
        USDJPY: "米ドル/円",
        SP500: "S&P 500",
        NASDAQ: "NASDAQ",
      };
      for (const key of ["N225", "DJI", "USDJPY", "SP500", "NASDAQ"]) {
        const item = data.indices[key];
        if (item) {
          const name = indicesMapping[key] || key;
          const pct = item.percent || item.change_percent;
          fragment.appendChild(renderStockItem(key, name, item.price, pct));
        }
      }
    }

    // Render Stocks
    const usStocks = data.stocks?.us || [];
    const jpStocks = data.stocks?.jp || [];
    if (usStocks.length > 0 || jpStocks.length > 0) {
      const title = document.createElement("div");
      title.className = "section-title";
      title.textContent = "登録銘柄";
      fragment.appendChild(title);

      for (const s of [...usStocks, ...jpStocks]) {
        const pct = s.change_percent || s.percent;
        fragment.appendChild(renderStockItem(s.symbol, s.name, s.price, pct));
      }
    }

    container.textContent = ""; // Clear existing
    if (fragment.childNodes.length > 0) {
      container.appendChild(fragment);
    } else {
      const emptyDiv = document.createElement("div");
      emptyDiv.className = "meta";
      emptyDiv.style.textAlign = "center";
      emptyDiv.style.padding = "10px";
      emptyDiv.textContent = "表示可能なデータがありません";
      container.appendChild(emptyDiv);
    }

    const now = new Date();
    const timeStr = now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    setSafeText($("stockRefreshTime"), timeStr);

    card.style.display = "block";
  } catch (err) {
    if (stockPollInterval) {
      console.error("Failed to fetch/render stocks:", err);
    }
  }
}

function startStockPolling(base) {
  if (stockPollInterval) clearTimeout(stockPollInterval);

  async function poll() {
    await fetchAndRenderStocks(base);
    if (stockPollInterval !== null) {
      stockPollInterval = setTimeout(poll, 5000);
    }
  }

  stockPollInterval = "active"; // 追跡用フラグ
  poll();
}

function stopStockPolling() {
  if (stockPollInterval) {
    clearTimeout(stockPollInterval);
    stockPollInterval = null;
  }
  const card = $("stockPricesCard");
  if (card) card.style.display = "none";
}

function setHealth(health) {
  if (health?.ok) {
    setSafeText(healthPill, "起動済み");
    healthPill.className = "pill ok";
    setSafeText(
      healthMeta,
      `${health.base} / model=${health.data?.model || "-"} / badge=${health.data?.badge || "-"}`,
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
    ctx.health?.ok ? `badge        : ${ctx.health.data?.badge || ""}` : "",
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
  const message = error && error.message ? error.message : String(error || "不明なエラー");
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

async function waitForBackendReady(maxWaitMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    const ctx = await send("getContext");
    if (ctx?.health?.ok) {
      return ctx;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("バックエンド起動の待機がタイムアウトしました");
}

bindAsyncButton("refreshBtn", () => withBusy($("refreshBtn"), refresh));
async function waitForBackendStopped(maxWaitMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    const ctx = await send("getContext");
    if (!ctx?.health?.ok) {
      return ctx;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("バックエンド停止の待機がタイムアウトしました");
}

bindAsyncButton("startBtn", () =>
  withBusy($("startBtn"), async () => {
    $("startBtn").textContent = "起動中...";
    const res = await send("startBackend");
    if (!res?.ok) throw new Error(res?.error || "起動に失敗しました");
    await waitForBackendReady();
    await refresh();
  }),
);
bindAsyncButton("stopBtn", () =>
  withBusy($("stopBtn"), async () => {
    if (!confirm("バックエンドを停止しますか？")) return;
    stopStockPolling();
    $("stopBtn").textContent = "停止中...";
    const res = await send("stopBackend");
    if (!res?.ok) throw new Error(res?.error || "停止に失敗しました");
    await waitForBackendStopped();
    await refresh();
  }),
);
bindAsyncButton("openMainBtn", async () => {
  const res = await send("openMain");
  if (!res?.ok) throw new Error(res?.error || "メイン画面を開けませんでした");
});
bindAsyncButton("openSetupBtn", async () => {
  const res = await send("openSetup");
  if (!res?.ok) throw new Error(res?.error || "API設定画面を開けませんでした");
});
bindAsyncButton("openSettingsBtn", async () => {
  const res = await send("openSettings");
  if (!res?.ok) throw new Error(res?.error || "設定画面を開けませんでした");
});
bindAsyncButton("copyDiagBtn", async () => {
  try {
    await navigator.clipboard.writeText(diagBox.textContent || "no diagnostics");
    $("copyDiagBtn").textContent = "コピー済み";
    setTimeout(() => {
      $("copyDiagBtn").textContent = "診断情報をコピー";
    }, 1200);
  } catch {
    $("copyDiagBtn").textContent = "コピー失敗";
    setTimeout(() => {
      $("copyDiagBtn").textContent = "診断情報をコピー";
    }, 1200);
  }
});

refresh().catch((e) => {
  handleUIError(e);
  diagBox.textContent = e.stack || e.message;
});
