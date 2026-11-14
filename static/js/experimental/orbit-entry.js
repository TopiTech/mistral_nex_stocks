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
    aiDiveTier4ProviderLabel: document.getElementById("ai-dive-provider-label"),
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

  // Canvas keyboard operation: arrows cycle the reference stock, Enter
  // confirms it as the center, and D/C/M/H mirror the global shortcuts. This
  // gives the focused canvas itself a direct keyboard path independent of the
  // global shortcuts (which intentionally ignore focused controls —
  // role="application" is in their suppression list, so without these local
  // cases the keys the canvas aria-label promises (D/C/H) would be dead).
  canvas.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const stocks = state.state.stockList || [];
    const current = state.state.selectedSymbol;

    // Navigation keys require a stock list.
    if (stocks.length) {
      let idx = stocks.findIndex((s) => s.symbol === current);
      if (idx === -1) idx = 0;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        e.preventDefault();
        const next = stocks[(idx + 1) % stocks.length];
        state.setSelectedSymbol(next.symbol);
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        const prev = stocks[(idx - 1 + stocks.length) % stocks.length];
        state.setSelectedSymbol(prev.symbol);
        return;
      }
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (current) state.setSelectedSymbol(current);
        return;
      }
    }

    // (L26, 2026-11-07 review) Single-letter shortcuts promised by the canvas
    // aria-label, active on canvas focus (global ones are suppressed there).
    switch (e.key) {
      case "d":
      case "D":
        e.preventDefault();
        if (current) state.openAiDive(current);
        break;
      case "c":
      case "C":
        e.preventDefault();
        state.toggleConnectMode();
        break;
      case "m":
      case "M":
        e.preventDefault();
        state.toggleReducedMotion();
        break;
      case "h":
      case "H":
      case "?":
        e.preventDefault();
        accessibilityController.openHelpModal();
        break;
      default:
        break;
    }
  });

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
    tier4ProviderLabel: elements.aiDiveTier4ProviderLabel,
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
      const chg = Number.isFinite(stock.changePercent)
        ? stock.changePercent
        : 0;
      const chgSign = chg >= 0 ? "+" : "";
      elements.centerChange.textContent = `${chgSign}${chg.toFixed(2)}%`;
      elements.centerChange.className = `center-stat-change ${chg >= 0 ? "text-pos" : "text-neg"}`;
    }
  }

  // 8.5. Bind Search Palette & Modal Backdrops
  let activeSearchIdx = -1;

  function setActiveSearchOption(nextIdx) {
    const container = elements.searchResults;
    const input = elements.searchInput;
    if (!container || !input) return;
    const options = Array.from(container.querySelectorAll('[role="option"]'));
    if (!options.length) {
      activeSearchIdx = -1;
      input.removeAttribute("aria-activedescendant");
      return;
    }
    const clampedIdx =
      ((nextIdx % options.length) + options.length) % options.length;
    activeSearchIdx = clampedIdx;
    options.forEach((opt, idx) => {
      opt.classList.toggle("active", idx === clampedIdx);
    });
    const activeOpt = options[clampedIdx];
    if (activeOpt.id) {
      input.setAttribute("aria-activedescendant", activeOpt.id);
    }
    activeOpt.scrollIntoView?.({ block: "nearest" });
  }

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

  if (elements.searchModal) {
    elements.searchModal.addEventListener("click", (e) => {
      if (e.target === elements.searchModal) {
        accessibilityController.closeSearchModal();
      }
    });
  }

  if (elements.helpModal) {
    elements.helpModal.addEventListener("click", (e) => {
      if (e.target === elements.helpModal) {
        accessibilityController.closeHelpModal();
      }
    });
  }

  if (elements.searchInput) {
    elements.searchInput.addEventListener("input", (e) => {
      renderSearchResults(e.target.value);
    });

    elements.searchInput.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      const container = elements.searchResults;
      if (!container) return;
      const options = Array.from(container.querySelectorAll('[role="option"]'));
      if (!options.length) return;

      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setActiveSearchOption(
            activeSearchIdx < 0 ? 0 : (activeSearchIdx + 1) % options.length,
          );
          break;
        case "ArrowUp":
          e.preventDefault();
          setActiveSearchOption(
            activeSearchIdx <= 0 ? options.length - 1 : activeSearchIdx - 1,
          );
          break;
        case "Home":
          e.preventDefault();
          setActiveSearchOption(0);
          break;
        case "End":
          e.preventDefault();
          setActiveSearchOption(options.length - 1);
          break;
        case "Enter": {
          e.preventDefault();
          const targetIdx = activeSearchIdx >= 0 ? activeSearchIdx : 0;
          const target = options[targetIdx];
          if (target && typeof target.click === "function") {
            target.click();
          }
          break;
        }
        default:
          break;
      }
    });
  }

  function renderSearchResults(query = "") {
    const container = elements.searchResults;
    if (!container) return;
    container.textContent = "";
    activeSearchIdx = -1;
    if (elements.searchInput) {
      elements.searchInput.removeAttribute("aria-activedescendant");
    }

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

    let optionSeq = 0;
    matched.forEach((st) => {
      const optionIndex = optionSeq++;
      const item = document.createElement("button");
      item.type = "button";
      item.id = `orbit-search-option-${optionIndex}`;
      item.className = "search-result-item";
      // combobox/listbox 契約(FE-tpl-2): 結果項目を option として告知する。
      item.setAttribute("role", "option");
      item.setAttribute(
        "aria-selected",
        String(st.symbol === state.state.selectedSymbol),
      );
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
      const chg = Number.isFinite(st.changePercent) ? st.changePercent : 0;
      const sign = chg >= 0 ? "+" : "";
      chgSpan.className = `search-res-change ${chg >= 0 ? "text-pos" : "text-neg"}`;
      chgSpan.textContent = `${sign}${chg.toFixed(2)}%`;

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
  const marketButtons = [
    { key: "us", btn: elements.marketUsBtn },
    { key: "jp", btn: elements.marketJpBtn },
    { key: "all", btn: elements.marketAllBtn },
  ].filter((item) => !!item.btn);

  function switchMarket(market) {
    if (state.state.market === market) return;
    state.set({ market });

    marketButtons.forEach((item) => {
      const active = item.key === market;
      item.btn.classList.toggle("active", active);
      item.btn.setAttribute("aria-pressed", String(active));
      item.btn.setAttribute("tabindex", active ? "0" : "-1");
    });

    loadObservatoryData();
  }

  marketButtons.forEach((item, index) => {
    const isCurrent = (state.state.market || "us") === item.key;
    // URL(?market=jp / ?market=all)から初期化された状態とボタン表示を
    // 初期時点で一致させる。テンプレートの US デフォルトのまま残すと、
    // JP データ表示中に US ボタンが押下状態に見える(aria 状態の嘘)。
    // switchMarket と同じ 3 状態チャンネル(class / aria-pressed /
    // tabindex)を再読み込みなしで同期する。
    item.btn.classList.toggle("active", isCurrent);
    item.btn.setAttribute("aria-pressed", String(isCurrent));
    item.btn.setAttribute("tabindex", isCurrent ? "0" : "-1");
    item.btn.addEventListener("click", () => switchMarket(item.key));
    item.btn.addEventListener("keydown", (event) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (
        [
          "ArrowLeft",
          "ArrowRight",
          "ArrowUp",
          "ArrowDown",
          "Home",
          "End",
        ].includes(event.key)
      ) {
        event.preventDefault();
        const direction =
          event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
        const nextIndex =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? marketButtons.length - 1
              : (index + direction + marketButtons.length) %
                marketButtons.length;
        const nextItem = marketButtons[nextIndex];
        if (nextItem && nextItem.btn) {
          nextItem.btn.focus();
          switchMarket(nextItem.key);
        }
      }
    });
  });

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
    { silent = false } = {},
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

    // (L27) サイレント更新(定期/復帰時)ではローディングオーバーレイを
    // 出さず、失敗時も既存の表示を壊さない。初回とユーザー操作由来の
    // 読み込みは従来どおり全体オーバーレイ+エラー表示。
    if (!silent) setLoading(true);
    if (!silent) hideError();

    try {
      // First try /api/stocks to get rich portfolio & tracked stocks
      const stocksReqInit = { signal: controller.signal };
      const res = window.apiFetch
        ? await window.apiFetch("/api/stocks", stocksReqInit, {
            showToast: false,
          })
        : await fetch("/api/stocks", stocksReqInit);
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
          if (silent) {
            loadObservatoryData(retryCount + 1, requestGeneration, {
              silent: true,
            });
          } else {
            loadObservatoryData(retryCount + 1, requestGeneration);
          }
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
          rawList = [
            ...(data.stocks.us || []).map((s) => ({
              ...s,
              market: s.market || "us",
            })),
            ...(data.stocks.jp || []).map((s) => ({
              ...s,
              market: s.market || "jp",
            })),
            ...(data.stocks.idx || []).map((s) => ({
              ...s,
              market: s.market || "idx",
            })),
          ];
        }
      }

      // If stock count is low, supplement with /api/heatmap
      if (rawList.length < 8) {
        try {
          const hmMarket = market === "all" ? "us" : market;
          const hmUrl = `/api/heatmap?market=${hmMarket}`;
          const hmInit = { signal: controller.signal };
          const hmRes = window.apiFetch
            ? await window.apiFetch(hmUrl, hmInit, { showToast: false })
            : await fetch(hmUrl, hmInit);
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
        if (silent) {
          console.warn(
            "[Observatory] Refresh returned no stocks; keeping current data",
          );
          return;
        }
        showError("表示可能な銘柄データを取得できませんでした。");
        return;
      }

      hideError();
      state.setStocks(normalizedList);
      setLoading(false);
      renderer.start();
    } catch (err) {
      if (err.name !== "AbortError" && isCurrentRequest()) {
        console.error("[Observatory] Load error:", err);
        if (!silent) {
          const reason =
            err?.details?.reason ||
            err?.data?.details?.reason ||
            err?.message ||
            "接続を確認してください。";
          showError(`市場データの取得に失敗しました（${reason}）`);
        }
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

  // (L27, 2026-11-07 review) 観測所は初期マウント時に一度だけデータを読み
  // 込んでいたため、ページは LIVE バッジを表示したまま価格が永遠に凍結
  // していた。可視復帰時と可視中の低頻度間隔で(既存のgeneration/abort
  // ガードを再利用して)サイレント再取得する。 hidden 中は何もしない。
  const OBSERVATORY_REFRESH_INTERVAL_MS = 60000;
  let observatoryRefreshTimer = null;
  let observatoryUnloadHandlers = [];

  startObservatoryRefresh();

  function observatoryRefreshHandler() {
    // hidden 中は何も取得しない(renderer も rAF を止めている)。
    // 読み込み中の再取得も安全: loadObservatoryData は generation/abort
    // ガードで古い要求を無効化し、最後の応答のみが採用される。
    if (document.hidden) return;
    loadObservatoryData(0, null, { silent: true });
  }

  function startObservatoryRefresh() {
    if (typeof document.addEventListener === "function") {
      observatoryUnloadHandlers.push([
        document,
        "visibilitychange",
        observatoryVisibilityRefresh,
      ]);
      document.addEventListener(
        "visibilitychange",
        observatoryVisibilityRefresh,
      );
    }
    if (typeof window.setInterval === "function") {
      observatoryRefreshTimer = window.setInterval(
        observatoryIntervalRefresh,
        OBSERVATORY_REFRESH_INTERVAL_MS,
      );
    }
  }

  function observatoryVisibilityRefresh() {
    observatoryRefreshHandler();
  }

  function observatoryIntervalRefresh() {
    observatoryRefreshHandler();
  }

  // Cleanup on unload
  window.addEventListener("beforeunload", () => {
    loadGeneration += 1;
    cancelObservatoryLoadRetry();
    if (observatoryRefreshTimer !== null) {
      window.clearInterval(observatoryRefreshTimer);
      observatoryRefreshTimer = null;
    }
    observatoryUnloadHandlers.forEach(([target, type, fn]) => {
      target.removeEventListener(type, fn);
    });
    observatoryUnloadHandlers = [];
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
