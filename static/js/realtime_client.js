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

  class RealtimeStockClient {
    constructor() {
      this.priceStore = {};
      this.domCache = new Map();
    }

    _getElements(symbol) {
      let cached = this.domCache.get(symbol);
      const isStale =
        !cached ||
        cached.wrappers.length === 0 ||
        cached.ptsElements.length === 0 ||
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

        const wrapperSelectors = [
          `.stock-wrapper[data-symbol="${symbol}"]`,
          `.stock-wrapper[data-symbol="${bareSymbol}"]`,
          `.stock-wrapper[data-symbol="${cleanCode}.T"]`,
          `.stock-wrapper[data-symbol="${cleanCode}"]`,
        ];
        const wrappers = Array.from(
          document.querySelectorAll(wrapperSelectors.join(",")),
        );

        const ptsSelectors = [
          `.stock-wrapper[data-symbol="${symbol}"] .compact-pts`,
          `.stock-wrapper[data-symbol="${bareSymbol}"] .compact-pts`,
          `.stock-wrapper[data-symbol="${cleanCode}.T"] .compact-pts`,
          `.stock-wrapper[data-symbol="${cleanCode}"] .compact-pts`,
        ];
        const ptsElements = Array.from(
          document.querySelectorAll(ptsSelectors.join(",")),
        );

        cached = {
          bareSymbol,
          cleanCode,
          wrappers,
          ptsElements,
        };
        this.domCache.set(symbol, cached);
      }
      return cached;
    }

    /**
     * Apply PTS (after-hours) quote deltas to matching stock cards.
     * @param {Object} deltas - Map of symbol -> { price, pts_trading, ... }
     */
    handlePtsDeltas(deltas) {
      window.requestAnimationFrame(() => {
        Object.keys(deltas).forEach((symbol) => {
          const data = deltas[symbol];
          if (!data) return;

          const cached = this._getElements(symbol);
          cached.ptsElements.forEach((el) => {
            let txt = "";
            if (data.price != null && typeof data.price === "number") {
              txt =
                typeof window.formatPrice === "function"
                  ? `PTS ${window.formatPrice(data.price, data)}`
                  : `PTS ${data.price}`;
            } else if (data.price != null) {
              txt = `PTS ${data.price}`;
            }
            if (el.textContent !== txt) el.textContent = txt;
            el.hidden = !txt;
            const wrapper = el.closest(".stock-wrapper");
            if (wrapper && wrapper.__stockData) {
              wrapper.__stockData.pts_price = data.price;
            }
          });
        });
      });
    }

    /**
     * Apply realtime price deltas to matching DOM elements.
     * @param {Object} deltas - Map of symbol -> { price, change, ... }
     */
    handleDeltas(deltas) {
      window.requestAnimationFrame(() => {
        Object.keys(deltas).forEach((symbol) => {
          const data = deltas[symbol];
          if (!data) return;

          const prevPrice = this.priceStore[symbol];
          const newPrice = data.price;
          this.priceStore[symbol] = newPrice;

          const cached = this._getElements(symbol);

          // 1. Sync delta with global window.state.stocks
          if (window.state && window.state.stocks) {
            ["us", "jp"].forEach((m) => {
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
                  if (data.price != null) sItem.price = data.price;
                  if (data.change != null) sItem.change = data.change;
                  if (data.change_percent != null)
                    sItem.change_percent = data.change_percent;
                  if (data.volume != null) sItem.volume = data.volume;
                }
              }
            });
          }

          // 2. Stock card wrappers
          cached.wrappers.forEach((wrapper) => {
            const currentStock = wrapper.__stockData
              ? { ...wrapper.__stockData, ...data, is_realtime: true }
              : { ...data, is_realtime: true };
            if (typeof window.updateExistingCard === "function") {
              window.updateExistingCard(wrapper, currentStock);
            } else if (typeof window.updateStockUI === "function") {
              window.updateStockUI(wrapper, currentStock);
            } else {
              // Fallback: update .compact-price inside wrapper ONLY
              const priceEl = wrapper.querySelector(".compact-price");
              if (priceEl) {
                priceEl.classList.add("scraping-success", "updating");
                setTimeout(() => priceEl.classList.remove("updating"), 1200);
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
                  const flashClass =
                    newPrice > prevPrice ? "flash-up" : "flash-down";
                  priceEl.classList.add(flashClass);
                  setTimeout(() => {
                    priceEl.classList.remove(flashClass);
                  }, 1200);
                }
              }
            }
          });
        });
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
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
  });
})();
