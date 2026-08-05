/**
 * realtime_client.js - Realtime Stock Data Stream & UI Flash Highlighter
 * Handlers for Server-Sent Events (SSE) realtime_update stream.
 * Enabled ONLY when sse_mode === '2' (TradingView Realtime Mode).
 */

(function () {
  "use strict";

  class RealtimeStockClient {
    constructor() {
      this.eventSource = null;
      this.priceStore = {};
      this.reconnectTimer = null;
    }

    init() {
      if (!window.EventSource) {
        console.warn("Browser does not support Server-Sent Events (SSE).");
        return;
      }
      // Check current SSE Mode (0 = disabled, 1 = complementary, 2 = tradingview_realtime)
      const currentMode = localStorage.getItem("mns_sse_mode") || "2";
      if (currentMode !== "2") {
        // Only active in Mode 2 (TradingView Realtime Mode)
        return;
      }
      this.connect();
    }

    connect() {
      if (this.eventSource) {
        this.eventSource.close();
      }

      this.eventSource = new EventSource("/api/stocks/stream?mode=2");

      this.eventSource.addEventListener("realtime_update", (e) => {
        try {
          const payload = JSON.parse(e.data);
          if (payload && payload.deltas) {
            this.handleDeltas(payload.deltas);
          }
        } catch (err) {
          console.error("Failed to parse realtime SSE payload:", err);
        }
      });

      this.eventSource.onerror = (err) => {
        console.warn(
          "Realtime SSE connection error. Reconnecting in 5 seconds...",
          err,
        );
        this.eventSource.close();
        if (!this.reconnectTimer) {
          this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            const currentMode = localStorage.getItem("mns_sse_mode") || "2";
            if (currentMode === "2") {
              this.connect();
            }
          }, 5000);
        }
      };
    }

    handleDeltas(deltas) {
      window.requestAnimationFrame(() => {
        Object.keys(deltas).forEach((symbol) => {
          const data = deltas[symbol];
          if (!data) return;

          const prevPrice = this.priceStore[symbol];
          const newPrice = data.price;
          this.priceStore[symbol] = newPrice;

          const sanitizedSym = symbol.replace(/[\.\:]/g, "_");

          // 1. Direct price text elements (e.g. .stock-price-TSLA)
          const directPriceElements = document.querySelectorAll(
            `.stock-price-${sanitizedSym}`,
          );
          directPriceElements.forEach((el) => {
            const formatted =
              typeof newPrice === "number"
                ? typeof window.formatPrice === "function"
                  ? window.formatPrice(newPrice, data)
                  : newPrice.toLocaleString(undefined, {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })
                : newPrice;
            if (el.textContent !== formatted) {
              el.textContent = formatted;
            }
            if (prevPrice !== undefined && prevPrice !== newPrice) {
              const flashClass =
                newPrice > prevPrice ? "flash-up" : "flash-down";
              el.classList.add(flashClass);
              setTimeout(() => {
                el.classList.remove(flashClass);
              }, 1200);
            }
          });

          // 2. Stock card wrappers with data-symbol attribute
          const wrappers = document.querySelectorAll(
            `.stock-wrapper[data-symbol="${symbol}"]`,
          );
          wrappers.forEach((wrapper) => {
            const currentStock = wrapper.__stockData
              ? { ...wrapper.__stockData, ...data }
              : data;
            if (typeof window.updateExistingCard === "function") {
              window.updateExistingCard(wrapper, currentStock);
            } else if (typeof window.updateStockUI === "function") {
              window.updateStockUI(wrapper, currentStock);
            } else {
              // Fallback: update .compact-price inside wrapper ONLY
              const priceEl = wrapper.querySelector(".compact-price");
              if (priceEl) {
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
    window.realtimeClient.init();
  });
})();
