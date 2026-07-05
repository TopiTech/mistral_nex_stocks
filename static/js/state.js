// DEFAULT_SYMBOLS and APP_CONFIG are initialized by config_init.js
// #region UI Core Configuration
// --- UI Core Configuration ---
const getCssVar = (name, fallback) => {
  return typeof window !== "undefined"
    ? window
        .getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim() || fallback
    : fallback;
};

const CONSTANTS = {
  COLORS: {
    PRIMARY: getCssVar("--primary", "#6bb6ff"),
    SUCCESS: getCssVar("--success", "#7dffb0"),
    DANGER: getCssVar("--danger", "#ff7d7d"),
    WARNING: getCssVar("--warning", "#ffcc66"),
    PURPLE: getCssVar("--purple", "#c28bff"),
    GOLD: getCssVar("--gold", "#ffd86b"),
    TEXT_MUTE: getCssVar("--text-mute", "#9ca3af"),
    CHIP_BG: getCssVar("--card-bg", "rgba(255, 255, 255, 0.06)"),
  },
  POLLING: {
    INITIAL_STOCKS: 60000,
    INDICES: 30000,
    SSE_RECONNECT: 3000,
    PORTFOLIO_DEBOUNCE: 500,
  },
  PERIODS: ["1d", "5d", "1mo", "3mo", "6mo", "1y", "max"],
  DEFAULT_PERIOD: "3mo",
  CHART_TYPES: {
    LINE: "line",
    CANDLESTICK: "candlestick",
  },
  PREFETCH: {
    PERIOD: "3mo",
    MAX_ITEMS: 24,
    CACHE_TTL_MS: 180000,
  },
  TIMEOUT: {
    STOCK_HISTORY: 30000,
    STOCK_HISTORY_RETRY: 60000,
    NEWS_REQUEST: 120000,
    SEARCH: 10000,
    ANALYSIS: 120000,
  },
};

/**
 * Centralized application state manager.
 * Manages stock lists (us/jp/idx), indices, favorites, streaming toggle,
 * analysis/news loading flags, and exchange rates.
 * Provides change-tracking to reduce redundant UI updates.
 */
class StateManager {
  constructor() {
    this.stocks = { us: [], jp: [], idx: [] };
    this.indices = {};
    this.favorites = this.loadFavorites();
    this.isStreaming = localStorage.getItem("isStreamingEnabled") !== "false";
    this.isAnalyzing = false;
    this.isLoadingNews = false;
    this.exchangeRate = 1.0;
    this.isYfinanceRateLimited = false;
  }

  /**
   * Loads stock Favorites from LocalStorage.
   * @returns {Set<string>} Set of favorited stock keys.
   */
  loadFavorites() {
    try {
      const f = JSON.parse(localStorage.getItem("favorites") || "[]");
      return Array.isArray(f) ? new Set(f) : new Set();
    } catch {
      return new Set();
    }
  }

  /**
   * Persists current favorites to LocalStorage.
   */
  saveFavorites() {
    localStorage.setItem("favorites", JSON.stringify([...this.favorites]));
  }

  /**
   * 銘柄をお気に入りに登録または解除します。
   * @param {string} key - 銘柄を識別するキー (例: "us:AAPL")。
   */
  toggleFavorite(key) {
    if (this.favorites.has(key)) this.favorites.delete(key);
    else this.favorites.add(key);
    this.saveFavorites();
  }

  isFavorite(key) {
    return this.favorites.has(key);
  }

  /**
   * Updates global stock data.
   * @param {Object} data - New stocks data object.
   */
  updateStocks(data) {
    this.stocks = data;
    _rebuildStockKeyIndex();
  }

  /**
   * Updates index data (Nikkei, Dow, FX, etc.).
   * @param {Object} data - Index information object from API.
   */
  updateIndices(data) {
    this.indices = data;
    if (data?.USDJPY) {
      const price = Number(data.USDJPY.price) || null;
      if (price !== null && !isNaN(price)) {
        this.exchangeRate = price;
      } else if (!this.exchangeRate || this.exchangeRate === 1.0) {
        this.exchangeRate = 150.0;
      }
    } else if (!this.exchangeRate || this.exchangeRate === 1.0) {
      this.exchangeRate = 150.0;
    }
  }
}

const state = new StateManager();

// #region Registry & Cache
const chartInstances = new WeakMap();
const stockDetailsCache = new Map();
const historyPrefetchCache = new Map();
const historyPrefetchInFlight = new Map();
const stockRealtimeUpdateAt = new Map();
const sparklineUpdateAt = new Map();
const sparklineSignatureMap = new Map();
const stockHashMap = new Map(); // Added for change detection
const detailCloseGeneration = new WeakMap();
const compactLayoutTransitionCleanupMap = new WeakMap();
// Registry for O(1) wrapper lookups: stockKey -> Set<wrapper element>
// Avoids repeated querySelectorAll('.stock-wrapper[data-stock-key=...]') on every SSE tick
const wrapperRegistryMap = new Map();
let historyPrefetchTimer = null;
let historyPrefetchLastRunAt = 0;
let historyPrefetchJobTimers = [];
let portfolioChartLastAnimatedAt = 0;
const MAX_INITIAL_SNAPSHOT_AGE_MS = 15 * 60 * 1000;

// Global constant exchange rate for simple portfolio calc if needed
let portfolioFixedExchangeRate = null;

// P4修正: ポートフォリオの毎秒フルリビルドをデバウンスで抑制
let _portfolioRenderTimer = null;
const debouncedRenderPortfolio = (
  delay = CONSTANTS.POLLING.PORTFOLIO_DEBOUNCE,
) => {
  if (_portfolioRenderTimer) clearTimeout(_portfolioRenderTimer);
  _portfolioRenderTimer = setTimeout(() => {
    _portfolioRenderTimer = null;
    renderPortfolio();
  }, delay);
};

function isReducedMotionPreferred() {
  return !!(
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function getChartAnimationEnabled(requested = true) {
  return requested !== false && !isReducedMotionPreferred();
}

function resolvePortfolioChartAnimationControl() {
  if (isReducedMotionPreferred()) {
    return { animation: false, updateMode: "none" };
  }

  const now = Date.now();
  const shouldAnimate = now - portfolioChartLastAnimatedAt >= 1500;
  if (shouldAnimate) {
    portfolioChartLastAnimatedAt = now;
    return {
      animation: { duration: 320, easing: "linear" },
      updateMode: undefined,
    };
  }

  return { animation: false, updateMode: "none" };
}

function parseCssTimeToMs(value) {
  const text = String(value || "").trim();
  if (!text) return 0;
  if (text.endsWith("ms")) {
    const num = Number(text.slice(0, -2));
    return Number.isFinite(num) ? num : 0;
  }
  if (text.endsWith("s")) {
    const num = Number(text.slice(0, -1));
    return Number.isFinite(num) ? num * 1000 : 0;
  }
  const num = Number(text);
  return Number.isFinite(num) ? num : 0;
}

const getTransitionFallbackMs = (el, extraPaddingMs = 80) => {
  if (!el || !window.getComputedStyle) return 500;

  const style = window.getComputedStyle(el);
  const durations = String(style.transitionDuration ?? "")
    .split(",")
    .map(parseCssTimeToMs);
  const delays = String(style.transitionDelay ?? "")
    .split(",")
    .map(parseCssTimeToMs);
  const len = Math.max(durations.length, delays.length, 1);

  let maxMs = 0;
  for (let i = 0; i < len; i++) {
    const d = durations[i] ?? durations[0] ?? 0;
    const t = delays[i] ?? delays[0] ?? 0;
    maxMs = Math.max(maxMs, d + t);
  }
  return Math.max(180, Math.ceil(maxMs + extraPaddingMs));
};

// #endregion Registry & Cache

// #region Cache Eviction
const PREFETCH_CACHE_MAX_SIZE = 50;

function _enforcePrefetchCacheLimit() {
  // FIFO eviction: remove oldest entries when exceeding maxSize
  if (historyPrefetchCache.size >= PREFETCH_CACHE_MAX_SIZE) {
    // Get the oldest entry (first one added, iteration order maintains insertion order in Map)
    const firstKey = historyPrefetchCache.keys().next().value;
    if (firstKey) {
      historyPrefetchCache.delete(firstKey);
    }
  }
}

// APIキーはサーバーサイド（config.json / DPAPI/keyring）で安全に管理され、
// APP_CONFIG（サーバーサイド埋め込みJSON）からフロントエンドに状態のみ通知されます。
// レガシーlocalStorage/sessionStorage保存コードはセキュリティ強化のため削除済み。
// clearLegacyApiKeyStorage() が各ページのロード時に起動され、残存データを確実に消去します。

let HAS_MISTRAL_API_KEY = !!APP_CONFIG.has_mistral_api_key;
let HAS_LANGSEARCH_API_KEY = !!APP_CONFIG.has_langsearch_api_key;
let HAS_TAVILY_API_KEY = !!APP_CONFIG.has_tavily_api_key;

async function refreshCredentialState() {
  try {
    const response = await fetch("/api/credentials", { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (response.ok && data && data.ok !== false) {
      // Update credential flags from the backend response (not just static APP_CONFIG)
      HAS_MISTRAL_API_KEY = !!data.has_mistral_api_key;
      HAS_LANGSEARCH_API_KEY = !!data.has_langsearch_api_key;
      HAS_TAVILY_API_KEY = !!data.has_tavily_api_key;
      return data;
    }
  } catch (error) {
    console.warn("Failed to refresh backend credential state:", error);
  }

  return {
    has_mistral_api_key: HAS_MISTRAL_API_KEY,
    has_langsearch_api_key: HAS_LANGSEARCH_API_KEY,
    has_tavily_api_key: HAS_TAVILY_API_KEY,
  };
}

const makeStockKey = (market, symbol) => `${market}:${symbol}`;

/**
 * 株価データのハッシュ値を計算して、変更検出に使用する
 */
const computeStockHash = (s) => {
  if (!s) return "";
  return [
    s.price,
    s.change,
    s.change_percent,
    (s.chart_data || []).length,
  ].join("|");
};

const makeDomSafeKey = (stockKey) =>
  String(stockKey ?? "").replace(/[^a-zA-Z0-9_-]/g, "_");

// O(1) stock lookup index: stockKey -> stock object
const _stockKeyIndex = new Map();

function _rebuildStockKeyIndex() {
  _stockKeyIndex.clear();
  const s = state.stocks;
  for (const list of [s.us, s.jp, s.idx]) {
    if (!Array.isArray(list)) continue;
    for (const stock of list) {
      const key = makeStockKey(stock.market, stock.symbol);
      _stockKeyIndex.set(key, stock);
    }
  }
}

function getStockByKey(stockKey) {
  if (_stockKeyIndex.has(stockKey)) return _stockKeyIndex.get(stockKey);
  _rebuildStockKeyIndex();
  return _stockKeyIndex.get(stockKey) || null;
}

function registerWrapper(stockKey, wrapper) {
  if (!wrapperRegistryMap.has(stockKey))
    wrapperRegistryMap.set(stockKey, new Set());
  wrapperRegistryMap.get(stockKey).add(wrapper);
}

function unregisterWrapper(stockKey, wrapper) {
  const wrappers = wrapperRegistryMap.get(stockKey);
  if (wrappers) {
    wrappers.delete(wrapper);
    // Remove empty set from map to prevent accumulation
    if (wrappers.size === 0) {
      wrapperRegistryMap.delete(stockKey);
    }
  }
}

function findAllWrappersByStockKey(stockKey) {
  const set = wrapperRegistryMap.get(stockKey);
  return set ? Array.from(set) : [];
}

function findWrapperByStockKey(stockKey) {
  const set = wrapperRegistryMap.get(stockKey);
  return set?.values().next().value ?? null;
}

function scheduleCompactLayoutAfterTransition(
  targetEl,
  listContainer,
  propertyName = "max-height",
  useCompactLayout = true,
) {
  if (!listContainer) return;
  if (!targetEl) {
    if (useCompactLayout) compactStockCardLayout(listContainer);
    return;
  }

  const previousCleanup = compactLayoutTransitionCleanupMap.get(targetEl);
  if (typeof previousCleanup === "function") {
    previousCleanup();
  }

  let done = false;
  let fallbackTimer = null;
  const cleanup = () => {
    targetEl.removeEventListener("transitionend", onTransitionEnd);
    if (fallbackTimer) {
      clearTimeout(fallbackTimer);
      fallbackTimer = null;
    }
    if (compactLayoutTransitionCleanupMap.get(targetEl) === cleanup) {
      compactLayoutTransitionCleanupMap.delete(targetEl);
    }
  };

  const finalize = () => {
    if (done) return;
    done = true;
    cleanup();
    if (useCompactLayout) compactStockCardLayout(listContainer);
  };

  const onTransitionEnd = (event) => {
    if (event.target !== targetEl) return;
    if (propertyName && event.propertyName !== propertyName) return;
    finalize();
  };

  targetEl.addEventListener("transitionend", onTransitionEnd);
  fallbackTimer = setTimeout(finalize, getTransitionFallbackMs(targetEl));
  compactLayoutTransitionCleanupMap.set(targetEl, cleanup);
}

function getHistoryPrefetchKey(stockKey, period) {
  return `${stockKey}|${period}`;
}

function buildLocalDateKey(input) {
  const d = input instanceof Date ? input : new Date(input);
  if (!Number.isFinite(d.getTime())) return "";
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function getStockSnapshotTsMs(stock) {
  const value = Number(stock?.snapshot_ts_ms ?? stock?.snapshot_ts ?? 0);
  return Number.isFinite(value) ? value : 0;
}

function hasFreshSparklineData(stock) {
  const snapshotTs = getStockSnapshotTsMs(stock);
  if (snapshotTs > 0) {
    const ageMs = Math.max(0, Date.now() - snapshotTs);
    if (ageMs <= MAX_INITIAL_SNAPSHOT_AGE_MS) return true;
  }
  // snapshot timestamp が欠落していても、SSE更新で届いたデータは描画許可する
  return Boolean(stock?.__live_update);
}

function setSparklineVisibility(wrapper, visible) {
  const sparkline = wrapper?.querySelector(".sparkline");
  if (!sparkline) return;
  const wasHidden = sparkline.getAttribute("aria-hidden") === "true";
  sparkline.style.opacity = visible ? "1" : "0";
  sparkline.style.pointerEvents = visible ? "auto" : "none";
  sparkline.setAttribute("aria-hidden", visible ? "false" : "true");

  if (visible && wasHidden) {
    sparkline.classList.remove("sparkline-reveal");
    requestAnimationFrame(() => {
      sparkline.classList.add("sparkline-reveal");
    });
  }
}

function isSparklineHidden(wrapper) {
  const sparkline = wrapper?.querySelector(".sparkline");
  if (!sparkline) return true;
  return sparkline.getAttribute("aria-hidden") === "true";
}

function getSparklineSignature(data) {
  if (!Array.isArray(data) || data.length === 0) return "";
  return data
    .map((point) => {
      const x = Number(point?.x);
      const price = Number(point?.price);
      return `${Number.isFinite(x) ? x : ""}:${Number.isFinite(price) ? price : ""}`;
    })
    .join("|");
}

function isElementInViewport(el) {
  if (!el) return false;
  // Use IntersectionObserver-driven visibility cache to avoid reflows
  return el.dataset.visible === "true";
}

function shouldUpdateSparkline(wrapper, stockKey, data) {
  if (!wrapper || !stockKey || !Array.isArray(data) || data.length === 0)
    return false;
  if (!isElementInViewport(wrapper)) return false;
  const signature = getSparklineSignature(data);
  if (!signature) return false;
  if (sparklineSignatureMap.get(stockKey) === signature) return false;
  sparklineSignatureMap.set(stockKey, signature);
  sparklineUpdateAt.set(stockKey, Date.now());
  return true;
}

function normalizeHistoryData(history = []) {
  const formattedData = history.map((d) => ({
    x: d.x,
    date: new Date(d.x).toLocaleDateString(),
    price: d.c,
    o: d.o,
    h: d.h,
    l: d.l,
    c: d.c,
    v: d.v != null ? d.v : 0,
    ma5: d.ma5,
    ma25: d.ma25,
  }));
  const ohlcData = history.map((d) => ({
    x: d.x,
    o: d.o,
    h: d.h,
    l: d.l,
    c: d.c,
    v: d.v != null ? d.v : 0,
  }));
  return { formattedData, ohlcData };
}

function applyHistoryToStockAndWrapper(wrapper, formattedData, ohlcData) {
  if (!wrapper) return;
  if (wrapper.__stockData) {
    wrapper.__stockData.chart_data = formattedData;
    wrapper.__stockData.ohlc_data = ohlcData;
  }
  const stockKey = wrapper.dataset.stockKey;
  const liveStock = getStockByKey(stockKey);
  if (liveStock) {
    liveStock.chart_data = formattedData;
    liveStock.ohlc_data = ohlcData;
  }
}
