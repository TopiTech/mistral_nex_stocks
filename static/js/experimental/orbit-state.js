/**
 * orbit-state.js - State management for Market Observatory.
 *
 * Implements a reactive, encapsulated state store with subscription support
 * and History API synchronization.
 */

(function (global) {
  "use strict";

  class ObservatoryState {
    constructor() {
      // Parse URL parameters if present
      const urlParams = new URLSearchParams(window.location.search);
      const initialSymbol = urlParams.get("symbol")
        ? urlParams.get("symbol").trim().toUpperCase()
        : "";
      const initialMarket = (urlParams.get("market") || "us")
        .trim()
        .toLowerCase();

      // Check prefers-reduced-motion
      const prefersReduced =
        window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const storedReduced = localStorage.getItem(
        "mns_observatory_reduced_motion",
      );

      this.data = {
        market: ["us", "jp", "all"].includes(initialMarket)
          ? initialMarket
          : "us",
        stocks: new Map(),
        stockList: [],
        selectedSymbol: initialSymbol,
        hoveredSymbol: null,
        draggedSymbol: null,
        dragPos: null,
        isOverCenterDrop: false,
        timeCursor: 0, // 0 = latest/live, negative integer = historical steps back
        timeGranularity: "3mo", // 1d, 5d, 1mo, 3mo, 1y
        isConnectMode: false,
        connectedSymbols: [], // Max 3 symbols
        aiDiveOpen: false,
        aiDiveSymbol: null,
        aiAnalysisStatus: "idle", // idle, loading, complete, error, cancelled
        aiAnalysisResult: null,
        aiComparisonStatus: "idle",
        aiComparisonResult: null,
        reducedMotion:
          storedReduced !== null ? storedReduced === "true" : prefersReduced,
        paused: false,
        loading: true,
        error: null,
        historyMap: new Map(), // symbol:granularity -> normalized history array
        activeHistory: [],
      };

      this._listeners = new Set();
      this._historyAbortControllers = new Map();
      this._aiAbortController = null;
    }

    get state() {
      return this.data;
    }

    subscribe(listener) {
      if (typeof listener === "function") {
        this._listeners.add(listener);
      }
      return () => {
        this._listeners.delete(listener);
      };
    }

    notify(key, value) {
      for (const listener of this._listeners) {
        try {
          listener(key, value, this.data);
        } catch (e) {
          console.error("[ObservatoryState] Listener error:", e);
        }
      }
    }

    set(patch) {
      let changed = false;
      for (const [k, v] of Object.entries(patch)) {
        if (this.data[k] !== v) {
          this.data[k] = v;
          changed = true;
          this.notify(k, v);
        }
      }
      if (
        changed &&
        (patch.selectedSymbol !== undefined || patch.market !== undefined)
      ) {
        this.syncUrl();
      }
    }

    setStocks(stockList) {
      const map = new Map();
      const validList = [];
      for (const s of stockList) {
        if (s && s.symbol) {
          map.set(s.symbol, s);
          validList.push(s);
        }
      }
      this.data.stocks = map;
      this.data.stockList = validList;

      // If no selected symbol or selected symbol not in list, pick the first or top stock
      if (!this.data.selectedSymbol || !map.has(this.data.selectedSymbol)) {
        if (validList.length > 0) {
          this.data.selectedSymbol = validList[0].symbol;
        }
      }

      this.notify("stocks", validList);
      this.notify("selectedSymbol", this.data.selectedSymbol);
      this.syncUrl();
    }

    setSelectedSymbol(symbol) {
      if (!symbol) return;
      const clean = String(symbol).trim().toUpperCase();
      if (this.data.selectedSymbol === clean) return;

      this.data.selectedSymbol = clean;
      this.data.timeCursor = 0; // Reset time cursor to latest on symbol change
      this.notify("selectedSymbol", clean);
      this.syncUrl();
    }

    setHoveredSymbol(symbol) {
      const clean = symbol ? String(symbol).trim().toUpperCase() : null;
      if (this.data.hoveredSymbol === clean) return;
      this.data.hoveredSymbol = clean;
      this.notify("hoveredSymbol", clean);
    }

    setDraggedSymbol(symbol, pos, isOverCenter = false) {
      this.data.draggedSymbol = symbol;
      this.data.dragPos = pos;
      this.data.isOverCenterDrop = isOverCenter;
      this.notify("draggedSymbol", symbol);
      this.notify("dragPos", pos);
      this.notify("isOverCenterDrop", isOverCenter);
    }

    setTimeCursor(cursor) {
      const clamped = Math.max(cursor, 0);
      if (this.data.timeCursor === clamped) return;
      this.data.timeCursor = clamped;
      this.notify("timeCursor", clamped);
    }

    setTimeGranularity(granularity) {
      const valid = ["1d", "5d", "1mo", "3mo", "1y"];
      if (!valid.includes(granularity)) return;
      if (this.data.timeGranularity === granularity) return;
      this.data.timeGranularity = granularity;
      this.data.timeCursor = 0;
      this.notify("timeGranularity", granularity);
    }

    toggleConnectMode(forceState) {
      const next =
        typeof forceState === "boolean" ? forceState : !this.data.isConnectMode;
      this.data.isConnectMode = next;
      if (!next) {
        this.data.connectedSymbols = [];
        this.data.aiComparisonStatus = "idle";
        this.data.aiComparisonResult = null;
      }
      this.notify("isConnectMode", next);
      this.notify("connectedSymbols", this.data.connectedSymbols);
    }

    toggleConnectedSymbol(symbol) {
      if (!symbol) return;
      const clean = String(symbol).trim().toUpperCase();
      const current = [...this.data.connectedSymbols];
      const idx = current.indexOf(clean);

      if (idx >= 0) {
        current.splice(idx, 1);
      } else {
        if (current.length >= 3) {
          current.shift(); // Keep max 3
        }
        current.push(clean);
      }

      this.data.connectedSymbols = current;
      this.data.aiComparisonStatus = "idle";
      this.data.aiComparisonResult = null;
      this.notify("connectedSymbols", current);
    }

    clearConnectedSymbols() {
      this.data.connectedSymbols = [];
      this.data.aiComparisonStatus = "idle";
      this.data.aiComparisonResult = null;
      this.notify("connectedSymbols", []);
    }

    openAiDive(symbol) {
      const targetSymbol = symbol
        ? String(symbol).trim().toUpperCase()
        : this.data.selectedSymbol;
      if (!targetSymbol) return;
      this.data.aiDiveOpen = true;
      this.data.aiDiveSymbol = targetSymbol;
      this.data.aiAnalysisStatus = "idle";
      this.data.aiAnalysisResult = null;
      this.notify("aiDiveOpen", true);
      this.notify("aiDiveSymbol", targetSymbol);
    }

    closeAiDive() {
      if (this._aiAbortController) {
        this._aiAbortController.abort();
        this._aiAbortController = null;
      }
      this.data.aiDiveOpen = false;
      this.data.aiDiveSymbol = null;
      this.data.aiAnalysisStatus = "idle";
      this.notify("aiDiveOpen", false);
      this.notify("aiDiveSymbol", null);
    }

    togglePause(force) {
      const next = typeof force === "boolean" ? force : !this.data.paused;
      this.data.paused = next;
      this.notify("paused", next);
    }

    toggleReducedMotion(force) {
      const next =
        typeof force === "boolean" ? force : !this.data.reducedMotion;
      this.data.reducedMotion = next;
      try {
        localStorage.setItem("mns_observatory_reduced_motion", String(next));
      } catch (_e) {
        // ignore
      }
      this.notify("reducedMotion", next);
    }

    syncUrl() {
      try {
        const url = new URL(window.location.href);
        if (this.data.selectedSymbol) {
          url.searchParams.set("symbol", this.data.selectedSymbol);
        }
        if (this.data.market && this.data.market !== "us") {
          url.searchParams.set("market", this.data.market);
        } else {
          url.searchParams.delete("market");
        }
        window.history.replaceState({}, "", url.toString());
      } catch (_e) {
        // ignore
      }
    }
  }

  global.ObservatoryState = ObservatoryState;
})(typeof window !== "undefined" ? window : this);
