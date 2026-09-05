/**
 * orbit-entry.js - Main bootstrap and lifecycle coordinator for Market Observatory.
 *
 * Initializes state, renderer, controllers, fetches market data,
 * and manages responsive lifecycle events.
 */

document.addEventListener("DOMContentLoaded", () => {
  "use strict";

  const canvas = document.getElementById("orbit-canvas");
  if (!canvas) return;

  const elements = {
    canvas,
    loadingOverlay: document.getElementById("observatory-loading"),
    errorOverlay: document.getElementById("observatory-error"),
    errorMsg: document.getElementById("observatory-error-msg"),
    retryBtn: document.getElementById("observatory-retry-btn"),

    // Market toggle
    marketUsBtn: document.getElementById("market-toggle-us"),
    marketJpBtn: document.getElementById("market-toggle-jp"),
    marketAllBtn: document.getElementById("market-toggle-all"),

    // Controls
    motionBtn: document.getElementById("btn-motion-toggle"),
    pauseBtn: document.getElementById("btn-pause-toggle"),
    helpBtn: document.getElementById("btn-shortcuts-help"),
    helpModal: document.getElementById("shortcuts-help-modal"),
    helpCloseBtn: document.getElementById("shortcuts-help-close"),

    // Search Palette
    searchBtn: document.getElementById("btn-orbit-search"),
    searchModal: document.getElementById("orbit-search-modal"),
    searchInput: document.getElementById("orbit-search-input"),
    searchResults: document.getElementById("orbit-search-results"),
    searchCloseBtn: document.getElementById("orbit-search-close"),

    // Timeline floating HUD
    timeSlider: document.getElementById("timeline-slider"),
    timeDisplay: document.getElementById("timeline-display-text"),
    timeBadge: document.getElementById("timeline-status-badge"),
    timePlayBtn: document.getElementById("timeline-play-btn"),
    timeNowBtn: document.getElementById("timeline-now-btn"),
    granularityGroup: document.getElementById("timeline-granularity-group"),

    // Central card HUD
    centerCard: document.getElementById("center-stock-card"),
    centerSymbol: document.getElementById("center-stock-symbol"),
    centerName: document.getElementById("center-stock-name"),
    centerPrice: document.getElementById("center-stock-price"),
    centerChange: document.getElementById("center-stock-change"),
    centerAiDiveBtn: document.getElementById("center-ai-dive-btn"),

    // Constellation comparison drawer
    constellationToggleBtn: document.getElementById("btn-constellation-toggle"),
    constellationDrawer: document.getElementById("constellation-drawer"),
    constellationCloseBtn: document.getElementById("constellation-close-btn"),
    constellationCount: document.getElementById("constellation-count-text"),
    constellationTableContainer: document.getElementById(
      "constellation-table-container",
    ),
    constellationChartWrapper: document.getElementById(
      "constellation-chart-wrapper",
    ),
    constellationChartCanvas: document.getElementById(
      "constellation-chart-canvas",
    ),
    constellationAiBtn: document.getElementById("constellation-ai-btn"),
    constellationAiResult: document.getElementById("constellation-ai-result"),

    // AI Dive Modal
    aiDiveOverlay: document.getElementById("ai-dive-overlay"),
    aiDiveCloseBtn: document.getElementById("ai-dive-close-btn"),
    aiDiveSymbolTitle: document.getElementById("ai-dive-symbol-title"),
    aiDiveNameSubtitle: document.getElementById("ai-dive-name-subtitle"),
    aiDiveTier1: document.getElementById("ai-dive-tier1"),
    aiDiveTier2: document.getElementById("ai-dive-tier2"),
    aiDiveTier3: document.getElementById("ai-dive-tier3"),
    aiDiveTier4: document.getElementById("ai-dive-tier4"),
    aiDiveStartBtn: document.getElementById("ai-dive-start-btn"),

    // Accessibility
    liveRegion: document.getElementById("observatory-live-region"),
    srTableContainer: document.getElementById("observatory-sr-table-container"),
  };

  // 1. Initialize State
  const state = new window.ObservatoryState();

  // 2. Initialize Renderer
  const renderer = new window.OrbitRenderer(canvas, state);

  // 3. Initialize Gesture Controller
  const gestureController = new window.GestureController(
    canvas,
    state,
    renderer,
  );

  // 4. Initialize Temporal Controller
  const temporalController = new window.TemporalController(state, {
    canvas,
    timeSlider: elements.timeSlider,
    timeDisplay: elements.timeDisplay,
    timeBadge: elements.timeBadge,
    timePlayBtn: elements.timePlayBtn,
    timeNowBtn: elements.timeNowBtn,
    granularityGroup: elements.granularityGroup,
  });

  // 5. Initialize Constellation Controller
  const constellationController = new window.ConstellationController(state, {
    toggleBtn: elements.constellationToggleBtn,
    drawer: elements.constellationDrawer,
    closeBtn: elements.constellationCloseBtn,
    countText: elements.constellationCount,
    listContainer: elements.constellationTableContainer,
    chartWrapper: elements.constellationChartWrapper,
    chartCanvas: elements.constellationChartCanvas,
    aiCompareBtn: elements.constellationAiBtn,
    aiResultContainer: elements.constellationAiResult,
  });

  // 6. Initialize AI Dive Controller
  const aiDiveController = new window.AiDiveController(state, {
    overlay: elements.aiDiveOverlay,
    closeBtn: elements.aiDiveCloseBtn,
    symbolTitle: elements.aiDiveSymbolTitle,
    nameSubtitle: elements.aiDiveNameSubtitle,
    tier1Container: elements.aiDiveTier1,
    tier2Container: elements.aiDiveTier2,
    tier3Container: elements.aiDiveTier3,
    tier4Container: elements.aiDiveTier4,
    startAiBtn: elements.aiDiveStartBtn,
  });

  // 7. Initialize Accessibility Controller
  const accessibilityController = new window.AccessibilityController(state, {
    motionBtn: elements.motionBtn,
    pauseBtn: elements.pauseBtn,
    helpBtn: elements.helpBtn,
    helpModal: elements.helpModal,
    helpCloseBtn: elements.helpCloseBtn,
    searchModal: elements.searchModal,
    searchInput: elements.searchInput,
    aiDiveOverlay: elements.aiDiveOverlay,
    liveRegion: elements.liveRegion,
    srTableContainer: elements.srTableContainer,
    onSearchInput: (query) => renderSearchResults(query),
  });

  // 8. Bind Central Stock Info Card
  state.subscribe((key, val, data) => {
    if (key === "selectedSymbol" || key === "stocks") {
      updateCenterCard(data);
    }
  });

  if (elements.centerAiDiveBtn) {
    elements.centerAiDiveBtn.addEventListener("click", () => {
      state.openAiDive(state.state.selectedSymbol);
    });
  }

  function updateCenterCard(data) {
    const symbol = data.selectedSymbol;
    const stock = data.stocks.get(symbol);
    if (!stock) return;

    if (elements.centerSymbol) elements.centerSymbol.textContent = stock.symbol;
    if (elements.centerName)
      elements.centerName.textContent =
        stock.displayName || stock.name || stock.symbol;

    if (elements.centerPrice) {
      elements.centerPrice.textContent =
        stock.price > 0
          ? window.ObservatoryDataAdapter.formatPrice(stock.price, stock)
          : "--";
    }

    if (elements.centerChange) {
      const chgSign = stock.changePercent >= 0 ? "+" : "";
      elements.centerChange.textContent = `${chgSign}${stock.changePercent.toFixed(2)}%`;
      elements.centerChange.className = `center-stat-change ${stock.changePercent >= 0 ? "text-pos" : "text-neg"}`;
    }
  }

  // 8.5. Bind Search Palette
  if (elements.searchBtn) {
    elements.searchBtn.addEventListener("click", () => {
      accessibilityController.openSearchModal();
    });
  }

  if (elements.searchCloseBtn) {
    elements.searchCloseBtn.addEventListener("click", () => {
      accessibilityController.closeSearchModal();
    });
  }

  if (elements.searchInput) {
    elements.searchInput.addEventListener("input", (e) => {
      renderSearchResults(e.target.value);
    });
  }

  function renderSearchResults(query = "") {
    const container = elements.searchResults;
    if (!container) return;
    container.textContent = "";

    const cleanQuery = query.trim().toUpperCase();
    const allStocks = state.state.stockList || [];

    const matched = allStocks
      .filter((s) => {
        if (!cleanQuery) return true;
        const sym = (s.symbol || "").toUpperCase();
        const name = (s.displayName || s.name || "").toUpperCase();
        const sector = (s.sector || "").toUpperCase();
        return (
          sym.includes(cleanQuery) ||
          name.includes(cleanQuery) ||
          sector.includes(cleanQuery)
        );
      })
      .slice(0, 15);

    if (!matched.length) {
      const empty = document.createElement("div");
      empty.className = "constellation-hint";
      empty.textContent = "一致する銘柄が見つかりませんでした";
      container.appendChild(empty);
      return;
    }

    matched.forEach((st) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "search-result-item";
      if (st.symbol === state.state.selectedSymbol) {
        item.classList.add("selected");
      }

      const left = document.createElement("span");
      left.className = "search-res-left";

      const symSpan = document.createElement("span");
      symSpan.className = "search-res-symbol";
      symSpan.textContent = st.symbol;

      const nameSpan = document.createElement("span");
      nameSpan.className = "search-res-name";
      nameSpan.textContent = st.displayName || st.name || st.symbol;

      left.appendChild(symSpan);
      left.appendChild(nameSpan);

      const right = document.createElement("span");
      right.className = "search-res-right";

      const priceSpan = document.createElement("span");
      priceSpan.className = "search-res-price";
      priceSpan.textContent =
        st.price > 0
          ? window.ObservatoryDataAdapter.formatPrice(st.price, st)
          : "--";

      const chgSpan = document.createElement("span");
      const sign = st.changePercent >= 0 ? "+" : "";
      chgSpan.className = `search-res-change ${st.changePercent >= 0 ? "text-pos" : "text-neg"}`;
      chgSpan.textContent = `${sign}${st.changePercent.toFixed(2)}%`;

      right.appendChild(priceSpan);
      right.appendChild(chgSpan);

      item.appendChild(left);
      item.appendChild(right);

      item.addEventListener("click", () => {
        state.setSelectedSymbol(st.symbol);
        if (renderer && typeof renderer.triggerShockwave === "function") {
          renderer.triggerShockwave();
        }
        accessibilityController.closeSearchModal();
      });

      container.appendChild(item);
    });
  }

  // 9. Bind Market Selector
  function switchMarket(market) {
    if (state.state.market === market) return;
    state.set({ market });

    if (elements.marketUsBtn) {
      const isUs = market === "us";
      elements.marketUsBtn.classList.toggle("active", isUs);
      elements.marketUsBtn.setAttribute("aria-pressed", String(isUs));
    }
    if (elements.marketJpBtn) {
      const isJp = market === "jp";
      elements.marketJpBtn.classList.toggle("active", isJp);
      elements.marketJpBtn.setAttribute("aria-pressed", String(isJp));
    }
    if (elements.marketAllBtn) {
      const isAll = market === "all";
      elements.marketAllBtn.classList.toggle("active", isAll);
      elements.marketAllBtn.setAttribute("aria-pressed", String(isAll));
    }

    loadObservatoryData();
  }

  elements.marketUsBtn?.addEventListener("click", () => switchMarket("us"));
  elements.marketJpBtn?.addEventListener("click", () => switchMarket("jp"));
  elements.marketAllBtn?.addEventListener("click", () => switchMarket("all"));

  // 10. Data Fetching
  let loadAbortController = null;
  let loadRetryTimeout = null;
  let loadGeneration = 0;

  function cancelObservatoryLoadRetry() {
    if (loadRetryTimeout !== null) {
      clearTimeout(loadRetryTimeout);
      loadRetryTimeout = null;
    }
  }

  async function loadObservatoryData(
    retryCount = 0,
    expectedGeneration = null,
  ) {
    if (expectedGeneration !== null && expectedGeneration !== loadGeneration) {
      return;
    }

    const requestGeneration =
      expectedGeneration === null ? loadGeneration + 1 : loadGeneration;
    if (expectedGeneration === null) {
      loadGeneration = requestGeneration;
      cancelObservatoryLoadRetry();
    }

    if (loadAbortController) {
      loadAbortController.abort();
    }
    const controller = new AbortController();
    loadAbortController = controller;

    const market = state.state.market;
    const isCurrentRequest = () =>
      loadGeneration === requestGeneration &&
      loadAbortController === controller &&
      state.state.market === market;

    setLoading(true);
    hideError();

    try {
      // First try /api/stocks to get rich portfolio & tracked stocks
      const res = await (window.apiFetch || fetch)(`/api/stocks`, {
        signal: controller.signal,
      });
      const data =
        res && typeof res.json === "function"
          ? await res.json().catch(() => null)
          : (res?.data ?? res);
      if (!isCurrentRequest()) return;

      let rawList = [];

      if (data && data.fetching && retryCount < 8) {
        // Data is being fetched in background, poll after 2 seconds
        const retryTimeout = setTimeout(() => {
          if (loadRetryTimeout !== retryTimeout || !isCurrentRequest()) return;
          loadRetryTimeout = null;
          loadObservatoryData(retryCount + 1, requestGeneration);
        }, 2000);
        loadRetryTimeout = retryTimeout;
        return;
      }

      if (data && data.stocks) {
        if (market === "us") {
          rawList = data.stocks.us || [];
        } else if (market === "jp") {
          rawList = data.stocks.jp || [];
        } else {
          rawList = [...(data.stocks.us || []), ...(data.stocks.jp || [])];
        }
      }

      // If stock count is low, supplement with /api/heatmap
      if (rawList.length < 8 && market !== "all") {
        try {
          const hmRes = await (window.apiFetch || fetch)(
            `/api/heatmap?market=${market}`,
            {
              signal: controller.signal,
            },
          );
          const hmData =
            hmRes && typeof hmRes.json === "function"
              ? await hmRes.json().catch(() => null)
              : (hmRes?.data ?? hmRes);
          if (!isCurrentRequest()) return;
          if (hmData && Array.isArray(hmData.stocks)) {
            const existingSymbols = new Set(rawList.map((s) => s.symbol));
            for (const s of hmData.stocks) {
              if (!existingSymbols.has(s.symbol)) {
                rawList.push(s);
              }
            }
          }
        } catch (_e) {
          // Ignore heatmap fallback failure
        }
      }

      if (!isCurrentRequest()) return;

      const normalizedList = rawList
        .map((s) => window.ObservatoryDataAdapter.normalizeStock(s, { market }))
        .filter(Boolean);

      if (!normalizedList.length) {
        showError("表示可能な銘柄データを取得できませんでした。");
        return;
      }

      state.setStocks(normalizedList);
      setLoading(false);
      renderer.start();
    } catch (err) {
      if (err.name !== "AbortError" && isCurrentRequest()) {
        console.error("[Observatory] Load error:", err);
        showError("市場データの取得に失敗しました。接続を確認してください。");
      }
    }
  }

  function setLoading(isLoading) {
    if (elements.loadingOverlay) {
      if (
        !isLoading &&
        elements.loadingOverlay.contains(document.activeElement)
      ) {
        document.activeElement.blur();
      }
      elements.loadingOverlay.classList.toggle("hidden", !isLoading);
      elements.loadingOverlay.setAttribute("aria-hidden", String(!isLoading));
      if (!isLoading) {
        elements.loadingOverlay.setAttribute("inert", "");
      } else {
        elements.loadingOverlay.removeAttribute("inert");
      }
    }
  }

  function showError(msg) {
    setLoading(false);
    if (elements.errorOverlay) {
      elements.errorOverlay.classList.remove("hidden");
      elements.errorOverlay.setAttribute("aria-hidden", "false");
      elements.errorOverlay.removeAttribute("inert");
    }
    if (elements.errorMsg) {
      elements.errorMsg.textContent = msg;
    }
  }

  function hideError() {
    if (elements.errorOverlay) {
      if (elements.errorOverlay.contains(document.activeElement)) {
        document.activeElement.blur();
      }
      elements.errorOverlay.classList.add("hidden");
      elements.errorOverlay.setAttribute("aria-hidden", "true");
      elements.errorOverlay.setAttribute("inert", "");
    }
  }

  if (elements.retryBtn) {
    elements.retryBtn.addEventListener("click", () => {
      loadObservatoryData();
    });
  }

  // Start initial load
  loadObservatoryData();

  // Cleanup on unload
  window.addEventListener("beforeunload", () => {
    loadGeneration += 1;
    cancelObservatoryLoadRetry();
    renderer.destroy();
    gestureController.destroy();
    temporalController.destroy();
    constellationController.destroy();
    aiDiveController.destroy();
    accessibilityController.destroy();
    if (loadAbortController) {
      loadAbortController.abort();
    }
  });
});
