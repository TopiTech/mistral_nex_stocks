/**
 * realtime_client.js - Realtime Stock Data Stream & UI Flash Highlighter
 * Handlers for Server-Sent Events (SSE) realtime_update stream.
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
      this.connect();
    }

    connect() {
      if (this.eventSource) {
        this.eventSource.close();
      }

      this.eventSource = new EventSource("/api/stocks/stream");

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
        console.warn("SSE connection error. Reconnecting in 5 seconds...", err);
        this.eventSource.close();
        if (!this.reconnectTimer) {
          this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.connect();
          }, 5000);
        }
      };
    }

    handleDeltas(deltas) {
      window.requestAnimationFrame(() => {
        Object.keys(deltas).forEach((symbol) => {
          const data = deltas[symbol];
          const prevPrice = this.priceStore[symbol];
          const newPrice = data.price;
          this.priceStore[symbol] = newPrice;

          // Locate DOM elements by data-symbol or class
          const elements = document.querySelectorAll(
            `[data-symbol="${symbol}"], .stock-price-${symbol.replace(/[\.\:]/g, "_")}`,
          );
          elements.forEach((el) => {
            el.textContent =
              typeof newPrice === "number"
                ? newPrice.toLocaleString(undefined, {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })
                : newPrice;

            if (prevPrice !== undefined && prevPrice !== newPrice) {
              const flashClass =
                newPrice > prevPrice ? "flash-up" : "flash-down";
              el.classList.add(flashClass);
              setTimeout(() => {
                el.classList.remove(flashClass);
              }, 1200);
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
