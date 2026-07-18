// #region Unified API Error Handler
/**
 * Unified API error handler that categorizes errors and shows appropriate toast messages.
 */
const APIErrorType = Object.freeze({
  NETWORK: "network", // fetch itself failed (offline, DNS, CORS)
  TIMEOUT: "timeout", // request timed out (AbortError)
  HTTP_ERROR: "http", // non-2xx HTTP response
  PARSE_FAIL: "parse", // JSON parsing failed
  RATE_LIMIT: "rate_limit", // 429
  FORBIDDEN: "forbidden", // 403
  SERVER_ERROR: "server", // 500+
  UNKNOWN: "unknown",
});

const API_ERROR_MESSAGES = {
  [APIErrorType.NETWORK]: () =>
    "ネットワーク接続を確認できません。オフラインになっていないか確認してください。",
  [APIErrorType.TIMEOUT]: (path) =>
    `リクエストがタイムアウトしました（${path}）。サーバーの負荷が高い可能性があります。`,
  [APIErrorType.RATE_LIMIT]: () =>
    "レート制限に達しました。しばらく待ってから再試行してください。",
  [APIErrorType.FORBIDDEN]: () =>
    "アクセスが拒否されました。ローカル環境からのみアクセス可能です。",
  [APIErrorType.SERVER_ERROR]: () =>
    "サーバーエラーが発生しました。しばらくしてから再試行してください。",
  [APIErrorType.PARSE_FAIL]: () => "サーバーからの応答を解析できませんでした。",
  [APIErrorType.HTTP_ERROR]: (status, path) =>
    `HTTP ${status} エラー（${path}）`,
  [APIErrorType.UNKNOWN]: (err) =>
    `予期しないエラー: ${err?.message || "不明"}`,
};

const API_ERROR_COLORS = {
  [APIErrorType.NETWORK]: "#ff7d7d",
  [APIErrorType.TIMEOUT]: "#E67E22",
  [APIErrorType.RATE_LIMIT]: "#ffcc66",
  [APIErrorType.FORBIDDEN]: "#ff7d7d",
  [APIErrorType.SERVER_ERROR]: "#ff7d7d",
  [APIErrorType.PARSE_FAIL]: "#ffcc66",
  [APIErrorType.HTTP_ERROR]: "#ff7d7d",
  [APIErrorType.UNKNOWN]: "#ff7d7d",
};

/**
 * Logger instance used by api.js for diagnostics.
 * Falls back to console.log if the global `logger` is not defined
 * (e.g. when this module is loaded independently in tests).
 * Routes that require structured logging override this by defining
 * a global `logger` before loading api.js (see index_main.js).
 */
const $logger = typeof logger !== "undefined" && logger ? logger : console;

/**
 * Classify an error from a fetch call into APIErrorType.
 * @param {Error} error - The caught error object
 * @param {Response} [response] - Optional fetch Response object
 * @returns {{ type: string, message: string, color: string }}
 */
function classifyAPIError(error, response) {
  if (error?.name === "AbortError") {
    const path = error?.message?.match(/\/\S+/)?.[0] || "unknown";
    return {
      type: APIErrorType.TIMEOUT,
      message: API_ERROR_MESSAGES[APIErrorType.TIMEOUT](path),
      color: API_ERROR_COLORS[APIErrorType.TIMEOUT],
    };
  }
  if (error instanceof TypeError) {
    // Network errors (messages differ by browser: Chrome "Failed to fetch", Firefox "NetworkError when attempting to fetch resource")
    return {
      type: APIErrorType.NETWORK,
      message: API_ERROR_MESSAGES[APIErrorType.NETWORK](),
      color: API_ERROR_COLORS[APIErrorType.NETWORK],
    };
  }
  if (response) {
    if (response.status === 429) {
      return {
        type: APIErrorType.RATE_LIMIT,
        message: API_ERROR_MESSAGES[APIErrorType.RATE_LIMIT](),
        color: API_ERROR_COLORS[APIErrorType.RATE_LIMIT],
      };
    }
    if (response.status === 403) {
      return {
        type: APIErrorType.FORBIDDEN,
        message: API_ERROR_MESSAGES[APIErrorType.FORBIDDEN](),
        color: API_ERROR_COLORS[APIErrorType.FORBIDDEN],
      };
    }
    if (response.status >= 500) {
      return {
        type: APIErrorType.SERVER_ERROR,
        message: API_ERROR_MESSAGES[APIErrorType.SERVER_ERROR](),
        color: API_ERROR_COLORS[APIErrorType.SERVER_ERROR],
      };
    }
    return {
      type: APIErrorType.HTTP_ERROR,
      message: API_ERROR_MESSAGES[APIErrorType.HTTP_ERROR](
        response.status,
        response.url?.replace(/^.*\/\/.*?\//, "/") || "",
      ),
      color: API_ERROR_COLORS[APIErrorType.HTTP_ERROR],
    };
  }
  if (error instanceof SyntaxError) {
    return {
      type: APIErrorType.PARSE_FAIL,
      message: API_ERROR_MESSAGES[APIErrorType.PARSE_FAIL](),
      color: API_ERROR_COLORS[APIErrorType.PARSE_FAIL],
    };
  }
  return {
    type: APIErrorType.UNKNOWN,
    message: API_ERROR_MESSAGES[APIErrorType.UNKNOWN](error),
    color: API_ERROR_COLORS[APIErrorType.UNKNOWN],
  };
}

async function apiFetch(url, options = {}, behaviors = {}) {
  const showToastOnError = behaviors.showToast !== false;
  // csrfFetch は utils.js で定義されており、CSRF トークン注入を行う
  const csrfOptions = { ...options };
  let response;
  try {
    response = await csrfFetch(url, csrfOptions);
  } catch (error) {
    const classified = classifyAPIError(error);
    $logger.error(
      `[apiFetch] ${classified.type}: ${classified.message}`,
      error,
    );
    if (showToastOnError) showToast(classified.message, classified.color);
    throw Object.assign(new Error(classified.message), {
      type: classified.type,
    });
  }
  if (!response.ok) {
    let errorBody;
    try {
      errorBody = await response.json().catch(() => null);
    } catch (_e) {
      errorBody = null;
    }
    const errorMessage =
      errorBody?.error || errorBody?.message || `HTTP ${response.status}`;
    const classified = classifyAPIError(null, response);
    const enhancedMessage = `${classified.message}${errorBody?.details?.reason ? `（${errorBody.details.reason}）` : ""}`;
    $logger.error(
      `[apiFetch] ${classified.type} ${response.status}: ${errorMessage}`,
    );
    if (showToastOnError) showToast(enhancedMessage, classified.color);
    const err = new Error(enhancedMessage);
    err.type = classified.type;
    err.status = response.status;
    err.response = response;
    throw err;
  }
  let data;
  try {
    data = await response.json();
  } catch (error) {
    const classified = classifyAPIError(error);
    $logger.error(
      `[apiFetch] ${classified.type}: ${classified.message}`,
      error,
    );
    if (showToastOnError) showToast(classified.message, classified.color);
    throw Object.assign(new Error(classified.message), {
      type: classified.type,
    });
  }
  return { response, data };
}

// #endregion Unified API Error Handler

// #region SSE & Real-time Integration
/**
 * SSE and polling real-time communication manager.
 * Wraps APIClient for SSE lifecycle management.
 */
// Single SSE client. connectSSE() (below) is the only SSE entry point and it
// opens the connection via sseApiClient.openSSE with autoReconnect:false, so
// application-level reconnection is owned solely by connectSSE — there is no
// second/competing reconnect manager. (M-7: removed the unused sseManager
// wrapper that duplicated the openSSE path to avoid confusion / drift.)
const sseApiClient = new APIClient("/api");

/**
 * Maximum time (ms) to keep showing skeletons before falling back to a
 * timeout/error state when no stock data has arrived. Referenced as a bare
 * global from both api.js and index_main.js, so it must be a module-level
 * const (not a property of sseState).
 */
const INITIAL_SKELETON_MAX_WAIT_MS = 8000;

/**
 * Namespace for all SSE connection lifecycle state.
 * Previously these were loose module-level vars scattered across api.js and
 * index_main.js. Grouping them here improves discoverability and makes the
 * coupling between files more explicit. A future refactor should encapsulate
 * this in a proper class with methods.
 */
const sseState = {
  /** @type {EventSource|null} */
  stockEventSource: null,
  reconnectAttempts: 0,
  /** @type {number|null} */
  reconnectTimer: null,
  /** @type {number|null} */
  fallbackPolling: null,
  disconnectedSince: 0,
  lastNotifyAt: 0,
  skeletonShownAt: 0,
};

// M-5: SSE connection state is managed exclusively through sseState.
// All code should read/write sseState properties directly.
// Backward-compatible let aliases have been removed in favor of sseState.

function setStreamingIndicatorText(text) {
  const btn = DOM.get("streamToggleBtn");
  const label = btn?.querySelector(".stream-text");
  if (label) label.textContent = text;
}

function handleYfinanceRateLimitStatus(isLimited) {
  if (isLimited !== undefined) {
    const apiStatus = DOM.get("apiStatus");
    if (isLimited && !state.isYfinanceRateLimited) {
      state.isYfinanceRateLimited = true;
      showToast(
        "⚠️ Yahoo Financeのアクセス制限を検知しました。UAをローテーションして待機中です。(約60秒後に自動再試行されます)",
        "#ffcc66",
      );
      setStreamingIndicatorText("Streaming Paused (Rate Limited)");
      if (apiStatus) {
        apiStatus.textContent = "● Data Limited";
        apiStatus.style.color = "var(--acc-orange)";
      }
    } else if (!isLimited && state.isYfinanceRateLimited) {
      state.isYfinanceRateLimited = false;
      showToast(
        "✅ Yahoo Financeのアクセス制限が解除されました。更新を再開します。",
        "#7dffb0",
      );
      setStreamingIndicatorText(
        state.isStreaming ? "Live Streaming" : "Streaming Paused (60s polling)",
      );
      if (apiStatus) {
        apiStatus.style.color = ""; // Clear inline color so CSS classes can style it
        if (typeof updateApiStatus === "function") {
          updateApiStatus();
        } else {
          apiStatus.textContent = "● AI Ready";
        }
      }
    }
  }
}

function startSseFallbackPolling() {
  if (sseState.fallbackPolling) return;
  sseState.fallbackPolling = setInterval(() => {
    fetchInitialStocks();
  }, 30000);
}

function stopSseFallbackPolling() {
  if (!sseState.fallbackPolling) return;
  clearInterval(sseState.fallbackPolling);
  sseState.fallbackPolling = null;
}

const INDEX_BAR_CONFIG = [
  { label: "日経平均", key: "N225" },
  { label: "NYダウ", key: "DJI" },
  { label: "ドル円", key: "USDJPY" },
  { label: "ユーロ円", key: "EURJPY" },
  { label: "NASDAQ", key: "NASDAQ" },
  { label: "S&P500", key: "SP500" },
  { label: "VIX", key: "VIX" },
];

const formatIndexNumber = (value) =>
  value != null ? Number(value).toLocaleString() : "--";

function buildIndexChip(label, key) {
  const chip = document.createElement("span");
  chip.className = "index-chip";
  chip.dataset.indexKey = key;

  const strong = document.createElement("strong");
  strong.textContent = label;
  chip.appendChild(strong);

  chip.appendChild(createEl("span", "index-price", "--"));
  chip.appendChild(createEl("span", "index-change", "--"));

  // Event listeners for global tooltip
  chip.addEventListener("mouseenter", (e) => showIndexTooltip(e, key));
  chip.addEventListener("mousemove", (e) => moveIndexTooltip(e));
  chip.addEventListener("mouseleave", () => hideIndexTooltip());

  return chip;
}
function showIndexTooltip(event, key) {
  const tooltip = document.getElementById("indices-tooltip");
  const idx = state.indices[key];
  if (!tooltip || !idx) return;

  tooltip.textContent = "";
  const rows = [
    { label: "始値:", value: formatIndexNumber(idx.open), cls: "index-open" },
    { label: "高値:", value: formatIndexNumber(idx.high), cls: "index-high" },
    { label: "安値:", value: formatIndexNumber(idx.low), cls: "index-low" },
    {
      label: "出来高:",
      value: formatIndexNumber(idx.volume),
      cls: "index-volume",
    },
  ];
  for (const row of rows) {
    const div = document.createElement("div");
    div.className = "tooltip-row";
    const labelSpan = document.createElement("span");
    labelSpan.textContent = row.label;
    const valueSpan = document.createElement("span");
    valueSpan.className = row.cls;
    valueSpan.textContent = row.value;
    div.append(labelSpan, valueSpan);
    tooltip.appendChild(div);
  }
  tooltip.classList.add("show");
  moveIndexTooltip(event);
}

function moveIndexTooltip(event) {
  const tooltip = document.getElementById("indices-tooltip");
  if (!tooltip || !tooltip.classList.contains("show")) return;

  const rect = tooltip.getBoundingClientRect();
  const x = event.clientX - rect.width / 2;
  const y = event.clientY + 25; // 25px below the cursor to avoid overlap

  tooltip.style.left = `${Math.max(10, Math.min(x, window.innerWidth - rect.width - 10))}px`;
  tooltip.style.top = `${Math.min(y, window.innerHeight - rect.height - 10)}px`;
}

function hideIndexTooltip() {
  const tooltip = document.getElementById("indices-tooltip");
  if (tooltip) tooltip.classList.remove("show");
}

function ensureIndicesBarStructure(bar) {
  if (!bar || bar.dataset.initialized === "true") return;
  const fragment = document.createDocumentFragment();
  for (let copy = 0; copy < 2; copy++) {
    INDEX_BAR_CONFIG.forEach(({ label, key }) => {
      fragment.appendChild(buildIndexChip(label, key));
    });
  }
  bar.replaceChildren(fragment);
  bar.dataset.initialized = "true";
}

function updateSingleIndexChip(chip, idx) {
  if (!chip || !idx) return;
  const changeNum = Number(idx.change) || 0;
  const cls = changeNum >= 0 ? "pos" : "neg";
  const sign = changeNum >= 0 ? "+" : "";
  const priceEl = chip.querySelector(".index-price");
  const changeEl = chip.querySelector(".index-change");
  const openEl = chip.querySelector(".index-open");
  const highEl = chip.querySelector(".index-high");
  const lowEl = chip.querySelector(".index-low");
  const volEl = chip.querySelector(".index-volume");

  if (priceEl) {
    const nextPrice = formatIndexNumber(idx.price);
    const oldPriceStr = priceEl.textContent;
    if (oldPriceStr !== nextPrice) {
      priceEl.textContent = nextPrice;
      if (oldPriceStr !== "--") {
        const oldP = parseFloat(oldPriceStr.replace(/,/g, ""));
        const newP = parseFloat(nextPrice.replace(/,/g, ""));
        if (!isNaN(oldP) && !isNaN(newP) && oldP !== newP) {
          const flashCls = newP > oldP ? "flash-up" : "flash-down";
          priceEl.classList.remove("flash-up", "flash-down");
          void priceEl.offsetWidth;
          priceEl.classList.add(flashCls);
        }
      }
    }
    // Live update indicator
    priceEl.classList.add("updating");
    if (priceEl.__updateTimer) clearTimeout(priceEl.__updateTimer);
    priceEl.__updateTimer = setTimeout(
      () => priceEl.classList.remove("updating"),
      600,
    );
  }
  if (changeEl) {
    const changeText = idx.change != null ? idx.change : "--";
    const pctText = idx.percent != null ? idx.percent : "--";
    changeEl.className = `index-change ${cls}`;
    changeEl.textContent = `${sign}${changeText} (${sign}${pctText}%)`;
  }
  if (openEl) openEl.textContent = formatIndexNumber(idx.open);
  if (highEl) highEl.textContent = formatIndexNumber(idx.high);
  if (lowEl) lowEl.textContent = formatIndexNumber(idx.low);
  if (volEl) volEl.textContent = formatIndexNumber(idx.volume);
}

function updateIndicesBar(indices) {
  if (!indices) return;
  state.updateIndices(indices);
  const bar = DOM.get("indices-bar");
  if (!bar) return;
  ensureIndicesBarStructure(bar);

  INDEX_BAR_CONFIG.forEach(({ key }) => {
    const idx = indices[key];
    if (!idx) return;
    bar
      .querySelectorAll(`.index-chip[data-index-key="${key}"]`)
      .forEach((chip) => updateSingleIndexChip(chip, idx));
  });
}

function mergeStocksWithExistingHistory(
  nextData,
  existingData,
  isDiff = false,
) {
  const chooseHistorySeries = (incomingSeries, prevSeries) => {
    const incoming = Array.isArray(incomingSeries) ? incomingSeries : [];
    const prev = Array.isArray(prevSeries) ? prevSeries : [];
    if (incoming.length === 0) return prev;
    if (prev.length === 0) return incoming;
    // SSE軽量ペイロード（短い履歴）で長い履歴を上書きしない
    return incoming.length >= prev.length ? incoming : prev;
  };

  const merged = { us: [], jp: [], idx: [] };
  ["us", "jp", "idx"].forEach((market) => {
    const prevMap = new Map(
      (existingData?.[market] || []).map((s) => [s.symbol, s]),
    );
    const incoming = nextData?.[market] || [];
    const rows = isDiff ? [...(existingData?.[market] || [])] : incoming;
    const rowMap = new Map(rows.map((s) => [s.symbol, s]));
    incoming.forEach((s) => {
      if (s?._removed) {
        rowMap.delete(s.symbol);
        return;
      }
      const prev = prevMap.get(s.symbol) || {};
      const chartData = chooseHistorySeries(s.chart_data, prev.chart_data);
      const ohlcData = chooseHistorySeries(s.ohlc_data, prev.ohlc_data);
      rowMap.set(s.symbol, {
        ...prev,
        ...s,
        market,
        chart_data: Array.isArray(chartData) ? chartData : [],
        ohlc_data: Array.isArray(ohlcData) ? ohlcData : [],
      });
    });
    merged[market] = [...rowMap.values()];
  });
  return merged;
}

/**
 * Establish SSE (Server-Sent Events) connection for real-time stock data.
 * Falls back to periodic polling when streaming is disabled.
 * Delegates actual SSE management to APIClient for heartbeat monitoring
 * and exponential-backoff reconnection.
 */
function connectSSE() {
  // H-3: Always stop fallback polling first to prevent SSE + polling race condition.
  // This must happen before any SSE connection attempt to guarantee only one
  // data-fetching mechanism is active at a time.
  stopSseFallbackPolling();
  pollingManager.clearInterval("fallback-polling");
  if (sseState.reconnectTimer) {
    clearTimeout(sseState.reconnectTimer);
    sseState.reconnectTimer = null;
  }

  if (sseApiClient.currentEventSource) {
    sseApiClient.closeSSE();
    sseState.stockEventSource = null;
  }

  if (!state.isStreaming) {
    $logger.info("Streaming is disabled. Switching to 60s background polling.");
    setStreamingIndicatorText("Streaming Paused (60s polling)");
    stopSseFallbackPolling();
    pollingManager.setInterval("fallback-polling", fetchInitialStocks, 60000);
    return;
  }

  setStreamingIndicatorText(
    sseState.reconnectAttempts > 0 ? "Reconnecting..." : "Live Streaming",
  );

  if (state.stocks.us.length === 0 && state.stocks.jp.length === 0) {
    renderSkeletons();
  }

  /**
   * Process incoming SSE data: update state, re-render UI.
   * @param {Object} data - Parsed SSE payload
   */
  const processSseData = (data) => {
    try {
      handleYfinanceRateLimitStatus(data.is_yfinance_rate_limited);

      // Reset reconnect state on successful message
      if (
        sseState.reconnectAttempts > 0 ||
        sseApiClient.sseReconnectAttempt > 0
      ) {
        sseState.reconnectAttempts = 0;
        sseApiClient.sseReconnectAttempt = 0;
        sseState.disconnectedSince = 0;
        stopSseFallbackPolling();
        setStreamingIndicatorText("Live Streaming");
      }

      if (document.hidden) {
        // When tab is hidden, update state only (no UI re-render)
        if (data.stocks)
          state.updateStocks(
            mergeStocksWithExistingHistory(
              data.stocks,
              state.stocks,
              data.stream_event === "diff",
            ),
          );
        if (data.indices) state.updateIndices(data.indices);
        return;
      }

      if (data.stocks) {
        updateStocksFromSseData(data);
      }
      if (data.indices) {
        updateIndicesBar(data.indices);
      }
    } catch (e) {
      $logger.error("SSE message processing error:", e);
    }
  };

  /**
   * Handle SSE connection errors with fallback polling and scheduled reconnect.
   * @param {Error} error
   */
  const handleSseError = (error) => {
    $logger.error("SSE error:", error);
    if (!state.isStreaming) return;

    if (!sseState.disconnectedSince) sseState.disconnectedSince = Date.now();

    // H-6: Start fallback polling first to ensure continuous data flow,
    // while APIClient schedules SSE reconnection in the background.
    startSseFallbackPolling();

    const now = Date.now();
    if (now - sseState.lastNotifyAt > 20000) {
      showToast(
        "⚠️ リアルタイム配信が一時切断されました。再接続を試行中です",
        "#ffcc66",
      );
      sseState.lastNotifyAt = now;
    }
  };

  // Let APIClient manage heartbeat monitoring and auto reconnection;
  sseState.stockEventSource = sseApiClient.openSSE(
    "/stocks/stream",
    processSseData,
    handleSseError,
    {
      autoReconnect: true,
      maxReconnectAttempts: 7,
      onReconnect: (es) => {
        sseState.stockEventSource = es;
        sseState.reconnectAttempts = sseApiClient.sseReconnectAttempt;
        if (sseState.reconnectAttempts > 0) {
          setStreamingIndicatorText(
            `Reconnecting... (${sseState.reconnectAttempts})`,
          );
        }
      },
    },
  );
}

/**
 * Update stock cards from SSE data payload (differential update for performance).
 * @param {Object} data - SSE payload with stocks.us, stocks.jp, stocks.idx
 */
function updateStocksFromSseData(data) {
  const isInitialSnapshot = data.stream_event === "initial_snapshot";
  const incomingData = {
    us: (data.stocks.us || []).map((s) => ({
      ...s,
      market: "us",
      __live_update: !isInitialSnapshot,
    })),
    jp: (data.stocks.jp || []).map((s) => ({
      ...s,
      market: "jp",
      __live_update: !isInitialSnapshot,
    })),
    idx: (data.stocks.idx || []).map((s) => ({
      ...s,
      market: "idx",
      __live_update: !isInitialSnapshot,
    })),
  };
  const nextData = mergeStocksWithExistingHistory(
    incomingData,
    state.stocks,
    data.stream_event === "diff",
  );

  const hasSkeleton = document.querySelector(".skeleton-card") !== null;
  const hasAnyCards = document.querySelectorAll(".stock-wrapper").length > 0;
  const incomingCount =
    nextData.us.length + nextData.jp.length + nextData.idx.length;

  state.updateStocks(nextData);

  // Handle empty initial payload: keep skeleton display
  if (incomingCount === 0 && hasSkeleton && !hasAnyCards) {
    if (
      sseState.skeletonShownAt &&
      Date.now() - sseState.skeletonShownAt > INITIAL_SKELETON_MAX_WAIT_MS
    ) {
      renderInitialLoadingTimeoutState();
      sseState.skeletonShownAt = 0;
    }
    return;
  }
  if (incomingCount > 0) {
    sseState.skeletonShownAt = 0;
  }

  const shouldFullRender = hasSkeleton || !hasAnyCards;
  if (shouldFullRender) {
    renderStocks("us", state.stocks.us);
    renderStocks("jp", state.stocks.jp);
    renderStocks("idx", state.stocks.idx);
  } else {
    // Differential update: only update changed cards
    ["us", "jp", "idx"].forEach((market) => {
      (state.stocks[market] || []).forEach((stock) => {
        const stockKey = makeStockKey(market, stock.symbol);
        const lastTs = stockHashMap.get(stockKey);
        const currentTs =
          stock.snapshot_ts_ms ||
          stock.price +
            "|" +
            stock.change +
            "|" +
            (stock.chart_data || []).length;
        if (lastTs === currentTs) return;
        stockHashMap.set(stockKey, currentTs);
        findAllWrappersByStockKey(stockKey).forEach((wrapper) =>
          updateExistingCard(wrapper, stock),
        );
      });
    });
  }

  const activeTab = document.querySelector(".tab.active")?.id;
  if (activeTab === "tab-portfolio") debouncedRenderPortfolio();
}

let _loadIndicesInterval = null;

async function loadIndicesLoop() {
  if (_loadIndicesInterval) return;
  const fetchIndices = async () => {
    try {
      const res = await fetch("/api/indices");
      if (!res.ok) throw new Error("Fetch failed");
      const data = await res.json();
      updateIndicesBar(data);
    } catch (e) {
      $logger.warn("Index fetch error:", e);
    }
  };
  fetchIndices();
  _loadIndicesInterval = setInterval(fetchIndices, 30000);
}

function stopLoadIndicesLoop() {
  if (_loadIndicesInterval) {
    clearInterval(_loadIndicesInterval);
    _loadIndicesInterval = null;
  }
}

window.addEventListener("beforeunload", () => {
  stopLoadIndicesLoop();
  stopSseFallbackPolling();
  pollingManager.clearAll();
});

// #endregion SSE & Real-time Integration

// =============================================
// News & Trends — Extracted Helper Functions
// =============================================

function _normalizeNewsSectionContent(raw, sectionKey) {
  let text = String(raw || "").trim();
  if (!text) return "";
  text = text
    .replace(/^```[a-zA-Z0-9_-]*\s*\n?/, "")
    .replace(/\n?```$/, "")
    .trim();

  try {
    const parsed = JSON.parse(text);
    if (
      parsed &&
      typeof parsed === "object" &&
      !Array.isArray(parsed) &&
      sectionKey in parsed
    ) {
      return parsed[sectionKey];
    }
    return parsed;
  } catch (_) {
    return text;
  }
}

function _isMetadataLine(line) {
  return /^(?:source|date|url)\s*:/i.test(String(line || "").trim());
}

function _isNoiseLine(line) {
  const s = String(line || "").trim();
  if (!s) return true;
  const lower = s.toLowerCase();
  if (_isMetadataLine(s)) return true;
  if (lower.startsWith("http://") || lower.startsWith("https://")) return true;
  if (lower.includes("news.google.com/rss/articles")) return true;
  if (/<[^>]+>/.test(s)) return true;
  if (/(?:<a\s|<li|<ol|<ul)/i.test(s)) return true;
  return false;
}

function _flattenStructuredItem(item) {
  if (item == null) return "";
  if (typeof item === "string") return item.trim();
  if (typeof item === "number" || typeof item === "boolean")
    return String(item);
  if (Array.isArray(item)) {
    return item.map(_flattenStructuredItem).filter(Boolean).join(" / ");
  }
  if (typeof item === "object") {
    const topic = String(item.topic || item.title || "").trim();
    const summary = String(item.summary || item.description || "").trim();
    const impact =
      item.market_impact && typeof item.market_impact === "object"
        ? Object.entries(item.market_impact)
            .map(([k, v]) => `${k}: ${String(v || "").trim()}`)
            .filter((x) => x && !x.endsWith(": "))
            .join(" | ")
        : "";
    const parts = [topic, summary, impact].filter(Boolean);
    if (parts.length) return parts.join(" - ");
    return Object.entries(item)
      .map(([k, v]) => `${k}: ${String(v || "").trim()}`)
      .filter((x) => x && !x.endsWith(": "))
      .join(" | ");
  }
  return "";
}

function _parseNewsItems(raw) {
  if (Array.isArray(raw)) {
    return raw
      .map(_flattenStructuredItem)
      .filter(Boolean)
      .filter((x) => !_isNoiseLine(x));
  }
  if (raw && typeof raw === "object") {
    const values = Object.values(raw)
      .map(_flattenStructuredItem)
      .filter(Boolean)
      .filter((x) => !_isNoiseLine(x));
    if (values.length) return values;
    return [];
  }

  let text = String(raw || "").trim();
  if (!text) return [];

  text = text
    .replace(/^```[a-zA-Z0-9_-]*\s*\n?/, "")
    .replace(/\n?```$/, "")
    .trim();

  if (!text) return [];

  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed
        .map(_flattenStructuredItem)
        .filter(Boolean)
        .filter((x) => !_isNoiseLine(x));
    }
    if (parsed && typeof parsed === "object") {
      const values = Object.values(parsed)
        .map(_flattenStructuredItem)
        .filter(Boolean)
        .filter((x) => !_isNoiseLine(x));
      if (values.length) return values;
    }
  } catch (_) {}

  if (text.startsWith("[") && text.endsWith("]")) {
    const inner = text.slice(1, -1).trim();
    const split = inner
      .split(/'\s*,\s*'|"\s*,\s*"|」\s*,\s*「/g)
      .map((x) => x.replace(/^['"「\s]+|['"」\s]+$/g, "").trim())
      .filter(Boolean);
    if (split.length > 1) return split;
    text = inner.replace(/^['"「\s]+|['"」\s]+$/g, "").trim();
  }

  const lines = text
    .split(/\n+|\s*[•▪]\s*/g)
    .map((x) => x.replace(/^[-*]\s+|^\d+[.)]\s+/, "").trim())
    .map((x) => x.replace(/^\[\d+\]\s*/, "").trim())
    .map((x) => x.replace(/^summary\s*:\s*/i, "").trim())
    .map((x) =>
      x
        .replace(
          /^"(?:topic|summary|details|market_impact|title|description)"\s*:\s*/,
          "",
        )
        .trim(),
    )
    .map((x) => x.replace(/^"|"$/g, "").trim())
    .filter((x) => !_isNoiseLine(x))
    .filter((x) => !/^[\[{\]}]$/.test(x))
    .filter(Boolean);
  if (lines.length) return lines;

  if (
    /^[\s\[{]/.test(text) ||
    /"(?:us|jp|trends|topic|summary|details|market_impact)"\s*:/.test(text)
  ) {
    return [];
  }
  return _isNoiseLine(text) ? [] : [text];
}

function _ensureMinimumNewsLines(items, minLines = 5) {
  const normalized = [];
  const seen = new Set();
  items.forEach((line) => {
    const s = String(line || "").trim();
    if (!s) return;
    if (/(?:<a\s|<li|<ol|<ul|<[^>]+>)/i.test(s)) return;
    if (/^https?:\/\//i.test(s) || /news\.google\.com\/rss\/articles/i.test(s))
      return;
    if (/^(?:source|date|url)\s*:/i.test(s)) return;
    if (seen.has(s)) return;
    seen.add(s);
    normalized.push(s);
  });
  return normalized;
}

function _renderNewsContent(el, content, sectionKey) {
  if (!el) return;
  const normalizedContent = _normalizeNewsSectionContent(content, sectionKey);
  const parsedItems = _parseNewsItems(normalizedContent);
  const items = _ensureMinimumNewsLines(parsedItems, 5).slice(0, 12);
  if (!items.length) {
    el.textContent = "情報を取得できませんでした";
    return { displayCount: 0, parsedCount: parsedItems.length };
  }
  if (items.length === 1) {
    el.textContent = items[0];
    return { displayCount: 1, parsedCount: parsedItems.length };
  }
  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    const lineDiv = document.createElement("div");
    lineDiv.className = "news-line";

    const bulletSpan = document.createElement("span");
    bulletSpan.className = "news-bullet";
    bulletSpan.textContent = "•";

    const textSpan = document.createElement("span");
    textSpan.textContent = item;

    lineDiv.appendChild(bulletSpan);
    lineDiv.appendChild(textSpan);
    fragment.appendChild(lineDiv);
  });
  el.textContent = "";
  el.appendChild(fragment);
  return { displayCount: items.length, parsedCount: parsedItems.length };
}

function _getStatusBadge(status) {
  const badges = {
    success: "✓",
    empty: "◉",
    error: "✗",
    timeout: "⏱",
    pending: "⏳",
    unknown: "?",
  };
  const colors = {
    success: "#27ae60",
    empty: "#f39c12",
    error: "#e74c3c",
    timeout: "#E67E22",
    pending: "#95a5a6",
    unknown: "#95a5a6",
  };
  return { badge: badges[status] || "?", color: colors[status] || "#666" };
}

function _buildNewsMetaStatsEl(
  newsMetaStatsEl,
  usStats,
  jpStats,
  trStats,
  usStatus,
  jpStatus,
  trendsStatus,
  data,
) {
  if (!newsMetaStatsEl) return;
  const tagCount = Array.isArray(data.trending_raw)
    ? data.trending_raw.length
    : 0;
  const timestamp =
    data.us?.timestamp || data.jp?.timestamp || data.trends?.timestamp || "";
  let timeLabel = "--:--";
  if (timestamp) {
    const d = new Date(timestamp);
    if (!Number.isNaN(d.getTime())) {
      timeLabel = d.toLocaleTimeString("ja-JP", {
        hour: "2-digit",
        minute: "2-digit",
      });
    }
  }
  newsMetaStatsEl.textContent = "";
  const outerSpan = document.createElement("span");
  outerSpan.style.cssText = "display:inline-flex;gap:8px;align-items:center;";

  const countSpan = document.createElement("span");
  countSpan.textContent = `表示 US:${usStats.displayCount}件 JP:${jpStats.displayCount}件 TR:${trStats.displayCount}件`;
  outerSpan.appendChild(countSpan);

  const badgeSpan = document.createElement("span");
  badgeSpan.style.cssText = "border-left:1px solid #ddd;padding-left:8px;";
  const usBadge = document.createElement("span");
  usBadge.style.cssText = `color:${usStatus.color};font-weight:bold;`;
  usBadge.textContent = `US${usStatus.badge}`;
  const jpBadge = document.createElement("span");
  jpBadge.style.cssText = `color:${jpStatus.color};font-weight:bold;`;
  jpBadge.textContent = `JP${jpStatus.badge}`;
  const trBadge = document.createElement("span");
  trBadge.style.cssText = `color:${trendsStatus.color};font-weight:bold;`;
  trBadge.textContent = `TR${trendsStatus.badge}`;
  badgeSpan.appendChild(usBadge);
  badgeSpan.appendChild(document.createTextNode(" "));
  badgeSpan.appendChild(jpBadge);
  badgeSpan.appendChild(document.createTextNode(" "));
  badgeSpan.appendChild(trBadge);
  outerSpan.appendChild(badgeSpan);

  const timeSpan = document.createElement("span");
  timeSpan.style.cssText = "border-left:1px solid #ddd;padding-left:8px;";
  timeSpan.textContent = `更新: ${timeLabel}`;
  outerSpan.appendChild(timeSpan);

  newsMetaStatsEl.appendChild(outerSpan);
}

// #region News & Trends
async function loadNews(forceRefresh = false) {
  if (state.isLoadingNews || !HAS_MISTRAL_API_KEY) {
    if (!HAS_MISTRAL_API_KEY) showToast("❌ APIキーが未設定です", "#ff7d7d");
    return;
  }
  const usBox = DOM.get("news-us");
  const jpBox = DOM.get("news-jp");
  const trendsBox = DOM.get("news-trends");
  const refreshBtn = DOM.get("newsRefreshBtn");
  const newsMetaStatsEl = DOM.get("news-meta-stats");
  const newsUrl = forceRefresh ? "/api/news?force=true" : "/api/news";

  state.isLoadingNews = true;
  setButtonLoading(refreshBtn, "検索中...");
  usBox?.classList.remove("show");
  jpBox?.classList.remove("show");
  trendsBox?.classList.remove("show");
  if (usBox) usBox.textContent = "最新情報を検索・分析中...";
  if (jpBox) jpBox.textContent = "最新情報を検索・分析中...";
  if (trendsBox) trendsBox.textContent = "最新情報を検索・分析中...";
  if (newsMetaStatsEl) newsMetaStatsEl.textContent = "表示件数: 取得中...";

  let timeoutId = null;
  try {
    const headers = {
      "Content-Type": "application/json",
    };

    const newsRequestController = new AbortController();
    timeoutId = setTimeout(() => {
      newsRequestController.abort();
    }, CONSTANTS.TIMEOUT.NEWS_REQUEST);

    const { response: res, data } = await apiFetch(newsUrl, {
      method: "POST",
      headers,
      signal: newsRequestController.signal,
    });

    if (!res.ok) {
      const errorData = data || {};
      throw new APIError(
        res.status,
        errorData.error_code || 9999,
        errorData.message || `HTTP ${res.status}`,
        errorData.details,
      );
    }

    // data は apiFetch が既にパース済み（{response, data} の data）。
    // バックグラウンドで生成中の場合は fetching:true が返る。
    // クライアント側でバックオフ付き再試行し、完了後に描画する。
    if (data && data.fetching) {
      const maxAttempts = 12;
      let attempt = 0;
      let finished = false;
      while (attempt < maxAttempts) {
        attempt += 1;
        const backoff = Math.min(1500 * attempt, 9000);
        await new Promise((resolve) => setTimeout(resolve, backoff));
        const poll = await apiFetch("/api/news", { method: "POST", headers });
        if (!poll.response.ok) break;
        const pollData = poll.data;
        if (pollData && !pollData.fetching) {
          Object.assign(data, pollData);
          finished = true;
          break;
        }
      }
      if (!finished) {
        throw new Error(
          "ニュース要約の生成がタイムアウトしました。しばらくしてからページを再読み込みしてください。",
        );
      }
    }

    if (data.error) {
      throw new APIError(
        400,
        data.error_code || 9999,
        data.error,
        data.details,
      );
    }

    const retrieveStatus = data.retrieve_status || {
      us: data.us?.status || "success",
      jp: data.jp?.status || "success",
      trends: data.trends?.status || "success",
    };

    const usStatus = _getStatusBadge(retrieveStatus.us);
    const jpStatus = _getStatusBadge(retrieveStatus.jp);
    const trendsStatus = _getStatusBadge(retrieveStatus.trends);

    const usStats = _renderNewsContent(usBox, data.us?.content, "us") || {
      displayCount: 0,
      parsedCount: 0,
    };
    const jpStats = _renderNewsContent(jpBox, data.jp?.content, "jp") || {
      displayCount: 0,
      parsedCount: 0,
    };
    const trStats = _renderNewsContent(
      trendsBox,
      data.trends?.content,
      "trends",
    ) || {
      displayCount: 0,
      parsedCount: 0,
    };

    // トレンドバッジの同期更新
    if (data.trending_raw && Array.isArray(data.trending_raw)) {
      renderTrendingBadges(data.trending_raw);
    }

    _buildNewsMetaStatsEl(
      newsMetaStatsEl,
      usStats,
      jpStats,
      trStats,
      usStatus,
      jpStatus,
      trendsStatus,
      data,
    );

    requestAnimationFrame(() => {
      usBox?.classList.add("show");
      jpBox?.classList.add("show");
      trendsBox?.classList.add("show");
    });
  } catch (e) {
    $logger.error("News error:", e);
    const message =
      e?.name === "AbortError"
        ? "ニュース取得がタイムアウトしました。一部の情報が表示されない可能性があります。"
        : `ニュース取得エラー: ${e.message}`;
    $logger.warn(message);

    if (newsMetaStatsEl) {
      newsMetaStatsEl.textContent = "";
      if (e?.name === "AbortError") {
        const timeoutSpan = document.createElement("span");
        timeoutSpan.style.cssText = "color:#E67E22;font-weight:bold;";
        timeoutSpan.textContent = "⏱ タイムアウト: 部分結果を表示しています";
        newsMetaStatsEl.appendChild(timeoutSpan);
      } else {
        newsMetaStatsEl.textContent = "表示件数: 取得失敗";
      }
    }

    if (e?.name !== "AbortError") {
      showToast(message, "#ff7d7d");
      if (usBox) {
        usBox.textContent = `エラー: ${e.message}`;
        usBox.classList.add("show");
      }
      if (jpBox) {
        jpBox.textContent = "情報取得失敗";
        jpBox.classList.add("show");
      }
      if (trendsBox) {
        trendsBox.textContent = "情報取得失敗";
        trendsBox.classList.add("show");
      }
    } else {
      requestAnimationFrame(() => {
        usBox?.classList.add("show");
        jpBox?.classList.add("show");
        trendsBox?.classList.add("show");
      });
    }
  } finally {
    if (timeoutId) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    state.isLoadingNews = false;
    resetButton(refreshBtn);
  }
}

const forceRefreshNews = async () => {
  if (!state.isLoadingNews) await loadNews(true);
};

async function searchStocks() {
  const input = document.getElementById("searchInput");
  const q = input?.value.trim();
  const box = document.getElementById("search-results");
  const list = document.getElementById("search-results-list");

  if (!q || q.length < 2) {
    showToast("⚠️ 検索ワードは2文字以上入力してください", "#ffcc66");
    return;
  }
  if (box) box.style.display = "block";
  if (list) {
    list.textContent = "";
    list.appendChild(createEl("div", "no-results", "検索中..."));
  }
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();

    if (!res.ok) {
      if (list) {
        list.textContent = "";
        list.appendChild(
          createEl(
            "div",
            "no-results",
            `エラー: ${data?.error || data?.message || `HTTP ${res.status}`}`,
          ),
        );
      }
      return;
    }
    if (data.error) {
      if (list) {
        list.textContent = "";
        list.appendChild(
          createEl("div", "no-results", `エラー: ${data.error}`),
        );
      }
      return;
    }
    if (!data.results?.length) {
      if (list) {
        list.textContent = "";
        list.appendChild(
          createEl("div", "no-results", "該当する銘柄が見つかりませんでした。"),
        );
      }
      return;
    }
    if (list) list.textContent = "";
    data.results.forEach((item) => {
      const row = document.createElement("div");
      row.className = "search-result-item";

      const label = document.createElement("span");
      // L-8: Backend no longer provides a hardcoded fallback string.
      // Display "名称不明" here if the name field is missing.
      const displayName = item.name || "名称不明";
      label.textContent = `${item.symbol || ""} - ${displayName}`;
      row.appendChild(label);

      const exchange = document.createElement("span");
      exchange.textContent = item.exchange || "";
      row.appendChild(exchange);

      row.addEventListener("click", () =>
        addStockPrompt(item.symbol, item.name),
      );
      list?.appendChild(row);
    });
  } catch (e) {
    $logger.error("Search error:", e);
    if (list) {
      list.textContent = "";
      list.appendChild(
        createEl("div", "no-results", "検索中にエラーが発生しました。"),
      );
    }
  }
}

function addStockPrompt(symbol, name) {
  let activeTab = document.querySelector(".tab.active")?.id.replace("tab-", "");
  // ポートフォリオタブは市場ではないので、デフォルトで "us" を使う
  if (!activeTab || activeTab === "portfolio") activeTab = "us";
  const marketNames = { us: "米国", jp: "日本", idx: "インデックス/ETF" };
  const normalizedSymbol = normalizeSymbolForMarketClient(symbol, activeTab);
  const normalizeNote =
    normalizedSymbol !==
    String(symbol || "")
      .trim()
      .toUpperCase()
      ? `\n\n※ 日本株コードとして ${normalizedSymbol} で登録します。`
      : "";
  if (
    confirm(
      `${symbol}（${name}）を${marketNames[activeTab]}タブに追加しますか？${normalizeNote}`,
    )
  ) {
    addStock(symbol, name, activeTab);
  }
}

const normalizeSymbolForMarketClient = (symbol, market) => {
  const s = String(symbol ?? "")
    .trim()
    .toUpperCase();
  if (market === "jp" && /^\d{4}$/.test(s)) return `${s}.T`;
  return s;
};

async function addStock(symbol, name, market) {
  const normalizedSymbol = normalizeSymbolForMarketClient(symbol, market);
  if (
    normalizedSymbol !==
    String(symbol || "")
      .trim()
      .toUpperCase()
  ) {
    showToast(
      `ℹ️ 日本株コードを ${normalizedSymbol} に補正して登録します`,
      "#6bb6ff",
    );
  }

  try {
    const { response: res, data } = await apiFetch("/api/stocks/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: normalizedSymbol, name, market }),
    });
    if (!res.ok || data.error) {
      showToast(`❌ 追加エラー: ${data.error || "不明なエラー"}`, "#ff7d7d");
      return;
    }
    const marketNames = { us: "米国", jp: "日本", idx: "インデックス/ETF" };
    showToast(
      `✅ ${normalizedSymbol} を ${marketNames[market]}市場に追加しました`,
      "#7dffb0",
    );
    setActiveTab(market);
    const resultBox = DOM.get("search-results");
    const searchInput = DOM.get("searchInput");
    if (resultBox) resultBox.style.display = "none";
    if (searchInput) searchInput.value = "";
    await fetchInitialStocks();
  } catch (e) {
    $logger.error("Add stock error:", e);
    showToast("❌ 通信エラーが発生しました", "#ff7d7d");
  }
}

// /api/chat が fetching:True を返した際のポーリング設定（バックグラウンドAI実行対応）
const CHAT_POLL_MAX_ATTEMPTS = 6; // CHAT_PREPARE_WAIT_SEC(8s) の約2.5倍までポーリング
const CHAT_POLL_INTERVAL_MS = 2000;

async function sendChat(wrapper) {
  const stockKey = wrapper.dataset.stockKey;
  const input = wrapper.querySelector(".chat-input");
  const log = wrapper.querySelector(".chat-log");
  const msg = input?.value.trim();
  if (!msg || !HAS_MISTRAL_API_KEY) return;

  const stock = getStockByKey(stockKey);
  const userDiv = document.createElement("div");
  userDiv.className = "chat-msg user";
  userDiv.textContent = msg;
  log.appendChild(userDiv);
  if (input) input.value = "";
  log.scrollTop = log.scrollHeight;

  const aiDiv = document.createElement("div");
  aiDiv.className = "chat-msg ai";
  aiDiv.textContent = "考え中...";
  log.appendChild(aiDiv);

  try {
    const payload = {
      symbol: stock?.symbol || stockKey,
      market: stock?.market || "us",
      message: msg,
    };
    let data = {};
    let resOk = false;
    // Mistral 呼び出しはバックグラウンドで非同期実行される場合がある。
    // fetching:True が返る場合は短い間隔でポーリングして完了を待つ。
    for (let attempt = 0; attempt <= CHAT_POLL_MAX_ATTEMPTS; attempt++) {
      const { response: res, data: fetched } = await apiFetch("/api/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      data = fetched || {};
      if (!res.ok) {
        const detailReason = data?.details?.reason
          ? String(data.details.reason)
          : "";
        const errMsg =
          detailReason ||
          String(data.message || data.error || `HTTP ${res.status}`);
        throw new Error(errMsg);
      }
      if (!data.fetching) {
        resOk = true;
        break;
      }
      await sleep(CHAT_POLL_INTERVAL_MS);
    }
    if (!resOk) {
      throw new Error("AI応答の生成がタイムアウトしました");
    }
    aiDiv.textContent = data.reply || "応答を取得できませんでした";
  } catch (e) {
    aiDiv.textContent = "通信エラーが発生しました";
    showToast("❌ チャット通信エラー: " + e.message, "#ff7d7d");
  }
  log.scrollTop = log.scrollHeight;
}

function applyAnalysisResult(wrapper, stock, data) {
  clearAnalysisError(wrapper);
  const stockKey = wrapper.dataset.stockKey;
  const aiSection = wrapper.querySelector(".ai-section");
  if (data.search_failed && aiSection) {
    const box = document.createElement("div");
    box.className = "ai-warning-banner";
    box.style.cssText =
      "margin:8px 0 12px;padding:8px 10px;border-radius:8px;background:rgba(230,126,34,0.14);border:1px solid rgba(230,126,34,0.45);color:#ffe5d9;font-size:0.82rem;";
    box.textContent =
      "⚠️ 最新ニュースの取得に失敗したため、基本財務データのみで分析しています。";
    const aiSlider = aiSection.querySelector(".ai-slider");
    aiSection.insertBefore(box, aiSlider || null);
  }
  const recEl = wrapper.querySelector(".ai-rec");
  const sentEl = wrapper.querySelector(".ai-sent");
  const targetEl = wrapper.querySelector(".ai-target");
  const upsideEl = wrapper.querySelector(".ai-upside");
  const catEl = wrapper.querySelector(".ai-cat");
  const riskEl = wrapper.querySelector(".ai-risk");

  // Retrieve previous state for diffing
  let prevData = null;
  try {
    prevData = JSON.parse(localStorage.getItem(`ai_prev_${stockKey}`));
  } catch (e) {}

  // Save new state
  localStorage.setItem(`ai_prev_${stockKey}`, JSON.stringify(data));

  // Determine diff logic
  const getDiffArrow = (prev, curr, goodVals, badVals) => {
    if (!prev || prev === curr) return null;
    if (goodVals.includes(curr) && badVals.includes(prev)) {
      return { text: "▲ 改善", color: "#7dffb0" };
    }
    if (badVals.includes(curr) && goodVals.includes(prev)) {
      return { text: "▼ 悪化", color: "#ff7d7d" };
    }
    return { text: "● 変化", color: "#ffcc66" };
  };

  const applyArrowToElement = (el, valText, arrowObj) => {
    if (!el) return;
    el.textContent = "";
    el.appendChild(document.createTextNode(valText ?? "--"));
    if (arrowObj) {
      const arrowSpan = document.createElement("span");
      arrowSpan.style.marginLeft = "5px";
      arrowSpan.style.color = arrowObj.color;
      arrowSpan.textContent = arrowObj.text;
      el.appendChild(arrowSpan);
    }
  };

  const recArrow = getDiffArrow(
    prevData?.recommendation,
    data.recommendation,
    ["強い買い", "買い"],
    ["強い売り", "売り", "中立"],
  );

  const sentArrow = getDiffArrow(
    prevData?.sentiment,
    data.sentiment,
    ["強気"],
    ["弱気", "中立"],
  );

  applyArrowToElement(recEl, data.recommendation, recArrow);
  applyArrowToElement(sentEl, data.sentiment, sentArrow);
  if (targetEl)
    targetEl.textContent =
      data.target_price_3m != null
        ? formatPrice(data.target_price_3m, stock)
        : "--";
  if (upsideEl) {
    const upside = data.upside_3m ?? "";
    upsideEl.textContent = upside ? `上昇余地: ${upside}` : "";
    const upsideNum = parseFloat(String(upside).replace("%", ""));
    if (!upside || !Number.isFinite(upsideNum) || upsideNum === 0) {
      upsideEl.style.color = "#9ca3af";
    } else {
      upsideEl.style.color =
        upside.includes("+") || upsideNum > 0 ? "#7dffb0" : "#ff7d7d";
    }
  }

  const catalystsText =
    Array.isArray(data.key_catalysts) && data.key_catalysts.length
      ? data.key_catalysts.join(" / ")
      : "--";
  if (catEl) catEl.textContent = catalystsText;

  const risksText =
    Array.isArray(data.risk_factors) && data.risk_factors.length
      ? data.risk_factors.join(" / ")
      : "--";
  if (riskEl) riskEl.textContent = risksText;

  // 確信度やニュース影響がある場合は追加カードとして表示するロジック（オプション）
  const aiSlider = recEl?.closest(".ai-slider");
  if (aiSlider && (data.confidence || data.latest_news_impact)) {
    // 既存のConfidence/Newsカードがあれば削除して再作成
    aiSlider.querySelectorAll(".ai-extra-card").forEach((c) => c.remove());

    // Analyzed At Card
    if (data.analyzed_at) {
      const dateCard = document.createElement("div");
      dateCard.className = "ai-card ai-extra-card";

      const dateTitle = document.createElement("div");
      dateTitle.className = "ai-card-title";
      dateTitle.textContent = "分析日時";

      const dateContent = document.createElement("div");
      dateContent.className = "ai-card-content";
      dateContent.style.fontSize = "0.85rem";
      dateContent.textContent = new Date(data.analyzed_at).toLocaleString();

      dateCard.appendChild(dateTitle);
      dateCard.appendChild(dateContent);
      aiSlider.appendChild(dateCard);
    }

    if (data.confidence) {
      const confCard = document.createElement("div");
      confCard.className = "ai-card ai-extra-card";

      const confTitle = document.createElement("div");
      confTitle.className = "ai-card-title";
      confTitle.textContent = "AI確信度";

      const confContent = document.createElement("div");
      confContent.className = "ai-card-content";
      confContent.textContent = data.confidence;

      const confLabel = document.createElement("div");
      confLabel.className = "ai-confidence-label";
      confLabel.textContent = "Intelligence Confidence";

      confCard.appendChild(confTitle);
      confCard.appendChild(confContent);
      confCard.appendChild(confLabel);
      aiSlider.appendChild(confCard);
    }

    if (data.latest_news_impact) {
      const newsCard = document.createElement("div");
      newsCard.className = "ai-card ai-extra-card";

      const newsTitle = document.createElement("div");
      newsTitle.className = "ai-card-title";
      newsTitle.textContent = "最新ニュース影響";

      const newsContent = document.createElement("div");
      newsContent.className = "ai-card-content";
      newsContent.textContent = data.latest_news_impact;

      newsCard.appendChild(newsTitle);
      newsCard.appendChild(newsContent);
      aiSlider.appendChild(newsCard);
    }
  }

  if (aiSection) {
    const listContainer = wrapper.closest(".stocks-list");
    aiSection.classList.add("show");
    scheduleCompactLayoutAfterTransition(
      aiSection,
      listContainer,
      "max-height",
      false,
    );
  }
}

function clearAnalysisError(wrapper) {
  const errorBox = wrapper.querySelector(".ai-error-banner");
  if (errorBox) errorBox.remove();
  const warnBox = wrapper.querySelector(".ai-warning-banner");
  if (warnBox) warnBox.remove();
}

function applyAnalysisError(wrapper, message) {
  const aiSection = wrapper.querySelector(".ai-section");
  if (!aiSection) return;
  const listContainer = wrapper.closest(".stocks-list");
  aiSection.classList.add("show");
  scheduleCompactLayoutAfterTransition(
    aiSection,
    listContainer,
    "max-height",
    false,
  );

  let box = aiSection.querySelector(".ai-error-banner");
  if (!box) {
    box = document.createElement("div");
    box.className = "ai-error-banner";
    box.style.cssText =
      "margin:8px 0 12px;padding:8px 10px;border-radius:8px;background:rgba(255,125,125,0.14);border:1px solid rgba(255,125,125,0.45);color:#ffd7d7;font-size:0.82rem;";
    aiSection.insertBefore(box, aiSection.querySelector(".ai-slider") || null);
  }
  box.textContent = `分析エラー: ${message || "不明なエラー"}`;

  const recEl = wrapper.querySelector(".ai-rec");
  const sentEl = wrapper.querySelector(".ai-sent");
  const targetEl = wrapper.querySelector(".ai-target");
  const upsideEl = wrapper.querySelector(".ai-upside");
  const catEl = wrapper.querySelector(".ai-cat");
  const riskEl = wrapper.querySelector(".ai-risk");
  if (recEl) recEl.textContent = "エラー";
  if (sentEl) sentEl.textContent = "エラー";
  if (targetEl) targetEl.textContent = "--";
  if (upsideEl) upsideEl.textContent = "";
  if (catEl) catEl.textContent = "--";
  if (riskEl) riskEl.textContent = "--";
}

const ANALYZE_POLL_MAX_ATTEMPTS = 6;
const ANALYZE_POLL_INTERVAL_MS = 2000;

async function requestStockAnalysis(stockKey) {
  if (!HAS_MISTRAL_API_KEY) throw new Error("APIキーが未設定です");
  const stock = getStockByKey(stockKey);
  if (!stock) throw new Error("最新の銘柄データを取得できませんでした");

  const headers = {
    "Content-Type": "application/json",
  };

  const payload = {
    symbol: stock.symbol,
    name: stock.name,
    price: stock.price,
    chart_data: stock.chart_data ?? [],
    sector: stock.sector,
    industry: stock.industry,
    market_cap: stock.market_cap,
    pe_ratio: stock.pe_ratio,
    market: stock.market,
  };

  let data = {};
  let resOk = false;

  for (let attempt = 0; attempt <= ANALYZE_POLL_MAX_ATTEMPTS; attempt++) {
    const { response: res, data: fetched } = await apiFetch("/api/analyze-v2", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    data = fetched || {};
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    if (!data.fetching) {
      resOk = true;
      break;
    }
    await sleep(ANALYZE_POLL_INTERVAL_MS);
  }

  if (!resOk) {
    throw new Error(
      "AI分析の生成がタイムアウトしました。しばらく待ってから再試行してください。",
    );
  }

  if (data.parsed === false || !data.recommendation) {
    throw new Error(data.error || "AIの応答を構造化できませんでした");
  }
  return { stock, data };
}

async function analyzeStock(btnEl, wrapper) {
  const stockKey = wrapper.dataset.stockKey;
  if (state.isAnalyzing || !HAS_MISTRAL_API_KEY) {
    if (!HAS_MISTRAL_API_KEY) {
      applyAnalysisError(wrapper, "APIキーが未設定です");
      showToast("❌ APIキーが未設定です", "#ff7d7d");
    }
    return;
  }
  setButtonLoading(btnEl, "AI分析中...");
  state.isAnalyzing = true;
  try {
    const { stock, data } = await requestStockAnalysis(stockKey);
    // すべてのラッパーに反映
    findAllWrappersByStockKey(stockKey).forEach((w) =>
      applyAnalysisResult(w, stock, data),
    );
  } catch (e) {
    $logger.error("Analysis error:", e);
    findAllWrappersByStockKey(stockKey).forEach((w) =>
      applyAnalysisError(w, e.message),
    );
    showToast(`❌ 分析中にエラー: ${e.message}`, "#ff7d7d");
  } finally {
    resetButton(btnEl);
    state.isAnalyzing = false;
  }
}

let bulkAnalyzeCancelled = false;

async function bulkAnalyzeFavorites() {
  if (state.isAnalyzing || !HAS_MISTRAL_API_KEY) {
    if (!HAS_MISTRAL_API_KEY) {
      setBulkAnalyzeStatus(
        "APIキーが未設定です。設定画面でキーを登録してください。",
        "error",
      );
      showToast("❌ APIキーが未設定です", "#ff7d7d");
    }
    return;
  }
  const btn = DOM.get("bulkAnalyzeFavoritesBtn");
  const cancelBtn = DOM.get("cancelBulkAnalyzeBtn");
  const progressWrapper = DOM.get("bulkAnalyzeProgressWrapper");
  const progressBar = DOM.get("bulkAnalyzeProgressBar");

  const favorites = [...state.favorites];
  const targetKeys = favorites.filter((stockKey) => !!getStockByKey(stockKey));
  if (!targetKeys.length) {
    setBulkAnalyzeStatus(
      "お気に入り銘柄がありません。★を付けた銘柄だけが対象です。",
      "error",
    );
    return;
  }
  state.isAnalyzing = true;
  bulkAnalyzeCancelled = false;

  if (btn) setButtonLoading(btn, "お気に入り分析中...");
  if (cancelBtn) {
    cancelBtn.classList.remove("hidden");
    cancelBtn.disabled = false;
    cancelBtn.onclick = () => {
      bulkAnalyzeCancelled = true;
      cancelBtn.disabled = true;
      setBulkAnalyzeStatus("キャンセル処理中...", "running");
    };
  }
  if (progressWrapper) {
    progressWrapper.classList.remove("hidden");
  }
  if (progressBar) {
    progressBar.style.width = "0%";
  }

  const success = [];
  const failed = [];
  try {
    const totalCount = targetKeys.length;
    setBulkAnalyzeStatus(
      `お気に入り ${totalCount} 件を2並列でAI分析します...`,
      "running",
    );

    // 2 concurrent workers pull from a shared queue
    let completedCount = 0;
    const queue = targetKeys.map((stockKey, idx) => ({ stockKey, idx }));
    let queueIndex = 0;
    const queueLock = () => {
      if (bulkAnalyzeCancelled || queueIndex >= queue.length) return null;
      const item = queue[queueIndex++];
      return item;
    };

    const worker = async () => {
      while (true) {
        const item = queueLock();
        if (!item) break;
        const { stockKey, idx } = item;
        const stock = getStockByKey(stockKey);
        if (!stock) continue;

        // Update progress
        if (progressBar) {
          const pct = Math.round((completedCount / totalCount) * 100);
          progressBar.style.width = `${Math.min(pct, 99)}%`;
        }

        const completedList = [
          ...success.map(
            (item) =>
              `✓ ${item.symbol}: ${item.recommendation} / ${item.sentiment}`,
          ),
          ...failed.map((item) => `✗ ${item.symbol}: ${item.error}`),
        ];
        const logSuffix =
          completedList.length > 0
            ? `\n\n【完了した銘柄】\n${completedList.join("\n")}`
            : "";
        setBulkAnalyzeStatus(
          `(${completedCount + 1}/${totalCount}) ${stock.symbol} を分析中...\n` +
            `並列ワーカー動作中 | 完了: ${success.length}件 / 失敗: ${failed.length}件${logSuffix}`,
          "running",
        );

        findAllWrappersByStockKey(stockKey).forEach((wrapper) => {
          const aiSection = wrapper.querySelector(".ai-section");
          if (aiSection) {
            const listContainer = wrapper.closest(".stocks-list");
            aiSection.classList.add("show");
            scheduleCompactLayoutAfterTransition(
              aiSection,
              listContainer,
              "max-height",
              false,
            );
          }
        });

        try {
          const result = await requestStockAnalysis(stockKey);
          findAllWrappersByStockKey(stockKey).forEach((w) =>
            applyAnalysisResult(w, result.stock, result.data),
          );
          success.push({
            symbol: result.stock.symbol,
            recommendation: result.data.recommendation ?? "--",
            sentiment: result.data.sentiment ?? "--",
          });
        } catch (e) {
          $logger.error(`Bulk analysis failed (${stock.symbol}):`, e);
          findAllWrappersByStockKey(stockKey).forEach((w) =>
            applyAnalysisError(w, e.message),
          );
          failed.push({
            symbol: stock.symbol,
            error: e.message || "不明なエラー",
          });
        }

        completedCount++;

        // Small delay between items to avoid overwhelming the API
        await sleep(250);
      }
    };

    // Start 2 concurrent workers
    const workers = [worker(), worker()];
    await Promise.all(workers);

    if (progressBar && !bulkAnalyzeCancelled) {
      progressBar.style.width = "100%";
    }

    if (bulkAnalyzeCancelled) {
      const message =
        `一括AI分析がキャンセルされました。\n` +
        `完了分 成功: ${success.length}件 / 失敗: ${failed.length}件\n\n` +
        (success.length
          ? `【成功】\n` +
            success
              .map(
                (item) =>
                  `・${item.symbol}: ${item.recommendation} / ${item.sentiment}`,
              )
              .join("\n") +
            `\n\n`
          : "") +
        (failed.length
          ? `【失敗】\n` +
            failed.map((item) => `・${item.symbol}: ${item.error}`).join("\n")
          : "");
      setBulkAnalyzeStatus(message.trim(), "error");
      showToast("⚠️ 一括AI分析をキャンセルしました", "#ffcc66");
    } else {
      const successLines = success.map(
        (item) =>
          `・${item.symbol}: ${item.recommendation} / ${item.sentiment}`,
      );
      const failedLines = failed.map(
        (item) => `・${item.symbol}: ${item.error}`,
      );
      const message =
        `一括AI分析が完了しました。\n` +
        `成功: ${success.length}件 / 失敗: ${failed.length}件\n\n` +
        (successLines.length
          ? `【成功】\n${successLines.join("\n")}\n\n`
          : "") +
        (failedLines.length ? `【失敗】\n${failedLines.join("\n")}` : "");
      setBulkAnalyzeStatus(message.trim(), failed.length ? "error" : "success");
    }
  } finally {
    state.isAnalyzing = false;
    if (btn) resetButton(btn);
    if (cancelBtn) {
      cancelBtn.classList.add("hidden");
    }
    if (progressWrapper) {
      setTimeout(() => {
        if (!state.isAnalyzing) {
          progressWrapper.classList.add("hidden");
        }
      }, 2000);
    }
  }
}

// #endregion News & Trends
