/**
 * tradingview_manager.js - TradingView Widget lifecycle and rendering manager.
 *
 * Manages TradingView Ticker Tape widget and Advanced Real-Time Chart widget.
 */

(function (window) {
  "use strict";

  const DEFAULT_TAPE_SYMBOLS = [
    { proName: "INDEX:NKY", title: "日経225", description: "日経225" },
    { proName: "FOREXCOM:DJI", title: "ダウ平均", description: "ダウ平均" },
    { proName: "FOREXCOM:SPXUSD", title: "S&P 500", description: "S&P 500" },
    {
      proName: "FOREXCOM:NSXUSD",
      title: "ナスダック総合",
      description: "ナスダック総合",
    },
    {
      proName: "CAPITALCOM:DXY",
      title: "ドルインデックス",
      description: "ドルインデックス",
    },
    { proName: "CAPITALCOM:VIX", title: "VIX指数", description: "VIX指数" },
    {
      proName: "OTC:SFTBY",
      title: "ソフトバンクG",
      description: "ソフトバンクG",
    },
    { proName: "NYSE:MUFG", title: "三菱UFJ", description: "三菱UFJ" },
    { proName: "NYSE:SONY", title: "ソニーG", description: "ソニーG" },
    { proName: "TVC:GOLD", title: "金", description: "金" },
    { proName: "TVC:USOIL", title: "原油", description: "原油" },
    {
      proName: "FOREXCOM:JP225",
      title: "日経225先物",
      description: "日経225先物",
    },
    { proName: "FRED:DGS10", title: "米10年債", description: "米10年債" },
    { proName: "FX:USDJPY", title: "ドル円", description: "ドル円" },
    { proName: "FX:EURUSD", title: "ユーロドル", description: "ユーロドル" },
    { proName: "FX:GBPJPY", title: "ポンド円", description: "ポンド円" },
    { proName: "BITSTAMP:BTCUSD", title: "BTC/USD", description: "BTC/USD" },
    { proName: "BITFLYER:BTCJPY", title: "BTC/JPY", description: "BTC/JPY" },
    { proName: "COINBASE:BTCUSD", title: "BTCUSD.P", description: "BTCUSD.P" },
  ];

  /**
   * Resolve an internal stock symbol (e.g., 7203.T, AAPL, ^GSPC) to TradingView symbol.
   * NOTE: mirrors the backend INDEX_MAP in utils/tradingview_mapper.py. The
   * server-provided tv_symbol (SSE mode 2) is preferred; this is only a fallback
   * for non-SSE paths (/api/stocks, modes 0/1). Keep both in sync.
   * @param {string} symbol
   * @returns {string}
   */
  function mapTickerToTvSymbol(symbol) {
    if (!symbol) return "";
    const clean = String(symbol).trim().toUpperCase();
    if (clean === "9984" || clean === "9984.T") return "OTC:SFTBY";
    if (clean === "8306" || clean === "8306.T") return "NYSE:MUFG";
    if (clean === "6758" || clean === "6758.T") return "NYSE:SONY";
    if (clean === "^GSPC" || clean === "SPX") return "FOREXCOM:SPXUSD";
    if (clean === "^IXIC" || clean === "NASDAQ") return "FOREXCOM:NSXUSD";
    if (clean === "^DJI" || clean === "DJI") return "FOREXCOM:DJI";
    if (clean === "^N225" || clean === "NI225") return "INDEX:NKY";
    if (clean === "^TOPX" || clean === "TOPIX") return "TSE:TOPIX";
    if (clean === "DXY") return "CAPITALCOM:DXY";
    if (clean === "^VIX" || clean === "VIX") return "CAPITALCOM:VIX";
    if (clean === "GOLD") return "TVC:GOLD";
    if (clean === "USOIL") return "TVC:USOIL";
    if (clean === "NK2251!") return "FOREXCOM:JP225";
    if (clean === "US10Y") return "FRED:DGS10";
    if (clean === "USDJPY" || clean === "USDJPY=X") return "FX:USDJPY";
    if (clean === "EURUSD" || clean === "EURUSD=X") return "FX:EURUSD";
    if (clean === "GBPJPY" || clean === "GBPJPY=X") return "FX:GBPJPY";
    if (clean === "BTCUSD") return "BITSTAMP:BTCUSD";
    if (clean === "BTCJPY") return "BITFLYER:BTCJPY";
    if (clean === "BTCUSD.P") return "COINBASE:BTCUSD";
    if (clean.endsWith(".T")) return `TSE:${clean.slice(0, -2)}`;
    if (clean.startsWith("^")) return `INDEX:${clean.slice(1)}`;
    return `NASDAQ:${clean}`;
  }

  const TradingViewManager = {
    /**
     * Clear container element safely.
     * @param {string|HTMLElement} container
     */
    clearContainer(container) {
      const el =
        typeof container === "string"
          ? document.getElementById(container)
          : container;
      if (el) {
        el.innerHTML = "";
      }
    },

    /**
     * Render or refresh TradingView Ticker Tape widget.
     * @param {string} containerId
     * @param {Array<{proName: string, title?: string}>} [customSymbols]
     */
    initTickerTape(containerId, customSymbols) {
      const container = document.getElementById(containerId);
      if (!container) return;

      this.clearContainer(container);

      const symbolsToUse =
        customSymbols && customSymbols.length > 0
          ? customSymbols
          : DEFAULT_TAPE_SYMBOLS;

      const widgetContainer = document.createElement("div");
      widgetContainer.className = "tradingview-widget-container";
      widgetContainer.style.width = "100%";
      widgetContainer.style.height = "48px";

      const widgetInner = document.createElement("div");
      widgetInner.className = "tradingview-widget-container__widget";
      widgetContainer.appendChild(widgetInner);

      const isDark = !document.body.classList.contains("light-mode");
      const config = {
        symbols: symbolsToUse,
        showSymbolLogo: true,
        isTransparent: true,
        displayMode: "adaptive",
        colorTheme: isDark ? "dark" : "light",
        locale: "ja",
      };

      const script = document.createElement("script");
      script.type = "text/javascript";
      script.src =
        "https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js";
      script.async = true;
      script.text = JSON.stringify(config);

      const nonce =
        document.querySelector("script[nonce]")?.getAttribute("nonce") || "";
      if (nonce) {
        script.setAttribute("nonce", nonce);
      }

      // If the TradingView CDN script fails to load (offline / adblock / CSP
      // block), clear the band and deactivate it so the next SSE snapshot with
      // tv_ticker_tape data can retry initialization instead of leaving an
      // empty 48px strip forever.
      script.onerror = () => {
        this.clearContainer(container);
        container.classList.remove("active");
      };

      widgetContainer.appendChild(script);
      container.appendChild(widgetContainer);
    },

    /**
     * Render TradingView Advanced Real-Time Chart inside target container.
     * @param {string} containerId
     * @param {string} ticker
     */
    renderAdvancedChart(containerId, ticker) {
      const container = document.getElementById(containerId);
      if (!container) return;

      this.clearContainer(container);
      // Accept server-provided TV symbols (e.g. "NASDAQ:AAPL") directly so the
      // client-side fallback mapping cannot drift from the backend mapper.
      const tvSymbol = String(ticker).includes(":")
        ? ticker
        : mapTickerToTvSymbol(ticker);
      const isDark = !document.body.classList.contains("light-mode");

      const mountWidget = () => {
        if (window.TradingView && window.TradingView.widget) {
          try {
            new window.TradingView.widget({
              container_id: containerId,
              symbol: tvSymbol,
              interval: "D",
              timezone: "Asia/Tokyo",
              theme: isDark ? "dark" : "light",
              style: "1",
              locale: "ja",
              toolbar_bg: isDark ? "#1e222d" : "#f1f3f6",
              enable_publishing: false,
              allow_symbol_change: true,
              save_image: false,
              autosize: true,
              hide_side_toolbar: false,
            });
          } catch (e) {
            console.error("TradingView Advanced Chart render error:", e);
          }
        }
      };

      if (window.TradingView && window.TradingView.widget) {
        mountWidget();
      } else {
        // Dynamically load tv.js if not yet available
        let tvScript = document.getElementById("tv-script-js");
        if (!tvScript) {
          tvScript = document.createElement("script");
          tvScript.id = "tv-script-js";
          tvScript.src = "https://s3.tradingview.com/tv.js";
          tvScript.async = true;
          const nonce =
            document.querySelector("script[nonce]")?.getAttribute("nonce") ||
            "";
          if (nonce) tvScript.setAttribute("nonce", nonce);
          tvScript.onload = mountWidget;
          document.head.appendChild(tvScript);
        } else {
          tvScript.addEventListener("load", mountWidget);
        }
      }
    },
  };

  window.TradingViewManager = TradingViewManager;
})(window);
