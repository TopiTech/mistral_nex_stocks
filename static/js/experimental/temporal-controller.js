/**
 * temporal-controller.js - Timeline scrubbing and historical data controller for Market Observatory.
 *
 * Supports intuitive timeline range scrubbing (Left = Oldest, Right = Live),
 * Shift+wheel granularity switching, client-side historical caching,
 * automated timelapse playback, and central reference card historical price updating.
 */

(function (global) {
  "use strict";

  const GRANULARITY_PERIODS = ["1d", "5d", "1mo", "3mo", "1y"];

  class TemporalController {
    constructor(state, elements) {
      this.state = state;
      this.els = elements || {};
      this.historyCache = new Map(); // key: symbol:granularity -> array of normalized history
      this.inFlightRequests = new Map();
      this.activeHistoryKey = null;
      this.isPlaying = false;
      this.playTimer = null;

      this.bindEvents();
      this.bindState();
    }

    bindEvents() {
      // 1. Wheel on canvas & viewport
      if (this.els.canvas) {
        this._wheelHandler = (e) => this.handleWheel(e);
        this.els.canvas.addEventListener("wheel", this._wheelHandler, {
          passive: false,
        });
      }

      // 2. Timeline slider UI
      if (this.els.timeSlider) {
        this._sliderHandler = (e) => {
          const val = parseInt(e.target.value, 10);
          const historyKey = `${this.state.state.selectedSymbol}:${this.state.state.timeGranularity}`;
          const history = this.historyCache.get(historyKey) || [];
          if (!history.length) return;

          const maxIdx = history.length - 1;
          const cursor = Math.max(0, maxIdx - val);
          this.state.setTimeCursor(cursor);
        };

        this.els.timeSlider.addEventListener("input", this._sliderHandler);
        this.els.timeSlider.addEventListener("change", this._sliderHandler);
      }

      // 3. Granularity button pills
      if (this.els.granularityGroup) {
        this._granularityHandler = (e) => {
          const btn = e.target.closest("[data-granularity]");
          if (btn) {
            const gran = btn.getAttribute("data-granularity");
            this.stopTimelapse();
            this.state.setTimeGranularity(gran);
          }
        };
        this.els.granularityGroup.addEventListener(
          "click",
          this._granularityHandler,
        );
      }

      // 4. Quick jump button (Return to Live)
      if (this.els.timeNowBtn) {
        this.els.timeNowBtn.addEventListener("click", () => {
          this.stopTimelapse();
          this.state.setTimeCursor(0);
        });
      }

      // 5. Timelapse Play/Pause button
      if (this.els.timePlayBtn) {
        this.els.timePlayBtn.addEventListener("click", () => {
          this.toggleTimelapse();
        });
      }
    }

    toggleTimelapse() {
      if (this.isPlaying) {
        this.stopTimelapse();
      } else {
        this.startTimelapse();
      }
    }

    startTimelapse() {
      const historyKey = `${this.state.state.selectedSymbol}:${this.state.state.timeGranularity}`;
      const history = this.historyCache.get(historyKey) || [];
      if (!history.length) return;

      const maxIdx = history.length - 1;
      const currentCursor = this.state.state.timeCursor;

      if (currentCursor === 0) {
        // If currently at Live, restart from oldest point on far left (cursor = maxIdx)
        this.state.setTimeCursor(maxIdx);
      }

      this.isPlaying = true;
      if (this.els.timePlayBtn) {
        this.els.timePlayBtn.textContent = "⏸ 一時停止";
      }

      this.playTimer = setInterval(() => {
        const cursor = this.state.state.timeCursor;
        if (cursor <= 0) {
          this.stopTimelapse();
        } else {
          this.state.setTimeCursor(cursor - 1);
        }
      }, 250);
    }

    stopTimelapse() {
      this.isPlaying = false;
      if (this.playTimer) {
        clearInterval(this.playTimer);
        this.playTimer = null;
      }
      if (this.els.timePlayBtn) {
        this.els.timePlayBtn.textContent = "▶️ 再生";
      }
    }

    bindState() {
      this.state.subscribe((key, val, data) => {
        if (key === "selectedSymbol" || key === "timeGranularity") {
          this.stopTimelapse();
          this.fetchHistoryForSymbol(data.selectedSymbol, data.timeGranularity);
        } else if (key === "timeCursor") {
          this.updateTimelineUI(data);
        }
      });

      // Initial history fetch for default or existing selected symbol
      if (this.state.state.selectedSymbol) {
        this.fetchHistoryForSymbol(
          this.state.state.selectedSymbol,
          this.state.state.timeGranularity,
        );
      }
    }

    destroy() {
      this.stopTimelapse();
      for (const controller of this.inFlightRequests.values()) {
        controller.abort();
      }
      this.inFlightRequests.clear();
      if (this.els.canvas && this._wheelHandler) {
        this.els.canvas.removeEventListener("wheel", this._wheelHandler);
      }
      if (this.els.timeSlider && this._sliderHandler) {
        this.els.timeSlider.removeEventListener("input", this._sliderHandler);
        this.els.timeSlider.removeEventListener("change", this._sliderHandler);
      }
      if (this.els.granularityGroup && this._granularityHandler) {
        this.els.granularityGroup.removeEventListener(
          "click",
          this._granularityHandler,
        );
      }
    }

    handleWheel(e) {
      // Prevent hijacking scroll inside open modals or drawers
      if (
        e.target.closest &&
        e.target.closest(
          ".ai-dive-modal, .constellation-drawer, .shortcuts-modal, .orbit-search-container",
        )
      ) {
        return;
      }

      e.preventDefault();

      if (e.shiftKey) {
        // Shift + Wheel: Cycle time granularity
        const currentGran = this.state.state.timeGranularity;
        let idx = GRANULARITY_PERIODS.indexOf(currentGran);
        if (idx === -1) idx = 3; // default 3mo

        if (e.deltaY > 0) {
          idx = (idx + 1) % GRANULARITY_PERIODS.length;
        } else {
          idx =
            (idx - 1 + GRANULARITY_PERIODS.length) % GRANULARITY_PERIODS.length;
        }
        this.state.setTimeGranularity(GRANULARITY_PERIODS[idx]);
        if (typeof global.showToast === "function") {
          global.showToast(
            `⏱ 時間粒度: ${GRANULARITY_PERIODS[idx].toUpperCase()}`,
            "#38bdf8",
          );
        }
      } else {
        // Normal Wheel: Scrub time cursor
        const historyKey = `${this.state.state.selectedSymbol}:${this.state.state.timeGranularity}`;
        const history = this.historyCache.get(historyKey) || [];
        if (!history.length) return;

        const maxCursor = Math.max(0, history.length - 1);
        const delta = e.deltaY > 0 ? 1 : -1; // Positive = older in time, Negative = newer
        const currentCursor = this.state.state.timeCursor;
        const newCursor = Math.max(
          0,
          Math.min(currentCursor + delta, maxCursor),
        );

        this.state.setTimeCursor(newCursor);
      }
    }

    async fetchHistoryForSymbol(symbol, granularity) {
      if (!symbol) return;
      const cleanSymbol = String(symbol).trim().toUpperCase();
      const period = granularity || "3mo";
      const cacheKey = `${cleanSymbol}:${period}`;
      this.activeHistoryKey = cacheKey;

      // A new symbol/period supersedes every other pending history request.
      // Abort them rather than allowing an old response to repaint the current
      // timeline after the user has moved on.
      for (const [pendingKey, pendingController] of this.inFlightRequests) {
        if (pendingKey !== cacheKey) {
          pendingController.abort();
          this.inFlightRequests.delete(pendingKey);
        }
      }

      if (this.historyCache.has(cacheKey)) {
        if (this.isCurrentHistoryContext(cleanSymbol, period)) {
          this.applyHistoricalData(
            cleanSymbol,
            period,
            this.historyCache.get(cacheKey),
          );
        }
        return;
      }

      if (this.inFlightRequests.has(cacheKey)) {
        return;
      }

      const controller = new AbortController();
      this.inFlightRequests.set(cacheKey, controller);

      try {
        const stock = this.state.state.stocks.get(cleanSymbol);
        const market = stock?.market || this.state.state.market || "us";
        const url = `/api/stock-history?symbol=${encodeURIComponent(cleanSymbol)}&market=${encodeURIComponent(market)}&period=${encodeURIComponent(period)}`;

        const res = await (global.apiFetch || fetch)(url, {
          signal: controller.signal,
        });
        const data =
          res && typeof res.json === "function"
            ? await res.json().catch(() => null)
            : (res?.data ?? res);

        if (!this.isCurrentHistoryContext(cleanSymbol, period)) return;

        if (data && data.fetching) {
          // Data is currently being retrieved on server, retry in 1.5 seconds
          setTimeout(() => {
            if (this.isCurrentHistoryContext(cleanSymbol, period)) {
              this.fetchHistoryForSymbol(cleanSymbol, period);
            }
          }, 1500);
          this.updateTimelineUI(this.state.state, false);
          return;
        }

        if (data && Array.isArray(data.history) && data.history.length) {
          const normalized = global.ObservatoryDataAdapter
            ? global.ObservatoryDataAdapter.normalizeHistory(data.history)
            : data.history;

          this.historyCache.set(cacheKey, normalized);
          if (this.isCurrentHistoryContext(cleanSymbol, period)) {
            this.applyHistoricalData(cleanSymbol, period, normalized);
          }
        } else {
          if (this.isCurrentHistoryContext(cleanSymbol, period)) {
            this.updateTimelineUI(this.state.state, false);
          }
        }
      } catch (err) {
        if (
          err.name !== "AbortError" &&
          this.isCurrentHistoryContext(cleanSymbol, period)
        ) {
          console.warn("[TemporalController] History fetch error:", err);
          this.updateTimelineUI(this.state.state, false);
        }
      } finally {
        if (this.inFlightRequests.get(cacheKey) === controller) {
          this.inFlightRequests.delete(cacheKey);
        }
      }
    }

    isCurrentHistoryContext(symbol, period) {
      return (
        this.activeHistoryKey === `${symbol}:${period}` &&
        String(this.state.state.selectedSymbol || "")
          .trim()
          .toUpperCase() === symbol &&
        this.state.state.timeGranularity === period
      );
    }

    applyHistoricalData(symbol, period, history) {
      if (!this.isCurrentHistoryContext(symbol, period)) return;
      if (!history || !history.length) {
        this.updateTimelineUI(this.state.state, false);
        return;
      }

      const maxIdx = history.length - 1;

      // Update slider bounds: min = 0 (oldest), max = maxIdx (latest)
      if (this.els.timeSlider) {
        this.els.timeSlider.disabled = false;
        this.els.timeSlider.min = "0";
        this.els.timeSlider.max = String(maxIdx);
        // Slider value: maxIdx - timeCursor (far right when timeCursor == 0)
        const sliderVal = Math.max(0, maxIdx - this.state.state.timeCursor);
        this.els.timeSlider.value = String(sliderVal);
      }

      this.updateTimelineUI(this.state.state, true, history);
    }

    updateTimelineUI(stateData, hasHistory = true, activeHistory = null) {
      const historyKey = `${stateData.selectedSymbol}:${stateData.timeGranularity}`;
      const history = activeHistory || this.historyCache.get(historyKey) || [];

      if (!hasHistory || !history.length) {
        if (this.els.timeDisplay) {
          this.els.timeDisplay.textContent = "リアルタイム (最新)";
        }
        if (this.els.timeBadge) {
          this.els.timeBadge.textContent = "LIVE";
          this.els.timeBadge.className = "time-badge live";
        }
        if (this.els.timeSlider) {
          this.els.timeSlider.disabled = true;
        }
        return;
      }

      const maxIdx = history.length - 1;
      const cursor = Math.min(stateData.timeCursor, maxIdx);
      const sliderVal = Math.max(0, maxIdx - cursor);

      if (this.els.timeSlider) {
        this.els.timeSlider.disabled = false;
        this.els.timeSlider.min = "0";
        this.els.timeSlider.max = String(maxIdx);
        this.els.timeSlider.value = String(sliderVal);
      }

      // Time cursor calculation: 0 = latest (last index of history), n = n steps back
      const historyIndex = Math.max(0, maxIdx - cursor);
      const point = history[historyIndex];

      if (this.els.timeDisplay && point) {
        if (cursor === 0) {
          this.els.timeDisplay.textContent = `最新: ${point.dateStr}`;
        } else {
          this.els.timeDisplay.textContent = `観測日時: ${point.dateStr} (${cursor}段階前)`;
        }
      }

      if (this.els.timeBadge) {
        if (cursor === 0) {
          this.els.timeBadge.textContent = "LIVE";
          this.els.timeBadge.className = "time-badge live";
        } else {
          this.els.timeBadge.textContent = "SCRUB";
          this.els.timeBadge.className = "time-badge scrubbed";
        }
      }

      // Update central card reference stock HUD with scrubbed historical price
      const centerPriceEl = document.getElementById("center-stock-price");
      const centerChangeEl = document.getElementById("center-stock-change");

      if (point && cursor > 0) {
        const histPrice = point.close || point.price || 0;
        const histChg = point.changePercent || 0;
        const sign = histChg >= 0 ? "+" : "";

        if (centerPriceEl) {
          centerPriceEl.textContent =
            histPrice > 0
              ? global.formatPrice
                ? global.formatPrice(histPrice, stateData.market)
                : `$${histPrice.toFixed(2)}`
              : "--";
        }
        if (centerChangeEl) {
          centerChangeEl.textContent = `${sign}${histChg.toFixed(2)}%`;
          centerChangeEl.className = `center-stat-change ${histChg >= 0 ? "text-pos" : "text-neg"}`;
        }
      } else if (cursor === 0) {
        const currentStock = stateData.stocks.get(stateData.selectedSymbol);
        if (currentStock) {
          if (centerPriceEl) {
            centerPriceEl.textContent =
              currentStock.price > 0
                ? global.formatPrice
                  ? global.formatPrice(currentStock.price, currentStock.market)
                  : `$${currentStock.price.toFixed(2)}`
                : "--";
          }
          if (centerChangeEl) {
            const liveChg = currentStock.changePercent || 0;
            const sign = liveChg >= 0 ? "+" : "";
            centerChangeEl.textContent = `${sign}${liveChg.toFixed(2)}%`;
            centerChangeEl.className = `center-stat-change ${liveChg >= 0 ? "text-pos" : "text-neg"}`;
          }
        }
      }

      // Update granularity buttons active state
      if (this.els.granularityGroup) {
        const btns =
          this.els.granularityGroup.querySelectorAll("[data-granularity]");
        btns.forEach((btn) => {
          const gran = btn.getAttribute("data-granularity");
          const isActive = gran === stateData.timeGranularity;
          btn.classList.toggle("active", isActive);
          btn.setAttribute("aria-pressed", String(isActive));
        });
      }
    }
  }

  global.TemporalController = TemporalController;
})(typeof window !== "undefined" ? window : this);
