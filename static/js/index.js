// #region Security Utilities

// --- DOM Cache Helper ---
const DOM = {
  _cache: new Map(),
  get(id) {
    if (!this._cache.has(id)) {
      this._cache.set(id, document.getElementById(id));
    }
    return this._cache.get(id);
  },
  clear() {
    this._cache.clear();
  },
};

// --- Security Utilities ---
/**
 * HTMLをサニタイズしてXSS攻撃を防止
 * @param {string} html - サニタイズ対象のHTML文字列
 * @returns {string} サニタイズされたHTML
 */
const SANITIZE_CACHE = new Map();
const SANITIZE_CACHE_MAX_SIZE = 1000;
const PREFETCH_CACHE_MAX_SIZE = 50;

function sanitizeHTML(html) {
  if (!html || typeof html !== "string") {
    return "";
  }

  // 短い文字列のみキャッシュ（パフォーマンス最適化）
  if (html.length <= 200) {
    const cached = SANITIZE_CACHE.get(html);
    if (cached !== undefined) return cached;

    const div = document.createElement("div");
    div.textContent = html;
    const result = div.innerHTML;

    // LRU-like cache cleanup
    if (SANITIZE_CACHE.size >= SANITIZE_CACHE_MAX_SIZE) {
      const firstKey = SANITIZE_CACHE.keys().next().value;
      SANITIZE_CACHE.delete(firstKey);
    }
    SANITIZE_CACHE.set(html, result);
    return result;
  }

  // 長い文字列はキャッシュしない
  const div = document.createElement("div");
  div.textContent = html;
  return div.innerHTML;
}

/**
 * シンボル入力を検証
 * @param {string} symbol - 検証対象のシンボル
 * @returns {Object} {valid: boolean, value?: string, error?: string}
 */
function validateSymbol(symbol) {
  if (!symbol || typeof symbol !== "string") {
    return { valid: false, error: "シンボルを入力してください" };
  }

  const trimmed = symbol.trim().toUpperCase();

  if (trimmed.length < 1 || trimmed.length > 15) {
    return { valid: false, error: "シンボルは1-15文字である必要があります" };
  }

  if (!/^[A-Z0-9^][A-Z0-9._\-^=]{0,14}$/.test(trimmed)) {
    return { valid: false, error: "無効なシンボル形式です" };
  }

  // 安全でない文字のチェック
  if (/[/\\\0]|\.\./.test(trimmed)) {
    return { valid: false, error: "安全でない文字が含まれています" };
  }

  return { valid: true, value: trimmed };
}

/**
 * 数値入力を検証
 * @param {string|number} value - 検証対象の値
 * @param {number} min - 最小値
 * @param {number} max - 最大値
 * @returns {Object} {valid: boolean, value?: number, error?: string}
 */
function validateNumberInput(value, min, max) {
  const num = parseFloat(value);

  if (isNaN(num)) {
    return { valid: false, error: "有効な数値を入力してください" };
  }

  if (num < min || num > max) {
    return { valid: false, error: `${min}から${max}の間で入力してください` };
  }

  return { valid: true, value: num };
}

/**
 * テキスト入力をサニタイズ
 * @param {string} text - サニタイズ対象のテキスト
 * @param {number} maxLength - 最大長
 * @returns {string} サニタイズされたテキスト
 */
function sanitizeTextInput(text, maxLength = 1000) {
  if (!text || typeof text !== "string") {
    return "";
  }

  let sanitized = text.trim();

  // 長さ制限
  if (sanitized.length > maxLength) {
    sanitized = sanitized.substring(0, maxLength);
  }

  // 制御文字の削除（改行とタブは許可）
  sanitized = sanitized.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "");

  return sanitized;
}

function isValidHexColor(value) {
  return typeof value === "string" && /^#[0-9A-Fa-f]{6}$/.test(value.trim());
}

function sanitizeHexColor(value, fallback = "#6bb6ff") {
  return isValidHexColor(value) ? value.trim() : fallback;
}

// #endregion Security Utilities

// #region Logger
// --- Logger ---
class Logger {
  constructor(module) {
    this.module = module;
    this.enabled = localStorage.getItem("debugEnabled") === "true";
    // 機密情報を検出するパターン
    this.sensitivePatterns = [
      /api[_-]?key['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /token['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /password['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /authorization['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /secret['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
    ];
  }

  /**
   * 機密情報をマスクしてログ出力
   */
  _sanitize(args) {
    return args.map((arg) => {
      if (typeof arg === "string") {
        let sanitized = arg;
        this.sensitivePatterns.forEach((pattern) => {
          sanitized = sanitized.replace(pattern, "[REDACTED]");
        });
        return sanitized;
      }
      if (typeof arg === "object" && arg !== null) {
        const str = JSON.stringify(arg);
        let sanitized = str;
        this.sensitivePatterns.forEach((pattern) => {
          sanitized = sanitized.replace(pattern, "[REDACTED]");
        });
        try {
          return JSON.parse(sanitized);
        } catch {
          return sanitized;
        }
      }
      return arg;
    });
  }

  debug(...args) {
    if (this.enabled)
      console.debug(`[${this.module}]`, ...this._sanitize(args));
  }

  info(...args) {
    console.info(`[${this.module}]`, ...this._sanitize(args));
  }

  warn(...args) {
    console.warn(`[${this.module}]`, ...this._sanitize(args));
  }

  error(...args) {
    console.error(`[${this.module}]`, ...this._sanitize(args));
  }
}

const logger = new Logger("Frontend");

// #endregion Logger

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
    NEWS_REQUEST: 90000,
    SEARCH: 10000,
    ANALYSIS: 120000,
  },
};

/**
 * StateManager: Centralized state for the entire application.
 * Manages stock lists, indices, favorites, and UI-wide loading states.
 * Helps tracking changes and reduces redundant UI updates.
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
  }

  /**
   * Updates index data (Nikkei, Dow, FX, etc.).
   * @param {Object} data - Index information object from API.
   */
  updateIndices(data) {
    this.indices = data;
    if (data?.USDJPY) this.exchangeRate = data.USDJPY.price ?? 1.0;
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
const debouncedRenderPortfolio = (delay = 300) => {
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

// #region Chart.js Plugins
// --- Chart.js Plugins ---
const crosshairPlugin = {
  id: "crosshair",
  afterDraw: (chart) => {
    if (chart.tooltip?._active?.length && chart.scales.y && chart.scales.x) {
      const activePoint = chart.tooltip._active[0];
      const ctx = chart.ctx;
      const x = activePoint.element.x;
      const topY = chart.scales.y.top;
      const bottomY = chart.scales.y.bottom;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, topY);
      ctx.lineTo(x, bottomY);
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(255, 255, 255, 0.2)";
      ctx.setLineDash([3, 3]);
      ctx.stroke();
      ctx.restore();
    }
  },
};
Chart.register(crosshairPlugin);

const DEFAULT_SYMBOLS = (() => {
  try {
    const data = DOM.get("default-symbols-data")?.textContent;
    return data ? JSON.parse(data) : { us: [], jp: [], idx: [] };
  } catch {
    return { us: [], jp: [], idx: [] };
  }
})();
const APP_CONFIG = window.APP_CONFIG ?? {};

// Settings button navigation (moved from inline onclick for CSP hygiene)
DOM.get("settingsBtn")?.addEventListener("click", () => {
  window.location.href = "/settings";
});

// #endregion Chart.js Plugins

// #region Cache Eviction
function _enforcePrefetchCacheLimit() {
  // LRU eviction: remove oldest entries when exceeding maxSize
  if (historyPrefetchCache.size >= PREFETCH_CACHE_MAX_SIZE) {
    // Get the oldest entry (first one added, iteration order maintains insertion order in Map)
    const firstKey = historyPrefetchCache.keys().next().value;
    if (firstKey) {
      historyPrefetchCache.delete(firstKey);
    }
  }
}

const legacyMistralApiKey =
  sessionStorage.getItem("MISTRAL_API_KEY") ??
  localStorage.getItem("MISTRAL_API_KEY") ??
  "";
const legacyLangsearchApiKey =
  sessionStorage.getItem("LANGSEARCH_API_KEY") ??
  localStorage.getItem("LANGSEARCH_API_KEY") ??
  "";

let MISTRAL_API_KEY = APP_CONFIG.has_mistral_api_key ? "" : legacyMistralApiKey;
let LANGSEARCH_API_KEY = APP_CONFIG.has_langsearch_api_key
  ? ""
  : legacyLangsearchApiKey;
let HAS_MISTRAL_API_KEY = !!(APP_CONFIG.has_mistral_api_key || MISTRAL_API_KEY);
let HAS_LANGSEARCH_API_KEY = !!(
  APP_CONFIG.has_langsearch_api_key || LANGSEARCH_API_KEY
);

function clearLegacyBrowserCredentials(options = {}) {
  const mistral = options.mistral !== false;
  const langsearch = options.langsearch !== false;
  if (mistral) {
    sessionStorage.removeItem("MISTRAL_API_KEY");
    localStorage.removeItem("MISTRAL_API_KEY");
  }
  if (langsearch) {
    sessionStorage.removeItem("LANGSEARCH_API_KEY");
    localStorage.removeItem("LANGSEARCH_API_KEY");
  }
}

async function migrateLegacyCredentialsToBackend() {
  if (APP_CONFIG.has_mistral_api_key || !legacyMistralApiKey) {
    clearLegacyBrowserCredentials({ mistral: true, langsearch: false });
    return;
  }

  try {
    const response = await fetch("/api/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mistral_api_key: legacyMistralApiKey,
        langsearch_api_key: legacyLangsearchApiKey,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data?.ok === false) {
      throw new Error(
        data?.error ?? data?.message ?? `HTTP ${response.status}`,
      );
    }
    MISTRAL_API_KEY = "";
    LANGSEARCH_API_KEY = "";
    clearLegacyBrowserCredentials();
  } catch (error) {
    console.warn("Legacy credential migration failed:", error);
  }
}

async function refreshCredentialState() {
  try {
    const response = await fetch("/api/credentials", { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (response.ok && data && data.ok !== false) {
      HAS_MISTRAL_API_KEY = Boolean(
        data.has_mistral_api_key || MISTRAL_API_KEY,
      );
      HAS_LANGSEARCH_API_KEY = Boolean(
        data.has_langsearch_api_key || LANGSEARCH_API_KEY,
      );
      return data;
    }
  } catch (error) {
    console.warn("Failed to refresh backend credential state:", error);
  }

  HAS_MISTRAL_API_KEY = Boolean(
    APP_CONFIG.has_mistral_api_key || MISTRAL_API_KEY,
  );
  HAS_LANGSEARCH_API_KEY = Boolean(
    APP_CONFIG.has_langsearch_api_key || LANGSEARCH_API_KEY,
  );
  return {
    has_mistral_api_key: HAS_MISTRAL_API_KEY,
    has_langsearch_api_key: HAS_LANGSEARCH_API_KEY,
  };
}

migrateLegacyCredentialsToBackend();

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

function getStockByKey(stockKey) {
  const s = state.stocks;
  const all = [...(s.us || []), ...(s.jp || []), ...(s.idx || [])];
  return (
    all.find((st) => makeStockKey(st.market, st.symbol) === stockKey) || null
  );
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
  const rect = el.getBoundingClientRect();
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  return (
    rect.bottom >= 0 && rect.right >= 0 && rect.top <= vh && rect.left <= vw
  );
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

// #region Stock History & Prefetch
function getFreshPrefetchedHistory(stockKey, period) {
  const key = getHistoryPrefetchKey(stockKey, period);
  const entry = historyPrefetchCache.get(key);
  if (!entry) return null;
  if (Date.now() - entry.ts > CONSTANTS.PREFETCH.CACHE_TTL_MS) {
    historyPrefetchCache.delete(key);
    return null;
  }
  return entry;
}

async function fetchStockHistoryPayload(symbol, market, period) {
  const fetchUrl = `/api/stock-history?symbol=${encodeURIComponent(symbol)}&market=${market}&period=${period}`;
  const controller = new AbortController();
  const timeoutId = setTimeout(
    () => controller.abort(),
    CONSTANTS.TIMEOUT.STOCK_HISTORY,
  );

  const doFetch = async () => {
    try {
      const res = await fetch(fetchUrl, { signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP Error: ${res.status}`);
      const data = await res.json();
      if (data?.error) throw new Error(data.error);
      if (!data?.history?.length)
        throw new Error("表示可能なヒストリカルデータがありません。");
      return normalizeHistoryData(data.history);
    } catch (err) {
      if (err.name === "AbortError" || err instanceof TypeError) {
        logger.warn(`Fetch failed for ${symbol} (${period}), retrying...`);
        const retryController = new AbortController();
        const retryTimeoutId = setTimeout(
          () => retryController.abort(),
          CONSTANTS.TIMEOUT.STOCK_HISTORY_RETRY,
        );
        try {
          const retryRes = await fetch(fetchUrl, {
            signal: retryController.signal,
          });
          if (!retryRes.ok) throw new Error(`HTTP Error: ${retryRes.status}`);
          const retryData = await retryRes.json();
          if (retryData?.error) throw new Error(retryData.error);
          if (!retryData?.history?.length)
            throw new Error("表示可能なヒストリカルデータがありません。");
          return normalizeHistoryData(retryData.history);
        } finally {
          clearTimeout(retryTimeoutId);
        }
      }
      throw err;
    }
  };

  try {
    return await doFetch();
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * 個別銘柄のヒストリカルデータを不揮発性/揮発性キャッシュから取得、またはAPIから取得します。
 * @param {HTMLElement} wrapper - 銘柄カードを包むDOM要素。
 * @param {string} period - 取得期間 (例: "3mo")。
 */
function prefetchStockHistory(wrapper, period = CONSTANTS.PREFETCH.PERIOD) {
  if (!wrapper || wrapper.dataset.marketContext === "portfolio") return;
  const stockKey = wrapper.dataset.stockKey;
  const stock = wrapper.__stockData || getStockByKey(stockKey);
  if (!stock || !stock.symbol || !stock.market) return;
  const startedAt = Date.now();

  const cacheKey = getHistoryPrefetchKey(stockKey, period);
  if (getFreshPrefetchedHistory(stockKey, period)) return;
  if (historyPrefetchInFlight.has(cacheKey)) return;

  const task = fetchStockHistoryPayload(stock.symbol, stock.market, period)
    .then(({ formattedData, ohlcData }) => {
      _enforcePrefetchCacheLimit();
      historyPrefetchCache.set(cacheKey, {
        formattedData,
        ohlcData,
        ts: Date.now(),
      });
      const latestRealtimeAt = stockRealtimeUpdateAt.get(stockKey) || 0;
      if (latestRealtimeAt > startedAt) return;
      if (!wrapper.isConnected) return;
      applyHistoryToStockAndWrapper(wrapper, formattedData, ohlcData);
    })
    .catch(() => {
      // 先読み失敗は無視し、通常の展開時取得にフォールバック
    })
    .finally(() => {
      historyPrefetchInFlight.delete(cacheKey);
    });

  historyPrefetchInFlight.set(cacheKey, task);
}

function scheduleHistoryPrefetchWarmup() {
  if (historyPrefetchTimer) clearTimeout(historyPrefetchTimer);
  historyPrefetchJobTimers.forEach((timerId) => clearTimeout(timerId));
  historyPrefetchJobTimers = [];
  historyPrefetchTimer = setTimeout(() => {
    const now = Date.now();
    if (now - historyPrefetchLastRunAt < 400) return;
    historyPrefetchLastRunAt = now;

    for (const [key, entry] of historyPrefetchCache.entries()) {
      if (!entry || now - entry.ts > CONSTANTS.PREFETCH.CACHE_TTL_MS) {
        historyPrefetchCache.delete(key);
      }
    }

    const activeTabId = document.querySelector(".tab.active")?.id || "";
    const activeMarket = activeTabId.startsWith("tab-")
      ? activeTabId.slice(4)
      : "";
    if (!activeMarket || activeMarket === "portfolio") return;

    const activeContainer = document.getElementById(`${activeMarket}-stocks`);
    if (!activeContainer) return;

    const wrappers = Array.from(
      activeContainer.querySelectorAll(".stock-wrapper"),
    );
    if (!wrappers.length) return;
    const targets = wrappers.slice(0, CONSTANTS.PREFETCH.MAX_ITEMS);
    targets.forEach((wrapper, idx) => {
      const timerId = setTimeout(
        () => prefetchStockHistory(wrapper, CONSTANTS.PREFETCH.PERIOD),
        idx * 90,
      );
      historyPrefetchJobTimers.push(timerId);
    });
  }, 250);
}

function clearStockCardMinHeights(container) {
  if (!container) return;
  container.querySelectorAll(".stock-wrapper").forEach((wrapper) => {
    // ハードリセット: transition中に残る minHeight の取り残しを防ぐ
    wrapper.style.minHeight = "0px";
    wrapper.style.height = "";
    requestAnimationFrame(() => {
      wrapper.style.minHeight = "";
    });
  });
}

function compactStockCardLayout(container) {
  if (!container) return;
  clearStockCardMinHeights(container);
}

/**
 * Mistral AI APIの接続状況を確認し、ヘッダーのバッジを更新します。
 */
const domElements = {
  get apiStatus() {
    if (!this._apiStatus) {
      this._apiStatus = DOM.get("api-status-badge") || DOM.get("apiStatus");
    }
    return this._apiStatus;
  },
};

async function updateApiStatus() {
  const badge = domElements.apiStatus;
  if (!badge) return;
  if (!HAS_MISTRAL_API_KEY) {
    badge.textContent = "● API Key Required";
    badge.classList.add("inactive");
    return;
  }
  try {
    const res = await fetch("/api/health");
    if (res.ok) {
      badge.textContent = "Mistral API: Connected";
      badge.classList.remove("inactive");
      badge.style.background = "rgba(107, 182, 255, 0.2)";
      badge.style.color = "#6bb6ff";
    }
  } catch (e) {
    badge.textContent = "Mistral API: Disconnected";
    badge.classList.add("inactive");
    badge.style.background = "rgba(255, 125, 125, 0.2)";
    badge.style.color = "#ff7d7d";
  }
}

function escapeHtml(text) {
  if (text === null || text === undefined) return "";
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML.replace(/\n/g, "<br>");
}

function sanitizeNewsContent(text) {
  return escapeHtml(text);
}

/**
 * DOM要素を安全に作成するヘルパー
 * innerHTML を代替し、XSSリスクを排除する
 */
function createEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

/**
 * detail-panel をDOM APIで構築（innerHTML 不使用）
 */
function buildDetailPanel(stock, marketContext, uniqueId, savedColor, isPortfolio) {
  const safeColor = sanitizeHexColor(savedColor || "#6bb6ff");

  const detail = document.createElement("div");
  detail.className = "detail-panel";

  const inner = createEl("div", "detail-inner");

  // Expand toggle button
  inner.appendChild(createEl("button", "expand-toggle-btn"));

  // Portfolio detail block
  if (isPortfolio) {
    const shares = toFiniteNumber(stock.shares, 0);
    const avgPrice = toFiniteNumber(stock.avg_price, 0);
    const currentPrice = toFiniteNumber(stock.price, 0);
    const plVal = (currentPrice - avgPrice) * shares;
    const plPct = avgPrice > 0 ? ((currentPrice - avgPrice) / avgPrice) * 100 : 0;
    const plClass = plVal >= 0 ? "pos" : "neg";
    const plSign = plVal >= 0 ? "+" : "";

    const pfBlock = createEl("div", "pf-detail-block");
    pfBlock.style.cssText = "background:rgba(255,255,255,0.05);padding:10px;border-radius:8px;margin-bottom:12px;font-size:0.9rem;";

    const row1 = document.createElement("div");
    row1.style.cssText = "display:flex;justify-content:space-between;margin-bottom:4px;";
    const s1 = document.createElement("span");
    s1.textContent = "保有株数: ";
    const s1Strong = document.createElement("strong");
    s1Strong.className = "pf-shares";
    s1Strong.textContent = String(shares);
    s1.appendChild(s1Strong);
    const s2 = document.createElement("span");
    s2.textContent = "平均取得単価: ";
    const s2Strong = document.createElement("strong");
    s2Strong.className = "pf-avgprice";
    s2Strong.textContent = avgPrice.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    s2.appendChild(s2Strong);
    row1.appendChild(s1);
    row1.appendChild(s2);

    const row2 = document.createElement("div");
    row2.style.cssText = "display:flex;justify-content:space-between;";
    const s3 = document.createElement("span");
    s3.textContent = "評価額: ";
    const s3Strong = document.createElement("strong");
    s3Strong.className = "pf-value";
    s3Strong.textContent = (currentPrice * shares).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    s3.appendChild(s3Strong);
    const s4 = document.createElement("span");
    s4.textContent = "評価損益: ";
    const s4Strong = document.createElement("strong");
    s4Strong.className = `pf-pl ${plClass}`;
    s4Strong.textContent = `${plSign}${plVal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} (${plSign}${plPct.toFixed(2)}%)`;
    s4.appendChild(s4Strong);
    row2.appendChild(s3);
    row2.appendChild(s4);

    pfBlock.appendChild(row1);
    pfBlock.appendChild(row2);
    inner.appendChild(pfBlock);
  }

  // Detail info section
  const info = createEl("div", "detail-info");
  const infoItems = [
    { label: "現在値:", cls: "detail-current", val: formatPrice(stock.price, stock) },
    { label: "高値:", cls: "detail-high", val: formatPrice(stock.high, stock) },
    { label: "安値:", cls: "detail-low", val: formatPrice(stock.low, stock) },
    { label: "出来高:", cls: "detail-volume", val: stock.volume != null ? Number(stock.volume).toLocaleString() : "--" },
    { label: "セクター:", cls: "detail-sector extra", val: "--", extraCls: "detail-item-sector" },
    { label: "業種:", cls: "detail-industry extra", val: "--", extraCls: "detail-item-industry" },
    { label: "時価総額:", cls: "detail-mcap extra", val: "--", extraCls: "detail-item-mcap" },
    { label: "PER:", cls: "detail-pe extra", val: "--", extraCls: "detail-item-pe" },
  ];
  infoItems.forEach(({ label, cls, val, extraCls }) => {
    const item = createEl("div", `detail-item ${extraCls || ""}`.trim());
    const strong = document.createElement("strong");
    strong.textContent = label;
    const span = createEl("span", cls, val);
    item.appendChild(strong);
    item.appendChild(span);
    info.appendChild(item);
  });

  // Color picker
  const colorItem = createEl("div", "detail-item");
  const colorLabel = document.createElement("strong");
  colorLabel.textContent = "カード色:";
  const colorInput = document.createElement("input");
  colorInput.id = `card-color-picker-${uniqueId}`;
  colorInput.name = "card-color-picker";
  colorInput.className = "card-color-picker";
  colorInput.type = "color";
  colorInput.value = safeColor;
  colorInput.setAttribute("aria-label", "メインカラー設定");
  colorItem.appendChild(colorLabel);
  colorItem.appendChild(colorInput);
  info.appendChild(colorItem);
  inner.appendChild(info);

  // Detail actions
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  actions.style.cssText = "display:flex;gap:8px;margin-bottom:12px;";
  const pfBtn = createEl("button", "pf-edit-btn", "💼 ポートフォリオ設定");
  pfBtn.style.cssText = "flex:1;padding:6px;border-radius:5px;border:1px solid var(--primary);background:transparent;color:var(--primary);cursor:pointer;font-size:0.8rem;";
  const alertBtn = createEl("button", "alert-edit-btn", "🔔 アラート設定");
  alertBtn.style.cssText = "flex:1;padding:6px;border-radius:5px;border:1px solid var(--acc-red);background:transparent;color:var(--acc-red);cursor:pointer;font-size:0.8rem;";
  actions.appendChild(pfBtn);
  actions.appendChild(alertBtn);
  inner.appendChild(actions);

  // Chart controls (hidden for portfolio)
  const chartControls = createEl("div", "chart-controls");
  chartControls.style.cssText = isPortfolio ? "display:none;" : "";

  // Type controls
  const typeGroup = createEl("div", "control-group type-controls");
  const isLine = getChartPref(makeStockKey(stock.market || "us", stock.symbol), "type", "line") !== "candlestick";
  const lineBtn = createEl("button", `control-btn ${isLine ? "active" : ""}`, "ライン");
  lineBtn.dataset.type = "line";
  const candleBtn = createEl("button", `control-btn ${!isLine ? "active" : ""}`, "ロウソク足");
  candleBtn.dataset.type = "candlestick";
  typeGroup.appendChild(lineBtn);
  typeGroup.appendChild(candleBtn);
  chartControls.appendChild(typeGroup);

  // Volume controls
  const volGroup = createEl("div", "control-group volume-controls");
  const volOn = getChartPref(makeStockKey(stock.market || "us", stock.symbol), "volume", "on") === "on";
  const volOnBtn = createEl("button", `control-btn ${volOn ? "active" : ""}`, "出来高ON");
  volOnBtn.dataset.volume = "on";
  const volOffBtn = createEl("button", `control-btn ${!volOn ? "active" : ""}`, "出来高OFF");
  volOffBtn.dataset.volume = "off";
  volGroup.appendChild(volOnBtn);
  volGroup.appendChild(volOffBtn);
  chartControls.appendChild(volGroup);

  // Period controls
  const periodGroup = createEl("div", "control-group period-controls");
  const stockKey = makeStockKey(stock.market || "us", stock.symbol);
  CONSTANTS.PERIODS.forEach((p) => {
    const btn = createEl("button", `control-btn ${getChartPref(stockKey, "period", "3mo") === p ? "active" : ""}`, p.toUpperCase());
    btn.dataset.period = p;
    periodGroup.appendChild(btn);
  });
  chartControls.appendChild(periodGroup);
  inner.appendChild(chartControls);

  // Chart container
  const chartContainer = createEl("div", "chart-container");
  chartContainer.style.cssText = isPortfolio ? "display:none;" : "";
  const chartCanvas = createEl("canvas", "chart-canvas");
  chartContainer.appendChild(chartCanvas);
  inner.appendChild(chartContainer);

  // PnL chart for portfolio
  if (isPortfolio) {
    const pnlContainer = createEl("div", "chart-container");
    pnlContainer.style.cssText = "margin-top:10px;height:240px;";
    const pnlLabel = document.createElement("div");
    pnlLabel.style.cssText = "font-size:0.8rem;opacity:0.6;margin-bottom:5px;";
    pnlLabel.textContent = "損益率推移 (3ヶ月)";
    const pnlCanvas = createEl("canvas", "chart-canvas-pnl");
    pnlContainer.appendChild(pnlLabel);
    pnlContainer.appendChild(pnlCanvas);
    inner.appendChild(pnlContainer);
  }

  // Analyze button
  inner.appendChild(createEl("button", "analyze-btn", "🔍 AI分析実行"));

  // AI section
  const aiSection = createEl("div", "ai-section");
  const aiTitle = document.createElement("div");
  aiTitle.className = "ai-title";
  aiTitle.textContent = "📈 分析結果 ";
  const aiBadge = createEl("span", "ai-badge", "AI");
  aiTitle.appendChild(aiBadge);
  aiSection.appendChild(aiTitle);

  const aiSlider = createEl("div", "ai-slider");
  const aiCards = [
    { title: "推奨", cls: "ai-rec" },
    { title: "センチメント", cls: "ai-sent" },
    { title: "目標価格 / 3ヶ月", cls: "ai-target", hasUpside: true },
    { title: "注目ポイント", cls: "ai-cat" },
    { title: "リスク要因", cls: "ai-risk" },
  ];
  aiCards.forEach(({ title, cls, hasUpside }) => {
    const card = createEl("div", "ai-card");
    card.appendChild(createEl("div", "ai-card-title", title));
    card.appendChild(createEl("div", `${cls} ai-card-content`, "分析中..."));
    if (hasUpside) {
      const upside = createEl("div", "ai-upside ai-card-content", "");
      upside.style.cssText = "font-weight:700;margin-top:4px;";
      card.appendChild(upside);
    }
    aiSlider.appendChild(card);
  });
  aiSection.appendChild(aiSlider);
  inner.appendChild(aiSection);

  // Chat section
  inner.appendChild(createEl("button", "chat-toggle-btn", "💡 AIに質問する"));
  const chatSection = createEl("div", "chat-section");
  const chatTitle = document.createElement("div");
  chatTitle.className = "ai-title";
  chatTitle.textContent = "💬 AIに質問 ";
  const chatBadge = createEl("span", "ai-badge", "AI");
  chatTitle.appendChild(chatBadge);
  chatSection.appendChild(chatTitle);
  chatSection.appendChild(createEl("div", "chat-log", ""));
  chatSection.lastChild.setAttribute("role", "log");
  chatSection.lastChild.setAttribute("aria-live", "polite");
  const chatInputWrapper = createEl("div", "chat-input-wrapper");
  const chatInput = document.createElement("input");
  chatInput.id = `chat-input-${uniqueId}`;
  chatInput.name = "chat-input";
  chatInput.className = "chat-input";
  chatInput.placeholder = "業績の見通しは？";
  chatInput.setAttribute("aria-label", "AIへの質問");
  const chatSendBtn = createEl("button", "chat-send-btn", "送信");
  chatSendBtn.type = "button";
  chatInputWrapper.appendChild(chatInput);
  chatInputWrapper.appendChild(chatSendBtn);
  chatSection.appendChild(chatInputWrapper);
  inner.appendChild(chatSection);

  detail.appendChild(inner);
  return detail;
}

function currencyPrefixFromCode(code) {
  switch ((code || "").toUpperCase()) {
    case "JPY":
      return "¥";
    case "USD":
      return "$";
    case "EUR":
      return "€";
    case "GBP":
      return "£";
    default:
      return code ? `${code} ` : "";
  }
}

function getCurrencySymbol(stock) {
  return currencyPrefixFromCode(stock?.currency);
}

function formatPrice(value, stock) {
  const num = Number(value);
  const prefix = getCurrencySymbol(stock);
  if (Number.isFinite(num)) return `${prefix}${num.toLocaleString()}`;
  return `${prefix}${value ?? "--"}`;
}

function getChartPref(stockKey, pref, defaultVal) {
  return localStorage.getItem(`chart_${pref}_${stockKey}`) || defaultVal;
}

function setChartPref(stockKey, pref, val) {
  localStorage.setItem(`chart_${pref}_${stockKey}`, val);
}

function getStockColor(stockKey) {
  try {
    const colors = JSON.parse(localStorage.getItem("stock_colors") || "{}");
    return colors[stockKey] || null;
  } catch {
    return null;
  }
}

function saveStockColor(stockKey, color) {
  const normalized = isValidHexColor(color) ? color.trim() : null;
  if (!normalized) return;

  let colors = {};
  try {
    colors = JSON.parse(localStorage.getItem("stock_colors") || "{}");
  } catch {
    colors = {};
  }
  colors[stockKey] = normalized;
  localStorage.setItem("stock_colors", JSON.stringify(colors));
}

window.updateStockColor = function updateStockColor(stockKey, color) {
  const normalized = isValidHexColor(color) ? color.trim() : null;
  if (!normalized) return;

  saveStockColor(stockKey, normalized);
  const wrappers = findAllWrappersByStockKey(stockKey);
  wrappers.forEach((wrapper) => {
    const card = wrapper.querySelector(".compact-card");
    const symbolEl = wrapper.querySelector(".compact-symbol");
    if (card) card.style.borderLeftColor = normalized;
    if (symbolEl) symbolEl.style.color = normalized;
  });
};

function getSortOrder(market) {
  try {
    const parsed = JSON.parse(localStorage.getItem(`sort_${market}`) || "[]");
    return Array.isArray(parsed)
      ? parsed.filter((s) => typeof s === "string")
      : [];
  } catch {
    return [];
  }
}

function orderIndex(order, symbol) {
  const idx = order.indexOf(symbol);
  return idx === -1 ? Number.MAX_SAFE_INTEGER : idx;
}

function setActiveTab(tab) {
  DOM.get("tab-us")?.classList.toggle("active", tab === "us");
  DOM.get("tab-jp")?.classList.toggle("active", tab === "jp");
  DOM.get("tab-idx")?.classList.toggle("active", tab === "idx");
  document
    .getElementById("tab-portfolio")
    ?.classList.toggle("active", tab === "portfolio");

  const us = DOM.get("us-stocks");
  const jp = DOM.get("jp-stocks");
  const idx = DOM.get("idx-stocks");
  const pf = DOM.get("portfolio-wrapper");

  if (us) us.style.display = tab === "us" ? "grid" : "none";
  if (jp) jp.style.display = tab === "jp" ? "grid" : "none";
  if (idx) idx.style.display = tab === "idx" ? "grid" : "none";
  if (pf) pf.style.display = tab === "portfolio" ? "block" : "none";

  if (tab === "portfolio") {
    // ポートフォリオタブを開いた瞬間の為替レートを固定する (視認性向上のため)
    portfolioFixedExchangeRate = state.indices?.USDJPY?.price || null;
    renderPortfolio();
  }

  requestAnimationFrame(() => {
    scheduleHistoryPrefetchWarmup();
  });
}

// #region Portfolio Management
/**
 * ポートフォリオ全体のレンダリングを実行します。
 * 為替レートの適用や損益計算も含みます。
 */
function renderPortfolio() {
  const container = DOM.get("portfolio-stocks");
  const summaryContainer = document.getElementById(
    "portfolio-summary-container",
  );
  if (!container) return;

  const allStocks = getAllStocks();
  const holdings = allStocks.filter((s) => {
    const sh = toFiniteNumber(s.shares, NaN);
    return Number.isFinite(sh) && sh > 0;
  });

  if (holdings.length === 0) {
    if (summaryContainer) summaryContainer.style.display = "none";
    container.textContent = "";
    const empty = document.createElement("div");
    empty.className = "no-results";
    empty.style.gridColumn = "1/-1";
    empty.style.padding = "40px";
    empty.style.textAlign = "center";
    empty.style.color = "#9ca3af";
    empty.textContent =
      "保有銘柄がありません。銘柄詳細からポートフォリオ設定を行ってください。";
    container.appendChild(empty);
    return;
  }

  // 既存のカードを保持したまま更新する (全削除によるチラつきを防止)
  const existingKeys = new Set();
  holdings.forEach((stock) => {
    const stockKey = makeStockKey(stock.market, stock.symbol);
    existingKeys.add(stockKey);
    const registeredSet = wrapperRegistryMap.get(stockKey);
    const wrapper = registeredSet
      ? Array.from(registeredSet).find((w) => w.closest("#portfolio-stocks"))
      : null;

    if (wrapper) {
      updateExistingCard(wrapper, stock);
    } else {
      container.appendChild(createStockCard(stock, "portfolio"));
    }
  });

  // 不要になったカードを削除
  Array.from(container.querySelectorAll(".stock-wrapper")).forEach((w) => {
    if (!existingKeys.has(w.dataset.stockKey)) {
      w.querySelectorAll("canvas").forEach((canvas) => destroyChart(canvas));
      unregisterWrapper(w.dataset.stockKey, w);
      w.remove();
    }
  });

  renderFavorites();

  if (summaryContainer) {
    summaryContainer.style.display = "block";
    drawPortfolioSummaryChart(holdings);
  }
}
// #endregion Portfolio Management

// #region Portfolio Logic
let lastPfChartSignature = "";
const portfolioChartCache = new Map();

function computeHoldingsHash(holdings) {
  return holdings
    .map((h) => `${h.market}:${h.symbol}:${h.shares}:${h.avg_price}`)
    .join("|");
}

function drawPortfolioSummaryChart(holdings) {
  const canvas = DOM.get("pf-summary-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const chartAnimationControl = resolvePortfolioChartAnimationControl();

  // 最新レートを取得 (state経由)
  const usdJpyRate = portfolioFixedExchangeRate || state.exchangeRate || null;
  const isMixedCurrency =
    holdings.some((s) => s.currency === "USD") &&
    holdings.some((s) => s.currency === "JPY");

  if (holdings.some((s) => s.currency === "USD") && !usdJpyRate) {
    document
      .getElementById("pf-summary-loading")
      ?.style.setProperty("display", "block");
    canvas.style.display = "none";
    updatePortfolioHeader(
      holdings,
      null,
      isMixedCurrency,
      chartAnimationControl,
    );
    return;
  }
  document
    .getElementById("pf-summary-loading")
    ?.style.setProperty("display", "none");
  canvas.style.display = "block";

  // 1. 全銘柄からユニークな「日付文字列 (YYYY-MM-DD)」を抽出 (時差によるズレを防止)
  const allDates = new Set();
  const stockHistoryMap = new Map(); // stockKey -> Map<dateStr, price>

  holdings.forEach((stock) => {
    const stockKey = makeStockKey(stock.market, stock.symbol);
    const dayMap = new Map();
    if (stock.chart_data?.length) {
      stock.chart_data.forEach((d) => {
        const dObj = new Date(d.x);
        if (isNaN(dObj.getTime())) return;
        const dateStr = buildLocalDateKey(dObj);
        if (!dateStr) return;
        allDates.add(dateStr);
        dayMap.set(dateStr, d.price ?? d.c ?? 0);
      });
    }
    stockHistoryMap.set(stockKey, {
      days: dayMap,
      shares: toFiniteNumber(stock.shares, 0),
      avgPrice: toFiniteNumber(stock.avg_price, 0),
      rate: stock.currency === "USD" ? usdJpyRate : 1.0,
    });
  });

  const sortedDates = Array.from(allDates).sort();
  if (sortedDates.length < 2) {
    updatePortfolioHeader(
      holdings,
      usdJpyRate,
      isMixedCurrency,
      chartAnimationControl,
    );
    return;
  }

  // 2. 日付ごとに全銘柄の「時価 - コスト」を合算。データ欠損時は前方補填。
  const lastPrices = new Map(); // stockKey -> price

  const dataPoints = sortedDates.map((dateStr) => {
    let totalValue = 0;
    let totalCost = 0;

    holdings.forEach((stock) => {
      const stockKey = makeStockKey(stock.market, stock.symbol);
      const info = stockHistoryMap.get(stockKey);

      let price = info.days.get(dateStr);
      if (price === undefined) {
        price = lastPrices.get(stockKey) || 0; // 前方の有効な値を採用
      } else {
        lastPrices.set(stockKey, price);
      }

      totalValue += price * info.shares * info.rate;
    });
    return { x: new Date(dateStr).getTime(), y: totalValue };
  });

  // 3. データの変更がない場合は再描画をスキップ (SSEなどでのチラつき防止)
  const currentSignature = JSON.stringify(
    dataPoints.map((p) => p.y.toFixed(0)),
  );
  if (pfSummaryChartInstance && currentSignature === lastPfChartSignature) {
    updatePortfolioHeader(
      holdings,
      usdJpyRate,
      isMixedCurrency,
      chartAnimationControl,
    );
    return;
  }
  lastPfChartSignature = currentSignature;

  // ヘッダー表示を更新
  updatePortfolioHeader(
    holdings,
    usdJpyRate,
    isMixedCurrency,
    chartAnimationControl,
  );

  // 描画処理 (既存チャートの更新または新規作成)

  if (pfSummaryChartInstance) {
    // 既存のチャートデータを更新 (アニメーションなしまたはスムース)
    pfSummaryChartInstance.data.datasets[0].data = dataPoints;
    pfSummaryChartInstance.data.datasets[0].borderColor = "#6bb6ff";
    pfSummaryChartInstance.options.animation = chartAnimationControl.animation;
    pfSummaryChartInstance.update(chartAnimationControl.updateMode);
    return;
  }

  pfSummaryChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "合計評価額",
          data: dataPoints,
          borderColor: "#6bb6ff",
          borderWidth: 2,
          fill: {
            target: "origin",
            above: "rgba(107, 182, 255, 0.2)",
          },
          tension: 0.3,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: chartAnimationControl.animation,
      interaction: { intersect: false, mode: "index" },
      plugins: { legend: { display: false } },
      scales: {
        x: {
          type: "time",
          time: { unit: "day", displayFormats: { day: "MM/dd" } },
          ticks: { color: "#ccc", maxTicksLimit: 10 },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
        y: {
          ticks: {
            color: "#ccc",
            callback: (val) => Number(val).toLocaleString(),
          },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
      },
    },
  });
}

function calculatePortfolioMetrics(holdings, currentFxRate, prevFxRate) {
  let totalCurrentValueJPY = 0;
  let totalCostJPY = 0;
  let totalTodayPlJPY = 0;

  holdings.forEach((stock) => {
    const shares = toFiniteNumber(stock.shares, 0);
    const avgPrice = toFiniteNumber(stock.avg_price, 0);
    const currentPrice = toFiniteNumber(stock.price, 0);
    const changeLocal = toFiniteNumber(stock.change, 0);

    const isUSD = stock.currency === "USD" || stock.market === "us";
    const curRate = isUSD ? currentFxRate : 1.0;
    const prvRate = isUSD ? prevFxRate : 1.0;

    // avg_fx_rate が null/undefined/0 の場合は現在の為替レートをデフォルトとする
    const rawAvgFx = stock.avg_fx_rate;
    const avgFxRate =
      rawAvgFx !== null && rawAvgFx !== undefined && Number(rawAvgFx) > 0
        ? Number(rawAvgFx)
        : curRate;

    const costRate = isUSD ? avgFxRate : 1.0;

    totalCurrentValueJPY += shares * currentPrice * curRate;
    totalCostJPY += shares * avgPrice * costRate;

    const prevPriceLocal = currentPrice - changeLocal;
    totalTodayPlJPY +=
      shares * (currentPrice * curRate - prevPriceLocal * prvRate);
  });

  return {
    totalValue: totalCurrentValueJPY,
    totalCost: totalCostJPY,
    totalPl: totalCurrentValueJPY - totalCostJPY,
    todayPl: totalTodayPlJPY,
  };
}

function updatePortfolioHeader(
  holdings,
  usdJpyRate,
  isMixedCurrency,
  chartAnimationControl,
) {
  if (usdJpyRate === null && isMixedCurrency) {
    const valEl = DOM.get("pf-total-value");
    if (valEl) valEl.textContent = "為替データ取得中...";
    return;
  }

  const currentFxRate = usdJpyRate || 1.0;
  const usdJpyChange = toFiniteNumber(state.indices?.USDJPY?.change, 0);
  const prevFxRate = currentFxRate - usdJpyChange;

  const metrics = calculatePortfolioMetrics(
    holdings,
    currentFxRate,
    prevFxRate,
  );

  const plClass = metrics.totalPl >= 0 ? "pos" : "neg";
  const plSign = metrics.totalPl >= 0 ? "+" : "";
  const todayPlClass = metrics.todayPl >= 0 ? "pos" : "neg";
  const todayPlSign = metrics.todayPl >= 0 ? "+" : "";
  const unitLabel = isMixedCurrency ? " (JPY換算)" : " (JPY)";

  const valEl = DOM.get("pf-total-value");
  if (valEl)
    valEl.textContent =
      metrics.totalValue.toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
      }) + unitLabel;
  const plEl = DOM.get("pf-total-pl");
  if (plEl) {
    plEl.textContent = "";
    const span = document.createElement("span");
    span.className = plClass;
    span.textContent = `${plSign}${metrics.totalPl.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
    plEl.appendChild(span);
  }
  const tdayEl = DOM.get("pf-today-pl");
  if (tdayEl) {
    tdayEl.textContent = "";
    const span = document.createElement("span");
    span.className = todayPlClass;
    span.textContent = `${todayPlSign}${metrics.todayPl.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
    tdayEl.appendChild(span);
  }

  // Draw sector chart
  drawSectorPieChart(holdings, currentFxRate, chartAnimationControl);
}

let pfSummaryChartInstance = null;
let pfSectorChartInstance = null;
function drawSectorPieChart(holdings, usdJpyRate, chartAnimationControl) {
  const canvas = DOM.get("pf-sector-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  const sectorMap = {};
  holdings.forEach((stock) => {
    const sector = stock.sector || "Other";
    const shares = toFiniteNumber(stock.shares, 0);
    const price = toFiniteNumber(stock.price, 0);
    const rate = stock.currency === "USD" ? usdJpyRate : 1.0;
    const value = shares * price * rate;

    sectorMap[sector] = (sectorMap[sector] || 0) + value;
  });

  const sortedSectors = Object.entries(sectorMap).sort((a, b) => b[1] - a[1]);
  const labels = sortedSectors.map((s) => s[0]);
  const data = sortedSectors.map((s) => s[1]);

  const colors = [
    "#6bb6ff",
    "#7dffb0",
    "#ff7d7d",
    "#ffcc66",
    "#ff7daa",
    "#9bc9ff",
    "#a3e635",
    "#f87171",
    "#fbbf24",
    "#f472b6",
    "#818cf8",
    "#34d399",
    "#fb7185",
    "#eab308",
    "#c084fc",
  ];

  if (pfSectorChartInstance) {
    pfSectorChartInstance.data.labels = labels;
    pfSectorChartInstance.data.datasets[0].data = data;
    pfSectorChartInstance.options.animation =
      chartAnimationControl?.animation ?? false;
    pfSectorChartInstance.update(chartAnimationControl?.updateMode ?? "none");
    return;
  }

  pfSectorChartInstance = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: labels,
      datasets: [
        {
          data: data,
          backgroundColor: colors,
          borderColor: "rgba(11, 16, 32, 0.8)",
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: chartAnimationControl?.animation ?? false,
      plugins: {
        legend: {
          position: "right",
          labels: { color: "#ccc", font: { size: 10 }, boxWidth: 10 },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const val = ctx.raw;
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const pct = ((val / total) * 100).toFixed(1);
              return ` ${ctx.label}: ${val.toLocaleString(undefined, { maximumFractionDigits: 0 })} JPY (${pct}%)`;
            },
          },
        },
      },
      cutout: "60%",
    },
  });
}

function applySortOrder(market, stocks) {
  const order = getSortOrder(market);
  const defaultSymbols = DEFAULT_SYMBOLS[market] || [];
  const userStocks = stocks.filter((s) => !defaultSymbols.includes(s.symbol));
  const defaultStocks = stocks.filter((s) => defaultSymbols.includes(s.symbol));
  const sortedUser = [...userStocks].sort(
    (a, b) => orderIndex(order, a.symbol) - orderIndex(order, b.symbol),
  );
  const sortedDefault = [...defaultStocks].sort(
    (a, b) =>
      defaultSymbols.indexOf(a.symbol) - defaultSymbols.indexOf(b.symbol),
  );
  return [...sortedUser, ...sortedDefault];
}

function getAllStocks() {
  return [...state.stocks.us, ...state.stocks.jp, ...state.stocks.idx];
}

const formatMarketCap = (value) => {
  const num = Number(value);
  if (Number.isNaN(num)) return value ?? "--";
  return num.toLocaleString();
};

function isBlankDetailValue(value, field) {
  if (value === null || value === undefined) return true;
  if (typeof value === "string" && value.trim() === "") return true;
  if (field === "pe_ratio" || field === "market_cap") {
    const num = Number(value);
    return !Number.isFinite(num) || num <= 0;
  }
  return false;
}

function setDetailItemVisibility(wrapper, field, visible) {
  const row = wrapper.querySelector(`.detail-item-${field}`);
  if (!row) return;
  row.classList.toggle("detail-item-hidden", !visible);
  row.style.display = visible ? "" : "none";
  row.setAttribute("aria-hidden", visible ? "false" : "true");
}

function hideBulkAnalyzeStatus() {
  const box = DOM.get("bulkAnalyzeStatus");
  if (!box) return;
  box.classList.remove("show", "running", "success", "error");
  setTimeout(() => {
    if (!box.classList.contains("show")) box.textContent = "";
  }, 350);
}

function setBulkAnalyzeStatus(message = "", type = "") {
  const box = DOM.get("bulkAnalyzeStatus");
  if (!box) return;
  if (!message) {
    hideBulkAnalyzeStatus();
    return;
  }
  box.textContent = message;
  box.className = "bulk-analyze-status show";
  if (type) box.classList.add(type);
}

function destroyChart(el) {
  if (!el) return;
  if (el.__destroyTimer) {
    clearTimeout(el.__destroyTimer);
    el.__destroyTimer = null;
  }
  const chart = chartInstances.get(el);
  if (chart) {
    try {
      chart.destroy();
    } catch (e) {
      console.warn("Chart destruction failed:", e);
    } finally {
      chartInstances.delete(el);
      if (el.__chart) el.__chart = null;
    }
  }
}

function cancelScheduledDestroy(root) {
  if (!root) return;
  root.querySelectorAll("canvas").forEach((canvas) => {
    if (canvas.__destroyTimer) {
      clearTimeout(canvas.__destroyTimer);
      canvas.__destroyTimer = null;
    }
  });
}

function triggerPriceFlash(priceEl, flashClass) {
  if (!priceEl) return;
  if (!priceEl.__flashCleanupHandler) {
    priceEl.__flashCleanupHandler = (event) => {
      if (
        event.animationName === "flash-green" ||
        event.animationName === "flash-red"
      ) {
        priceEl.classList.remove("flash-up", "flash-down");
      }
    };
    priceEl.addEventListener("animationend", priceEl.__flashCleanupHandler);
  }
  priceEl.classList.remove("flash-up", "flash-down");
  void priceEl.offsetWidth;
  priceEl.classList.add(flashClass);
}

function clearChartError(wrapper) {
  const container =
    wrapper.querySelector(".chart-container") ||
    wrapper.querySelector(".chart-canvas-container");
  if (!container) return;
  const err = container.querySelector(".chart-error");
  if (err) err.remove();
}

function showChartError(wrapper, msg, type = "error") {
  const container =
    wrapper.querySelector(".chart-container") ||
    wrapper.querySelector(".chart-canvas-container");
  if (!container) return;

  destroyChart(wrapper.querySelector(".chart-canvas"));
  clearChartError(wrapper);

  const errDiv = document.createElement("div");
  errDiv.className = `chart-error ${type}`;
  const icon = type === "info" ? "ℹ️" : "⚠️";
  const iconDiv = createEl("div", "chart-error-icon", icon);
  const msgDiv = createEl("div", "chart-error-msg", msg);
  errDiv.appendChild(iconDiv);
  errDiv.appendChild(msgDiv);
  container.appendChild(errDiv);
}

const drawSparkline = (wrapper, data) => {
  const canvas = wrapper.querySelector(".spark-canvas");
  if (!canvas || !data?.length) return;
  setSparklineVisibility(wrapper, true);
  const ctx = canvas.getContext("2d");
  const stockKey = wrapper.dataset.stockKey;
  if (stockKey) {
    const signature = getSparklineSignature(data);
    if (signature) sparklineSignatureMap.set(stockKey, signature);
  }

  const existingChart = chartInstances.get(canvas);
  if (existingChart) {
    existingChart.data.labels = data.map((_, i) => i);
    existingChart.data.datasets[0].data = data.map((d) => d.price);
    existingChart.update("none");
    return;
  }

  destroyChart(canvas);

  const chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: data.map((_, i) => i),
      datasets: [
        {
          data: data.map((d) => d.price),
          borderColor: "#6bb6ff",
          borderWidth: 1.5,
          fill: false,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      elements: { line: { tension: 0.3 } },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } },
    },
  });
  chartInstances.set(canvas, chart);
};

function getDatasetHiddenStateByLabel(chart) {
  const hiddenByLabel = new Map();
  if (!chart?.data?.datasets) return hiddenByLabel;
  chart.data.datasets.forEach((ds, index) => {
    if (!ds?.label) return;
    hiddenByLabel.set(ds.label, !chart.isDatasetVisible(index));
  });
  return hiddenByLabel;
}

function applyDatasetHiddenStateByLabel(chart, hiddenByLabel) {
  if (!chart?.data?.datasets || !hiddenByLabel) return;
  chart.data.datasets.forEach((ds, index) => {
    if (!ds?.label) return;
    if (hiddenByLabel.has(ds.label)) {
      const shouldBeHidden = !!hiddenByLabel.get(ds.label);
      chart.setDatasetVisibility(index, !shouldBeHidden);
    }
  });
}

// #endregion API Status & Formatting Helpers

// #region Stock Chart Rendering
function buildVolumeSeries(lineData = [], ohlcData = []) {
  const volumeByTs = new Map();
  (ohlcData || []).forEach((d) => {
    const ts = Number(d?.x);
    const v = Number(d?.v);
    if (Number.isFinite(ts)) {
      volumeByTs.set(ts, Number.isFinite(v) ? v : 0);
    }
  });

  return (lineData || []).map((d) => {
    const ts = Number(d?.x);
    const direct = Number(d?.v);
    const y = Number.isFinite(direct)
      ? direct
      : volumeByTs.has(ts)
        ? volumeByTs.get(ts)
        : 0;
    return { x: ts, y: Number.isFinite(y) ? y : 0 };
  });
}

const isIntradayPeriodMode = (period) => period === "1d" || period === "5d";

function ensureVolumeScale(chart, showVolume) {
  if (!chart?.options) return;
  if (!chart.options.scales) chart.options.scales = {};
  if (showVolume && !chart.options.scales.yVolume) {
    chart.options.scales.yVolume = {
      display: true,
      position: "right",
      beginAtZero: true,
      ticks: {
        color: "#ccc",
        maxTicksLimit: 4,
        callback: function (value) {
          return Number(value).toLocaleString();
        },
      },
      grid: { drawOnChartArea: false },
    };
  } else if (chart.options.scales.yVolume) {
    chart.options.scales.yVolume.display = showVolume;
  }
}

function createBaseChartOptions(animate, timeConfig, showVolume) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: animate ? undefined : false,
    interaction: { intersect: false, mode: "index" },
    scales: {
      x: {
        type: "time",
        time: timeConfig,
        ticks: { color: "#ccc", maxTicksLimit: 8 },
        grid: { color: "rgba(255,255,255,0.05)" },
      },
      y: {
        position: "left",
        ticks: {
          color: "#ccc",
          callback: function (value) {
            return Number(value).toLocaleString();
          },
        },
        grid: { color: "rgba(255,255,255,0.05)" },
      },
      yVolume: {
        display: showVolume,
        position: "right",
        beginAtZero: true,
        ticks: {
          color: "#ccc",
          maxTicksLimit: 4,
          callback: function (value) {
            return Number(value).toLocaleString();
          },
        },
        grid: { drawOnChartArea: false },
      },
    },
  };
}

function drawChart(wrapper, data, ohlcData, options = {}) {
  const animate = getChartAnimationEnabled(options.animate);
  const animateVolumeOnly = options.animateVolumeOnly === true;
  const canvas = wrapper.querySelector(".chart-canvas");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  // 注: destroyChart はコチでは呼びません。先に「既存チャートを更新」パスを試み、
  // 失敗した場合のみ destroy して再生成する（降徹: 新旧切り替えの障子を除去）

  if (data.length < 2) return;

  const stockKey = wrapper.dataset.stockKey;
  const type = getChartPref(stockKey, "type", "line");
  const period = getChartPref(stockKey, "period", "3mo");
  const showVolume = getChartPref(stockKey, "volume", "on") !== "off";

  // 既存チャートが同じタイプであればデータだけ更新（再生成しない）
  const existingChart = chartInstances.get(canvas);
  const currentIntradayMode = isIntradayPeriodMode(period);
  const existingPeriod = existingChart?.$period;
  const existingIntradayMode = isIntradayPeriodMode(existingPeriod);
  const shouldRecreateForPeriodModeChange = !!(
    existingChart &&
    existingPeriod &&
    existingIntradayMode !== currentIntradayMode
  );

  if (
    existingChart &&
    existingChart.config.type ===
    (type === "candlestick" ? "candlestick" : "line")
  ) {
    if (shouldRecreateForPeriodModeChange) {
      destroyChart(canvas);
    }
    const currentLen = existingChart.data.datasets[0]?.data?.length || 0;
    const hiddenByLabel = getDatasetHiddenStateByLabel(existingChart);
    // 期間やタイプが大幅に変わった場合のみ再生成させる (しきい値を緩めて、SSE更新などでのカクつきを抑える)
    if (
      !shouldRecreateForPeriodModeChange &&
      Math.abs(data.length - currentLen) <= 20
    ) {
      const isLine = type !== "candlestick";
      if (isLine) {
        existingChart.data.datasets[0].data = data.map((d) => ({
          x: d.x,
          y: d.price,
        }));
        // MAデータの更新
        const ma5Data = data
          .filter((d) => d.ma5 != null)
          .map((d) => ({ x: d.x, y: d.ma5 }));
        const ma25Data = data
          .filter((d) => d.ma25 != null)
          .map((d) => ({ x: d.x, y: d.ma25 }));
        const dsMa5 = existingChart.data.datasets.find(
          (ds) => ds.label === "MA5",
        );
        const dsMa25 = existingChart.data.datasets.find(
          (ds) => ds.label === "MA25",
        );
        if (dsMa5) dsMa5.data = ma5Data;
        if (dsMa25) dsMa25.data = ma25Data;

        const volumeData = buildVolumeSeries(data, ohlcData);
        const dsVolume = existingChart.data.datasets.find(
          (ds) => ds.label === "出来高",
        );
        if (showVolume) {
          if (dsVolume) {
            dsVolume.data = volumeData;
          } else {
            existingChart.data.datasets.push({
              type: "bar",
              label: "出来高",
              data: volumeData,
              yAxisID: "yVolume",
              backgroundColor: "rgba(107, 182, 255, 0.2)",
              borderColor: "rgba(107, 182, 255, 0.5)",
              borderWidth: 1,
              barThickness: "flex",
            });
          }
        } else if (dsVolume) {
          existingChart.data.datasets = existingChart.data.datasets.filter(
            (ds) => ds.label !== "出来高",
          );
        }
        ensureVolumeScale(existingChart, showVolume);
        applyDatasetHiddenStateByLabel(existingChart, hiddenByLabel);
      } else {
        const baseData = ohlcData && ohlcData.length > 0 ? ohlcData : data;
        existingChart.data.datasets[0].data = baseData.map((d) => {
          const ts = d.x || (d.date ? new Date(d.date).getTime() : 0);
          return {
            x: ts,
            o: d.o != null ? d.o : d.price,
            h: d.h != null ? d.h : d.price,
            l: d.l != null ? d.l : d.price,
            c: d.c != null ? d.c : d.price,
          };
        });
        const volumeData = baseData.map((d) => ({
          x: d.x || (d.date ? new Date(d.date).getTime() : 0),
          y: d.v != null && d.v !== 0 ? d.v : 0,
        }));
        const dsVolume = existingChart.data.datasets.find(
          (ds) => ds.label === "出来高",
        );

        if (showVolume) {
          if (dsVolume) {
            dsVolume.data = volumeData;
          } else {
            existingChart.data.datasets.push({
              type: "bar",
              label: "出来高",
              data: volumeData,
              yAxisID: "yVolume",
              backgroundColor: "rgba(107, 182, 255, 0.2)",
              borderColor: "rgba(107, 182, 255, 0.5)",
              borderWidth: 1,
              barThickness: "flex",
            });
          }
        } else if (dsVolume) {
          existingChart.data.datasets = existingChart.data.datasets.filter(
            (ds) => ds.label !== "出来高",
          );
        }
        ensureVolumeScale(existingChart, showVolume);
        applyDatasetHiddenStateByLabel(existingChart, hiddenByLabel);

        if (existingChart.data.datasets[0])
          delete existingChart.data.datasets[0].animations;
        const nextVolumeDs = existingChart.data.datasets.find(
          (ds) => ds.label === "出来高",
        );
        if (nextVolumeDs) delete nextVolumeDs.animations;

        // 出来高アニメーションは維持しつつ、更新キューを潰して遅延を最小化
        if (!animate && animateVolumeOnly && showVolume) {
          if (existingChart.data.datasets[0]) {
            existingChart.data.datasets[0].animations = {
              x: { duration: 0 },
              y: { duration: 0 },
              o: { duration: 0 },
              h: { duration: 0 },
              l: { duration: 0 },
              c: { duration: 0 },
            };
          }
          const volumeDs = existingChart.data.datasets.find(
            (ds) => ds.label === "出来高",
          );
          if (volumeDs) {
            volumeDs.animations = {
              x: { duration: 0 },
              y: { duration: 90, easing: "linear" },
            };
          }
          existingChart.stop();
        }
      }

      const updateMode =
        !isLine && !animate && animateVolumeOnly && showVolume
          ? undefined
          : animate
            ? undefined
            : "none";
      existingChart.$period = period;
      existingChart.update(updateMode);
      return;
    }
  }

  // タイプが変わった場合は古いチャートを破棄して再生成
  destroyChart(canvas);

  const isIntradayPeriod = period === "1d" || period === "5d";
  const timeConfig = isIntradayPeriod
    ? { unit: "hour", displayFormats: { hour: "MM/dd HH:mm", day: "MM/dd" } }
    : { unit: "day", displayFormats: { day: "MM/dd", hour: "MM/dd HH:mm" } };

  if (type === "candlestick") {
    // Ensure we have a valid array of data points
    const baseData = ohlcData && ohlcData.length > 0 ? ohlcData : data;

    const candleData = baseData
      .map((d) => {
        const ts = d.x || (d.date ? new Date(d.date).getTime() : 0);
        // Fallback to price if specific OHLC fields are missing
        return {
          x: ts,
          o: d.o != null ? d.o : d.price,
          h: d.h != null ? d.h : d.price,
          l: d.l != null ? d.l : d.price,
          c: d.c != null ? d.c : d.price,
        };
      })
      .filter((d) => d.x > 0);

    const volumeData = baseData
      .map((d) => ({
        x: d.x || (d.date ? new Date(d.date).getTime() : 0),
        y: d.v != null && d.v !== 0 ? d.v : 0,
      }))
      .filter((d) => d.x > 0);

    const datasets = [
      {
        label: "株価",
        data: candleData,
        yAxisID: "y",
        color: { up: "#7dffb0", down: "#ff7d7d", unchanged: "#999" },
        borderColor: { up: "#7dffb0", down: "#ff7d7d", unchanged: "#999" },
      },
    ];
    if (showVolume) {
      datasets.push({
        type: "bar",
        label: "出来高",
        data: volumeData,
        yAxisID: "yVolume",
        backgroundColor: "rgba(107, 182, 255, 0.2)",
        borderColor: "rgba(107, 182, 255, 0.5)",
        borderWidth: 1,
        barThickness: "flex",
      });
    }

    const chart = new Chart(ctx, {
      type: "candlestick",
      data: {
        datasets,
      },
      options: {
        ...createBaseChartOptions(animate, timeConfig, showVolume),
        parsing: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (context) {
                const fmt = (v) =>
                  v == null || !Number.isFinite(Number(v))
                    ? "--"
                    : Number(v).toLocaleString(undefined, {
                      maximumFractionDigits: 2,
                    });
                if (context.dataset.yAxisID === "yVolume")
                  return `出来高: ${context.raw.y.toLocaleString()}`;
                const d = context.raw;
                return `始:${fmt(d.o)} 高:${fmt(d.h)} 安:${fmt(d.l)} 終:${fmt(d.c)}`;
              },
            },
          },
        },
      },
    });
    chart.$period = period;
    chartInstances.set(canvas, chart);
  } else {
    // MA5/MA25 データセットを構築
    const ma5Data = data
      .filter((d) => d.ma5 != null)
      .map((d) => ({ x: d.x, y: d.ma5 }));
    const ma25Data = data
      .filter((d) => d.ma25 != null)
      .map((d) => ({ x: d.x, y: d.ma25 }));
    const volumeData = buildVolumeSeries(data, ohlcData);

    const datasets = [
      {
        label: "終値",
        data: data.map((d) => ({ x: d.x, y: d.price })),
        borderColor: "#6bb6ff",
        tension: 0.3,
        pointRadius: 0,
      },
    ];

    if (ma5Data.length > 0) {
      datasets.push({
        label: "MA5",
        data: ma5Data,
        borderColor: "#ffcc66",
        borderWidth: 1.2,
        borderDash: [4, 2],
        tension: 0.3,
        pointRadius: 0,
        fill: false,
      });
    }
    if (ma25Data.length > 0) {
      datasets.push({
        label: "MA25",
        data: ma25Data,
        borderColor: "#ff7daa",
        borderWidth: 1.2,
        borderDash: [6, 3],
        tension: 0.3,
        pointRadius: 0,
        fill: false,
      });
    }
    if (showVolume) {
      datasets.push({
        type: "bar",
        label: "出来高",
        data: volumeData,
        yAxisID: "yVolume",
        backgroundColor: "rgba(107, 182, 255, 0.2)",
        borderColor: "rgba(107, 182, 255, 0.5)",
        borderWidth: 1,
        barThickness: "flex",
      });
    }

    const chart = new Chart(ctx, {
      type: "line",
      data: { datasets },
      options: {
        ...createBaseChartOptions(animate, timeConfig, showVolume),
        plugins: {
          legend: {
            display: datasets.length > 1,
            labels: { color: "#ccc", boxWidth: 12, font: { size: 10 } },
          },
          tooltip: {
            callbacks: {
              label: function (context) {
                const fmt = (v) =>
                  v == null || !Number.isFinite(Number(v))
                    ? "--"
                    : Number(v).toLocaleString(undefined, {
                      maximumFractionDigits: 2,
                    });
                return `${context.dataset.label}: ${fmt(context.raw.y)}`;
              },
            },
          },
        },
      },
    });
    chart.$period = period;
    chartInstances.set(canvas, chart);
  }
}

function drawPnLChart(canvas, data, avgPrice, options = {}) {
  const animate = getChartAnimationEnabled(options.animate);
  const ctx = canvas.getContext("2d");
  if (!avgPrice || avgPrice <= 0 || !data || data.length < 2) {
    destroyChart(canvas);
    return;
  }

  const pnlData = data.map((d) => ({
    x: d.x,
    y: (((d.c ?? d.price) - avgPrice) / avgPrice) * 100,
  }));

  const existingChart = chartInstances.get(canvas);
  if (existingChart && existingChart.config.type === "line") {
    existingChart.data.datasets[0].data = pnlData;
    // 最新の損益状態に合わせて色を動的に更新
    existingChart.data.datasets[0].borderColor = (ctx) => {
      if (!ctx.chart.chartArea) return "#6bb6ff";
      return pnlData[pnlData.length - 1].y >= 0 ? "#7dffb0" : "#ff7d7d";
    };
    existingChart.update(animate ? undefined : "none");
    return;
  }

  // 既存チャートがない、またはタイプが違う場合は再生成
  destroyChart(canvas);

  const chart = new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "損益率 (%)",
          data: pnlData,
          borderColor: (ctx) => {
            if (!ctx.chart.chartArea) return "#6bb6ff";
            return pnlData[pnlData.length - 1].y >= 0 ? "#7dffb0" : "#ff7d7d";
          },
          borderWidth: 1.5,
          fill: {
            target: "origin",
            below: "rgba(255, 125, 125, 0.2)",
            above: "rgba(125, 255, 176, 0.2)",
          },
          tension: 0.3,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: animate ? undefined : false,
      interaction: { intersect: false, mode: "index" },
      plugins: { legend: { display: false } },
      scales: {
        x: {
          type: "time",
          time: { unit: "day", displayFormats: { day: "MM/dd" } },
          ticks: { color: "#ccc", maxTicksLimit: 8 },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
        y: {
          ticks: { color: "#ccc", callback: (val) => val.toFixed(2) + "%" },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
      },
    },
  });
  chartInstances.set(canvas, chart);
}

async function refreshStockChart(wrapper, period) {
  const stockKey = wrapper.dataset.stockKey;
  const stock = getStockByKey(stockKey);
  if (!stock) return;

  const prefetchEntry = getFreshPrefetchedHistory(stockKey, period);
  if (prefetchEntry) {
    clearChartError(wrapper);
    const { formattedData, ohlcData } = prefetchEntry;
    applyHistoryToStockAndWrapper(wrapper, formattedData, ohlcData);
    if (wrapper.dataset.marketContext !== "portfolio") {
      drawChart(wrapper, formattedData, ohlcData, { animate: true });
    } else {
      const pnlCanvas = wrapper.querySelector(".chart-canvas-pnl");
      if (pnlCanvas)
        drawPnLChart(pnlCanvas, formattedData, stock.avg_price, {
          animate: true,
        });
    }
    wrapper.dataset.lastRefresh = Date.now().toString();
    return;
  }

  const container = wrapper.querySelector(".chart-container");
  container?.classList?.add("loading");

  try {
    const { formattedData, ohlcData } = await fetchStockHistoryPayload(
      stock.symbol,
      stock.market,
      period,
    );
    wrapper.dataset.lastRefresh = Date.now().toString();

    historyPrefetchCache.set(getHistoryPrefetchKey(stockKey, period), {
      formattedData,
      ohlcData,
      ts: Date.now(),
    });

    clearChartError(wrapper);
    applyHistoryToStockAndWrapper(wrapper, formattedData, ohlcData);

    if (wrapper.dataset.marketContext !== "portfolio") {
      drawChart(wrapper, formattedData, ohlcData);
    } else {
      const pnlCanvas = wrapper.querySelector(".chart-canvas-pnl");
      if (pnlCanvas)
        drawPnLChart(pnlCanvas, formattedData, stock.avg_price, {
          animate: true,
        });
    }
  } catch (e) {
    logger.error("History fetch error:", e);
    const msg = e?.message ?? "";
    const isInformational =
      msg.includes("データが見つかりませんでした") ||
      msg.includes("存在しない") ||
      msg.includes("表示可能なヒストリカルデータがありません");
    showChartError(
      wrapper,
      isInformational
        ? msg
        : "通信エラーが発生しました。接続を確認してください。",
      isInformational ? "info" : "error",
    );
  } finally {
    container?.classList?.remove("loading");
  }
}

function renderDetailExtras(wrapper, detailData) {
  const sectorEl = wrapper.querySelector(".detail-sector");
  const industryEl = wrapper.querySelector(".detail-industry");
  const mcapEl = wrapper.querySelector(".detail-mcap");
  const peEl = wrapper.querySelector(".detail-pe");

  if (sectorEl) sectorEl.textContent = detailData.sector || "--";
  if (industryEl) industryEl.textContent = detailData.industry || "--";
  if (mcapEl) mcapEl.textContent = formatMarketCap(detailData.market_cap);
  if (peEl)
    peEl.textContent =
      detailData.pe_ratio != null
        ? Number(detailData.pe_ratio).toLocaleString(undefined, {
          minimumFractionDigits: 1,
          maximumFractionDigits: 1,
        })
        : "--";

  setDetailItemVisibility(
    wrapper,
    "sector",
    !isBlankDetailValue(detailData.sector),
  );
  setDetailItemVisibility(
    wrapper,
    "industry",
    !isBlankDetailValue(detailData.industry),
  );
  setDetailItemVisibility(
    wrapper,
    "mcap",
    !isBlankDetailValue(detailData.market_cap, "market_cap"),
  );
  setDetailItemVisibility(
    wrapper,
    "pe",
    !isBlankDetailValue(detailData.pe_ratio, "pe_ratio"),
  );
}

// #endregion Stock Chart Rendering

// #region Detail Panel Management
async function ensureStockDetails(wrapper) {
  const stockKey = wrapper.dataset.stockKey;
  if (stockDetailsCache.has(stockKey)) {
    renderDetailExtras(wrapper, stockDetailsCache.get(stockKey));
    return;
  }
  const symbol = wrapper.dataset.symbol;
  const market = wrapper.dataset.market || "us";
  try {
    const res = await fetch(
      `/api/stock-details?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`,
    );
    const data = await res.json();
    if (data && !data.error) {
      stockDetailsCache.set(stockKey, data);
      renderDetailExtras(wrapper, data);
    }
  } catch (e) {
    logger.warn("Details fetch error:", e);
  }
}
function renderFavorites() {
  document.querySelectorAll(".favorite-star").forEach((star) => {
    const wrapper = star.closest(".stock-wrapper");
    const stockKey = wrapper?.dataset?.stockKey;
    star.classList.toggle("active", !!stockKey && state.isFavorite(stockKey));
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Unified UI Update for a stock card.
 */
function updateStockUI(wrapper, stock) {
  const stockKey = wrapper.dataset.stockKey;
  const oldPrice = wrapper.__stockData?.price;
  const newPrice = stock.price;
  const isPortfolioTab = wrapper.dataset.marketContext === "portfolio";

  // Skip update if identical to avoid DOM thrashing
  // Check price AND chart freshness (for sparkline updates when price is static)
  const lastChartTs =
    Array.isArray(stock.chart_data) && stock.chart_data.length > 0
      ? stock.chart_data[stock.chart_data.length - 1].x
      : "";
  const oldDataStr = wrapper.dataset.lastDataHash;
  const newDataStr = `${stock.price}|${stock.change}|${stock.change_percent}|${stock.shares}|${stock.avg_price}|${lastChartTs}`;

  if (oldDataStr === newDataStr) return;
  wrapper.dataset.lastDataHash = newDataStr;

  const hasSparklinePoints =
    Array.isArray(stock.chart_data) && stock.chart_data.length > 0;
  const freshSparklineData = hasFreshSparklineData(stock) && hasSparklinePoints;

  wrapper.__stockData = { ...stock };
  stockRealtimeUpdateAt.set(stockKey, Date.now());
  checkAlerts(stock, oldPrice);

  // Update Compact View
  const priceEl = wrapper.querySelector(".compact-price");
  if (priceEl) {
    const formattedPrice = formatPrice(newPrice, stock);
    if (priceEl.textContent !== formattedPrice) {
      priceEl.textContent = formattedPrice;
      if (oldPrice != null && newPrice !== oldPrice && oldPrice !== "--") {
        const flashClass = newPrice > oldPrice ? "flash-up" : "flash-down";
        triggerPriceFlash(priceEl, flashClass);
      }
    }
  }

  const changeEl = wrapper.querySelector(".compact-change");
  if (changeEl) {
    const sign = stock.change >= 0 ? "+" : "";
    const nextCls = `compact-change ${stock.change >= 0 ? "pos" : "neg"}`;
    const nextText = `${sign}${stock.change} (${sign}${stock.change_percent}%)`;
    if (changeEl.className !== nextCls) changeEl.className = nextCls;
    if (changeEl.textContent !== nextText) changeEl.textContent = nextText;
  }

  if (isPortfolioTab) {
    updatePortfolioInfoElements(wrapper, stock);
  }

  // スパークラインの更新
  if (!hasSparklinePoints) {
    setSparklineVisibility(wrapper, false);
  } else {
    // 表示可否と再描画判定を分離し、再描画スキップ時も表示状態は安定させる
    const wasHidden = isSparklineHidden(wrapper);
    setSparklineVisibility(wrapper, true);

    // 鮮度が低いデータは可視状態を維持し、不要な再描画だけ抑制する
    if (!freshSparklineData && !wasHidden) {
      return;
    }

    // リロード直後の差し替えジャンプを抑えるため、初回ライブ更新の1回目は再描画を抑制
    const isFirstLiveRefresh =
      stock.__live_update && wrapper.dataset.liveSparkSeen !== "1";
    if (stock.__live_update) {
      wrapper.dataset.liveSparkSeen = "1";
    }
    if (isFirstLiveRefresh && !wasHidden) {
      return;
    }

    const needsInitialDraw = wasHidden;
    if (
      needsInitialDraw ||
      shouldUpdateSparkline(wrapper, stockKey, stock.chart_data)
    ) {
      drawSparkline(wrapper, stock.chart_data);
    }
  }

  // Update Detail Panel (only if not open to avoid jitter)
  const detail = wrapper.querySelector(".detail-panel");
  if (detail && !detail.classList.contains("open")) {
    const elMap = {
      ".detail-current": formatPrice(stock.price, stock),
      ".detail-high": formatPrice(stock.high, stock),
      ".detail-low": formatPrice(stock.low, stock),
      ".detail-volume":
        stock.volume != null ? Number(stock.volume).toLocaleString() : "--",
    };
    for (const [sel, val] of Object.entries(elMap)) {
      const el = wrapper.querySelector(sel);
      if (el && el.textContent !== String(val)) {
        el.textContent = val;
      }
    }
  }

  // Real-time Chart Update if open
  // ガード条件: ローディング中、または読み込み完了直後（2秒間）は更新をスキップしてアニメーションの衝突を防ぐ
  const container = wrapper.querySelector(".chart-container");
  const lastRefresh = parseInt(wrapper.dataset.lastRefresh || "0");
  const isCooldown = Date.now() - lastRefresh < 2000;

  if (
    detail?.classList.contains("open") &&
    container &&
    !container.classList.contains("loading") &&
    !isCooldown
  ) {
    requestAnimationFrame(() => {
      if (isPortfolioTab) {
        const pnlCanvas = wrapper.querySelector(".chart-canvas-pnl");
        if (pnlCanvas)
          drawPnLChart(pnlCanvas, stock.chart_data || [], stock.avg_price, {
            animate: false,
          });
      } else {
        // 期間保護: ユーザーが3mo以外の期間を選択中の場合、SSEの3moデータでチャートを上書きしない
        const currentPeriod = getChartPref(stockKey, "period", "3mo");
        if (currentPeriod === "3mo") {
          // 3mo デフォルト: 出来高アニメーションのみ残しつつ低遅延で更新
          const hasHistory =
            Array.isArray(stock.chart_data) && stock.chart_data.length >= 2;
          if (hasHistory) {
            const showVolume = getChartPref(stockKey, "volume", "on") !== "off";
            drawChart(wrapper, stock.chart_data || [], stock.ohlc_data || [], {
              animate: false,
              animateVolumeOnly: showVolume,
            });
          } else {
            // SSE軽量ペイロード時は既存チャートの末尾だけ追従させる
            const canvas = wrapper.querySelector(".chart-canvas");
            const chart = canvas ? chartInstances.get(canvas) : null;
            const isLine =
              getChartPref(stockKey, "type", "line") !== "candlestick";
            if (
              chart &&
              chart.data.datasets?.[0]?.data?.length > 0 &&
              stock.price != null
            ) {
              const lastPoint = chart.data.datasets[0].data.at(-1);
              if (lastPoint && lastPoint.y !== undefined) {
                lastPoint.y = stock.price;
              } else if (lastPoint && lastPoint.c !== undefined) {
                lastPoint.c = stock.price;
                if (stock.price > lastPoint.h) lastPoint.h = stock.price;
                if (stock.price < lastPoint.l) lastPoint.l = stock.price;
              }
              chart.update("none");
            }
          }
        } else {
          // 他期間選択中: 既存チャートの最終データポイントのみ現在価格に更新 (SSEデータで上書きしない)
          const canvas = wrapper.querySelector(".chart-canvas");
          if (canvas) {
            const chart = chartInstances.get(canvas);
            const isLine =
              getChartPref(stockKey, "type", "line") !== "candlestick";
            if (chart && chart.data.datasets?.[0]?.data?.length > 0) {
              const lastPoint = chart.data.datasets[0].data.at(-1);
              if (lastPoint && stock.price != null) {
                if (lastPoint.y !== undefined) {
                  // ラインチャート: y プロパティを更新
                  lastPoint.y = stock.price;
                } else if (lastPoint.c !== undefined) {
                  // ローソク足チャート: close を更新し、high/low も補正
                  lastPoint.c = stock.price;
                  if (stock.price > lastPoint.h) lastPoint.h = stock.price;
                  if (stock.price < lastPoint.l) lastPoint.l = stock.price;
                }

                chart.update("none");
              }
            }
          }
        }
      }
    });
    if (stockDetailsCache.has(stockKey))
      renderDetailExtras(wrapper, stockDetailsCache.get(stockKey));
  }
}

function updatePortfolioInfoElements(wrapper, stock) {
  const pfInfoEl = wrapper.querySelector(".compact-pf-info");
  const shares = toFiniteNumber(stock.shares, 0);
  const avgPrice = toFiniteNumber(stock.avg_price, 0);
  const currentPrice = toFiniteNumber(stock.price, 0);

  if (pfInfoEl && shares > 0) {
    const plVal = (currentPrice - avgPrice) * shares;
    const plSign = plVal >= 0 ? "+" : "";
    const plClass = plVal >= 0 ? "pos" : "neg";
    pfInfoEl.textContent = `保有: ${shares} | 損益: `;

    const plSpan = document.createElement("span");
    plSpan.className = plClass;
    plSpan.textContent = `${plSign}${plVal.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
    pfInfoEl.appendChild(plSpan);
  } else if (pfInfoEl) {
    pfInfoEl.textContent = "\u00A0";
  }

  // Detailed PF block
  const pfBlock = wrapper.querySelector(".pf-detail-block");
  if (pfBlock && shares > 0) {
    const plVal = (currentPrice - avgPrice) * shares;
    const plPct =
      avgPrice > 0 ? ((currentPrice - avgPrice) / avgPrice) * 100 : 0;
    const pfShares = pfBlock.querySelector(".pf-shares");
    const pfAvgprice = pfBlock.querySelector(".pf-avgprice");
    const pfValue = pfBlock.querySelector(".pf-value");
    const plEl = pfBlock.querySelector(".pf-pl");
    if (pfShares) pfShares.textContent = shares;
    if (pfAvgprice)
      pfAvgprice.textContent = avgPrice.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    if (pfValue)
      pfValue.textContent = (currentPrice * shares).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    if (plEl) {
      plEl.className = `pf-pl ${plVal >= 0 ? "pos" : "neg"}`;
      const sign = plVal >= 0 ? "+" : "";
      plEl.textContent = `${sign}${plVal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} (${sign}${plPct.toFixed(2)}%)`;
    }
  }
}

const updateExistingCard = (wrapper, stock) => updateStockUI(wrapper, stock);

// #region DOM Component Creation
function createStockCard(stock, marketContext) {
  const market = stock.market || "us";
  const stockKey = makeStockKey(market, stock.symbol);
  const domKey = makeDomSafeKey(stockKey);
  const uniqueId = `${marketContext}-${domKey}`;
  const savedColor = isValidHexColor(getStockColor(stockKey))
    ? getStockColor(stockKey).trim()
    : "";

  const wrapper = document.createElement("div");
  wrapper.className = "stock-wrapper";
  wrapper.dataset.symbol = stock.symbol;
  wrapper.dataset.market = market;
  wrapper.dataset.stockKey = stockKey;
  wrapper.dataset.marketContext = marketContext;
  wrapper.__stockData = { ...stock, market };

  const sign = stock.change >= 0 ? "+" : "";
  const isPortfolio = marketContext === "portfolio";

  // Compact Card Inner - DOM APIで構築
  const safeColor = sanitizeHexColor(savedColor || "#6bb6ff");
  const compact = document.createElement("div");
  compact.className = `compact-card ${market}`;
  compact.style.borderLeftColor = safeColor;

  const favStar = createEl("div", "favorite-star", "★");
  favStar.setAttribute("role", "button");
  favStar.setAttribute("aria-label", "お気に入り");
  compact.appendChild(favStar);

  const symEl = createEl("div", "compact-symbol", stock.symbol);
  symEl.style.color = safeColor;
  compact.appendChild(symEl);

  compact.appendChild(createEl("div", "compact-name", stock.name));

  const right = createEl("div", "compact-right");
  right.appendChild(createEl("div", "compact-price", formatPrice(stock.price, stock)));
  const changeClass = stock.change >= 0 ? "pos" : "neg";
  const changeEl = createEl("div", `compact-change ${changeClass}`, `${sign}${stock.change} (${sign}${stock.change_percent}%)`);
  right.appendChild(changeEl);
  right.appendChild(createEl("div", "compact-pf-info"));
  const sparkline = createEl("div", "sparkline");
  sparkline.setAttribute("aria-hidden", "true");
  const sparkCanvas = createEl("canvas", "spark-canvas");
  sparkline.appendChild(sparkCanvas);
  right.appendChild(sparkline);
  compact.appendChild(right);

  // Detail Panel - DOM APIで構築（innerHTML不使用）
  const detail = buildDetailPanel(stock, marketContext, uniqueId, savedColor, isPortfolio);

  // Events setup
  compact.addEventListener("click", (e) => {
    if (e.target.classList.contains("favorite-star")) return;
    toggleDetail(wrapper);
  });
  compact.querySelector(".favorite-star")?.addEventListener("click", (e) => {
    e.stopPropagation();
    state.toggleFavorite(stockKey);
    renderFavorites();
  });

  const setupBtn = (sel, cb) =>
    detail.querySelector(sel)?.addEventListener("click", cb);
  setupBtn(".analyze-btn", function () {
    const aiSection = detail.querySelector(".ai-section");
    const listContainer = wrapper.closest(".stocks-list");
    aiSection?.classList.add("show");
    scheduleCompactLayoutAfterTransition(aiSection, listContainer);
    analyzeStock(this, wrapper);
  });
  setupBtn(".chat-toggle-btn", () => {
    const chatSection = detail.querySelector(".chat-section");
    if (!chatSection) return;
    const listContainer = wrapper.closest(".stocks-list");
    chatSection.classList.toggle("show");
    scheduleCompactLayoutAfterTransition(
      chatSection,
      listContainer,
      "max-height",
      false,
    );
  });
  setupBtn(".chat-send-btn", () => sendChat(wrapper));
  setupBtn(".pf-edit-btn", () => openPortfolioModal(stockKey));
  setupBtn(".alert-edit-btn", () => openAlertModal(stockKey));

  detail
    .querySelector(".chat-input")
    ?.addEventListener(
      "keypress",
      (e) => e.key === "Enter" && sendChat(wrapper),
    );
  detail
    .querySelector(".card-color-picker")
    ?.addEventListener("input", function () {
      updateStockColor(stockKey, this.value);
    });

  detail.querySelectorAll(".control-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const isPeriod = !!btn.dataset.period;
      const isVolume = btn.dataset.volume !== undefined;
      const val = isPeriod
        ? btn.dataset.period
        : isVolume
          ? btn.dataset.volume
          : btn.dataset.type;
      setChartPref(
        stockKey,
        isPeriod ? "period" : isVolume ? "volume" : "type",
        val,
      );
      btn.parentElement
        .querySelectorAll(".control-btn")
        .forEach((b) => b.classList.toggle("active", b === btn));
      refreshStockChart(wrapper, getChartPref(stockKey, "period", "3mo"));
    });
  });

  setupBtn(".expand-toggle-btn", function () {
    wrapper.classList.toggle("is-expanded");
    // チャートのリサイズをトリガー（幅が変わるため）
    const canvas = wrapper.querySelector(".chart-canvas");
    if (canvas) {
      const chart = chartInstances.get(canvas);
      if (chart) chart.resize();
    }
  });

  wrapper.appendChild(compact);
  wrapper.appendChild(detail);
  updatePortfolioInfoElements(wrapper, stock);
  const hasSparklinePoints =
    Array.isArray(stock.chart_data) && stock.chart_data.length > 0;
  setSparklineVisibility(wrapper, hasSparklinePoints);
  if (hasSparklinePoints) {
    requestAnimationFrame(() => drawSparkline(wrapper, stock.chart_data || []));
  }
  // Register in O(1) lookup registry
  registerWrapper(stockKey, wrapper);
  return wrapper;
}

/**
 * 指定された市場の銘柄カードをレンダリングまたは更新します。
 * @param {string} market - 市場種別 ("us", "jp", "idx")。
 * @param {Array<Object>} stocks - レンダリング対象の銘柄データ配列。
 */
// #region Main Stock List Rendering
function renderStocks(market, stocks) {
  const container = document.getElementById(`${market}-stocks`);
  if (!container) return;

  // 初回ロードのスケルトン残留を防ぐ
  container.querySelectorAll(".skeleton-card").forEach((el) => el.remove());
  container.querySelectorAll(".no-results").forEach((el) => el.remove());

  const existingCards = new Map();
  container.querySelectorAll(".stock-wrapper").forEach((w) => {
    const key = w.dataset.stockKey;
    if (key) existingCards.set(key, w);
  });

  const sortedStocks = applySortOrder(market, stocks);
  const orderedWrappers = [];
  let createdCount = 0;
  let updatedCount = 0;
  sortedStocks.forEach((stock) => {
    const latestStock = { ...stock, market };
    const stockKey = makeStockKey(market, stock.symbol);
    let wrapper = existingCards.get(stockKey);
    if (wrapper) {
      updatedCount += 1;
      updateExistingCard(wrapper, latestStock);
      existingCards.delete(stockKey);
    } else {
      createdCount += 1;
      wrapper = createStockCard(latestStock, market);
    }
    orderedWrappers.push(wrapper);
  });

  existingCards.forEach((wrapper) => {
    wrapper
      .querySelectorAll("canvas")
      .forEach((canvas) => destroyChart(canvas));
    unregisterWrapper(wrapper.dataset.stockKey, wrapper);
    wrapper.remove();
  });

  if (document.querySelector(".tab.active")?.id === "tab-portfolio") {
    renderPortfolio();
  }

  // Reorder in-place without innerHTML="" to preserve Chart.js canvas state
  orderedWrappers.forEach((wrapper, i) => {
    if (wrapper.parentNode !== container) {
      container.appendChild(wrapper);
    } else {
      const currentAtIdx = container.children[i];
      if (currentAtIdx !== wrapper) {
        container.insertBefore(wrapper, currentAtIdx || null);
      }
    }
  });
  renderFavorites();
}

function toggleDetail(wrapper) {
  const detail = wrapper.querySelector(".detail-panel");
  if (!detail) return;
  const stockKey = wrapper.dataset.stockKey;
  const stock = wrapper.__stockData || getStockByKey(stockKey);
  const isOpen = detail.classList.contains("open");
  if (!isOpen) {
    cancelScheduledDestroy(detail);
    const openPanels = document.querySelectorAll(".detail-panel.open");
    if (openPanels.length >= 3) {
      closeDetailPanel(openPanels[0]);
    }

    // close->open の競合時に古い close コールバックを失効させる
    const generation = (detailCloseGeneration.get(detail) || 0) + 1;
    detailCloseGeneration.set(detail, generation);
    const isCurrentOpen = () =>
      detailCloseGeneration.get(detail) === generation;

    detail.classList.add("open");

    // 展開したカードが画面内に収まるようにスムーズスクロール
    setTimeout(() => {
      wrapper.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 100);
    const listContainer = detail.closest(".stocks-list");

    const syncOpenLayout = () => {
      if (!isCurrentOpen() || !detail.classList.contains("open")) return;
      compactStockCardLayout(listContainer);
    };

    const onOpenTransitionEnd = (event) => {
      if (event.target !== detail || !isCurrentOpen()) return;
      if (event.propertyName !== "max-height") return;
      clearTimeout(openFallbackTimer);
      detail.removeEventListener("transitionend", onOpenTransitionEnd);
      syncOpenLayout();
    };

    detail.addEventListener("transitionend", onOpenTransitionEnd);
    const openFallbackTimer = setTimeout(() => {
      if (!isCurrentOpen()) return;
      detail.removeEventListener("transitionend", onOpenTransitionEnd);
      syncOpenLayout();
    }, getTransitionFallbackMs(detail));

    if (stock) {
      const isPortfolio = wrapper.dataset.marketContext === "portfolio";
      const period = isPortfolio
        ? "3mo"
        : getChartPref(stockKey, "period", "3mo");
      refreshStockChart(wrapper, period);
      ensureStockDetails(wrapper);
    }
  } else {
    closeDetailPanel(detail);
  }
}

function closeDetailPanel(detail) {
  if (!detail) return;
  cancelScheduledDestroy(detail);
  const listContainer = detail.closest(".stocks-list");
  // 閉じ始めに固定された minHeight を解放し、折りたたみアニメーションの視認性を維持する
  clearStockCardMinHeights(listContainer);
  detail.classList.remove("open");
  const wrapper = detail.closest(".stock-wrapper");
  if (wrapper) wrapper.classList.remove("is-expanded");
  const fallbackMs = getTransitionFallbackMs(detail);

  const generation = (detailCloseGeneration.get(detail) || 0) + 1;
  detailCloseGeneration.set(detail, generation);
  const isCurrentClose = () => detailCloseGeneration.get(detail) === generation;

  const finalize = () => {
    if (!isCurrentClose() || detail.classList.contains("open")) return;
    detail.querySelectorAll("canvas").forEach((c) => {
      if (c.isConnected) destroyChart(c);
    });
  };

  const onTransitionEnd = (event) => {
    if (event.target !== detail || !isCurrentClose()) return;
    if (event.propertyName !== "max-height") return;
    clearTimeout(fallbackTimer);
    detail.removeEventListener("transitionend", onTransitionEnd);
    finalize();
    if (!detail.classList.contains("open")) {
      compactStockCardLayout(listContainer);
    }
  };

  // Ensure and register listener
  detail.addEventListener("transitionend", onTransitionEnd);
  const fallbackTimer = setTimeout(() => {
    if (!isCurrentClose()) return;
    detail.removeEventListener("transitionend", onTransitionEnd);
    finalize();
    if (!detail.classList.contains("open")) {
      compactStockCardLayout(listContainer);
    }
  }, fallbackMs);
}
// #endregion Detail Panel Management

function renderSkeletons() {
  skeletonShownAt = Date.now();
  const markets = ["us", "jp", "idx"];
  markets.forEach((m) => {
    const container = document.getElementById(`${m}-stocks`);
    if (!container) return;

    // 見栄えのために8個程度スケルトンを表示
    container.textContent = "";
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < 8; i++) {
      const card = createEl("div", "skeleton-card");
      card.appendChild(createEl("div", "skeleton skeleton-text"));
      card.appendChild(createEl("div", "skeleton skeleton-name"));
      card.appendChild(createEl("div", "skeleton skeleton-price"));
      fragment.appendChild(card);
    }
    container.appendChild(fragment);
  });
}

function renderInitialLoadingTimeoutState() {
  ["us", "jp", "idx"].forEach((m) => {
    const container = document.getElementById(`${m}-stocks`);
    if (!container) return;
    container.textContent = "";
    container.appendChild(createEl("div", "no-results", "データ取得待機中です。接続状態を確認し、しばらく待っても表示されない場合は更新してください。"));
  });
}

// #region SSE & Real-time Integration
/**
 * SSEおよびポーリングによるリアルタイム通信を管理するクライアントクラス。
 */
const sseManager = {
  client: new APIClient("/api"),
  currentSource: null,

  connect(url, onMessage, onError, options) {
    this.disconnect();
    this.currentSource = this.client.openSSE(url, onMessage, onError, options);
    return this.currentSource;
  },

  disconnect() {
    if (this.currentSource) {
      this.client.closeSSE();
      this.currentSource = null;
    }
  },
};

/**
 * Global alias for the SSE-specific API client to fix ReferenceError in legacy/external functions.
 */
const sseApiClient = sseManager.client;

let stockEventSource = null;
let sseReconnectAttempts = 0;
let sseReconnectTimer = null;
let sseFallbackPolling = null;
let sseDisconnectedSince = 0;
let lastSseNotifyAt = 0;
let skeletonShownAt = 0;
const INITIAL_SKELETON_MAX_WAIT_MS = 15000;

function setStreamingIndicatorText(text) {
  const btn = DOM.get("streamToggleBtn");
  const label = btn?.querySelector(".stream-text");
  if (label) label.textContent = text;
}

function startSseFallbackPolling() {
  if (sseFallbackPolling) return;
  sseFallbackPolling = setInterval(() => {
    fetchInitialStocks();
  }, 15000);
}

function stopSseFallbackPolling() {
  if (!sseFallbackPolling) return;
  clearInterval(sseFallbackPolling);
  sseFallbackPolling = null;
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

  const tooltip = createEl("div", "index-tooltip");
  const tooltipRows = [
    { label: "始値:", cls: "index-open" },
    { label: "高値:", cls: "index-high" },
    { label: "安値:", cls: "index-low" },
    { label: "出来高:", cls: "index-volume" },
  ];
  tooltipRows.forEach(({ label: rowLabel, cls }) => {
    const row = createEl("div", "tooltip-row");
    const labelSpan = document.createElement("span");
    labelSpan.textContent = rowLabel;
    row.appendChild(labelSpan);
    row.appendChild(createEl("span", cls, "--"));
    tooltip.appendChild(row);
  });
  chip.appendChild(tooltip);

  return chip;
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

  if (priceEl) priceEl.textContent = formatIndexNumber(idx.price);
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

function mergeStocksWithExistingHistory(nextData, existingData) {
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
    merged[market] = (nextData?.[market] || []).map((s) => {
      const prev = prevMap.get(s.symbol) || {};
      const chartData = chooseHistorySeries(s.chart_data, prev.chart_data);
      const ohlcData = chooseHistorySeries(s.ohlc_data, prev.ohlc_data);
      return {
        ...prev,
        ...s,
        market,
        chart_data: Array.isArray(chartData) ? chartData : [],
        ohlc_data: Array.isArray(ohlcData) ? ohlcData : [],
      };
    });
  });
  return merged;
}

/**
 * SSE接続を確立し、リアルタイムデータ配信を開始します。
 * ストリーミングが無効な場合は、定期的なポーリングにフォールバックします。
 */
function connectSSE() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }

  if (stockEventSource || sseApiClient.currentEventSource) {
    sseApiClient.closeSSE();
    stockEventSource = null;
  }

  if (window.pollingTask) {
    clearInterval(window.pollingTask);
    window.pollingTask = null;
  }

  if (!state.isStreaming) {
    logger.info("Streaming is disabled. Switching to 60s background polling.");
    setStreamingIndicatorText("Streaming Paused (60s polling)");
    stopSseFallbackPolling();
    window.pollingTask = setInterval(fetchInitialStocks, 60000);
    return;
  }

  setStreamingIndicatorText(
    sseReconnectAttempts > 0 ? "Reconnecting..." : "Live Streaming",
  );

  if (state.stocks.us.length === 0 && state.stocks.jp.length === 0) {
    renderSkeletons();
  }

  // APIClient を使用してSSE接続を開始（ハートビート監視＋自動再接続付き）
  stockEventSource = sseApiClient.openSSE(
    "/stocks/stream",
    // onMessage コールバック
    (data) => {
      try {
        // SSE接続復帰時に再接続カウンタをリセット
        if (sseReconnectAttempts > 0) {
          sseReconnectAttempts = 0;
          sseDisconnectedSince = 0;
          stopSseFallbackPolling();
          setStreamingIndicatorText("Live Streaming");
        }

        const isHidden = document.hidden;

        // Update Stocks
        if (data.stocks) {
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
          );

          const hasSkeleton = document.querySelector(".skeleton-card") !== null;
          const hasAnyCards =
            document.querySelectorAll(".stock-wrapper").length > 0;
          const incomingCount =
            nextData.us.length + nextData.jp.length + nextData.idx.length;

          state.updateStocks(nextData);

          if (isHidden) {
            return;
          }

          // 初回接続直後に空ペイロードが来る場合は、スケルトンを維持して次の実データを待つ
          if (incomingCount === 0 && hasSkeleton && !hasAnyCards) {
            if (
              skeletonShownAt &&
              Date.now() - skeletonShownAt > INITIAL_SKELETON_MAX_WAIT_MS
            ) {
              renderInitialLoadingTimeoutState();
              skeletonShownAt = 0;
            }
            return;
          }

          if (incomingCount > 0) {
            skeletonShownAt = 0;
          }

          const shouldFullRender = hasSkeleton || !hasAnyCards;

          if (shouldFullRender) {
            renderStocks("us", state.stocks.us);
            renderStocks("jp", state.stocks.jp);
            renderStocks("idx", state.stocks.idx);
          } else {
            const updateUI = (market, stocks) => {
              stocks.forEach((stock) => {
                const stockKey = makeStockKey(market, stock.symbol);
                // 変更検出による不要な再レンダリング防止
                const lastHash = stockHashMap.get(stockKey);
                const currentHash = computeStockHash(stock);
                if (lastHash === currentHash) return; // 変更なしはスキップ

                stockHashMap.set(stockKey, currentHash);
                const wrappers = findAllWrappersByStockKey(stockKey);
                wrappers.forEach((wrapper) => {
                  updateExistingCard(wrapper, stock);
                });
              });
            };
            updateUI("us", state.stocks.us);
            updateUI("jp", state.stocks.jp);
            updateUI("idx", state.stocks.idx);
          }

          // ポートフォリオタブが表示されている場合は、デバウンスで再描画
          const activeTab = document.querySelector(".tab.active")?.id;
          if (activeTab === "tab-portfolio") {
            debouncedRenderPortfolio();
          }
        }

        // Update Indices if provided
        if (data.indices) {
          if (isHidden) {
            state.updateIndices(data.indices);
            return;
          }
          updateIndicesBar(data.indices);
        }
      } catch (e) {
        logger.error("SSE message processing error:", e);
      }
    },
    // onError コールバック
    (error) => {
      logger.error("SSE error:", error);
      if (!state.isStreaming) return;

      if (!sseDisconnectedSince) sseDisconnectedSince = Date.now();
      sseReconnectAttempts += 1;
      startSseFallbackPolling();
      setStreamingIndicatorText(
        `Reconnecting... (${Math.min(sseReconnectAttempts, 9)})`,
      );

      const now = Date.now();
      if (now - lastSseNotifyAt > 20000) {
        showToast(
          "⚠️ リアルタイム配信が一時切断されました。再接続を試行中です",
          "#ffcc66",
        );
        lastSseNotifyAt = now;
      }

      const delay = Math.min(3000 + sseReconnectAttempts * 1000, 15000);
      sseReconnectTimer = setTimeout(() => {
        sseReconnectTimer = null;
        connectSSE();
      }, delay);
    },
    // SSE接続オプション
    {
      // 再接続は connectSSE 側で一元管理し、二重リトライを避ける
      autoReconnect: false,
      maxReconnectAttempts: 5,
      onReconnect: (es) => {
        stockEventSource = es;
      },
    },
  );
}

async function loadIndicesLoop() {
  const fetchIndices = async () => {
    try {
      const res = await fetch("/api/indices");
      if (!res.ok) throw new Error("Fetch failed");
      const data = await res.json();
      updateIndicesBar(data);
    } catch (e) {
      logger.warn("Index fetch error:", e);
    }
  };
  fetchIndices(); // 初回実行
  setInterval(fetchIndices, 30000); // 30秒おき
}

// #endregion SSE & Real-time Integration

// #region News & Trends
async function loadNews() {
  if (state.isLoadingNews || !HAS_MISTRAL_API_KEY) {
    if (!HAS_MISTRAL_API_KEY) showToast("❌ APIキーが未設定です", "#ff7d7d");
    return;
  }
  const usBox = DOM.get("news-us");
  const jpBox = DOM.get("news-jp");
  const trendsBox = DOM.get("news-trends");
  const refreshBtn = DOM.get("newsRefreshBtn");
  const newsMetaStatsEl = DOM.get("news-meta-stats");

  state.isLoadingNews = true;
  if (refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.innerHTML = '<span class="loading-spinner"></span>検索中...';
  }
  usBox?.classList.remove("show");
  jpBox?.classList.remove("show");
  trendsBox?.classList.remove("show");
  if (usBox) usBox.textContent = "最新情報を検索・分析中...";
  if (jpBox) jpBox.textContent = "最新情報を検索・分析中...";
  if (trendsBox) trendsBox.textContent = "最新情報を検索・分析中...";
  if (newsMetaStatsEl) newsMetaStatsEl.textContent = "表示件数: 取得中...";

  const normalizeNewsSectionContent = (raw, sectionKey) => {
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
  };

  const parseNewsItems = (raw) => {
    const isMetadataLine = (line) =>
      /^(?:source|date|url)\s*:/i.test(String(line || "").trim());
    const isNoiseLine = (line) => {
      const s = String(line || "").trim();
      if (!s) return true;
      const lower = s.toLowerCase();
      if (isMetadataLine(s)) return true;
      if (lower.startsWith("http://") || lower.startsWith("https://"))
        return true;
      if (lower.includes("news.google.com/rss/articles")) return true;
      if (/<[^>]+>/.test(s)) return true;
      if (/(?:<a\s|<li|<ol|<ul)/i.test(s)) return true;
      return false;
    };

    const flattenStructuredItem = (item) => {
      if (item == null) return "";
      if (typeof item === "string") return item.trim();
      if (typeof item === "number" || typeof item === "boolean")
        return String(item);
      if (Array.isArray(item)) {
        return item.map(flattenStructuredItem).filter(Boolean).join(" / ");
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
    };

    // normalize 側から配列/オブジェクトが来た場合は構造を保持して処理
    if (Array.isArray(raw)) {
      return raw
        .map(flattenStructuredItem)
        .filter(Boolean)
        .filter((x) => !isNoiseLine(x));
    }
    if (raw && typeof raw === "object") {
      const values = Object.values(raw)
        .map(flattenStructuredItem)
        .filter(Boolean)
        .filter((x) => !isNoiseLine(x));
      if (values.length) return values;
      return [];
    }

    let text = String(raw || "").trim();
    if (!text) return [];

    // 先頭/末尾のコードフェンスのみ取り除き、中身は保持する
    text = text
      .replace(/^```[a-zA-Z0-9_-]*\s*\n?/, "")
      .replace(/\n?```$/, "")
      .trim();

    if (!text) return [];

    // まずはJSONを優先して解釈（配列/オブジェクト）
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) {
        return parsed
          .map(flattenStructuredItem)
          .filter(Boolean)
          .filter((x) => !isNoiseLine(x));
      }
      if (parsed && typeof parsed === "object") {
        const values = Object.values(parsed)
          .map(flattenStructuredItem)
          .filter(Boolean)
          .filter((x) => !isNoiseLine(x));
        if (values.length) return values;
      }
    } catch (_) {
      // no-op
    }

    // "['a', 'b']" / "[\"a\", \"b\"]" など疑似配列を整形
    if (text.startsWith("[") && text.endsWith("]")) {
      const inner = text.slice(1, -1).trim();
      const split = inner
        .split(/'\s*,\s*'|"\s*,\s*"|」\s*,\s*「/g)
        .map((x) => x.replace(/^['"「\s]+|['"」\s]+$/g, "").trim())
        .filter(Boolean);
      if (split.length > 1) return split;
      text = inner.replace(/^['"「\s]+|['"」\s]+$/g, "").trim();
    }

    // 見出し記号や改行で分解
    const lines = text
      // 「・」は日本語の語句連結にも使われるため分割対象から外す
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
      .filter((x) => !isNoiseLine(x))
      .filter((x) => !/^[\[{\]}]$/.test(x))
      .filter(Boolean);
    if (lines.length) return lines;

    // JSON断片しか残らない場合は生表示を避ける
    if (
      /^[\s\[{]/.test(text) ||
      /"(?:us|jp|trends|topic|summary|details|market_impact)"\s*:/.test(text)
    ) {
      return [];
    }
    return isNoiseLine(text) ? [] : [text];
  };

  const ensureMinimumNewsLines = (items, rawContent, minLines = 5) => {
    const normalized = [];
    const seen = new Set();
    const pushUnique = (line) => {
      const s = String(line || "").trim();
      if (!s) return;
      if (/(?:<a\s|<li|<ol|<ul|<[^>]+>)/i.test(s)) return;
      if (
        /^https?:\/\//i.test(s) ||
        /news\.google\.com\/rss\/articles/i.test(s)
      )
        return;
      if (/^(?:source|date|url)\s*:/i.test(s)) return;
      if (seen.has(s)) return;
      seen.add(s);
      normalized.push(s);
    };

    items.forEach(pushUnique);

    return normalized;
  };

  const renderNewsContent = (el, content, sectionKey) => {
    if (!el) return;
    const normalizedContent = normalizeNewsSectionContent(content, sectionKey);
    const parsedItems = parseNewsItems(normalizedContent);
    const items = ensureMinimumNewsLines(
      parsedItems,
      normalizedContent,
      5,
    ).slice(0, 12);
    if (!items.length) {
      el.textContent = "情報を取得できませんでした";
      return { displayCount: 0, parsedCount: parsedItems.length };
    }
    if (items.length === 1) {
      el.textContent = items[0];
      return { displayCount: 1, parsedCount: parsedItems.length };
    }
    // DOM APIを使用して安全に要素を構築（innerHTMLの使用を避ける）
    const fragment = document.createDocumentFragment();
    items.forEach((item) => {
      const lineDiv = document.createElement("div");
      lineDiv.className = "news-line";

      const bulletSpan = document.createElement("span");
      bulletSpan.className = "news-bullet";
      bulletSpan.textContent = "•";

      const textSpan = document.createElement("span");
      textSpan.textContent = item; // textContentは自動的にエスケープ

      lineDiv.appendChild(bulletSpan);
      lineDiv.appendChild(textSpan);
      fragment.appendChild(lineDiv);
    });
    el.textContent = ""; // 既存の内容をクリア
    el.appendChild(fragment);
    return { displayCount: items.length, parsedCount: parsedItems.length };
  };

  let timeoutId = null;
  try {
    const headers = {
      "Content-Type": "application/json",
    };
    if (MISTRAL_API_KEY) {
      headers["Authorization"] = `Bearer ${MISTRAL_API_KEY}`;
    }
    if (LANGSEARCH_API_KEY) {
      headers["X-LangSearch-Key"] = LANGSEARCH_API_KEY;
    }

    const newsRequestController = new AbortController();
    timeoutId = setTimeout(() => {
      newsRequestController.abort();
    }, CONSTANTS.TIMEOUT.NEWS_REQUEST);

    const res = await fetch("/api/news", {
      method: "POST",
      headers,
      signal: newsRequestController.signal,
    });

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      throw new APIError(
        res.status,
        errorData.error_code || 9999,
        errorData.message || `HTTP ${res.status}`,
        errorData.details,
      );
    }

    const data = await res.json();
    if (data.error) {
      throw new APIError(
        400,
        data.error_code || 9999,
        data.error,
        data.details,
      );
    }

    // ステータスバッジを生成する関数
    const getStatusBadge = (status) => {
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
    };

    // 各セクションのステータスを取得
    const retrieveStatus = data.retrieve_status || {
      us: data.us?.status || "success",
      jp: data.jp?.status || "success",
      trends: data.trends?.status || "success",
    };

    const usStatus = getStatusBadge(retrieveStatus.us);
    const jpStatus = getStatusBadge(retrieveStatus.jp);
    const trendsStatus = getStatusBadge(retrieveStatus.trends);

    const usStats = renderNewsContent(usBox, data.us?.content, "us") || {
      displayCount: 0,
      parsedCount: 0,
    };
    const jpStats = renderNewsContent(jpBox, data.jp?.content, "jp") || {
      displayCount: 0,
      parsedCount: 0,
    };
    const trStats = renderNewsContent(
      trendsBox,
      data.trends?.content,
      "trends",
    ) || { displayCount: 0, parsedCount: 0 };

    // トレンドバッジの同期更新
    if (data.trending_raw && Array.isArray(data.trending_raw)) {
      renderTrendingBadges(data.trending_raw);
    }

    if (newsMetaStatsEl) {
      const tagCount = Array.isArray(data.trending_raw)
        ? data.trending_raw.length
        : 0;
      const timestamp =
        data.us?.timestamp ||
        data.jp?.timestamp ||
        data.trends?.timestamp ||
        "";
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
      // ステータスバッジを含めたメタ表示（DOM API使用）
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

    requestAnimationFrame(() => {
      usBox?.classList.add("show");
      jpBox?.classList.add("show");
      trendsBox?.classList.add("show");
    });
  } catch (e) {
    logger.error("News error:", e);
    const message =
      e?.name === "AbortError"
        ? "ニュース取得がタイムアウトしました。一部の情報が表示されない可能性があります。"
        : `ニュース取得エラー: ${e.message}`;
    logger.warn(message);

    // エラー時もコンテンツを表示状態にする
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

    // エラー時もボックスを表示してユーザーにフィードバック
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
      // タイムアウト時は既存のコンテンツを維持または部分表示
      requestAnimationFrame(() => {
        usBox?.classList.add("show");
        jpBox?.classList.add("show");
        trendsBox?.classList.add("show");
      });
    }
  } finally {
    // 必ずクリーンアップを実行
    if (timeoutId) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    state.isLoadingNews = false;
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.innerHTML = "🔄 リアルタイム更新";
    }
  }
}

const forceRefreshNews = async () => {
  if (!state.isLoadingNews) await loadNews();
};

async function searchStocks() {
  const q = DOM.get("searchInput")?.value.trim();
  const box = DOM.get("search-results");
  const list = DOM.get("search-results-list");
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
    if (data.error) {
      if (list) {
        list.textContent = "";
        list.appendChild(createEl("div", "no-results", `エラー: ${data.error}`));
      }
      return;
    }
    if (!data.results?.length) {
      if (list) {
        list.textContent = "";
        list.appendChild(createEl("div", "no-results", "該当する銘柄が見つかりませんでした。"));
      }
      return;
    }
    if (list) list.textContent = "";
    data.results.forEach((item) => {
      const row = document.createElement("div");
      row.className = "search-result-item";

      const label = document.createElement("span");
      label.textContent = `${item.symbol || ""} - ${item.name || ""}`;
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
    logger.error("Search error:", e);
    if (list) {
      list.textContent = "";
      list.appendChild(createEl("div", "no-results", "検索中にエラーが発生しました。"));
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
    const res = await fetch("/api/stocks/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: normalizedSymbol, name, market }),
    });
    const data = await res.json();
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
    logger.error("Add stock error:", e);
    showToast("❌ 通信エラーが発生しました", "#ff7d7d");
  }
}

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
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(MISTRAL_API_KEY
          ? { Authorization: `Bearer ${MISTRAL_API_KEY}` }
          : {}),
      },
      body: JSON.stringify({
        symbol: stock?.symbol || stockKey,
        market: stock?.market || "us",
        message: msg,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detailReason = data?.details?.reason
        ? String(data.details.reason)
        : "";
      const errMsg =
        detailReason ||
        String(data.message || data.error || `HTTP ${res.status}`);
      throw new Error(errMsg);
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
  } catch (e) { }

  // Save new state
  localStorage.setItem(`ai_prev_${stockKey}`, JSON.stringify(data));

  // Determine diff logic
  const getDiffArrow = (prev, curr, goodVals, badVals) => {
    if (!prev || prev === curr) return "";
    if (goodVals.includes(curr) && badVals.includes(prev))
      return '<span style="color:#7dffb0; margin-left:5px;">▲ 改善</span>';
    if (badVals.includes(curr) && goodVals.includes(prev))
      return '<span style="color:#ff7d7d; margin-left:5px;">▼ 悪化</span>';
    return '<span style="color:#ffcc66; margin-left:5px;">● 変化</span>';
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

  if (recEl) {
    recEl.textContent = "";
    const recText = document.createTextNode(data.recommendation ?? "--");
    recEl.appendChild(recText);
    if (recArrow) {
      const arrowSpan = document.createElement("span");
      arrowSpan.style.marginLeft = "5px";
      if (recArrow.includes("改善")) {
        arrowSpan.style.color = "#7dffb0";
        arrowSpan.textContent = "▲ 改善";
      } else if (recArrow.includes("悪化")) {
        arrowSpan.style.color = "#ff7d7d";
        arrowSpan.textContent = "▼ 悪化";
      } else {
        arrowSpan.style.color = "#ffcc66";
        arrowSpan.textContent = "● 変化";
      }
      recEl.appendChild(arrowSpan);
    }
  }
  if (sentEl) {
    sentEl.textContent = "";
    const sentText = document.createTextNode(data.sentiment ?? "--");
    sentEl.appendChild(sentText);
    if (sentArrow) {
      const arrowSpan = document.createElement("span");
      arrowSpan.style.marginLeft = "5px";
      if (sentArrow.includes("改善")) {
        arrowSpan.style.color = "#7dffb0";
        arrowSpan.textContent = "▲ 改善";
      } else if (sentArrow.includes("悪化")) {
        arrowSpan.style.color = "#ff7d7d";
        arrowSpan.textContent = "▼ 悪化";
      } else {
        arrowSpan.style.color = "#ffcc66";
        arrowSpan.textContent = "● 変化";
      }
      sentEl.appendChild(arrowSpan);
    }
  }
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

  const aiSection = recEl?.closest(".ai-section");
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

async function requestStockAnalysis(stockKey) {
  if (!HAS_MISTRAL_API_KEY) throw new Error("APIキーが未設定です");
  const stock = getStockByKey(stockKey);
  if (!stock) throw new Error("最新の銘柄データを取得できませんでした");

  const headers = {
    "Content-Type": "application/json",
  };
  if (MISTRAL_API_KEY) headers["Authorization"] = `Bearer ${MISTRAL_API_KEY}`;
  if (LANGSEARCH_API_KEY) headers["X-LangSearch-Key"] = LANGSEARCH_API_KEY;

  const res = await fetch("/api/analyze-v2", {
    method: "POST",
    headers,
    body: JSON.stringify({
      symbol: stock.symbol,
      name: stock.name,
      price: stock.price,
      chart_data: stock.chart_data ?? [],
      sector: stock.sector,
      industry: stock.industry,
      market_cap: stock.market_cap,
      pe_ratio: stock.pe_ratio,
      market: stock.market,
    }),
  });

  const data = await res.json();
  if (!res.ok || data.error || data.parsed === false || !data.recommendation) {
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
  const originalText = btnEl.innerHTML;
  btnEl.innerHTML = '<span class="loading-spinner"></span>AI分析中...';
  btnEl.disabled = true;
  state.isAnalyzing = true;
  try {
    const { stock, data } = await requestStockAnalysis(stockKey);
    // すべてのラッパーに反映
    findAllWrappersByStockKey(stockKey).forEach((w) =>
      applyAnalysisResult(w, stock, data),
    );
  } catch (e) {
    logger.error("Analysis error:", e);
    findAllWrappersByStockKey(stockKey).forEach((w) =>
      applyAnalysisError(w, e.message),
    );
    showToast(`❌ 分析中にエラー: ${e.message}`, "#ff7d7d");
  } finally {
    btnEl.innerHTML = originalText;
    btnEl.disabled = false;
    state.isAnalyzing = false;
  }
}

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
  const originalText = btn?.innerHTML ?? "";
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
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span>お気に入り分析中...';
  }
  const success = [];
  const failed = [];
  try {
    setBulkAnalyzeStatus(
      `お気に入り ${targetKeys.length} 件を順番にAI分析します...\nAPI負荷を抑えるため逐次実行中です。`,
      "running",
    );
    for (let i = 0; i < targetKeys.length; i++) {
      const stockKey = targetKeys[i];
      const stock = getStockByKey(stockKey);
      if (!stock) continue;
      setBulkAnalyzeStatus(
        `(${i + 1}/${targetKeys.length}) ${stock.symbol} を analysis中...\n完了: ${success.length}件 / 失敗: ${failed.length}件`,
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
        logger.error(`Bulk analysis failed (${stock.symbol}):`, e);
        findAllWrappersByStockKey(stockKey).forEach((w) =>
          applyAnalysisError(w, e.message),
        );
        failed.push({
          symbol: stock.symbol,
          error: e.message || "不明なエラー",
        });
      }
      await sleep(350);
    }
    const successLines = success.map(
      (item) => `・${item.symbol}: ${item.recommendation} / ${item.sentiment}`,
    );
    const failedLines = failed.map((item) => `・${item.symbol}: ${item.error}`);
    const message =
      `一括AI分析が完了しました。\n` +
      `成功: ${success.length}件 / 失敗: ${failed.length}件\n\n` +
      (successLines.length ? `【成功】\n${successLines.join("\n")}\n\n` : "") +
      (failedLines.length ? `【失敗】\n${failedLines.join("\n")}` : "");
    setBulkAnalyzeStatus(message.trim(), failed.length ? "error" : "success");
  } finally {
    state.isAnalyzing = false;
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalText || "★ お気に入り一括AI分析";
    }
  }
}

// #endregion News & Trends

// #region Initialization
document.addEventListener("DOMContentLoaded", async () => {
  await refreshCredentialState();
  if (!HAS_MISTRAL_API_KEY) {
    window.location.href = "/setup";
    return;
  }
  updateApiStatus();
  document
    .getElementById("newsRefreshBtn")
    ?.addEventListener("click", forceRefreshNews);
  DOM.get("searchBtn")?.addEventListener("click", searchStocks);
  DOM.get("searchInput")?.addEventListener("keypress", (e) => {
    if (e.key === "Enter") searchStocks();
  });
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
  document
    .getElementById("bulkAnalyzeFavoritesBtn")
    ?.addEventListener("click", bulkAnalyzeFavorites);

  const streamToggleBtn = DOM.get("streamToggleBtn");
  if (streamToggleBtn) {
    const updateBtnUI = () => {
      const isAct = state.isStreaming;
      streamToggleBtn.classList.toggle("active", isAct);
      streamToggleBtn.querySelector(".stream-text").textContent = isAct
        ? "Live Streaming"
        : "Streaming Paused (60s polling)";
    };
    updateBtnUI();
    streamToggleBtn.addEventListener("click", () => {
      state.isStreaming = !state.isStreaming;
      localStorage.setItem("isStreamingEnabled", state.isStreaming);
      updateBtnUI();
      if (state.isStreaming) {
        showToast("✅ リアルタイム配信を開始します", "#7dffb0");
        connectSSE();
      } else {
        if (stockEventSource || sseApiClient.currentEventSource) {
          sseApiClient.closeSSE();
          stockEventSource = null;
        }
        if (sseReconnectTimer) {
          clearTimeout(sseReconnectTimer);
          sseReconnectTimer = null;
        }
        stopSseFallbackPolling();
        setStreamingIndicatorText("Streaming Paused (60s polling)");
        showToast("⏸️ リアルタイム配信を停止しました", "#ffcc66");
        connectSSE();
      }
    });
  }

  document.addEventListener("visibilitychange", () => {
    const activeSource = stockEventSource || sseApiClient.currentEventSource;
    if (!document.hidden) {
      // 表示復帰時は描画を最新化する（hidden中はUI更新を抑止しているため）
      fetchInitialStocks();
      if (!activeSource || activeSource.readyState === EventSource.CLOSED) {
        connectSSE();
      }
    }
  });

  setActiveTab("us");
  setBulkAnalyzeStatus("");

  // 即時取得（Live StreamingのON/OFFにかかわらず最初の一回は描画する）
  fetchInitialStocks(true).then(() => {
    connectSSE();
  });
  loadIndicesLoop();
  loadTrending();

  // ヒートマップからの ?q= パラメータ処理
  const urlParams = new URLSearchParams(window.location.search);
  const qParam = urlParams.get("q");
  if (qParam) {
    const searchInput = DOM.get("searchInput");
    if (searchInput) {
      searchInput.value = qParam;
      setTimeout(() => searchStocks(), 500);
    }
  }
});

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
    }

    const url = force ? "/api/stocks?force=true" : "/api/stocks";
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    if (!data) return;

    // Handle new response format { stocks: { us, jp, idx }, indices: { ... } }
    const stocksObj = data.stocks || data;
    const stocks = {
      us: (stocksObj.us || []).map((s) => ({ ...s, market: "us" })),
      jp: (stocksObj.jp || []).map((s) => ({ ...s, market: "jp" })),
      idx: (stocksObj.idx || []).map((s) => ({ ...s, market: "idx" })),
    };
    state.updateStocks(stocks);

    if (data.indices) {
      updateIndicesBar(data.indices);
    }

    renderStocks("us", state.stocks.us);
    renderStocks("jp", state.stocks.jp);
    renderStocks("idx", state.stocks.idx);
    skeletonShownAt = 0;
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
    const res = await fetch("/api/trending");
    const data = await res.json();
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

function parseRequiredNonNegativeNumber(value, label) {
  const raw = String(value ?? "").trim();
  if (!raw) return { ok: false, error: `${label}を入力してください` };
  const num = Number(raw);
  if (!Number.isFinite(num))
    return { ok: false, error: `${label}は数値で入力してください` };
  if (num < 0) return { ok: false, error: `${label}は0以上で入力してください` };
  return { ok: true, value: num };
}

function parseOptionalNonNegativeNumber(value, label) {
  const raw = String(value ?? "").trim();
  if (!raw) return { ok: true, value: null };
  const num = Number(raw);
  if (!Number.isFinite(num))
    return { ok: false, error: `${label}は数値で入力してください` };
  if (num < 0) return { ok: false, error: `${label}は0以上で入力してください` };
  return { ok: true, value: num };
}

const toFiniteNumber = (value, fallback = 0) => {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
};

// -----------------------------------------------------
// Portfolio Modal Logic
// -----------------------------------------------------
function openPortfolioModal(stockKey) {
  const stock = getStockByKey(stockKey);
  if (!stock) return;
  const modal = DOM.get("portfolioModal");
  if (!modal) return;
  modal.classList.add("show");
  modal.style.display = "flex";
  DOM.get("pf-modal-symbol").textContent = `${stock.symbol} - ${stock.name}`;
  DOM.get("pf-shares-input").value = toFiniteNumber(stock.shares, 0);
  DOM.get("pf-price-input").value = toFiniteNumber(stock.avg_price, 0);
  const fxInput = DOM.get("pf-fx-rate-input");
  if (fxInput)
    fxInput.value =
      stock.avg_fx_rate !== undefined && stock.avg_fx_rate !== null
        ? toFiniteNumber(stock.avg_fx_rate, 0)
        : "";

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

      const res = await fetch("/api/stocks/portfolio", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok && !payload.error) {
        showToast("✅ ポートフォリオを更新しました", "#7dffb0");
        modal.classList.remove("show");
        setTimeout(() => (modal.style.display = "none"), 300);
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
    const modal = DOM.get("portfolioModal");
    if (modal) {
      modal.classList.remove("show");
      setTimeout(() => (modal.style.display = "none"), 300);
    }
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
  const modal = DOM.get("alertModal");
  if (!modal) return;

  modal.classList.add("show");
  modal.style.display = "flex";
  DOM.get("alert-modal-symbol").textContent = `${stock.symbol} - アラート設定`;
  DOM.get("alert-price-up").value = cfg.priceUp || "";
  DOM.get("alert-price-down").value = cfg.priceDown || "";
  DOM.get("alert-ma-cross").checked = !!cfg.maCross;

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
    modal.classList.remove("show");
    setTimeout(() => (modal.style.display = "none"), 300);
  };
}

DOM.get("closeAlertModal")?.addEventListener("click", () => {
  const modal = DOM.get("alertModal");
  if (modal) {
    modal.classList.remove("show");
    setTimeout(() => (modal.style.display = "none"), 300);
  }
});

// window click to close modals and search results
window.addEventListener("click", (e) => {
  ["portfolioModal", "alertModal"].forEach((id) => {
    const m = document.getElementById(id);
    if (e.target === m) {
      m.classList.remove("show");
      setTimeout(() => (m.style.display = "none"), 300);
    }
  });

  const searchInput = DOM.get("searchInput");
  const searchResults = DOM.get("search-results");
  if (searchResults && searchResults.style.display !== "none") {
    if (!searchResults.contains(e.target) && e.target !== searchInput) {
      searchResults.style.display = "none";
    }
  }
});

function checkAlerts(stock, oldPrice) {
  if (oldPrice === undefined || oldPrice === null) return;
  const stockKey = makeStockKey(stock.market, stock.symbol);
  const cfg = getAlertsConfig()[stockKey];
  if (!cfg) return;
  let updateRequired = false;

  const currentPrice = stock.price;

  if (cfg.priceUp && !cfg.triggeredUp && currentPrice >= cfg.priceUp) {
    showToast(
      `🔔 【${stock.symbol}】 目標価格 (${cfg.priceUp}) に到達しました！ 現在値: ${currentPrice}`,
      "#7dffb0",
    );
    cfg.triggeredUp = true;
    updateRequired = true;
  } else if (cfg.priceUp && cfg.triggeredUp && currentPrice < cfg.priceUp) {
    // リセット
    cfg.triggeredUp = false;
    updateRequired = true;
  }

  if (cfg.priceDown && !cfg.triggeredDown && currentPrice <= cfg.priceDown) {
    showToast(
      `📉 【${stock.symbol}】 設定価格 (${cfg.priceDown}) を下回りました。 現在値: ${currentPrice}`,
      "#ff7d7d",
    );
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
          showToast(
            `🚀 【${stock.symbol}】 5日移動平均線を上抜けました！`,
            "#ffcc66",
          );
        } else if (oldPrice > ma5 && currentPrice <= ma5) {
          showToast(
            `⚠️ 【${stock.symbol}】 5日移動平均線を下抜けました。`,
            "#ffcc66",
          );
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

function showToast(message, color = "#fff") {
  const container = DOM.get("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.style.setProperty("--toast-accent", color);
  toast.textContent = message;

  container.appendChild(toast);

  requestAnimationFrame(() => {
    toast.classList.add("show");
  });

  // auto remove
  setTimeout(() => {
    if (!toast.isConnected) return;
    toast.classList.remove("show");
    toast.classList.add("hide");
    const onTransitionEnd = () => {
      toast.removeEventListener("transitionend", onTransitionEnd);
      if (toast.isConnected) toast.remove();
    };
    toast.addEventListener("transitionend", onTransitionEnd);
    setTimeout(() => {
      toast.removeEventListener("transitionend", onTransitionEnd);
      if (toast.isConnected) toast.remove();
    }, 350);
  }, 5000);
}
// #endregion Initialization
