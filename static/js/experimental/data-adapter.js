/**
 * data-adapter.js - Normalizes market data for the Market Observatory.
 *
 * Provides defensive transformation, missing-value fallbacks,
 * and clamped visual mapping values without mutating raw responses.
 */

(function (global) {
  "use strict";

  function toFiniteNumberSafe(value, fallback = 0) {
    if (
      value === null ||
      value === undefined ||
      value === "" ||
      typeof value === "boolean"
    ) {
      return fallback;
    }
    const num = Number(value);
    return Number.isFinite(num) ? num : fallback;
  }

  function clamp(val, min, max) {
    return Math.min(Math.max(val, min), max);
  }

  function currencyForStock(stockOrMarket) {
    if (stockOrMarket && typeof stockOrMarket === "object") {
      const currency = String(stockOrMarket.currency || "").toUpperCase();
      if (currency) return currency;
      return String(stockOrMarket.market || "").toLowerCase() === "jp"
        ? "JPY"
        : "USD";
    }
    return String(stockOrMarket || "").toLowerCase() === "jp" ? "JPY" : "USD";
  }

  function formatPrice(value, stockOrMarket) {
    if (
      value === null ||
      value === undefined ||
      value === "" ||
      typeof value === "boolean"
    ) {
      return "--";
    }
    const num = Number(value);
    if (!Number.isFinite(num)) return "--";
    if (currencyForStock(stockOrMarket) === "JPY") {
      return `¥${Math.round(num).toLocaleString("ja-JP")}`;
    }
    return `$${num.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function formatMarketCap(value, stockOrMarket) {
    if (
      value === null ||
      value === undefined ||
      value === "" ||
      typeof value === "boolean"
    ) {
      return "--";
    }
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return "--";
    if (currencyForStock(stockOrMarket) === "JPY") {
      if (num >= 1e12) return `¥${(num / 1e12).toFixed(1)}兆`;
      if (num >= 1e8) return `¥${(num / 1e8).toFixed(0)}億`;
      return `¥${num.toLocaleString("ja-JP")}`;
    }
    if (num >= 1e12) return `$${(num / 1e12).toFixed(2)}T`;
    if (num >= 1e9) return `$${(num / 1e9).toFixed(2)}B`;
    if (num >= 1e6) return `$${(num / 1e6).toFixed(1)}M`;
    return `$${num.toLocaleString()}`;
  }

  /**
   * Normalize a raw stock object into an internal Observatory model.
   * @param {Object} raw - Raw stock data from API
   * @param {Object} [options] - Additional context (tier, isCenter, etc.)
   * @returns {Object} Normalized stock model
   */
  function inferMarketFromSymbol(symbol, fallbackMarket) {
    const normalizedFallback = String(fallbackMarket || "")
      .trim()
      .toLowerCase();
    if (
      normalizedFallback === "us" ||
      normalizedFallback === "jp" ||
      normalizedFallback === "idx"
    ) {
      return normalizedFallback;
    }
    if (/\.T$/i.test(symbol)) return "jp";
    if (symbol.startsWith("^") || symbol.includes("=X")) return "idx";
    return "us";
  }

  function formatLocalDateStr(ms) {
    if (!Number.isFinite(ms)) return "";
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return "";
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function normalizeObservatoryStock(raw, options = {}) {
    if (!raw || typeof raw !== "object") {
      return null;
    }

    const symbol = String(raw.symbol || options.symbol || "")
      .trim()
      .toUpperCase();
    if (!symbol) return null;

    const displayName = String(raw.name || raw.display_name || symbol).trim();
    const market = inferMarketFromSymbol(symbol, raw.market || options.market);
    const price = toFiniteNumberSafe(raw.price, 0);
    const rawChange = toFiniteNumberSafe(raw.change, 0);
    const rawChangePercent = toFiniteNumberSafe(
      raw.change_percent !== undefined ? raw.change_percent : raw.changePercent,
      0,
    );
    const volume = toFiniteNumberSafe(raw.volume, 0);
    const marketCap = toFiniteNumberSafe(
      raw.market_cap !== undefined ? raw.market_cap : raw.marketCap,
      0,
    );
    const sector = String(raw.sector || "その他").trim();
    const industry = String(raw.industry || "").trim();
    const peRatio = toFiniteNumberSafe(
      raw.pe_ratio !== undefined ? raw.pe_ratio : raw.peRatio,
      null,
    );
    // /api/stock-details と /api/stocks 行ビルダーは snake_case の
    // fifty_two_week_high/low を送出するため、フォールバック連鎖に含める
    // (camelCase(yfinance 原始キー)のみを見ていては常に 0 のままだった)。
    const high52 = toFiniteNumberSafe(
      raw.high52 !== undefined
        ? raw.high52
        : raw.fiftyTwoWeekHigh !== undefined
          ? raw.fiftyTwoWeekHigh
          : raw.fifty_two_week_high,
      0,
    );
    const low52 = toFiniteNumberSafe(
      raw.low52 !== undefined
        ? raw.low52
        : raw.fiftyTwoWeekLow !== undefined
          ? raw.fiftyTwoWeekLow
          : raw.fifty_two_week_low,
      0,
    );

    // Calculate volatility & momentum proxy
    const changeMagnitude = Math.abs(rawChangePercent);
    const volatility = clamp(changeMagnitude / 5, 0.2, 3.0);
    const momentum = clamp(rawChangePercent / 5, -2.0, 2.0);

    // Compute visual size metric (radius 14px ~ 36px)
    let sizeMetric = 20;
    if (marketCap > 0) {
      // Logarithmic scaling based on market cap (e.g. 1B to 3T)
      const logCap = Math.log10(Math.max(marketCap, 1e7));
      sizeMetric = clamp(14 + (logCap - 7) * 3.5, 14, 38);
    } else if (volume > 0) {
      const logVol = Math.log10(Math.max(volume, 1e4));
      sizeMetric = clamp(14 + (logVol - 4) * 3.0, 14, 34);
    }

    // Determine tier: center, inner (portfolio/fav), middle (watchlist), outer (market)
    const shares = toFiniteNumberSafe(
      raw.shares !== undefined ? raw.shares : options.shares,
      0,
    );
    const portfolioValue = toFiniteNumberSafe(
      raw.portfolio_value !== undefined
        ? raw.portfolio_value
        : raw.portfolioValue !== undefined
          ? raw.portfolioValue
          : options.portfolioValue,
      0,
    );
    const portfolioWeight = toFiniteNumberSafe(
      raw.portfolio_weight !== undefined
        ? raw.portfolio_weight
        : raw.portfolioWeight !== undefined
          ? raw.portfolioWeight
          : options.portfolioWeight,
      0,
    );
    const explicitTier = String(options.tier || raw.tier || "")
      .trim()
      .toLowerCase();
    const isHolding =
      shares > 0 ||
      portfolioValue > 0 ||
      portfolioWeight > 0 ||
      Boolean(raw.is_portfolio || raw.in_portfolio);
    const tier = options.isCenter
      ? "center"
      : explicitTier === "inner" ||
          explicitTier === "middle" ||
          explicitTier === "outer"
        ? explicitTier
        : isHolding
          ? "inner"
          : market === "idx"
            ? "outer"
            : "middle";

    // History data if present
    const chartData = Array.isArray(raw.chart_data) ? raw.chart_data : [];

    return {
      symbol,
      displayName,
      name: displayName,
      market,
      // Carry the currency through for price formatting (R10): the underlying
      // payloads often omit it, so default by market.
      currency: raw.currency || (market === "jp" ? "JPY" : "USD"),
      price,
      change: rawChange,
      changePercent: rawChangePercent,
      volume,
      marketCap,
      sector,
      industry,
      peRatio,
      high52,
      low52,
      volatility,
      momentum,
      radius: sizeMetric,
      tier,
      shares,
      portfolioValue,
      portfolioWeight,
      chartData,
      isCenter: Boolean(options.isCenter),
      dataAvailability: {
        hasPrice: price > 0,
        hasDetails: marketCap > 0 || sector !== "その他",
        hasChart: chartData.length > 0,
        hasPe: peRatio !== null && peRatio > 0,
      },
      updatedAt: raw.timestamp || raw.updated_at || Date.now(),
    };
  }

  /**
   * Normalize an array of historical points from /api/stock-history
   * @param {Array} rawHistory - Array of history objects or numbers
   * @returns {Array<{timestamp: number, dateStr: string, close: number, open: number, high: number, low: number, volume: number}>}
   */
  function normalizeStockHistory(rawHistory) {
    if (!Array.isArray(rawHistory)) return [];
    const points = rawHistory
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const close = toFiniteNumberSafe(
          item.c !== undefined
            ? item.c
            : item.close !== undefined
              ? item.close
              : item.price,
          0,
        );
        if (close <= 0) return null;

        const open = toFiniteNumberSafe(
          item.o !== undefined ? item.o : item.open,
          close,
        );
        const high = toFiniteNumberSafe(
          item.h !== undefined ? item.h : item.high,
          Math.max(open, close),
        );
        const low = toFiniteNumberSafe(
          item.l !== undefined ? item.l : item.low,
          Math.min(open, close),
        );
        const volume = toFiniteNumberSafe(
          item.v !== undefined ? item.v : item.volume,
          0,
        );
        const extractValidDateStr = (candidate) => {
          if (typeof candidate !== "string") return "";
          const trimmed = candidate.trim();
          if (!trimmed) return "";
          const head = trimmed.split(/[T\s]/)[0];
          if (/^\d{4}-\d{2}-\d{2}$/.test(head)) return head;
          const parsed = Date.parse(trimmed);
          return Number.isNaN(parsed) ? "" : formatLocalDateStr(parsed);
        };
        const serverDateCandidate =
          typeof item.date === "string" && item.date.trim()
            ? item.date.trim()
            : typeof item.date_str === "string" && item.date_str.trim()
              ? item.date_str.trim()
              : typeof item.datetime === "string" && item.datetime.trim()
                ? item.datetime.trim()
                : "";
        const rawDate =
          item.x !== undefined
            ? item.x
            : item.date || item.timestamp || item.datetime;
        let timestamp = Date.now();
        let dateStr = extractValidDateStr(serverDateCandidate);

        if (typeof rawDate === "number" && Number.isFinite(rawDate)) {
          const ms = rawDate > 1e11 ? rawDate : rawDate * 1000;
          if (!Number.isNaN(new Date(ms).getTime())) {
            timestamp = ms;
            if (!dateStr) {
              dateStr = formatLocalDateStr(timestamp);
            }
          }
        } else if (typeof rawDate === "string" && rawDate.trim()) {
          const trimmed = rawDate.trim();
          const parsed = Date.parse(trimmed);
          timestamp = Number.isNaN(parsed) ? Date.now() : parsed;
          if (!dateStr) {
            dateStr =
              extractValidDateStr(trimmed) || formatLocalDateStr(timestamp);
          }
        } else if (dateStr) {
          const parsed = Date.parse(dateStr);
          timestamp = Number.isNaN(parsed) ? Date.now() : parsed;
        }

        if (!dateStr) {
          dateStr = formatLocalDateStr(timestamp);
        }

        return {
          timestamp,
          dateStr,
          open,
          high,
          low,
          close,
          price: close,
          changePercent: 0,
          volume,
        };
      })
      .filter(Boolean)
      .sort((a, b) => a.timestamp - b.timestamp);

    // Δ% はライブの change_percent(前日終値基準)と同じ基準で計算する。
    // ギャップのある日は open 基準だと前日比と食い違い、同じ「±x.xx%」が
    // 別の値を指してしまうため、先頭のみ open 基準にフォールバックする。
    for (let i = 0; i < points.length; i++) {
      const baseClose = i > 0 ? points[i - 1].close : points[i].open;
      points[i].changePercent =
        baseClose > 0 ? ((points[i].close - baseClose) / baseClose) * 100 : 0;
    }
    return points;
  }

  /**
   * Extract stock for given historical time cursor
   * @param {Object} stock - Normalized stock model
   * @param {Array} history - Normalized history array
   * @param {number} cursorIndex - Index within history array
   * @returns {Object} Interpolated stock model
   */
  function interpolateStockAtHistoryIndex(stock, history, cursorIndex) {
    if (!stock) return null;
    if (
      !history ||
      !history.length ||
      cursorIndex < 0 ||
      cursorIndex >= history.length
    ) {
      return stock;
    }

    const currentPoint = history[cursorIndex];
    const prevPoint = cursorIndex > 0 ? history[cursorIndex - 1] : history[0];
    const basePoint = history[0];

    const historicalPrice = currentPoint.close;
    const priceChange = historicalPrice - prevPoint.close;
    const priceChangePercent =
      prevPoint.close > 0 ? (priceChange / prevPoint.close) * 100 : 0;
    const periodReturnPercent =
      basePoint.close > 0
        ? ((historicalPrice - basePoint.close) / basePoint.close) * 100
        : 0;

    return {
      ...stock,
      price: historicalPrice,
      change: priceChange,
      changePercent: priceChangePercent,
      periodReturnPercent,
      volume: currentPoint.volume || stock.volume,
      historicalDate: currentPoint.dateStr,
      historicalTimestamp: currentPoint.timestamp,
    };
  }

  global.ObservatoryDataAdapter = {
    normalizeStock: normalizeObservatoryStock,
    normalizeHistory: normalizeStockHistory,
    interpolateAtHistory: interpolateStockAtHistoryIndex,
    toFiniteNumberSafe,
    clamp,
    formatPrice,
    formatMarketCap,
  };
})(typeof window !== "undefined" ? window : this);
