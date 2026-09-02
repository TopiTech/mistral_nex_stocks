// #region Chart.js Plugins
// --- Chart.js Plugins ---
const domElements = {
  get apiStatus() {
    if (!this._apiStatus) {
      this._apiStatus = DOM.get("api-status-badge") || DOM.get("apiStatus");
    }
    return this._apiStatus;
  },
};

const crosshairPlugin = {
  id: "crosshair",
  afterDraw: (chart) => {
    if (chart.tooltip?._active?.length && chart.scales.y && chart.scales.x) {
      const activePoint = chart.tooltip._active[0];
      const ctx = chart.ctx;
      const x = activePoint.element.x;
      const y = activePoint.element.y;
      const leftX = chart.scales.x.left;
      const rightX = chart.scales.x.right;
      const topY = chart.scales.y.top;
      const bottomY = chart.scales.y.bottom;

      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, topY);
      ctx.lineTo(x, bottomY);
      ctx.moveTo(leftX, y);
      ctx.lineTo(rightX, y);
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(255, 255, 255, 0.35)";
      ctx.setLineDash([3, 3]);
      ctx.stroke();
      ctx.restore();
    }
  },
};

const aiTechnicalLinesPlugin = {
  id: "aiTechnicalLines",
  afterDatasetsDraw: (chart) => {
    const lines = chart.$aiTechnicalLines;
    if (!lines || !Array.isArray(lines) || lines.length === 0) return;
    const xScale = chart.scales.x;
    const yScale = chart.scales.y;
    if (!xScale || !yScale) return;

    const ctx = chart.ctx;
    ctx.save();

    lines.forEach((line) => {
      if (!line || line.start_price == null || line.end_price == null) return;

      let startX = xScale.left;
      let endX = xScale.right;

      if (line.start_date) {
        const startTs = new Date(line.start_date).getTime();
        if (Number.isFinite(startTs)) {
          const pixel = xScale.getPixelForValue(startTs);
          if (
            Number.isFinite(pixel) &&
            pixel >= xScale.left &&
            pixel <= xScale.right
          ) {
            startX = pixel;
          }
        }
      }

      if (line.end_date) {
        const endTs = new Date(line.end_date).getTime();
        if (Number.isFinite(endTs)) {
          const pixel = xScale.getPixelForValue(endTs);
          if (
            Number.isFinite(pixel) &&
            pixel >= xScale.left &&
            pixel <= xScale.right
          ) {
            endX = pixel;
          }
        }
      }

      const startY = yScale.getPixelForValue(line.start_price);
      const endY = yScale.getPixelForValue(line.end_price);

      if (!Number.isFinite(startY) || !Number.isFinite(endY)) return;

      const lineColor =
        line.color ||
        (line.type === "support"
          ? "#00ff88"
          : line.type === "resistance"
            ? "#ff3366"
            : "#3399ff");
      ctx.beginPath();
      ctx.moveTo(startX, startY);
      ctx.lineTo(endX, endY);
      ctx.lineWidth = line.type === "trend" ? 2.5 : 2;
      ctx.strokeStyle = lineColor;

      if (line.style === "dashed") {
        ctx.setLineDash([6, 4]);
      } else if (line.style === "dotted") {
        ctx.setLineDash([2, 3]);
      } else {
        ctx.setLineDash([]);
      }
      ctx.stroke();

      // Label Badge
      const currencySymbol =
        chart.$currency === "JPY" || chart.$market === "jp" ? "¥" : "$";
      const labelText =
        line.label ||
        `${(line.type || "").toUpperCase()} (${currencySymbol}${line.end_price})`;
      ctx.font = "bold 10px 'Orbitron', 'Noto Sans JP', sans-serif";
      const metrics = ctx.measureText(labelText);
      const bgWidth = metrics.width + 12;
      const bgHeight = 18;
      const badgeX = Math.min(endX - bgWidth, xScale.right - bgWidth - 4);
      const badgeY = endY - bgHeight / 2;

      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(13, 17, 30, 0.88)";
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (typeof ctx.roundRect === "function") {
        ctx.roundRect(badgeX, badgeY, bgWidth, bgHeight, 4);
      } else {
        ctx.rect(badgeX, badgeY, bgWidth, bgHeight);
      }
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = lineColor;
      ctx.fillText(labelText, badgeX + 6, badgeY + 13);
    });

    ctx.restore();
  },
};

if (typeof Chart !== "undefined") {
  Chart.register(crosshairPlugin);
  Chart.register(aiTechnicalLinesPlugin);
}

// Technical Indicators Calculation Helpers
function calculateSMA(series, period) {
  const result = [];
  for (let i = 0; i < series.length; i++) {
    if (i < period - 1) {
      result.push(null);
      continue;
    }
    let sum = 0;
    let validCount = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const val =
        typeof series[j] === "number"
          ? series[j]
          : (series[j]?.c ?? series[j]?.price);
      if (Number.isFinite(val)) {
        sum += val;
        validCount++;
      }
    }
    result.push(validCount === period ? sum / period : null);
  }
  return result;
}

function calculateEMA(series, period) {
  const result = [];
  const k = 2 / (period + 1);
  let prevEma = null;

  for (let i = 0; i < series.length; i++) {
    const val =
      typeof series[i] === "number"
        ? series[i]
        : (series[i]?.c ?? series[i]?.price);
    if (!Number.isFinite(val)) {
      result.push(null);
      continue;
    }
    if (prevEma === null) {
      if (i >= period - 1) {
        let sum = 0;
        for (let j = i - period + 1; j <= i; j++) {
          const v =
            typeof series[j] === "number"
              ? series[j]
              : (series[j]?.c ?? series[j]?.price);
          sum += v;
        }
        prevEma = sum / period;
        result.push(prevEma);
      } else {
        result.push(null);
      }
    } else {
      prevEma = val * k + prevEma * (1 - k);
      result.push(prevEma);
    }
  }
  return result;
}

function calculateBollingerBands(series, period = 20, multiplier = 2) {
  const upper = [];
  const middle = [];
  const lower = [];
  const sma = calculateSMA(series, period);

  for (let i = 0; i < series.length; i++) {
    const avg = sma[i];
    if (avg === null || i < period - 1) {
      upper.push(null);
      middle.push(null);
      lower.push(null);
      continue;
    }
    let varianceSum = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const val =
        typeof series[j] === "number"
          ? series[j]
          : (series[j]?.c ?? series[j]?.price);
      varianceSum += Math.pow(val - avg, 2);
    }
    const stdDev = Math.sqrt(varianceSum / period);
    middle.push(avg);
    upper.push(avg + multiplier * stdDev);
    lower.push(avg - multiplier * stdDev);
  }
  return { upper, middle, lower };
}

function calculateRSI(series, period = 14) {
  const result = [];
  if (series.length < period + 1) return series.map(() => null);

  let gains = 0;
  let losses = 0;

  for (let i = 1; i <= period; i++) {
    const prev =
      typeof series[i - 1] === "number"
        ? series[i - 1]
        : (series[i - 1]?.c ?? series[i - 1]?.price);
    const curr =
      typeof series[i] === "number"
        ? series[i]
        : (series[i]?.c ?? series[i]?.price);
    const change = curr - prev;
    if (change >= 0) gains += change;
    else losses -= change;
  }

  let avgGain = gains / period;
  let avgLoss = losses / period;

  result.push(...new Array(period).fill(null));
  if (avgLoss === 0) {
    result.push(avgGain === 0 ? 50 : 100);
  } else {
    const rs = avgGain / avgLoss;
    result.push(100 - 100 / (1 + rs));
  }

  for (let i = period + 1; i < series.length; i++) {
    const prev =
      typeof series[i - 1] === "number"
        ? series[i - 1]
        : (series[i - 1]?.c ?? series[i - 1]?.price);
    const curr =
      typeof series[i] === "number"
        ? series[i]
        : (series[i]?.c ?? series[i]?.price);
    const change = curr - prev;
    const gain = change >= 0 ? change : 0;
    const loss = change < 0 ? -change : 0;

    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;

    if (avgLoss === 0) {
      result.push(avgGain === 0 ? 50 : 100);
    } else {
      const rs = avgGain / avgLoss;
      result.push(100 - 100 / (1 + rs));
    }
  }
  return result;
}

function calculateMACD(
  series,
  fastPeriod = 12,
  slowPeriod = 26,
  signalPeriod = 9,
) {
  const fastEma = calculateEMA(series, fastPeriod);
  const slowEma = calculateEMA(series, slowPeriod);
  const macdLine = [];

  for (let i = 0; i < series.length; i++) {
    if (fastEma[i] !== null && slowEma[i] !== null) {
      macdLine.push(fastEma[i] - slowEma[i]);
    } else {
      macdLine.push(null);
    }
  }

  const validMacd = macdLine.filter((v) => v !== null);
  const signalValues = calculateEMA(validMacd, signalPeriod);

  const signalLine = [];
  const histogram = [];
  let sigIdx = 0;

  for (let i = 0; i < series.length; i++) {
    if (macdLine[i] === null) {
      signalLine.push(null);
      histogram.push(null);
    } else {
      const sig = signalValues[sigIdx++];
      signalLine.push(sig != null ? sig : null);
      histogram.push(
        sig != null && macdLine[i] != null ? macdLine[i] - sig : null,
      );
    }
  }

  return { macdLine, signalLine, histogram };
}

function calculateHeikinAshi(ohlcData) {
  if (!ohlcData || ohlcData.length === 0) return [];
  const haData = [];

  let prevHaOpen = (ohlcData[0].o + ohlcData[0].c) / 2;
  let prevHaClose =
    (ohlcData[0].o + ohlcData[0].h + ohlcData[0].l + ohlcData[0].c) / 4;

  for (let i = 0; i < ohlcData.length; i++) {
    const d = ohlcData[i];
    const ts = d.x || (d.date ? new Date(d.date).getTime() : 0);
    const o = d.o ?? d.price;
    const h = d.h ?? d.price;
    const l = d.l ?? d.price;
    const c = d.c ?? d.price;

    const haClose = (o + h + l + c) / 4;
    const haOpen = i === 0 ? (o + c) / 2 : (prevHaOpen + prevHaClose) / 2;
    const haHigh = Math.max(h, haOpen, haClose);
    const haLow = Math.min(l, haOpen, haClose);

    haData.push({
      x: ts,
      o: haOpen,
      h: haHigh,
      l: haLow,
      c: haClose,
      v: d.v || 0,
    });

    prevHaOpen = haOpen;
    prevHaClose = haClose;
  }
  return haData;
}

// Global configs are now initialized early in state.js to resolve loading order dependencies

// #endregion Chart.js Plugins

// #region Stock History & Prefetch
function getFreshPrefetchedHistory(stockKey, period, interval = "auto") {
  const key = getHistoryPrefetchKey(stockKey, period, interval);
  const entry = historyPrefetchCache.get(key);
  if (!entry) return null;
  if (Date.now() - entry.ts > CONSTANTS.PREFETCH.CACHE_TTL_MS) {
    historyPrefetchCache.delete(key);
    return null;
  }
  return entry;
}

async function fetchStockHistoryPayload(
  symbol,
  market,
  period,
  interval = "auto",
) {
  const cleanSymbol =
    symbol && typeof symbol === "string" && symbol.includes(":")
      ? symbol.split(":")[1]
      : symbol;
  const fetchUrl = `/api/stock-history?symbol=${encodeURIComponent(cleanSymbol)}&market=${market}&period=${period}&interval=${encodeURIComponent(interval || "auto")}`;

  const controller = new AbortController();
  const timeoutId = setTimeout(
    () => controller.abort(),
    CONSTANTS.TIMEOUT.STOCK_HISTORY,
  );

  const doFetch = async () => {
    let attempts = 0;
    const maxAttempts = 6;
    const delay = 1500;
    while (attempts < maxAttempts) {
      try {
        const { data } = await apiFetch(
          fetchUrl,
          { signal: controller.signal },
          { showToast: false },
        );
        if (data?.error) throw new Error(data.error);
        if (data?.fetching) {
          attempts++;
          if (attempts >= maxAttempts) {
            throw new Error(
              "履歴データの取得がタイムアウトしました。しばらくしてから再読み込みしてください。",
            );
          }
          await new Promise((resolve) => setTimeout(resolve, delay));
          continue;
        }
        if (!data?.history?.length) {
          if (data?.stale) {
            return normalizeHistoryData([]);
          }
          throw new Error("表示可能なヒストリカルデータがありません。");
        }
        return normalizeHistoryData(data.history);
      } catch (err) {
        if (err.name === "AbortError") {
          throw err;
        }
        if (err instanceof TypeError) {
          logger.warn(
            `Fetch failed for ${symbol} (${period}/${interval}), retrying...`,
          );
          const retryController = new AbortController();
          const abortRetry = () => retryController.abort();
          if (controller.signal.aborted) {
            retryController.abort();
          } else {
            controller.signal.addEventListener("abort", abortRetry, {
              once: true,
            });
          }
          const retryTimeoutId = setTimeout(
            () => retryController.abort(),
            CONSTANTS.TIMEOUT.STOCK_HISTORY_RETRY,
          );
          try {
            const { data: retryData } = await apiFetch(
              fetchUrl,
              {
                signal: retryController.signal,
              },
              { showToast: false },
            );
            if (retryData?.error) throw new Error(retryData.error);
            if (retryData?.fetching) {
              attempts++;
              if (attempts < maxAttempts) {
                await new Promise((resolve) => setTimeout(resolve, delay));
                continue;
              }
              throw new Error(
                "履歴データの取得がタイムアウトしました。しばらくしてから再読み込みしてください。",
              );
            }
            if (!retryData?.history?.length) {
              if (retryData?.stale) {
                return normalizeHistoryData([]);
              }
              throw new Error("表示可能なヒストリカルデータがありません。");
            }
            return normalizeHistoryData(retryData.history);
          } finally {
            clearTimeout(retryTimeoutId);
            controller.signal.removeEventListener("abort", abortRetry);
          }
        }
        throw err;
      }
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
 * @param {string} interval - 時間足 (例: "1d", "5m")。
 */
function prefetchStockHistory(
  wrapper,
  period = CONSTANTS.PREFETCH.PERIOD,
  interval,
) {
  if (!wrapper || wrapper.dataset.marketContext === "portfolio") return;
  const stockKey = wrapper.dataset.stockKey;
  const stock = wrapper.__stockData || getStockByKey(stockKey);
  if (!stock || !stock.symbol || !stock.market) return;
  const startedAt = Date.now();
  const effectiveInterval =
    interval || getChartPref(stockKey, "interval", "auto");

  const cacheKey = getHistoryPrefetchKey(stockKey, period, effectiveInterval);
  if (getFreshPrefetchedHistory(stockKey, period, effectiveInterval)) return;
  if (historyPrefetchInFlight.has(cacheKey)) return;

  const task = fetchStockHistoryPayload(
    stock.symbol,
    stock.market,
    period,
    effectiveInterval,
  )
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
    const stagger = CONSTANTS.PREFETCH.STAGGER_MS || 200;
    targets.forEach((wrapper, idx) => {
      const timerId = setTimeout(
        () => prefetchStockHistory(wrapper, CONSTANTS.PREFETCH.PERIOD),
        idx * stagger,
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

// Flush pending cache writes on page unload (safety net for 300ms debounce)
window.addEventListener("beforeunload", () => {
  _flushChartPrefsToStorage();
  _flushStockColors();
});

/**
 * Check Mistral AI API connectivity and update the header status badge.
 * Falls back to "API Key Required" or "Disconnected" on failure.
 */
async function updateApiStatus() {
  const badge = domElements.apiStatus;
  if (!badge) return;
  if (!HAS_MISTRAL_API_KEY) {
    badge.textContent = "● API Key Required";
    badge.classList.add("inactive");
    badge.classList.remove("connected");
    return;
  }
  try {
    await apiFetch("/api/health", {}, { showToast: false });
    badge.textContent = "Mistral API: Connected";
    badge.classList.remove("inactive");
    badge.classList.add("connected");
  } catch (_e) {
    badge.textContent = "Mistral API: Disconnected";
    badge.classList.add("inactive");
    badge.classList.remove("connected");
  }
}

// escapeHtmlはutils.jsで定義済み（全ページ共通）

/**
 * Safely create a DOM element using textContent (not innerHTML).
 * Eliminates XSS risk from raw HTML string construction.
 *
 * @param {string} tag - HTML tag name (e.g., "div", "span", "button")
 * @param {string} [className] - CSS class string
 * @param {string} [text] - Text content (set via textContent, not innerHTML)
 * @returns {HTMLElement}
 */
function createEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
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
  // Prefer the explicit currency; fall back on the market label so callers that
  // only carry a market string (e.g. the observatory's formatPrice(price,
  // market)) render ¥/$ instead of a raw "us "/"jp " prefix (R10).
  if (stock && typeof stock === "object") {
    if (stock.currency) return currencyPrefixFromCode(stock.currency);
    const m = String(stock.market || "").toLowerCase();
    if (m === "jp") return "¥";
    if (m === "us") return "$";
  } else if (typeof stock === "string") {
    const m = stock.toLowerCase();
    if (m === "jp" || m === "jpy") return "¥";
    if (m === "us" || m === "usd") return "$";
  }
  return "";
}

function formatPrice(value, stock) {
  const prefix = getCurrencySymbol(stock);
  if (
    value === null ||
    value === undefined ||
    value === "" ||
    typeof value === "boolean"
  ) {
    return `${prefix}--`;
  }
  const num = Number(value);
  if (Number.isFinite(num)) return `${prefix}${num.toLocaleString()}`;
  return `${prefix}${value ?? "--"}`;
}

// #region Chart Preferences Cache (in-memory + debounced localStorage persist)
//
// Performance: each getChartPref() call previously hit localStorage synchronously.
// With 3-4 calls per SSE tick per stock card, this caused measurable DOM I/O.
// The in-memory cache eliminates repeated reads; writes are debounced to batch
// consecutive preference changes into a single localStorage write.
//

const _chartPrefCache = new Map(); // key: "chart_${pref}_${stockKey}" -> value
const _chartPrefDirtyKeys = new Set();
let _chartPrefPersistTimer = null;

function _loadChartPrefIntoCache(key, defaultValue) {
  try {
    const raw = localStorage.getItem(key);
    const val = raw !== null ? raw : defaultValue;
    _chartPrefCache.set(key, val);
    return val;
  } catch {
    _chartPrefCache.set(key, defaultValue);
    return defaultValue;
  }
}

function _flushChartPrefsToStorage() {
  if (_chartPrefPersistTimer) {
    clearTimeout(_chartPrefPersistTimer);
    _chartPrefPersistTimer = null;
  }
  if (_chartPrefDirtyKeys.size === 0) return;
  try {
    for (const key of _chartPrefDirtyKeys) {
      const val = _chartPrefCache.get(key);
      if (val !== undefined) {
        localStorage.setItem(key, val);
      }
    }
  } catch {
    // storage full or blocked — silently degrade
  }
  _chartPrefDirtyKeys.clear();
}

if (typeof window !== "undefined") {
  window.addEventListener("beforeunload", _flushChartPrefsToStorage);
}

function _scheduleChartPrefPersist() {
  if (_chartPrefPersistTimer) clearTimeout(_chartPrefPersistTimer);
  _chartPrefPersistTimer = setTimeout(_flushChartPrefsToStorage, 300);
}

/**
 * Read a chart preference from the in-memory cache (lazy-populated from localStorage).
 * @param {string} stockKey
 * @param {string} pref - preference name ("type", "period", "volume")
 * @param {*} defaultVal
 * @returns {string}
 */
function getChartPref(stockKey, pref, defaultVal) {
  const key = `chart_${pref}_${stockKey}`;
  if (_chartPrefCache.has(key)) {
    return _chartPrefCache.get(key);
  }
  return _loadChartPrefIntoCache(key, defaultVal);
}

/**
 * Write a chart preference to the in-memory cache and schedule debounced persist.
 * @param {string} stockKey
 * @param {string} pref - preference name ("type", "period", "volume")
 * @param {string} val
 */
function setChartPref(stockKey, pref, val) {
  const key = `chart_${pref}_${stockKey}`;
  _chartPrefCache.set(key, val);
  _chartPrefDirtyKeys.add(key);
  _scheduleChartPrefPersist();
}

// Same pattern for stock_colors (bulk JSON)
let _stockColorsCache = null;
let _stockColorsDirty = false;
let _stockColorsPersistTimer = null;

function _loadStockColors() {
  if (_stockColorsCache !== null) return _stockColorsCache;
  try {
    const raw = JSON.parse(localStorage.getItem("stock_colors") || "{}");
    _stockColorsCache =
      typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  } catch {
    _stockColorsCache = {};
  }
  return _stockColorsCache;
}

function _flushStockColors() {
  if (_stockColorsPersistTimer) {
    clearTimeout(_stockColorsPersistTimer);
    _stockColorsPersistTimer = null;
  }
  if (!_stockColorsDirty) return;
  try {
    localStorage.setItem("stock_colors", JSON.stringify(_stockColorsCache));
  } catch {
    // silently degrade
  }
  _stockColorsDirty = false;
}

function _scheduleStockColorsPersist() {
  if (_stockColorsPersistTimer) clearTimeout(_stockColorsPersistTimer);
  _stockColorsPersistTimer = setTimeout(_flushStockColors, 300);
}

function getStockColor(stockKey) {
  const colors = _loadStockColors();
  return colors[stockKey] || null;
}

function saveStockColor(stockKey, color) {
  const normalized = isValidHexColor(color) ? color.trim() : null;
  if (!normalized) return;
  const colors = _loadStockColors();
  colors[stockKey] = normalized;
  _stockColorsCache = colors;
  _stockColorsDirty = true;
  _scheduleStockColorsPersist();
}

/**
 * Update the accent color for a stock card (border + symbol text).
 * Persists the color to localStorage.
 * @param {string} stockKey
 * @param {string} color - A valid 6-digit hex color.
 */
function updateStockColor(stockKey, color) {
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
}

// getSortOrderはutils.jsで定義済み（全ページ共通）

function orderIndex(order, symbol) {
  const idx = order.indexOf(symbol);
  return idx === -1 ? Number.MAX_SAFE_INTEGER : idx;
}

function setActiveTab(tab) {
  [
    ["tab-us", "us"],
    ["tab-jp", "jp"],
    ["tab-idx", "idx"],
    ["tab-portfolio", "portfolio"],
  ].forEach(([id, value]) => {
    const tabElement = DOM.get(id);
    tabElement?.classList.toggle("active", tab === value);
    tabElement?.setAttribute("aria-selected", String(tab === value));
    tabElement?.setAttribute("tabindex", tab === value ? "0" : "-1");
  });

  const us = DOM.get("us-stocks");
  const jp = DOM.get("jp-stocks");
  const idx = DOM.get("idx-stocks");
  const pf = DOM.get("portfolio-wrapper");

  [us, jp, idx, pf].forEach((panel) => panel?.removeAttribute("hidden"));
  if (us) {
    us.style.display = tab === "us" ? "grid" : "none";
    us.toggleAttribute("hidden", tab !== "us");
  }
  if (jp) {
    jp.style.display = tab === "jp" ? "grid" : "none";
    jp.toggleAttribute("hidden", tab !== "jp");
  }
  if (idx) {
    idx.style.display = tab === "idx" ? "grid" : "none";
    idx.toggleAttribute("hidden", tab !== "idx");
  }
  if (pf) {
    pf.style.display = tab === "portfolio" ? "block" : "none";
    pf.toggleAttribute("hidden", tab !== "portfolio");
  }

  if (tab === "portfolio") {
    // ポートフォリオタブを開いた瞬間の為替レートを固定する (視認性向上のため)
    portfolioFixedExchangeRate = state.indices?.USDJPY?.price || null;
    renderPortfolio();
  }

  requestAnimationFrame(() => {
    scheduleHistoryPrefetchWarmup();
  });
}

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
    const isHidden =
      hiddenByLabel instanceof Map
        ? hiddenByLabel.get(ds.label)
        : hiddenByLabel[ds.label];
    if (typeof isHidden === "boolean") {
      if (typeof chart.setDatasetVisibility === "function") {
        chart.setDatasetVisibility(index, !isHidden);
      }
      ds.hidden = isHidden;
    }
  });
}

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
        ticks: {
          color: "#ccc",
          maxTicksLimit: 8,
          font: {
            family: "'Orbitron', 'Noto Sans JP', sans-serif",
            size: 10,
          },
        },
        grid: { color: "rgba(255,255,255,0.05)" },
      },
      y: {
        position: "left",
        ticks: {
          color: "#ccc",
          font: {
            family: "'Orbitron', 'Noto Sans JP', sans-serif",
            size: 10,
          },
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
          font: {
            family: "'Orbitron', 'Noto Sans JP', sans-serif",
            size: 10,
          },
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
  const canvas =
    options.targetCanvas ||
    (wrapper ? wrapper.querySelector(".chart-canvas") : null) ||
    document.querySelector("#stock-detail-drawer .chart-canvas");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  if (!data || data.length < 2) return;

  const stockKey = wrapper
    ? wrapper.dataset.stockKey
    : options.stockKey || "default";
  const type = options.type || getChartPref(stockKey, "type", "candlestick");
  const period =
    options.period ||
    (wrapper ? getChartPref(stockKey, "period", "3mo") : "3mo");
  const interval =
    options.interval ||
    (wrapper ? getChartPref(stockKey, "interval", "auto") : "auto");
  const showVolume =
    (options.volume || getChartPref(stockKey, "volume", "on")) !== "off";

  // Indicator preferences
  const showMA5 = getChartPref(stockKey, "ind_ma5", "on") !== "off";
  const showMA25 = getChartPref(stockKey, "ind_ma25", "on") !== "off";
  const showMA75 = getChartPref(stockKey, "ind_ma75", "off") === "on";
  const showMA200 = getChartPref(stockKey, "ind_ma200", "off") === "on";
  const showBollinger = getChartPref(stockKey, "ind_bollinger", "off") === "on";
  const showRSI = getChartPref(stockKey, "ind_rsi", "off") === "on";
  const showMACD = getChartPref(stockKey, "ind_macd", "off") === "on";
  const showAILines = getChartPref(stockKey, "ind_ai", "on") !== "off";

  destroyChart(canvas);

  const isIntradayInterval =
    ["1m", "2m", "5m", "15m", "30m", "60m", "1h"].includes(interval) ||
    (interval === "auto" && (period === "1d" || period === "5d"));
  const timeConfig = isIntradayInterval
    ? {
        unit: ["1m", "2m", "5m", "15m"].includes(interval) ? "minute" : "hour",
        displayFormats: {
          minute: "HH:mm",
          hour: "MM/dd HH:mm",
          day: "MM/dd",
        },
      }
    : {
        unit:
          interval === "1mo" ? "month" : interval === "1wk" ? "week" : "day",
        displayFormats: {
          day: "MM/dd",
          week: "yyyy/MM/dd",
          month: "yyyy/MM",
          hour: "MM/dd HH:mm",
        },
      };

  const rawBaseData = ohlcData && ohlcData.length > 0 ? ohlcData : data;
  const normalizedOhlc = rawBaseData
    .map((d) => {
      const ts = d.x || (d.date ? new Date(d.date).getTime() : 0);
      return {
        x: ts,
        o: d.o != null ? d.o : d.price,
        h: d.h != null ? d.h : d.price,
        l: d.l != null ? d.l : d.price,
        c: d.c != null ? d.c : d.price,
        v: d.v != null ? d.v : d.y || 0,
        price: d.price != null ? d.price : d.c != null ? d.c : d.o,
      };
    })
    .filter((d) => d.x > 0);

  const closeSeries = normalizedOhlc.map((d) => ({ x: d.x, y: d.c }));
  const datasets = [];

  // 1. Primary Stock Data Series
  if (type === "candlestick" || type === "heikin_ashi") {
    const chartOhlc =
      type === "heikin_ashi"
        ? calculateHeikinAshi(normalizedOhlc)
        : normalizedOhlc;
    const candleData = chartOhlc.map((d) => ({
      x: d.x,
      o: d.o,
      h: d.h,
      l: d.l,
      c: d.c,
    }));
    datasets.push({
      type: "candlestick",
      label: type === "heikin_ashi" ? "平均足" : "ローソク足",
      data: candleData,
      yAxisID: "y",
      color: { up: "#00ff88", down: "#ff3366", unchanged: "#999" },
      borderColor: { up: "#00ff88", down: "#ff3366", unchanged: "#999" },
    });
  } else if (type === "area") {
    const gradient = ctx.createLinearGradient(0, 0, 0, 300);
    gradient.addColorStop(0, "rgba(0, 229, 255, 0.4)");
    gradient.addColorStop(1, "rgba(0, 229, 255, 0.0)");
    datasets.push({
      type: "line",
      label: "株価 (エリア)",
      data: closeSeries,
      borderColor: "#00e5ff",
      borderWidth: 2,
      backgroundColor: gradient,
      fill: true,
      tension: 0.3,
      pointRadius: 0,
      yAxisID: "y",
    });
  } else {
    // Standard Line
    datasets.push({
      type: "line",
      label: "終値",
      data: closeSeries,
      borderColor: "#6bb6ff",
      borderWidth: 2,
      tension: 0.2,
      pointRadius: 0,
      yAxisID: "y",
    });
  }

  // 2. Moving Averages
  if (showMA5) {
    const ma5Values = calculateSMA(normalizedOhlc, 5);
    const ma5Data = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: ma5Values[i] }))
      .filter((d) => d.y !== null);
    if (ma5Data.length > 0) {
      datasets.push({
        type: "line",
        label: "MA5",
        data: ma5Data,
        borderColor: "#ffcc66",
        borderWidth: 1.2,
        borderDash: [3, 2],
        tension: 0.2,
        pointRadius: 0,
        fill: false,
        yAxisID: "y",
      });
    }
  }

  if (showMA25) {
    const ma25Values = calculateSMA(normalizedOhlc, 25);
    const ma25Data = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: ma25Values[i] }))
      .filter((d) => d.y !== null);
    if (ma25Data.length > 0) {
      datasets.push({
        type: "line",
        label: "MA25",
        data: ma25Data,
        borderColor: "#ff7daa",
        borderWidth: 1.2,
        borderDash: [5, 3],
        tension: 0.2,
        pointRadius: 0,
        fill: false,
        yAxisID: "y",
      });
    }
  }

  if (showMA75) {
    const ma75Values = calculateSMA(normalizedOhlc, 75);
    const ma75Data = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: ma75Values[i] }))
      .filter((d) => d.y !== null);
    if (ma75Data.length > 0) {
      datasets.push({
        type: "line",
        label: "MA75",
        data: ma75Data,
        borderColor: "#00e5ff",
        borderWidth: 1.5,
        tension: 0.2,
        pointRadius: 0,
        fill: false,
        yAxisID: "y",
      });
    }
  }

  if (showMA200) {
    const ma200Values = calculateSMA(normalizedOhlc, 200);
    const ma200Data = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: ma200Values[i] }))
      .filter((d) => d.y !== null);
    if (ma200Data.length > 0) {
      datasets.push({
        type: "line",
        label: "MA200",
        data: ma200Data,
        borderColor: "#b388ff",
        borderWidth: 1.5,
        tension: 0.2,
        pointRadius: 0,
        fill: false,
        yAxisID: "y",
      });
    }
  }

  // 3. Bollinger Bands (±2σ)
  if (showBollinger) {
    const bb = calculateBollingerBands(normalizedOhlc, 20, 2);
    const upperData = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: bb.upper[i] }))
      .filter((d) => d.y !== null);
    const lowerData = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: bb.lower[i] }))
      .filter((d) => d.y !== null);

    if (upperData.length > 0) {
      datasets.push({
        type: "line",
        label: "BB +2σ",
        data: upperData,
        borderColor: "rgba(255, 170, 0, 0.7)",
        borderWidth: 1,
        borderDash: [4, 4],
        pointRadius: 0,
        fill: false,
        yAxisID: "y",
      });
      datasets.push({
        type: "line",
        label: "BB -2σ",
        data: lowerData,
        borderColor: "rgba(255, 170, 0, 0.7)",
        borderWidth: 1,
        borderDash: [4, 4],
        backgroundColor: "rgba(255, 170, 0, 0.06)",
        fill: "-1",
        pointRadius: 0,
        yAxisID: "y",
      });
    }
  }

  // 4. Volume Series
  if (showVolume) {
    const volumeData = normalizedOhlc.map((d) => ({
      x: d.x,
      y: d.v || 0,
    }));
    datasets.push({
      type: "bar",
      label: "出来高",
      data: volumeData,
      yAxisID: "yVolume",
      backgroundColor: normalizedOhlc.map((d) =>
        d.c >= d.o ? "rgba(0, 255, 136, 0.25)" : "rgba(255, 51, 102, 0.25)",
      ),
      borderColor: normalizedOhlc.map((d) =>
        d.c >= d.o ? "rgba(0, 255, 136, 0.5)" : "rgba(255, 51, 102, 0.5)",
      ),
      borderWidth: 1,
      barThickness: "flex",
    });
  }

  // 5. RSI Indicator Series (Sub-scale)
  if (showRSI) {
    const rsiValues = calculateRSI(normalizedOhlc, 14);
    const rsiData = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: rsiValues[i] }))
      .filter((d) => d.y !== null);
    if (rsiData.length > 0) {
      datasets.push({
        type: "line",
        label: "RSI(14)",
        data: rsiData,
        borderColor: "#e040fb",
        borderWidth: 1.5,
        pointRadius: 0,
        yAxisID: "yRSI",
      });
    }
  }

  // 6. MACD Indicator Series (Sub-scale)
  if (showMACD) {
    const macdObj = calculateMACD(normalizedOhlc, 12, 26, 9);
    const macdLineData = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: macdObj.macdLine[i] }))
      .filter((d) => d.y !== null);
    const signalLineData = normalizedOhlc
      .map((d, i) => ({ x: d.x, y: macdObj.signalLine[i] }))
      .filter((d) => d.y !== null);

    if (macdLineData.length > 0) {
      datasets.push({
        type: "line",
        label: "MACD",
        data: macdLineData,
        borderColor: "#00e5ff",
        borderWidth: 1.5,
        pointRadius: 0,
        yAxisID: "yMACD",
      });
      datasets.push({
        type: "line",
        label: "Signal",
        data: signalLineData,
        borderColor: "#ff9800",
        borderWidth: 1.5,
        pointRadius: 0,
        yAxisID: "yMACD",
      });
    }
  }

  const baseOptions = createBaseChartOptions(animate, timeConfig, showVolume);

  // Configure additional scales for RSI and MACD
  if (showRSI) {
    baseOptions.scales.yRSI = {
      display: true,
      position: "right",
      min: 0,
      max: 100,
      grid: { color: "rgba(224, 64, 251, 0.1)" },
      ticks: {
        color: "#e040fb",
        font: { size: 9 },
        stepSize: 30,
      },
    };
  }

  if (showMACD) {
    baseOptions.scales.yMACD = {
      display: true,
      position: "right",
      grid: { color: "rgba(0, 229, 255, 0.1)" },
      ticks: {
        color: "#00e5ff",
        font: { size: 9 },
        maxTicksLimit: 4,
      },
    };
  }

  const isFinancialChart = type === "candlestick" || type === "heikin_ashi";

  const chart = new Chart(ctx, {
    type: isFinancialChart ? "candlestick" : "line",
    data: { datasets },
    options: {
      ...baseOptions,
      parsing: false,
      plugins: {
        legend: {
          display: true,
          position: "top",
          labels: {
            color: "#ccc",
            boxWidth: 10,
            font: {
              family: "'Orbitron', 'Noto Sans JP', sans-serif",
              size: 10,
            },
          },
        },
        tooltip: {
          ...CHART_TOOLTIP_DEFAULTS,
          callbacks: {
            label: function (context) {
              const currSymbol = wrapper
                ? getCurrencySymbol(
                    wrapper.__stockData || getStockByKey(stockKey),
                  )
                : "";
              const fmt = (v) =>
                v == null || !Number.isFinite(Number(v))
                  ? "--"
                  : `${currSymbol}${Number(v).toLocaleString(undefined, {
                      maximumFractionDigits: 2,
                    })}`;
              if (context.dataset.yAxisID === "yVolume")
                return `出来高: ${context.raw.y?.toLocaleString() || "--"}`;
              if (context.dataset.yAxisID === "yRSI")
                return `RSI: ${context.raw.y != null ? context.raw.y.toFixed(2) : "--"}`;
              if (context.dataset.yAxisID === "yMACD")
                return `${context.dataset.label}: ${context.raw.y != null ? context.raw.y.toFixed(2) : "--"}`;
              if (context.raw.o != null) {
                const d = context.raw;
                return `始:${fmt(d.o)} 高:${fmt(d.h)} 安:${fmt(d.l)} 終:${fmt(d.c)}`;
              }
              return `${context.dataset.label}: ${fmt(context.raw.y)}`;
            },
          },
        },
      },
    },
  });

  chart.$period = period;
  chart.$market =
    options.market ||
    (wrapper ? wrapper.dataset.market : null) ||
    (stockKey && stockKey.endsWith(".T") ? "jp" : "us");
  chart.$currency =
    options.currency || (chart.$market === "jp" ? "JPY" : "USD");

  // Pass AI technical lines data to chart for the custom plugin if enabled
  if (showAILines) {
    const aiLines =
      options.aiTechnicalLines || (wrapper ? wrapper.__aiTechnicalLines : null);
    if (aiLines && Array.isArray(aiLines.lines)) {
      chart.$aiTechnicalLines = aiLines.lines;
    }
  }

  chartInstances.set(canvas, chart);
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
      plugins: {
        legend: { display: false },
        tooltip: {
          ...CHART_TOOLTIP_DEFAULTS,
          callbacks: {
            label: function (context) {
              const yVal = context.raw?.y;
              return `${context.dataset.label}: ${yVal != null && Number.isFinite(Number(yVal)) ? Number(yVal).toFixed(2) : "--"}%`;
            },
          },
        },
      },
      scales: {
        x: {
          type: "time",
          time: { unit: "day", displayFormats: { day: "MM/dd" } },
          ticks: {
            color: "#ccc",
            maxTicksLimit: 8,
            font: {
              family: "'Orbitron', 'Noto Sans JP', sans-serif",
              size: 10,
            },
          },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
        y: {
          ticks: {
            color: "#ccc",
            font: {
              family: "'Orbitron', 'Noto Sans JP', sans-serif",
              size: 10,
            },
            callback: (val) =>
              val != null && Number.isFinite(Number(val))
                ? Number(val).toFixed(2) + "%"
                : "--%",
          },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
      },
    },
  });
  chartInstances.set(canvas, chart);
}

const NO_HISTORY_MSG = "表示可能なヒストリカルデータがありません。";

// Reusable chart tooltip defaults to eliminate duplication across chart types
const CHART_TOOLTIP_DEFAULTS = {
  backgroundColor: "rgba(13, 17, 30, 0.88)",
  titleColor: "#9bc9ff",
  bodyColor: "#e8f0ff",
  borderColor: "rgba(107, 182, 255, 0.25)",
  borderWidth: 1,
  cornerRadius: 8,
  padding: 10,
  displayColors: false,
  titleFont: {
    family: "'Orbitron', 'Noto Sans JP', sans-serif",
    size: 11,
    weight: "bold",
  },
  bodyFont: {
    family: "'Noto Sans JP', sans-serif",
    size: 11,
  },
};

function drawFullscreenChartIfActive(
  wrapper,
  formattedData,
  ohlcData,
  period,
  interval,
) {
  if (!wrapper) return;
  const stockKey = wrapper.dataset.stockKey;
  if (!stockKey) return;
  const fsModal = document.getElementById("chart-fullscreen-modal");
  if (
    fsModal &&
    !fsModal.classList.contains("hidden") &&
    fsModal.dataset.stockKey === stockKey
  ) {
    const fsCanvas = document.getElementById("fs-chart-canvas");
    if (fsCanvas) {
      drawChart(wrapper, formattedData, ohlcData, {
        targetCanvas: fsCanvas,
        aiTechnicalLines: wrapper.__aiTechnicalLines,
        period: period,
        interval: interval,
      });
    }
  }
}

async function refreshStockChart(wrapper, period, interval) {
  const stockKey = wrapper.dataset.stockKey;
  const stock = getStockByKey(stockKey);
  if (!stock) return;

  const targetPeriod = period || getChartPref(stockKey, "period", "3mo");
  const targetInterval = interval || getChartPref(stockKey, "interval", "auto");

  const currentFetchId = Date.now().toString() + Math.random().toString();
  wrapper.dataset.chartFetchId = currentFetchId;

  if (typeof window.updateIntervalControlsVisibility === "function") {
    window.updateIntervalControlsVisibility(wrapper, stockKey, targetPeriod);
    const detailDrawer = document.getElementById("stock-detail-drawer");
    if (detailDrawer)
      window.updateIntervalControlsVisibility(
        detailDrawer,
        stockKey,
        targetPeriod,
      );
  }

  const prefetchEntry = getFreshPrefetchedHistory(
    stockKey,
    targetPeriod,
    targetInterval,
  );
  if (prefetchEntry) {
    clearChartError(wrapper);
    const { formattedData, ohlcData } = prefetchEntry;
    applyHistoryToStockAndWrapper(wrapper, formattedData, ohlcData);
    if (wrapper.dataset.marketContext !== "portfolio") {
      drawChart(wrapper, formattedData, ohlcData, {
        animate: true,
        period: targetPeriod,
        interval: targetInterval,
      });
      drawFullscreenChartIfActive(
        wrapper,
        formattedData,
        ohlcData,
        targetPeriod,
        targetInterval,
      );
    } else {
      const pnlCanvas =
        wrapper.querySelector(".chart-canvas-pnl") ||
        document.querySelector("#stock-detail-drawer .chart-canvas-pnl");
      if (pnlCanvas)
        drawPnLChart(pnlCanvas, formattedData, stock.avg_price, {
          animate: true,
        });
    }
    wrapper.dataset.lastRefresh = Date.now().toString();
    return;
  }

  const container =
    wrapper.querySelector(".chart-container") ||
    document.querySelector("#stock-detail-drawer .chart-container");
  container?.classList?.add("loading");

  try {
    let result;
    try {
      result = await fetchStockHistoryPayload(
        stock.symbol,
        stock.market,
        targetPeriod,
        targetInterval,
      );
    } catch (firstErr) {
      // 初回読み込み時にバックエンドがデータ未キャッシュの場合、
      // 一時的に空の履歴が返る可能性がある。短い遅延後にリトライする。
      const firstMsg = firstErr?.message ?? "";
      if (firstMsg.includes(NO_HISTORY_MSG)) {
        logger.info(
          `History empty on first attempt for ${stock.symbol}, retrying after delay...`,
        );
        clearChartError(wrapper);
        showChartError(wrapper, "データを読み込み中です...", "info");
        await new Promise((r) => setTimeout(r, 3000));
        // 再試行前にプレフェッチキャッシュを再チェック
        const retryPrefetch = getFreshPrefetchedHistory(
          stockKey,
          targetPeriod,
          targetInterval,
        );
        if (retryPrefetch) {
          result = retryPrefetch;
        } else {
          result = await fetchStockHistoryPayload(
            stock.symbol,
            stock.market,
            targetPeriod,
            targetInterval,
          );
        }
      } else {
        throw firstErr;
      }
    }

    if (wrapper.dataset.chartFetchId !== currentFetchId) {
      logger.debug(
        `Stale chart data fetch resolved for ${stock.symbol}, discarding.`,
      );
      return;
    }

    const { formattedData, ohlcData } = result;
    wrapper.dataset.lastRefresh = Date.now().toString();

    historyPrefetchCache.set(
      getHistoryPrefetchKey(stockKey, targetPeriod, targetInterval),
      {
        formattedData,
        ohlcData,
        ts: Date.now(),
      },
    );

    clearChartError(wrapper);
    applyHistoryToStockAndWrapper(wrapper, formattedData, ohlcData);

    if (wrapper.dataset.marketContext !== "portfolio") {
      drawChart(wrapper, formattedData, ohlcData, {
        period: targetPeriod,
        interval: targetInterval,
      });
      drawFullscreenChartIfActive(
        wrapper,
        formattedData,
        ohlcData,
        targetPeriod,
        targetInterval,
      );
    } else {
      const pnlCanvas =
        wrapper.querySelector(".chart-canvas-pnl") ||
        document.querySelector("#stock-detail-drawer .chart-canvas-pnl");
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
      msg.includes(NO_HISTORY_MSG);
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
  const findEl = (sel) =>
    wrapper.querySelector(sel) ||
    document.querySelector(`#stock-detail-drawer ${sel}`);
  const sectorEl = findEl(".detail-sector");
  const industryEl = findEl(".detail-industry");
  const mcapEl = findEl(".detail-mcap");
  const peEl = findEl(".detail-pe");

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
