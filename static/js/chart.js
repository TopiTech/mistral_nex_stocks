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
if (typeof Chart !== "undefined") {
  Chart.register(crosshairPlugin);
}
// Global configs are now initialized early in state.js to resolve loading order dependencies

// Settings button navigation (moved from inline onclick for CSP hygiene)
DOM.get("settingsBtn")?.addEventListener("click", () => {
  window.location.href = "/settings";
});

// #endregion Chart.js Plugins

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
    let attempts = 0;
    const maxAttempts = 6;
    const delay = 1500;
    while (attempts < maxAttempts) {
      try {
        const res = await fetch(fetchUrl, { signal: controller.signal });
        if (!res.ok) throw new Error(`HTTP Error: ${res.status}`);
        const data = await res.json();
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
            if (!retryData?.history?.length)
              throw new Error("表示可能なヒストリカルデータがありません。");
            return normalizeHistoryData(retryData.history);
          } finally {
            clearTimeout(retryTimeoutId);
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
    const res = await fetch("/api/health");
    if (res.ok) {
      badge.textContent = "Mistral API: Connected";
      badge.classList.remove("inactive");
      badge.classList.add("connected");
    }
  } catch (e) {
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
  return currencyPrefixFromCode(stock?.currency);
}

function formatPrice(value, stock) {
  const num = Number(value);
  const prefix = getCurrencySymbol(stock);
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
            ...CHART_TOOLTIP_DEFAULTS,
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
            labels: {
              color: "#ccc",
              boxWidth: 12,
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
      plugins: {
        legend: { display: false },
        tooltip: {
          ...CHART_TOOLTIP_DEFAULTS,
          callbacks: {
            label: function (context) {
              return `${context.dataset.label}: ${context.raw.y.toFixed(2)}%`;
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
            callback: (val) => val.toFixed(2) + "%",
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
    let result;
    try {
      result = await fetchStockHistoryPayload(
        stock.symbol,
        stock.market,
        period,
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
        const retryPrefetch = getFreshPrefetchedHistory(stockKey, period);
        if (retryPrefetch) {
          result = retryPrefetch;
        } else {
          result = await fetchStockHistoryPayload(
            stock.symbol,
            stock.market,
            period,
          );
        }
      } else {
        throw firstErr;
      }
    }

    const { formattedData, ohlcData } = result;
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
