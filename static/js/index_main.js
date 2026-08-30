// #region Initialization — Event Registration Helpers

/** Initialize search button and keyboard events */
function initSearchEvents() {
  const searchBtn = document.getElementById("searchBtn");
  const searchInput = document.getElementById("searchInput");
  const resultsContainer = document.getElementById("search-results-list");

  if (searchBtn) {
    searchBtn.addEventListener("click", (e) => {
      e.preventDefault();
      searchStocks();
    });
  }
  if (searchInput) {
    let focusIdx = -1;
    searchInput.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      const items = Array.from(
        resultsContainer
          ? resultsContainer.querySelectorAll(".search-result-item")
          : [],
      );
      if (!items.length) {
        if (e.key === "Enter") {
          e.preventDefault();
          searchStocks();
        }
        return;
      }

      if (e.key === "ArrowDown") {
        e.preventDefault();
        focusIdx = (focusIdx + 1) % items.length;
        items.forEach((item, idx) =>
          item.classList.toggle("highlighted", idx === focusIdx),
        );
        items[focusIdx]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        focusIdx = (focusIdx - 1 + items.length) % items.length;
        items.forEach((item, idx) =>
          item.classList.toggle("highlighted", idx === focusIdx),
        );
        items[focusIdx]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (focusIdx >= 0 && items[focusIdx]) {
          items[focusIdx].click();
          focusIdx = -1;
        } else {
          searchStocks();
        }
      }
    });

    searchInput.addEventListener("input", () => {
      focusIdx = -1;
    });
  }
}

/** Initialize tab switching events */
function initTabEvents() {
  const tabs = [
    ["tab-us", "us"],
    ["tab-jp", "jp"],
    ["tab-idx", "idx"],
    ["tab-portfolio", "portfolio"],
  ];
  tabs.forEach(([id, market], index) => {
    const tab = document.getElementById(id);
    if (!tab) return;
    tab.addEventListener("click", () => setActiveTab(market));
    tab.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setActiveTab(market);
        return;
      }
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const direction = event.key === "ArrowLeft" ? -1 : 1;
        const nextIndex =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? tabs.length - 1
              : (index + direction + tabs.length) % tabs.length;
        const nextTab = document.getElementById(tabs[nextIndex][0]);
        nextTab?.focus();
        setActiveTab(tabs[nextIndex][1]);
      }
    });
  });
}

/** Initialize 3-stage SSE mode selector events */
function initStreamToggleEvents() {
  const container = document.getElementById("sseModeSelector");
  if (!container) return;

  const currentMode = typeof getSseMode === "function" ? getSseMode() : 2;
  if (typeof updateSseModeSelectorUI === "function") {
    updateSseModeSelectorUI(currentMode);
  }

  container.addEventListener("click", (e) => {
    const btn = e.target.closest(".sse-mode-btn");
    if (!btn) return;
    const mode = parseInt(btn.dataset.mode, 10);
    if (!isNaN(mode) && typeof setSseMode === "function") {
      setSseMode(mode);
    }
  });
}

/** Initialize news refresh button */
function initNewsEvents() {
  document
    .getElementById("newsRefreshBtn")
    ?.addEventListener("click", forceRefreshNews);
}

/** Initialize navigation and settings button events */
function initNavigationEvents() {
  document.getElementById("settingsBtn")?.addEventListener("click", () => {
    window.location.href = "/settings";
  });
  document
    .getElementById("mobileSettingsBtn")
    ?.addEventListener("click", () => {
      window.location.href = "/settings";
    });
  document
    .getElementById("mobileAiPortfolioBtn")
    ?.addEventListener("click", () => {
      if (typeof setActiveTab === "function") {
        setActiveTab("portfolio");
      }
      const aiModeBtn = document.getElementById("pf-mode-ai");
      if (aiModeBtn) {
        aiModeBtn.click();
      }
      const pfWrapper = document.getElementById("portfolio-wrapper");
      if (pfWrapper) {
        pfWrapper.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
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
    const result = await fetchInitialStocks(true);
    if (result === INITIAL_STOCKS_FETCH_RESULT.UPDATED) {
      showToast("✅ 株価の同期が完了しました", "#7dffb0");
    } else if (result === INITIAL_STOCKS_FETCH_RESULT.FAILED) {
      showToast("❌ 同期エラーが発生しました", "#ff7d7d");
    } else {
      showToast("ℹ️ 最新データの到着を待っています", "#6bb6ff");
    }
  } catch (_e) {
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
/** Initialize visibility change handler: refresh stock data when the tab
 * regains focus and no live SSE stream is feeding updates.
 *
 * api_client.js already reconnects the SSE stream on visibilitychange, so a
 * full refresh is only needed when streaming is disabled (mode 0) or the
 * connection is down — without SSE, the 60s background poll is the only other
 * refresh, leaving data up to a minute stale after a tab switch.
 */
function initVisibilityHandler() {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    fetchInitialStocks();
    const activeSource =
      sseState.stockEventSource || sseApiClient.currentEventSource;
    if (!activeSource || activeSource.readyState === EventSource.CLOSED) {
      // api_client.js only *resumes* a paused stream; a fully disconnected
      // stream (e.g. max reconnect attempts reached while hidden) needs an
      // explicit restart. Mode 0 (disabled) stays on background polling.
      if (typeof getSseMode === "function" && getSseMode() !== 0) {
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
  const marketParam = (urlParams.get("market") || "").toLowerCase();
  const searchInput = DOM.get("searchInput");
  if (searchInput) {
    searchInput.value = qParam;
    setTimeout(() => searchStocks(), 500);
  }

  // Deep links from the browser extension must open the requested card, not
  // merely populate the search field. Rendering is asynchronous, so retry
  // briefly until the stock wrapper is available.
  let attempts = 0;
  const openRequestedCard = () => {
    attempts += 1;
    const stock = marketParam
      ? getStockByKey(makeStockKey(marketParam, qParam.toUpperCase()))
      : ["us", "jp", "idx"]
          .map((market) =>
            getStockByKey(makeStockKey(market, qParam.toUpperCase())),
          )
          .find(Boolean);
    if (stock) {
      const key = makeStockKey(stock.market, stock.symbol);
      const wrapper = findWrapperByStockKey(key);
      if (wrapper) {
        toggleDetail(wrapper);
        wrapper.scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
      }
    }
    if (attempts < 40) setTimeout(openRequestedCard, 250);
  };
  setTimeout(openRequestedCard, 600);
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
  initNavigationEvents();
  initTabEvents();
  initBulkAnalyzeEvents();
  initSyncEvents();
  initStreamToggleEvents();
  initIndicesEvents();
  initVisibilityHandler();

  setActiveTab("us");
  setBulkAnalyzeStatus("");

  // 初回データ取得 — force=false でキャッシュから即座に表示し、
  // 直後の connectSSE() が SSE 経由で最新データに更新する。
  // force=true だと yfinance への不要なリクエストが発生し、
  // 429 レート制限のリスクが高まるため、初回はキャッシュ利用を優先する。
  void fetchInitialStocks(false)
    .then(async (result) => {
      if (result === INITIAL_STOCKS_FETCH_RESULT.FAILED) {
        logger.warn("Initial stock fetch failed; continuing with SSE recovery");
      }
      await loadPortfolioSnapshot();
      connectSSE();
    })
    .catch((error) => {
      // fetchInitialStocks normally returns a result value, but preserve the
      // startup path if an unexpected rendering error escapes it.
      logger.error("Unexpected initial stock fetch failure:", error);
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

/** Initialize index bar pause button toggle */
function initIndicesEvents() {
  const indicesPauseBtn = document.getElementById("indices-pause-btn");
  if (indicesPauseBtn && !indicesPauseBtn._hasListener) {
    indicesPauseBtn._hasListener = true;
    indicesPauseBtn.addEventListener("click", () => {
      const wrapper = indicesPauseBtn.closest(".indices-bar-wrapper");
      if (!wrapper) return;
      const isPaused = wrapper.classList.toggle("paused");
      const icon = indicesPauseBtn.querySelector(".indices-pause-icon");
      if (icon) icon.textContent = isPaused ? "▶" : "⏸";
      indicesPauseBtn.setAttribute(
        "aria-label",
        isPaused
          ? "ティッカーの自動スクロールを再開"
          : "ティッカーの自動スクロールを停止",
      );
      indicesPauseBtn.setAttribute("aria-pressed", String(isPaused));
      indicesPauseBtn.classList.toggle("paused", isPaused);
    });
  }
}

document.addEventListener("DOMContentLoaded", initializeApp);

// A full /api/stocks response must never replace a fresher fetch or SSE update.
// The active request is aborted when superseded, and the generation check is
// retained because an abort can race with a response already being decoded.
let fetchInitialStocksGeneration = 0;
let fetchInitialStocksAbortController = null;

const INITIAL_STOCKS_FETCH_RESULT = Object.freeze({
  UPDATED: "updated",
  DEFERRED: "deferred",
  STALE: "stale",
  FAILED: "failed",
});

function isCurrentInitialStocksFetch(generation, abortController) {
  return (
    generation === fetchInitialStocksGeneration &&
    abortController === fetchInitialStocksAbortController &&
    !abortController.signal.aborted
  );
}

/**
 * Called by the SSE paths when they apply newer quote data.  Keep it on
 * window because api.js is loaded before this file and only invokes it later.
 */
function invalidatePendingInitialStocksFetch() {
  if (!fetchInitialStocksAbortController) return;
  fetchInitialStocksGeneration += 1;
  fetchInitialStocksAbortController.abort();
}

window.invalidatePendingInitialStocksFetch =
  invalidatePendingInitialStocksFetch;

async function fetchInitialStocks(force = false) {
  if (fetchInitialStocksAbortController) {
    fetchInitialStocksAbortController.abort();
  }
  const abortController = new AbortController();
  fetchInitialStocksAbortController = abortController;
  const myGeneration = ++fetchInitialStocksGeneration;
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
    const { data } = await apiFetch(
      url,
      { signal: abortController.signal },
      { showToast: false },
    );
    if (!isCurrentInitialStocksFetch(myGeneration, abortController)) {
      logger.info("Ignoring stale /api/stocks response");
      return INITIAL_STOCKS_FETCH_RESULT.STALE;
    }
    if (!data) return INITIAL_STOCKS_FETCH_RESULT.DEFERRED;

    if (data.fetching) {
      logger.info(
        "Initial stocks still fetching; deferring render to SSE/next sync",
      );
      return INITIAL_STOCKS_FETCH_RESULT.DEFERRED;
    }

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
    updateTabCounts();
    ["us", "jp", "idx"].forEach((market) => {
      document
        .getElementById(`${market}-stocks`)
        ?.setAttribute("aria-busy", "false");
    });
    sseState.skeletonShownAt = 0;
    scheduleHistoryPrefetchWarmup();

    // ポートフォリオタブが表示されている場合は再描画
    if (document.querySelector(".tab.active")?.id === "tab-portfolio") {
      renderPortfolio();
    }
    return INITIAL_STOCKS_FETCH_RESULT.UPDATED;
  } catch (e) {
    if (!isCurrentInitialStocksFetch(myGeneration, abortController)) {
      logger.info("Ignoring stale /api/stocks failure");
      return INITIAL_STOCKS_FETCH_RESULT.STALE;
    }
    if (e?.name === "AbortError") {
      return INITIAL_STOCKS_FETCH_RESULT.STALE;
    }
    logger.warn("Init fetch err:", e);
    ["us", "jp", "idx"].forEach((market) => {
      const container = document.getElementById(`${market}-stocks`);
      if (!container) return;
      container.setAttribute("aria-busy", "false");
      container.textContent = "";
      const error = createEl("div", "no-results data-error-state");
      error.style.gridColumn = "1 / -1";
      error.appendChild(
        createEl("strong", "", "株価データを取得できませんでした"),
      );
      error.appendChild(
        document.createTextNode("。通信状態を確認して再試行してください。"),
      );
      const retry = createEl("button", "retry-btn", "再試行");
      retry.type = "button";
      retry.addEventListener("click", () => fetchInitialStocks(force));
      error.appendChild(retry);
      container.appendChild(error);
    });
    return INITIAL_STOCKS_FETCH_RESULT.FAILED;
  } finally {
    if (fetchInitialStocksAbortController === abortController) {
      fetchInitialStocksAbortController = null;
    }
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
    btn.onclick = () => {
      const targetId = btn.getAttribute("data-target");
      const step = parseFloat(btn.getAttribute("data-step") || "0");
      const input = targetId ? document.getElementById(targetId) : null;
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
        input.value = parseFloat(val.toPrecision(12));
        input.dispatchEvent(new window.Event("input", { bubbles: true }));
        updatePortfolioModalTotalCost();
      }
    };
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
    } catch (_e) {
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

// Global Escape key handler for drawers and floating UI
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const fsModal = document.getElementById("chart-fullscreen-modal");
  if (fsModal && !fsModal.classList.contains("hidden")) {
    if (typeof closeFsChartModal === "function") {
      closeFsChartModal();
      return;
    }
  }
  const aiDrawer = document.getElementById("ai-drawer-overlay");
  if (aiDrawer && !aiDrawer.classList.contains("hidden")) {
    if (typeof closeAiDrawer === "function") {
      closeAiDrawer();
      return;
    }
  }
  const detailDrawer = document.getElementById("stock-detail-drawer-overlay");
  if (detailDrawer && !detailDrawer.classList.contains("hidden")) {
    if (typeof closeStockDetailDrawer === "function") {
      closeStockDetailDrawer();
      return;
    }
  }
  const searchResults = DOM.get("search-results");
  if (searchResults && searchResults.style.display !== "none") {
    searchResults.style.display = "none";
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
