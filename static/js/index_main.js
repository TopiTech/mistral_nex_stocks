// #region Initialization — Event Registration Helpers

/** Initialize search button and keyboard events */
function initSearchEvents() {
  const searchBtn = document.getElementById("searchBtn");
  const searchInput = document.getElementById("searchInput");

  if (searchBtn) {
    searchBtn.addEventListener("click", (e) => {
      e.preventDefault();
      searchStocks();
    });
  }
  if (searchInput) {
    searchInput.addEventListener("keypress", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        searchStocks();
      }
    });
  }
}

/** Initialize tab switching events */
function initTabEvents() {
  document
    .getElementById("tab-us")
    ?.addEventListener("click", () => setActiveTab("us"));
  document
    .getElementById("tab-jp")
    ?.addEventListener("click", () => setActiveTab("jp"));
  document
    .getElementById("tab-idx")
    ?.addEventListener("click", () => setActiveTab("idx"));
  document
    .getElementById("tab-portfolio")
    ?.addEventListener("click", () => setActiveTab("portfolio"));
}

/** Initialize streaming toggle button events */
function initStreamToggleEvents() {
  const streamToggleBtn = DOM.get("streamToggleBtn");
  if (!streamToggleBtn) return;

  const updateBtnUI = () => {
    const isAct = state.isStreaming;
    streamToggleBtn.classList.toggle("active", isAct);
    const textEl = streamToggleBtn.querySelector(".stream-text");
    if (textEl) {
      textEl.textContent = isAct
        ? "Live Streaming"
        : "Streaming Paused (60s polling)";
    }
  };
  updateBtnUI();

  streamToggleBtn.addEventListener("click", handleStreamToggle);
}

/** Handle stream toggle button click */
function handleStreamToggle() {
  state.isStreaming = !state.isStreaming;
  localStorage.setItem("isStreamingEnabled", state.isStreaming);

  const streamToggleBtn = DOM.get("streamToggleBtn");
  if (streamToggleBtn) {
    const isAct = state.isStreaming;
    streamToggleBtn.classList.toggle("active", isAct);
    const textEl = streamToggleBtn.querySelector(".stream-text");
    if (textEl) {
      textEl.textContent = isAct
        ? "Live Streaming"
        : "Streaming Paused (60s polling)";
    }
  }

  if (state.isStreaming) {
    // H-3: Stop fallback polling before connecting SSE to prevent race condition.
    // When re-enabling streaming, any active fallback polling must be stopped
    // so that SSE and polling don't run concurrently.
    stopSseFallbackPolling();
    pollingManager.clearInterval("fallback-polling");
    showToast("✅ リアルタイム配信を開始します", "#7dffb0");
    connectSSE();
  } else {
    if (sseApiClient.currentEventSource) {
      sseApiClient.closeSSE();
      sseState.stockEventSource = null;
    }
    if (sseState.reconnectTimer) {
      clearTimeout(sseState.reconnectTimer);
      sseState.reconnectTimer = null;
    }
    stopSseFallbackPolling();
    setStreamingIndicatorText("Streaming Paused (60s polling)");
    showToast("⏸️ リアルタイム配信を停止しました", "#ffcc66");
    pollingManager.setInterval("fallback-polling", fetchInitialStocks, 60000);
  }
}

/** Initialize news refresh button */
function initNewsEvents() {
  document
    .getElementById("newsRefreshBtn")
    ?.addEventListener("click", forceRefreshNews);
}

/** Initialize sync stocks button */
function initSyncEvents() {
  document
    .getElementById("syncStocksBtn")
    ?.addEventListener("click", handleSyncClick);
}

/** Handle sync stocks button click */
async function handleSyncClick() {
  const btn = document.getElementById("syncStocksBtn");
  if (btn) btn.disabled = true;
  showToast("🔄 株価を今すぐ同期しています...", "#6bb6ff");
  try {
    await fetchInitialStocks(true);
    showToast("✅ 株価の同期が完了しました", "#7dffb0");
  } catch (e) {
    showToast("❌ 同期エラーが発生しました", "#ff7d7d");
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** Initialize bulk analyze button */
function initBulkAnalyzeEvents() {
  document
    .getElementById("bulkAnalyzeFavoritesBtn")
    ?.addEventListener("click", bulkAnalyzeFavorites);
}

/** Initialize visibility change handler */
function initVisibilityHandler() {
  document.addEventListener("visibilitychange", () => {
    const activeSource =
      sseState.stockEventSource || sseApiClient.currentEventSource;
    if (!document.hidden) {
      fetchInitialStocks();
      if (!activeSource || activeSource.readyState === EventSource.CLOSED) {
        connectSSE();
      }
    }
  });
}

/** Handle ?q= URL parameter from heatmap page */
function handleUrlSearchParam() {
  const urlParams = new URLSearchParams(window.location.search);
  const qParam = urlParams.get("q");
  if (!qParam) return;
  const searchInput = DOM.get("searchInput");
  if (searchInput) {
    searchInput.value = qParam;
    setTimeout(() => searchStocks(), 500);
  }
}

/** Main initialization - called once on DOMContentLoaded */
async function initializeApp() {
  initSearchEvents();

  await refreshCredentialState();
  if (!HAS_MISTRAL_API_KEY) {
    window.location.href = "/setup";
    return;
  }

  updateApiStatus();
  initNewsEvents();
  initTabEvents();
  initBulkAnalyzeEvents();
  initSyncEvents();
  initStreamToggleEvents();
  initVisibilityHandler();

  setActiveTab("us");
  setBulkAnalyzeStatus("");

  // 初回データ取得
  fetchInitialStocks(true).then(async () => {
    await loadPortfolioSnapshot();
    connectSSE();
  });
  loadIndicesLoop();
  loadTrending();

  handleUrlSearchParam();
}

async function loadPortfolioSnapshot() {
  try {
    const { data } = await apiFetch(
      "/api/stocks/portfolio/snapshot",
      { method: "POST" },
      { showToast: false },
    );
    if (!data?.stocks) return;
    state.updateStocks(
      mergeStocksWithExistingHistory(data.stocks, state.stocks),
    );
    if (document.querySelector(".tab.active")?.id === "tab-portfolio") {
      renderPortfolio();
    }
  } catch (error) {
    logger.warn("Failed to load portfolio snapshot:", error);
  }
}

document.addEventListener("DOMContentLoaded", initializeApp);

async function fetchInitialStocks(force = false) {
  try {
    const hasAnyCards = document.querySelectorAll(".stock-wrapper").length > 0;
    const hasSkeleton = document.querySelector(".skeleton-card") !== null;
    const noStateData =
      (state.stocks.us?.length || 0) +
        (state.stocks.jp?.length || 0) +
        (state.stocks.idx?.length || 0) ===
      0;
    if (!hasAnyCards && !hasSkeleton && noStateData) {
      renderSkeletons();
      // Set timeout to show timeout state if skeleton persists beyond max wait
      setTimeout(() => {
        const stillSkeleton = document.querySelector(".skeleton-card") !== null;
        const stillNoData =
          (state.stocks.us?.length || 0) +
            (state.stocks.jp?.length || 0) +
            (state.stocks.idx?.length || 0) ===
          0;
        if (stillSkeleton && stillNoData) {
          renderInitialLoadingTimeoutState();
        }
      }, INITIAL_SKELETON_MAX_WAIT_MS || 8000);
    }

    const url = force ? "/api/stocks?force=true" : "/api/stocks";
    const { data } = await apiFetch(url, {}, { showToast: false });
    if (!data) return;

    handleYfinanceRateLimitStatus(data.is_yfinance_rate_limited);

    // Handle new response format { stocks: { us, jp, idx }, indices: { ... } }
    const stocksObj = data.stocks || data;
    const incomingData = {
      us: (stocksObj.us || []).map((s) => ({ ...s, market: "us" })),
      jp: (stocksObj.jp || []).map((s) => ({ ...s, market: "jp" })),
      idx: (stocksObj.idx || []).map((s) => ({ ...s, market: "idx" })),
    };
    // GET /api/stocks strips portfolio fields (H-3 security).
    // Merge with existing state to preserve portfolio data received via SSE.
    const stocks = mergeStocksWithExistingHistory(incomingData, state.stocks);
    state.updateStocks(stocks);

    if (data.indices) {
      updateIndicesBar(data.indices);
    }

    renderStocks("us", state.stocks.us);
    renderStocks("jp", state.stocks.jp);
    renderStocks("idx", state.stocks.idx);
    sseState.skeletonShownAt = 0;
    scheduleHistoryPrefetchWarmup();

    // ポートフォリオタブが表示されている場合は再描画
    if (document.querySelector(".tab.active")?.id === "tab-portfolio") {
      renderPortfolio();
    }
  } catch (e) {
    logger.warn("Init fetch err:", e);
  }
}

async function loadTrending() {
  try {
    const { data } = await apiFetch("/api/trending", {}, { showToast: false });
    if (data.trending && Array.isArray(data.trending)) {
      renderTrendingBadges(data.trending);
    }
  } catch (e) {
    logger.warn("Failed to load trending", e);
  }
}

function renderTrendingBadges(trendingList) {
  const container = DOM.get("trending-list");
  const area = DOM.get("trending-area");
  if (!container || !area) return;

  if (!trendingList || trendingList.length === 0) {
    area.style.display = "none";
    return;
  }

  area.style.display = "flex";
  container.textContent = "";
  const fragment = document.createDocumentFragment();
  trendingList.forEach((t) => {
    const badge = document.createElement("span");
    badge.className = "trending-badge";
    badge.textContent = t;
    fragment.appendChild(badge);
  });
  container.appendChild(fragment);
}

// -----------------------------------------------------
// Portfolio Modal Logic
// -----------------------------------------------------
let _pfSharesHandler = null;
let _pfPriceHandler = null;

function openPortfolioModal(stockKey) {
  const stock = getStockByKey(stockKey);
  if (!stock) return;

  const sharesInput = DOM.get("pf-shares-input");
  const priceInput = DOM.get("pf-price-input");
  const costDisplay = DOM.get("pf-modal-total-cost");

  if (_pfSharesHandler && sharesInput)
    sharesInput.removeEventListener("input", _pfSharesHandler);
  if (_pfPriceHandler && priceInput)
    priceInput.removeEventListener("input", _pfPriceHandler);

  const updatePortfolioModalTotalCost = () => {
    if (!sharesInput || !priceInput || !costDisplay) return;
    const shares = parseFloat(sharesInput.value) || 0;
    const price = parseFloat(priceInput.value) || 0;
    const total = shares * price;
    costDisplay.textContent = total.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  };

  _pfSharesHandler = updatePortfolioModalTotalCost;
  _pfPriceHandler = updatePortfolioModalTotalCost;

  openModal("portfolioModal", () => {
    DOM.get("pf-modal-symbol").textContent = `${stock.symbol} - ${stock.name}`;
    DOM.get("pf-shares-input").value = toFiniteNumber(stock.shares, 0);
    DOM.get("pf-price-input").value = toFiniteNumber(stock.avg_price, 0);
    const fxInput = DOM.get("pf-fx-rate-input");
    if (fxInput)
      fxInput.value =
        stock.avg_fx_rate !== undefined && stock.avg_fx_rate !== null
          ? toFiniteNumber(stock.avg_fx_rate, 0)
          : "";
    updatePortfolioModalTotalCost();
  });

  sharesInput?.addEventListener("input", updatePortfolioModalTotalCost);
  priceInput?.addEventListener("input", updatePortfolioModalTotalCost);

  // Setup step buttons
  document.querySelectorAll("#portfolioModal .pf-step-btn").forEach((btn) => {
    // Remove existing listener if any to avoid duplicates
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener("click", (e) => {
      const targetId = e.target.getAttribute("data-target");
      const step = parseFloat(e.target.getAttribute("data-step"));
      const input = document.getElementById(targetId);
      if (input) {
        let val = parseFloat(input.value) || 0;
        let increment = step;

        // Dynamically adjust step for price based on current value magnitude
        if (targetId === "pf-price-input") {
          if (val > 1000) increment = step * 100;
          else if (val > 100) increment = step * 10;
          else if (val < 10) increment = step * 0.1;
        } else {
          // shares step is 1 unless it's very large
          if (val > 1000) increment = step * 100;
          else if (val > 100) increment = step * 10;
        }

        val = Math.max(0, val + increment);
        // Fix precision issues
        input.value = parseFloat(val.toPrecision(12));
        updatePortfolioModalTotalCost();
      }
    });
  });

  const saveBtn = DOM.get("savePortfolioBtn");
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    try {
      const sharesInput = DOM.get("pf-shares-input")?.value;
      const avgPriceInput = DOM.get("pf-price-input")?.value;
      const fxRateInput = DOM.get("pf-fx-rate-input")?.value;

      const sharesParsed = parseRequiredNonNegativeNumber(
        sharesInput,
        "保有数",
      );
      if (!sharesParsed.ok) {
        showToast(`❌ ${sharesParsed.error}`, "#ff7d7d");
        return;
      }
      const avgPriceParsed = parseRequiredNonNegativeNumber(
        avgPriceInput,
        "平均取得単価",
      );
      if (!avgPriceParsed.ok) {
        showToast(`❌ ${avgPriceParsed.error}`, "#ff7d7d");
        return;
      }
      const fxRateParsed =
        fxRateInput && fxRateInput.trim() !== ""
          ? parseRequiredNonNegativeNumber(fxRateInput, "決済時為替レート")
          : { ok: true, value: null };
      if (!fxRateParsed.ok) {
        showToast(`❌ ${fxRateParsed.error}`, "#ff7d7d");
        return;
      }

      const requestBody = {
        symbol: stock.symbol,
        market: stock.market,
        shares: sharesParsed.value,
        avg_price: avgPriceParsed.value,
      };
      if (fxRateParsed.value !== null) {
        requestBody.avg_fx_rate = fxRateParsed.value;
      }

      const res = await csrfFetch("/api/stocks/portfolio", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok && !payload.error) {
        showToast("✅ ポートフォリオを更新しました", "#7dffb0");
        closeModal("portfolioModal");
        // Immediately update local state so portfolio data is not lost
        // when fetchInitialStocks() receives stripped data from GET /api/stocks.
        const market = stock.market;
        const list = state.stocks[market];
        if (Array.isArray(list)) {
          const idx = list.findIndex((s) => s.symbol === stock.symbol);
          if (idx !== -1) {
            const updated = {
              ...list[idx],
              shares: sharesParsed.value,
              avg_price: avgPriceParsed.value,
            };
            if (fxRateParsed.value !== null) {
              updated.avg_fx_rate = fxRateParsed.value;
            } else {
              delete updated.avg_fx_rate;
            }
            list[idx] = updated;
          }
        }
        // Force refresh data
        fetchInitialStocks();
      } else {
        const detailReason = payload?.details?.reason
          ? String(payload.details.reason)
          : "";
        const msg =
          detailReason ||
          (payload.error ? String(payload.error) : "更新に失敗しました");
        showToast(`❌ ${msg}`, "#ff7d7d");
      }
    } catch (e) {
      showToast("❌ 通信エラー", "#ff7d7d");
    } finally {
      saveBtn.disabled = false;
    }
  };
}

document
  .getElementById("closePortfolioModal")
  ?.addEventListener("click", () => {
    closeModal("portfolioModal");
  });

// -----------------------------------------------------
// Alerts Logic
// -----------------------------------------------------
function getAlertsConfig() {
  try {
    return JSON.parse(localStorage.getItem("userAlerts") || "{}");
  } catch {
    return {};
  }
}
function saveAlertsConfig(cfg) {
  localStorage.setItem("userAlerts", JSON.stringify(cfg));
}

function openAlertModal(stockKey) {
  const stock = getStockByKey(stockKey);
  if (!stock) return;
  const cfg = getAlertsConfig()[stockKey] || {};

  openModal("alertModal", () => {
    DOM.get("alert-modal-symbol").textContent =
      `${stock.symbol} - アラート設定`;
    DOM.get("alert-price-up").value = cfg.priceUp || "";
    DOM.get("alert-price-down").value = cfg.priceDown || "";
    DOM.get("alert-ma-cross").checked = !!cfg.maCross;
  });

  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }

  const saveBtn = DOM.get("saveAlertBtn");
  saveBtn.onclick = () => {
    const upParsed = parseOptionalNonNegativeNumber(
      DOM.get("alert-price-up")?.value,
      "目標到達価格",
    );
    if (!upParsed.ok) {
      showToast(`❌ ${upParsed.error}`, "#ff7d7d");
      return;
    }
    const downParsed = parseOptionalNonNegativeNumber(
      DOM.get("alert-price-down")?.value,
      "下落価格",
    );
    if (!downParsed.ok) {
      showToast(`❌ ${downParsed.error}`, "#ff7d7d");
      return;
    }

    const alerts = getAlertsConfig();
    alerts[stockKey] = {
      priceUp: upParsed.value,
      priceDown: downParsed.value,
      maCross: DOM.get("alert-ma-cross").checked,
      triggeredUp: false,
      triggeredDown: false,
    };
    saveAlertsConfig(alerts);
    showToast("✅ アラート設定を保存しました", "#7dffb0");
    closeModal("alertModal");
  };
}

DOM.get("closeAlertModal")?.addEventListener("click", () => {
  closeModal("alertModal");
});

// window click to close modals and search results
window.addEventListener("click", (e) => {
  ["portfolioModal", "alertModal"].forEach((id) => {
    const m = document.getElementById(id);
    if (e.target === m) {
      closeModal(id);
    }
  });

  const searchInput = DOM.get("searchInput");
  const searchBtn = DOM.get("searchBtn");
  const searchResults = DOM.get("search-results");
  if (searchResults && searchResults.style.display !== "none") {
    // Exclude both searchInput and searchBtn from triggering searchResults close.
    // Also check if searchBtn contains the target (in case it has child elements).
    const clickedSearchBtn =
      searchBtn === e.target || searchBtn?.contains(e.target);
    const clickedSearchInput =
      searchInput === e.target || searchInput?.contains(e.target);
    const clickedInsideResults = searchResults.contains(e.target);

    if (!clickedInsideResults && !clickedSearchInput && !clickedSearchBtn) {
      searchResults.style.display = "none";
    }
  }
});

function showBrowserNotification(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    new Notification(title, {
      body: body,
      icon: "/static/favicon.ico",
    });
  }
}

function checkAlerts(stock, oldPrice) {
  if (oldPrice === undefined || oldPrice === null) return;
  const stockKey = makeStockKey(stock.market, stock.symbol);
  const cfg = getAlertsConfig()[stockKey];
  if (!cfg) return;
  let updateRequired = false;

  const currentPrice = stock.price;

  if (cfg.priceUp && !cfg.triggeredUp && currentPrice >= cfg.priceUp) {
    const msg = `目標価格 (${cfg.priceUp}) に到達しました！ 現在値: ${currentPrice}`;
    showToast(`🔔 【${stock.symbol}】 ${msg}`, "#7dffb0");
    showBrowserNotification(`価格アラート: ${stock.symbol}`, msg);
    cfg.triggeredUp = true;
    updateRequired = true;
  } else if (cfg.priceUp && cfg.triggeredUp && currentPrice < cfg.priceUp) {
    // リセット
    cfg.triggeredUp = false;
    updateRequired = true;
  }

  if (cfg.priceDown && !cfg.triggeredDown && currentPrice <= cfg.priceDown) {
    const msg = `設定価格 (${cfg.priceDown}) を下回りました。 現在値: ${currentPrice}`;
    showToast(`📉 【${stock.symbol}】 ${msg}`, "#ff7d7d");
    showBrowserNotification(`価格アラート: ${stock.symbol}`, msg);
    cfg.triggeredDown = true;
    updateRequired = true;
  } else if (
    cfg.priceDown &&
    cfg.triggeredDown &&
    currentPrice > cfg.priceDown
  ) {
    cfg.triggeredDown = false;
    updateRequired = true;
  }

  // MA Cross Check
  if (cfg.maCross) {
    const history = stock.chart_data;
    if (history && history.length > 0) {
      const ma5 = history[history.length - 1].ma5;
      if (ma5) {
        if (oldPrice < ma5 && currentPrice >= ma5) {
          const msg = `5日移動平均線を上抜けました！`;
          showToast(`🚀 【${stock.symbol}】 ${msg}`, "#ffcc66");
          showBrowserNotification(`テクニカルアラート: ${stock.symbol}`, msg);
        } else if (oldPrice > ma5 && currentPrice <= ma5) {
          const msg = `5日移動平均線を下抜けました。`;
          showToast(`⚠️ 【${stock.symbol}】 ${msg}`, "#ffcc66");
          showBrowserNotification(`テクニカルアラート: ${stock.symbol}`, msg);
        }
      }
    }
  }

  if (updateRequired) {
    const alerts = getAlertsConfig();
    alerts[stockKey] = cfg;
    saveAlertsConfig(alerts);
  }
}

// showToastはutils.jsで定義済み（全ページ共通）
// #endregion Initialization
