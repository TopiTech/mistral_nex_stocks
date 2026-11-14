/**
 * realtime_client.js - Realtime Stock Data Stream & UI Flash Highlighter
 *
 * Handles ``realtime_update`` deltas pushed over the main SSE stream.
 *
 * The SSE connection itself is owned exclusively by api.js ``connectSSE()``
 * (APIClient with heartbeat monitoring + exponential-backoff reconnection).
 * This module does NOT open its own EventSource — it only exposes
 * ``window.handleRealtimeDeltas`` which api.js invokes when a
 * ``realtime_update`` event arrives. This keeps a single stream per page and
 * avoids duplicate connections / delta-consumption races.
 */

(function () {
  "use strict";

  function isJpSymbol(symbol, market) {
    if (market === "jp") return true;
    if (typeof symbol !== "string") return false;
    const upper = symbol.toUpperCase();
    if (upper.endsWith(".T")) return true;
    const bare = upper.includes(":")
      ? upper.slice(upper.lastIndexOf(":") + 1)
      : upper;
    const code = bare.endsWith(".T") ? bare.slice(0, -2) : bare;
    return /^[0-9][A-Z0-9]{3}$/.test(code);
  }

  function roundChange(value, symbol, market) {
    const decimals = isJpSymbol(symbol, market) ? 2 : 4;
    const factor = 10 ** decimals;
    let rounded = Math.round((value + Number.EPSILON) * factor) / factor;
    if (Object.is(rounded, -0)) rounded = 0;
    return rounded;
  }

  function roundPercent(value) {
    let rounded = Math.round((value + Number.EPSILON) * 100) / 100;
    if (Object.is(rounded, -0)) rounded = 0;
    return rounded;
  }

  /**
   * Calculate exponential backoff delay with full jitter for reconnection.
   * Prevents thundering herd when server recovers or network reconnects.
   *
   * @param {number} [attempt=0] - Reconnect attempt count (0-based)
   * @param {number} [baseDelay=1000] - Base delay in milliseconds
   * @param {number} [maxDelay=30000] - Maximum delay cap in milliseconds
   * @returns {number} Delay in milliseconds with jitter applied
   */
  function calculateReconnectDelay(
    attempt = 0,
    baseDelay = 1000,
    maxDelay = 30000,
  ) {
    const safeAttempt = Math.max(
      0,
      Number.isFinite(attempt) ? Math.floor(attempt) : 0,
    );
    const safeBase = Math.max(
      50,
      Number.isFinite(baseDelay) ? baseDelay : 1000,
    );
    const safeMax = Math.max(
      safeBase,
      Number.isFinite(maxDelay) ? maxDelay : 30000,
    );

    const calculatedDelay = Math.min(
      safeBase * Math.pow(2, safeAttempt),
      safeMax,
    );
    const jitter = 0.5 + Math.random() * 0.5;
    return Math.round(calculatedDelay * jitter);
  }

  class RealtimeStockClient {
    constructor() {
      // Previous prices are per-card UI state, not a symbol registry. They are
      // kept on the card element (__realtimePrevPrice) so their lifetime is the
      // card's lifetime: removing a watchlist entry/card releases the value
      // instead of leaving it in a map that grows for the whole session and
      // makes a re-added card flash against a price it never displayed.
      this.domCache = new Map();
      // Pending rAF batches. A snapshot is applied synchronously by api.js;
      // without flushing these first, a replayed delta queued before the
      // snapshot could run afterwards and regress the displayed price.
      this._pendingDeltaFrames = [];
      this.reconnectAttempt = 0;
      this.reconnectTimer = null;
      this.baseReconnectDelay = 1000;
      this.maxReconnectDelay = 30000;
    }

    /**
     * Compute reconnection backoff with full jitter for the given attempt.
     * @param {number} [attempt] - Reconnection attempt (defaults to internal counter)
     * @param {number} [baseDelay] - Base delay in milliseconds
     * @param {number} [maxDelay] - Maximum delay in milliseconds
     * @returns {number} Reconnection delay in ms with jitter applied
     */
    calculateReconnectDelay(attempt, baseDelay, maxDelay) {
      const att = attempt !== undefined ? attempt : this.reconnectAttempt;
      const base =
        baseDelay !== undefined ? baseDelay : this.baseReconnectDelay;
      const max = maxDelay !== undefined ? maxDelay : this.maxReconnectDelay;
      return calculateReconnectDelay(att, base, max);
    }

    /**
     * Get next reconnection delay with jitter and increment internal attempt counter.
     * @param {number} [baseDelay]
     * @param {number} [maxDelay]
     * @returns {number}
     */
    getNextReconnectDelay(baseDelay, maxDelay) {
      const delay = this.calculateReconnectDelay(
        this.reconnectAttempt,
        baseDelay,
        maxDelay,
      );
      this.reconnectAttempt++;
      return delay;
    }

    /**
     * Reset reconnection state and clear any pending reconnection timer.
     */
    resetReconnect() {
      this.reconnectAttempt = 0;
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
      }
    }

    /**
     * Schedule a reconnection callback with full jitter exponential backoff.
     * @param {Function} reconnectFn - Function to execute on reconnect
     * @param {Function} [onScheduled] - Optional callback receiving the calculated delay
     * @returns {number} The scheduled delay in ms
     */
    scheduleReconnect(reconnectFn, onScheduled) {
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
      }
      const delay = this.getNextReconnectDelay();
      if (typeof onScheduled === "function") {
        onScheduled(delay, this.reconnectAttempt);
      }
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        if (typeof reconnectFn === "function") {
          reconnectFn();
        }
      }, delay);
      return delay;
    }

    _getElements(symbol) {
      const registryVersion =
        typeof window.getWrapperRegistryVersion === "function"
          ? window.getWrapperRegistryVersion()
          : 0;
      let cached = this.domCache.get(symbol);
      const isStale =
        !cached ||
        cached.registryVersion !== registryVersion ||
        cached.wrappers.length === 0 ||
        cached.wrappers.some((el) => !el.isConnected) ||
        cached.ptsElements.some((el) => !el.isConnected);

      if (isStale) {
        // Bound the cache: prune entries whose elements no longer exist so a
        // long session with many add/remove cycles cannot grow it unboundedly.
        if (this.domCache.size > 500) {
          for (const [key, entry] of this.domCache) {
            const detached =
              !entry.wrappers.some((el) => el.isConnected) &&
              !entry.ptsElements.some((el) => el.isConnected);
            if (detached) this.domCache.delete(key);
          }
          if (this.domCache.size > 500) this.domCache.clear();
        }

        const bareSymbol = symbol.includes(":")
          ? symbol.slice(symbol.lastIndexOf(":") + 1)
          : symbol;
        const cleanCode = bareSymbol.replace(/\.T$/i, "");

        const esc =
          typeof CSS !== "undefined" && typeof CSS.escape === "function"
            ? CSS.escape
            : function (s) {
                return String(s).replace(/[^a-zA-Z0-9_-]/g, function (c) {
                  return "\\" + c;
                });
              };
        const wrapperSelectors = [
          `.stock-wrapper[data-symbol="${esc(symbol)}"]`,
          `.stock-wrapper[data-symbol="${esc(bareSymbol)}"]`,
          `.stock-wrapper[data-symbol="${esc(cleanCode + ".T")}"]`,
          `.stock-wrapper[data-symbol="${esc(cleanCode)}"]`,
        ];
        const wrappers = Array.from(
          document.querySelectorAll(wrapperSelectors.join(",")),
        );

        const ptsSelectors = [
          `.stock-wrapper[data-symbol="${esc(symbol)}"] .compact-pts`,
          `.stock-wrapper[data-symbol="${esc(bareSymbol)}"] .compact-pts`,
          `.stock-wrapper[data-symbol="${esc(cleanCode + ".T")}"] .compact-pts`,
          `.stock-wrapper[data-symbol="${esc(cleanCode)}"] .compact-pts`,
        ];
        const ptsElements = Array.from(
          document.querySelectorAll(ptsSelectors.join(",")),
        );

        cached = {
          bareSymbol,
          cleanCode,
          wrappers,
          ptsElements,
          registryVersion,
        };
        this.domCache.set(symbol, cached);
      }
      return cached;
    }

    /**
     * Queue one delta batch for the next animation frame and remember it so a
     * synchronous snapshot can flush (and thereby order) it first.
     * @param {Function} apply
     */
    _scheduleDelta(apply) {
      const entry = { frame: 0, apply };
      entry.frame = window.requestAnimationFrame(() => {
        const index = this._pendingDeltaFrames.indexOf(entry);
        if (index !== -1) this._pendingDeltaFrames.splice(index, 1);
        apply();
      });
      this._pendingDeltaFrames.push(entry);
    }

    /**
     * Apply every queued delta batch immediately, in arrival order, and cancel
     * its pending animation frame. Called by api.js before a snapshot/diff is
     * applied so an older queued delta cannot overwrite newer snapshot data.
     */
    flushPendingDeltas() {
      const pending = this._pendingDeltaFrames;
      this._pendingDeltaFrames = [];
      pending.forEach((entry) => {
        try {
          window.cancelAnimationFrame(entry.frame);
        } catch {
          // no-op: the frame may already have run
        }
        entry.apply();
      });
    }

    /**
     * Apply PTS (after-hours) quote deltas to matching stock cards.
     * @param {Object} deltas - Map of symbol -> { price, pts_trading, ... }
     */
    handlePtsDeltas(deltas) {
      if (document.hidden) {
        // Flush any pre-hide rAF delta batch first so paused animation frames
        // cannot fire upon tab restore and overwrite newer hidden-tab state.
        this.flushPendingDeltas();
        // (L14, 2026-11-07 review) rAF callbacks never fire while the tab
        // is hidden: the deltas (and their payload closures) piled up and
        // flushed in one burst on return — and a queued OLDER delta could
        // apply after a newer snapshot, regressing displayed prices for one
        // update cycle. Apply synchronously instead (mirrors processSseData).
        this._applyPtsDeltasNow(deltas);
        return;
      }
      this._scheduleDelta(() => this._applyPtsDeltasNow(deltas));
    }

    _applyPtsDeltasNow(deltas) {
      Object.keys(deltas).forEach((symbol) => {
        const data = deltas[symbol];
        if (!data) return;

        const cached = this._getElements(symbol);
        cached.ptsElements.forEach((el) => {
          const wrapper = el.closest(".stock-wrapper");
          const stockCtx = (wrapper && wrapper.__stockData) || data;
          let txt = "";
          if (data.price != null && typeof data.price === "number") {
            txt =
              typeof window.formatPrice === "function"
                ? `PTS ${window.formatPrice(data.price, stockCtx)}`
                : `PTS ${data.price}`;
          } else if (data.price != null) {
            txt = `PTS ${data.price}`;
          }
          if (el.textContent !== txt) el.textContent = txt;
          if (txt) {
            el.hidden = false;
            if (typeof el.removeAttribute === "function") {
              el.removeAttribute("aria-hidden");
            }
          } else {
            el.hidden = true;
            if (typeof el.setAttribute === "function") {
              el.setAttribute("aria-hidden", "true");
            }
          }
          if (wrapper && wrapper.__stockData) {
            wrapper.__stockData.pts_price = data.price;
          }
        });
      });
    }
    /**
     * Apply realtime price deltas to matching DOM elements.
     * @param {Object} deltas - Map of symbol -> { price, change, ... }
     */
    handleDeltas(deltas) {
      if (document.hidden) {
        this.flushPendingDeltas();
        // (L14) see handlePtsDeltas.
        this._applyDeltasNow(deltas);
        return;
      }
      this._scheduleDelta(() => this._applyDeltasNow(deltas));
    }

    _applyDeltasNow(deltas) {
      Object.keys(deltas).forEach((symbol) => {
        const data = deltas[symbol];
        if (!data) return;

        const newPrice = data.price;

        const cached = this._getElements(symbol);

        // 1. Sync delta with global window.state.stocks & compute change from yfinance previous_close
        let yfPrevClose = null;
        let foundStock = null;
        let foundMarket = null;
        if (
          data.previous_close != null &&
          typeof data.previous_close === "number" &&
          data.previous_close > 0
        ) {
          yfPrevClose = data.previous_close;
        }

        if (window.state && window.state.stocks) {
          ["us", "jp", "idx"].forEach((m) => {
            if (Array.isArray(window.state.stocks[m])) {
              const sItem = window.state.stocks[m].find(
                (st) =>
                  st &&
                  (st.symbol === symbol ||
                    st.symbol === cached.bareSymbol ||
                    st.symbol === `${cached.cleanCode}.T` ||
                    st.symbol === cached.cleanCode),
              );
              if (sItem) {
                foundStock = sItem;
                foundMarket = sItem.market || m;
                // Resolve yfinance previous close from stock object
                if (!yfPrevClose) {
                  if (
                    sItem.previous_close != null &&
                    Number(sItem.previous_close) > 0
                  ) {
                    yfPrevClose = Number(sItem.previous_close);
                  } else if (sItem.price != null && sItem.change != null) {
                    const p = Number(sItem.price);
                    const c = Number(sItem.change);
                    if (Number.isFinite(p) && Number.isFinite(c) && p - c > 0) {
                      yfPrevClose = p - c;
                      sItem.previous_close = yfPrevClose;
                    }
                  }
                }

                // Recalculate change and percentage using yfinance previous close
                if (
                  yfPrevClose &&
                  yfPrevClose > 0 &&
                  typeof data.price === "number" &&
                  Number.isFinite(data.price)
                ) {
                  const diff = data.price - yfPrevClose;
                  const itemMarket = sItem.market || m;
                  const newChange = roundChange(diff, symbol, itemMarket);
                  const newPct = roundPercent((diff / yfPrevClose) * 100);
                  data.change = newChange;
                  data.change_percent = newPct;
                  data.previous_close = yfPrevClose;
                }

                if (data.price != null) sItem.price = data.price;
                if (data.change != null) sItem.change = data.change;
                if (data.change_percent != null)
                  sItem.change_percent = data.change_percent;
                if (data.volume != null && Number(data.volume) > 0)
                  sItem.volume = data.volume;
              }
            }
          });
        }

        const effectiveMarket =
          data.market || (foundStock ? foundStock.market || foundMarket : null);

        // If not resolved from state, check if delta had calculated change/pct
        if (
          data.change == null &&
          yfPrevClose &&
          yfPrevClose > 0 &&
          typeof data.price === "number" &&
          Number.isFinite(data.price)
        ) {
          const diff = data.price - yfPrevClose;
          data.change = roundChange(diff, symbol, effectiveMarket);
          data.change_percent = roundPercent((diff / yfPrevClose) * 100);
          data.previous_close = yfPrevClose;
        }

        if (typeof data.change === "number" && Number.isFinite(data.change)) {
          data.change = roundChange(data.change, symbol, effectiveMarket);
        }
        if (
          typeof data.change_percent === "number" &&
          Number.isFinite(data.change_percent)
        ) {
          data.change_percent = roundPercent(data.change_percent);
        }

        // 2. Stock card wrappers
        cached.wrappers.forEach((wrapper) => {
          const currentStock = wrapper.__stockData
            ? { ...wrapper.__stockData, ...data, is_realtime: true }
            : { ...data, is_realtime: true };

          // Volume is strictly obtained from yfinance and must never be wiped out by scrapers
          if (
            wrapper.__stockData &&
            wrapper.__stockData.volume != null &&
            Number(wrapper.__stockData.volume) > 0
          ) {
            currentStock.volume = wrapper.__stockData.volume;
          } else if (
            foundStock &&
            foundStock.volume != null &&
            Number(foundStock.volume) > 0
          ) {
            currentStock.volume = foundStock.volume;
          } else if (data.volume != null && Number(data.volume) > 0) {
            currentStock.volume = data.volume;
          }

          if (typeof window.updateExistingCard === "function") {
            window.updateExistingCard(wrapper, currentStock);
          } else if (typeof window.updateStockUI === "function") {
            window.updateStockUI(wrapper, currentStock);
          } else {
            // Fallback: update .compact-price inside wrapper ONLY
            const prevPrice = wrapper.__realtimePrevPrice;
            wrapper.__realtimePrevPrice = newPrice;
            const priceEl = wrapper.querySelector(".compact-price");
            if (priceEl) {
              priceEl.classList.add("scraping-success", "updating");
              if (priceEl.__updatingTimer) {
                clearTimeout(priceEl.__updatingTimer);
              }
              priceEl.__updatingTimer = setTimeout(() => {
                priceEl.classList.remove("updating");
                priceEl.__updatingTimer = null;
              }, 1200);
              const formatted =
                typeof newPrice === "number"
                  ? typeof window.formatPrice === "function"
                    ? window.formatPrice(newPrice, data)
                    : newPrice.toLocaleString(undefined, {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                      })
                  : newPrice;
              if (priceEl.textContent !== formatted) {
                priceEl.textContent = formatted;
              }
              if (prevPrice !== undefined && prevPrice !== newPrice) {
                if (priceEl.__flashTimer) {
                  clearTimeout(priceEl.__flashTimer);
                  priceEl.__flashTimer = null;
                }
                priceEl.classList.remove("flash-up", "flash-down");
                const flashClass =
                  newPrice > prevPrice ? "flash-up" : "flash-down";
                priceEl.classList.add(flashClass);
                priceEl.__flashTimer = setTimeout(() => {
                  priceEl.classList.remove(flashClass);
                  priceEl.__flashTimer = null;
                }, 1200);
              }
            }
          }
        });
      });
    }
  }

  const initRealtimeStockClient = () => {
    if (typeof window !== "undefined") {
      window.RealtimeStockClient = RealtimeStockClient;
      window.calculateReconnectDelay = calculateReconnectDelay;
    }
    if (window.realtimeClient) return;
    window.realtimeClient = new RealtimeStockClient();
    // Single-entry dispatch used by api.js (connectSSE) for realtime_update events.
    window.handleRealtimeDeltas = (deltas) => {
      if (window.realtimeClient && deltas) {
        window.realtimeClient.handleDeltas(deltas);
      }
    };
    window.handlePtsDeltas = (deltas) => {
      if (window.realtimeClient && deltas) {
        window.realtimeClient.handlePtsDeltas(deltas);
      }
    };
    window.flushPendingRealtimeDeltas = () => {
      if (window.realtimeClient) {
        window.realtimeClient.flushPendingDeltas();
      }
    };
  };

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("DOMContentLoaded", initRealtimeStockClient);
  }
  if (
    typeof document !== "undefined" &&
    document.readyState &&
    document.readyState !== "loading"
  ) {
    initRealtimeStockClient();
  }
})();
