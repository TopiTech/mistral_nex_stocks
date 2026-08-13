/**
 * tradingview_manager.js - TradingView Widget lifecycle and rendering manager.
 *
 * Manages TradingView Ticker Tape widget and Advanced Real-Time Chart widget.
 */

(function (window) {
  "use strict";

  // Exactly ONE window-level TradingView error handler is registered at a time
  // (R9). renderAdvancedChart() creates a fresh closure per render (each
  // fullscreen open / exchange switch), and registering them all would (a)
  // leak listeners on every open/close and (b) let a stale closure's
  // doNyseFallbackSwitch re-render the OLD symbol over the current chart.
  // The previous handler is removed before the new one is installed, and
  // clearContainer() detaches it as well.
  let activeMessageHandler = null;
  let _tickerTapeGeneration = 0;

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
  /**
   * Helper to resolve exchange string to TradingView prefix on client side.
   * NOTE: mirrors utils/tradingview_mapper.py resolve_exchange_prefix. Keep
   * both in sync when exchange aliases are added or changed.
   * @param {string} exchange
   * @returns {string|null}
   */
  function resolveExchangePrefixJS(exchange) {
    if (!exchange || typeof exchange !== "string") return null;
    // Strip whitespace, hyphens, underscores and commas so aliases like
    // "NEW YORK STOCK EXCHANGE, INC." normalize to the same token the
    // server-side resolve_exchange_prefix matches.
    const ex = exchange
      .trim()
      .toUpperCase()
      .replace(/[\s\-_]/g, "")
      .replace(/,/g, "");
    if (
      [
        "NYQ",
        "NYSE",
        "NYS",
        "NYE",
        "PCX",
        "ARC",
        "ARCA",
        "NYSEARCA",
        "NYSEMKT",
        "NEWYORKSTOCKEXCHANGE",
        "NEWYORKSTOCKEXCHANGEINC",
      ].includes(ex)
    )
      return "NYSE";
    if (
      [
        "NMS",
        "NGM",
        "NCM",
        "NAS",
        "NASDAQ",
        "NASDAQGS",
        "NASDAQGM",
        "NASDAQCM",
        "NASDAQSTOCKMARKET",
      ].includes(ex)
    )
      return "NASDAQ";
    if (["ASE", "AMEX", "NYSEAMERICAN"].includes(ex)) return "AMEX";
    if (["TSE", "TYO", "JPX", "TOKYO"].includes(ex)) return "TSE";
    if (["PNK", "PINK", "OTC", "OTCMKTS", "OTCBB"].includes(ex)) return "OTC";
    if (["BAT", "BATS", "CBOE"].includes(ex)) return "BATS";
    if (["IEX"].includes(ex)) return "IEX";

    if (
      ex.includes("NYSE") ||
      ex.includes("NYQ") ||
      ex.includes("ARCA") ||
      ex.includes("PCX") ||
      ex.includes("NYE")
    )
      return "NYSE";
    if (
      ex.includes("NASDAQ") ||
      ex.includes("NMS") ||
      ex.includes("NGM") ||
      ex.includes("NCM") ||
      ex.includes("NAS")
    )
      return "NASDAQ";
    if (ex.includes("AMEX") || ex.includes("AMERICAN") || ex.includes("ASE"))
      return "AMEX";
    return null;
  }

  // Pre-populated safety dictionary for known NYSE/AMEX/NASDAQ stocks to prevent incorrect NASDAQ default
  const KNOWN_US_STOCK_TV_MAP = {
    IBM: "NYSE:IBM",
    IONQ: "NYSE:IONQ",
    "BRK.A": "NYSE:BRK.A",
    "BRK.B": "NYSE:BRK.B",
    JNJ: "NYSE:JNJ",
    JPM: "NYSE:JPM",
    V: "NYSE:V",
    MA: "NYSE:MA",
    UNH: "NYSE:UNH",
    PG: "NYSE:PG",
    HD: "NYSE:HD",
    BAC: "NYSE:BAC",
    XOM: "NYSE:XOM",
    CVX: "NYSE:CVX",
    KO: "NYSE:KO",
    NKE: "NYSE:NKE",
    DIS: "NYSE:DIS",
    WMT: "NYSE:WMT",
    LLY: "NYSE:LLY",
    ORCL: "NYSE:ORCL",
    PLTR: "NASDAQ:PLTR",
    PFE: "NYSE:PFE",
    ABBV: "NYSE:ABBV",
    MRK: "NYSE:MRK",
    CRM: "NYSE:CRM",
    BABA: "NYSE:BABA",
    SONY: "NYSE:SONY",
    MUFG: "NYSE:MUFG",
    SMFG: "NYSE:SMFG",
    TM: "NYSE:TM",
    HMC: "NYSE:HMC",
    GE: "NYSE:GE",
    T: "NYSE:T",
    C: "NYSE:C",
    F: "NYSE:F",
    X: "NYSE:X",
    CAT: "NYSE:CAT",
    MMM: "NYSE:MMM",
    LMT: "NYSE:LMT",
    RTX: "NYSE:RTX",
    BA: "NYSE:BA",
    GS: "NYSE:GS",
    MS: "NYSE:MS",
    WFC: "NYSE:WFC",
    SCHW: "NYSE:SCHW",
    AMT: "NYSE:AMT",
    SPG: "NYSE:SPG",
    LOW: "NYSE:LOW",
    DE: "NYSE:DE",
    SYK: "NYSE:SYK",
    MDT: "NYSE:MDT",
    EL: "NYSE:EL",
    CL: "NYSE:CL",
    KMB: "NYSE:KMB",
    MO: "NYSE:MO",
    PM: "NYSE:PM",
    M: "NYSE:M",
    L: "NYSE:L",
    W: "NYSE:W",
    K: "NYSE:K",
    U: "NYSE:U",
    AI: "NYSE:AI",
    UBER: "NYSE:UBER",
    LYFT: "NYSE:LYFT",
    RBLX: "NYSE:RBLX",
    COIN: "NASDAQ:COIN",
    NIO: "NYSE:NIO",
    XPEV: "NYSE:XPEV",
    LI: "NASDAQ:LI",
    TSM: "NYSE:TSM",
    ASML: "NASDAQ:ASML",
    RKT: "NYSE:RKT",
    SNOW: "NYSE:SNOW",
    NET: "NYSE:NET",
    PATH: "NYSE:PATH",
    SPOT: "NYSE:SPOT",
    SQ: "NYSE:SQ",
    SHOP: "NYSE:SHOP",
    SNAP: "NYSE:SNAP",
    TWLO: "NYSE:TWLO",
    // Popular NASDAQ stocks
    AAPL: "NASDAQ:AAPL",
    NVDA: "NASDAQ:NVDA",
    MSFT: "NASDAQ:MSFT",
    AMZN: "NASDAQ:AMZN",
    META: "NASDAQ:META",
    GOOGL: "NASDAQ:GOOGL",
    GOOG: "NASDAQ:GOOG",
    TSLA: "NASDAQ:TSLA",
    AMD: "NASDAQ:AMD",
    INTC: "NASDAQ:INTC",
    QCOM: "NASDAQ:QCOM",
    AVGO: "NASDAQ:AVGO",
    TXN: "NASDAQ:TXN",
    AMAT: "NASDAQ:AMAT",
    MU: "NASDAQ:MU",
    CSCO: "NASDAQ:CSCO",
    ADBE: "NASDAQ:ADBE",
    NFLX: "NASDAQ:NFLX",
    PYPL: "NASDAQ:PYPL",
    COST: "NASDAQ:COST",
    PEP: "NASDAQ:PEP",
  };

  /**
   * Resolve symbol metadata returning { tvSymbol, exchangePrefix, isFallback }.
   * @param {string} symbol
   * @param {string} [exchange]
   * @returns {{ tvSymbol: string, exchangePrefix: string, isFallback: boolean }}
   */
  function resolveTvSymbolMetaJS(symbol, exchange) {
    if (!symbol) return { tvSymbol: "", exchangePrefix: "", isFallback: false };
    const clean = String(symbol).trim().toUpperCase();

    // 1. If symbol already has exchange prefix (e.g., NYSE:IBM, NASDAQ:AAPL)
    if (clean.includes(":")) {
      const parts = clean.split(":");
      return {
        tvSymbol: clean,
        exchangePrefix: parts[0],
        isFallback: false,
      };
    }

    // 2. Check if exchange parameter was passed
    const prefixFromEx = resolveExchangePrefixJS(exchange);
    if (prefixFromEx) {
      return {
        tvSymbol: `${prefixFromEx}:${clean}`,
        exchangePrefix: prefixFromEx,
        isFallback: false,
      };
    }

    // 3. Dynamic lookup from the global state (window.state.stocks is keyed by
    //    market: {"us": [...], "jp": [...]}). (R20: replaces the dead
    //    ``window.appState`` reference which was never assigned anywhere.)
    if (window.state && window.state.stocks) {
      const match = ["us", "jp"]
        .map((m) =>
          Array.isArray(window.state.stocks[m])
            ? window.state.stocks[m].find(
                (s) => s && (s.symbol === clean || s.ticker === clean),
              )
            : undefined,
        )
        .find(Boolean);
      if (match) {
        if (match.tv_symbol && match.tv_symbol.includes(":")) {
          const pref = match.tv_symbol.split(":")[0];
          return {
            tvSymbol: match.tv_symbol,
            exchangePrefix: pref,
            isFallback: false,
          };
        }
        if (match.exchange) {
          const exPref = resolveExchangePrefixJS(match.exchange);
          if (exPref) {
            return {
              tvSymbol: `${exPref}:${clean}`,
              exchangePrefix: exPref,
              isFallback: false,
            };
          }
        }
      }
    }

    // 4. Known index mappings & overrides
    if (clean === "9984" || clean === "9984.T")
      return {
        tvSymbol: "OTC:SFTBY",
        exchangePrefix: "OTC",
        isFallback: false,
      };
    if (clean === "8306" || clean === "8306.T")
      return {
        tvSymbol: "NYSE:MUFG",
        exchangePrefix: "NYSE",
        isFallback: false,
      };
    if (clean === "6758" || clean === "6758.T")
      return {
        tvSymbol: "NYSE:SONY",
        exchangePrefix: "NYSE",
        isFallback: false,
      };
    if (clean === "^GSPC" || clean === "SPX")
      return {
        tvSymbol: "FOREXCOM:SPXUSD",
        exchangePrefix: "FOREXCOM",
        isFallback: false,
      };
    if (clean === "^IXIC" || clean === "NASDAQ")
      return {
        tvSymbol: "FOREXCOM:NSXUSD",
        exchangePrefix: "FOREXCOM",
        isFallback: false,
      };
    if (clean === "^DJI" || clean === "DJI")
      return {
        tvSymbol: "FOREXCOM:DJI",
        exchangePrefix: "FOREXCOM",
        isFallback: false,
      };
    if (clean === "^N225" || clean === "NI225")
      return {
        tvSymbol: "INDEX:NKY",
        exchangePrefix: "INDEX",
        isFallback: false,
      };
    if (clean === "^TOPX" || clean === "TOPIX")
      return {
        tvSymbol: "TSE:TOPIX",
        exchangePrefix: "TSE",
        isFallback: false,
      };
    if (clean === "DXY")
      return {
        tvSymbol: "CAPITALCOM:DXY",
        exchangePrefix: "CAPITALCOM",
        isFallback: false,
      };
    if (clean === "^VIX" || clean === "VIX")
      return {
        tvSymbol: "CAPITALCOM:VIX",
        exchangePrefix: "CAPITALCOM",
        isFallback: false,
      };
    if (clean === "GOLD")
      return {
        tvSymbol: "TVC:GOLD",
        exchangePrefix: "TVC",
        isFallback: false,
      };
    if (clean === "USOIL")
      return {
        tvSymbol: "TVC:USOIL",
        exchangePrefix: "TVC",
        isFallback: false,
      };
    if (clean === "NK2251!")
      return {
        tvSymbol: "FOREXCOM:JP225",
        exchangePrefix: "FOREXCOM",
        isFallback: false,
      };
    if (clean === "US10Y")
      return {
        tvSymbol: "FRED:DGS10",
        exchangePrefix: "FRED",
        isFallback: false,
      };
    if (clean === "USDJPY" || clean === "USDJPY=X")
      return {
        tvSymbol: "FX:USDJPY",
        exchangePrefix: "FX",
        isFallback: false,
      };
    if (clean === "EURUSD" || clean === "EURUSD=X")
      return {
        tvSymbol: "FX:EURUSD",
        exchangePrefix: "FX",
        isFallback: false,
      };
    if (clean === "GBPJPY" || clean === "GBPJPY=X")
      return {
        tvSymbol: "FX:GBPJPY",
        exchangePrefix: "FX",
        isFallback: false,
      };
    if (clean === "BTCUSD")
      return {
        tvSymbol: "BITSTAMP:BTCUSD",
        exchangePrefix: "BITSTAMP",
        isFallback: false,
      };
    if (clean === "BTCJPY")
      return {
        tvSymbol: "BITFLYER:BTCJPY",
        exchangePrefix: "BITFLYER",
        isFallback: false,
      };
    if (clean === "BTCUSD.P")
      return {
        tvSymbol: "COINBASE:BTCUSD",
        exchangePrefix: "COINBASE",
        isFallback: false,
      };
    if (clean.endsWith(".T"))
      return {
        tvSymbol: `TSE:${clean.slice(0, -2)}`,
        exchangePrefix: "TSE",
        isFallback: false,
      };
    if (clean.startsWith("^"))
      return {
        tvSymbol: `INDEX:${clean.slice(1)}`,
        exchangePrefix: "INDEX",
        isFallback: false,
      };

    // 5. Known US stock dictionary lookup
    if (KNOWN_US_STOCK_TV_MAP[clean]) {
      const tvSym = KNOWN_US_STOCK_TV_MAP[clean];
      return {
        tvSymbol: tvSym,
        exchangePrefix: tvSym.split(":")[0],
        isFallback: false,
      };
    }

    // 6. Symbol heuristics (dot/dash class shares or 1-2 character tickers default to NYSE)
    if (
      !clean.startsWith("^") &&
      !clean.endsWith(".T") &&
      !clean.includes(":")
    ) {
      if (clean.includes(".") || clean.includes("-")) {
        KNOWN_US_STOCK_TV_MAP[clean] = `NYSE:${clean}`;
        return {
          tvSymbol: `NYSE:${clean}`,
          exchangePrefix: "NYSE",
          isFallback: false,
        };
      }
      if (clean.length <= 2 && /^[A-Z]+$/.test(clean)) {
        KNOWN_US_STOCK_TV_MAP[clean] = `NYSE:${clean}`;
        return {
          tvSymbol: `NYSE:${clean}`,
          exchangePrefix: "NYSE",
          isFallback: false,
        };
      }
    }

    // Standard US stock symbol default fallback
    return {
      tvSymbol: `NASDAQ:${clean}`,
      exchangePrefix: "NASDAQ",
      isFallback: true,
    };
  }

  /**
   * Resolve an internal symbol to a TradingView formatted symbol (e.g. TSE:7203, NYSE:IBM, NASDAQ:AAPL).
   * @param {string} symbol
   * @param {string} [exchange]
   * @returns {string}
   */
  function mapTickerToTvSymbol(symbol, exchange) {
    const meta = resolveTvSymbolMetaJS(symbol, exchange);
    return meta.tvSymbol;
  }

  const TradingViewManager = {
    mapTickerToTvSymbol,
    /**
     * Clear container element safely.
     * @param {string|HTMLElement} container
     */
    clearContainer(container) {
      _tickerTapeGeneration++;
      const el =
        typeof container === "string"
          ? document.getElementById(container)
          : container;
      // Detach the window-level TradingView error handler: the chart it was
      // created for is being destroyed, and leaving it registered would let a
      // stale closure re-render an old symbol (R9).
      if (activeMessageHandler) {
        window.removeEventListener("message", activeMessageHandler);
        activeMessageHandler = null;
      }
      if (el) {
        el.replaceChildren();
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
      const currentGen = _tickerTapeGeneration;

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

      script.onerror = () => {
        if (currentGen !== _tickerTapeGeneration) return;
        this.clearContainer(container);
        container.classList.remove("active");
      };

      widgetContainer.appendChild(script);
      if (currentGen === _tickerTapeGeneration && container.isConnected) {
        container.appendChild(widgetContainer);
      }
    },

    /**
     * Render TradingView Advanced Real-Time Chart inside target container
     * with automatic NASDAQ -> NYSE fallback error recovery.
     * @param {string} containerId
     * @param {string} ticker
     * @param {string} [exchange]
     * @param {object} [options]
     */
    renderAdvancedChart(containerId, ticker, exchange, options = {}) {
      const container = document.getElementById(containerId);
      if (!container) return;

      this.clearContainer(container);

      const baseTicker = String(ticker).includes(":")
        ? String(ticker).split(":")[1]
        : String(ticker).trim().toUpperCase();

      const meta = resolveTvSymbolMetaJS(ticker, exchange);
      let currentTvSymbol = options.forceTvSymbol || meta.tvSymbol;
      const isDark = !document.body.classList.contains("light-mode");

      // Build Top Exchange Control / Notification Bar for full-screen modal
      const controlBar = document.createElement("div");
      controlBar.className = "tv-chart-control-bar";
      controlBar.style.display = "flex";
      controlBar.style.justifyContent = "space-between";
      controlBar.style.alignItems = "center";
      controlBar.style.padding = "6px 12px";
      controlBar.style.fontSize = "12px";
      controlBar.style.background = isDark ? "#131722" : "#e0e3eb";
      controlBar.style.color = isDark ? "#d1d4dc" : "#131722";
      controlBar.style.borderBottom = `1px solid ${isDark ? "#2a2e39" : "#cccccc"}`;

      const statusSpan = document.createElement("span");
      statusSpan.className = "tv-exchange-status";

      const currentPrefix = currentTvSymbol.includes(":")
        ? currentTvSymbol.split(":")[0]
        : "NASDAQ";
      if (options.switchedToNyse) {
        statusSpan.textContent =
          "⚠️ NASDAQでシンボルが見つからないためNYSEに自動切り替えしました";
        statusSpan.style.color = "#f0a500";
      } else if (meta.isFallback && currentPrefix === "NASDAQ") {
        statusSpan.textContent = "市場: NASDAQ (自動判定フォールバック)";
      } else {
        statusSpan.textContent = `市場: ${currentPrefix}`;
      }

      const switchBtnGroup = document.createElement("div");
      switchBtnGroup.style.display = "flex";
      switchBtnGroup.style.gap = "6px";

      const createExchangeBtn = (exName) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = exName;
        btn.style.padding = "2px 8px";
        btn.style.fontSize = "11px";
        btn.style.borderRadius = "4px";
        btn.style.cursor = "pointer";
        btn.style.border = "none";
        const isActive = currentPrefix === exName;
        btn.style.background = isActive
          ? "#2962ff"
          : isDark
            ? "#2a2e39"
            : "#d1d4dc";
        btn.style.color = isActive ? "#ffffff" : isDark ? "#d1d4dc" : "#131722";
        btn.onclick = () => {
          const newTvSym = `${exName}:${baseTicker}`;
          KNOWN_US_STOCK_TV_MAP[baseTicker] = newTvSym;
          this.renderAdvancedChart(containerId, baseTicker, exName, {
            forceTvSymbol: newTvSym,
            switchedToNyse: exName === "NYSE",
          });
        };
        return btn;
      };

      // Only show US exchange switches for non-JP equities and non-indices
      if (
        !baseTicker.endsWith(".T") &&
        !baseTicker.startsWith("^") &&
        !currentTvSymbol.startsWith("TSE:") &&
        !currentTvSymbol.startsWith("FOREXCOM:") &&
        !currentTvSymbol.startsWith("INDEX:")
      ) {
        switchBtnGroup.appendChild(createExchangeBtn("NASDAQ"));
        switchBtnGroup.appendChild(createExchangeBtn("NYSE"));
        switchBtnGroup.appendChild(createExchangeBtn("AMEX"));
      }

      controlBar.appendChild(statusSpan);
      controlBar.appendChild(switchBtnGroup);
      container.appendChild(controlBar);

      const chartInnerContainer = document.createElement("div");
      const innerId = `${containerId}-inner`;
      chartInnerContainer.id = innerId;
      chartInnerContainer.style.width = "100%";
      chartInnerContainer.style.height = "calc(100% - 31px)";
      container.appendChild(chartInnerContainer);

      let hasSwitched = false;
      const doNyseFallbackSwitch = (reason) => {
        if (hasSwitched) return;
        hasSwitched = true;
        console.warn(
          `TradingView symbol fallback triggered for ${baseTicker} (${reason}). Switching to NYSE:${baseTicker}`,
        );

        // Register in client-side map
        const newNyseSymbol = `NYSE:${baseTicker}`;
        KNOWN_US_STOCK_TV_MAP[baseTicker] = newNyseSymbol;

        // Update global state if the stock is tracked there (window.state.stocks
        // is keyed by market). (R20: replaces the dead ``window.appState`` lookup.)
        if (window.state && window.state.stocks) {
          const markets = ["us", "jp"];
          for (const m of markets) {
            const list = window.state.stocks[m];
            if (!Array.isArray(list)) continue;
            const st = list.find(
              (s) => s && (s.symbol === baseTicker || s.ticker === baseTicker),
            );
            if (st) {
              st.tv_symbol = newNyseSymbol;
              st.exchange = "NYSE";
              break;
            }
          }
        }

        // Re-render with NYSE force symbol
        this.renderAdvancedChart(containerId, baseTicker, "NYSE", {
          forceTvSymbol: newNyseSymbol,
          switchedToNyse: true,
        });
      };

      // Listen to TradingView postMessage events for symbol error detection.
      // Swap in the new handler and detach the previous one so only a single
      // window-level listener exists at any time (R9).
      const messageHandler = (event) => {
        try {
          if (!event.data) return;
          const dataStr =
            typeof event.data === "string"
              ? event.data
              : JSON.stringify(event.data);
          if (
            (currentPrefix === "NASDAQ" && meta.isFallback) ||
            options.forceTvSymbol
          ) {
            if (
              dataStr.includes("invalid-symbol") ||
              dataStr.includes("quote-error") ||
              dataStr.includes("symbol_not_found") ||
              dataStr.includes("symbol_error") ||
              dataStr.includes("Invalid symbol") ||
              dataStr.includes("Symbol not found")
            ) {
              window.removeEventListener("message", messageHandler);
              if (activeMessageHandler === messageHandler) {
                activeMessageHandler = null;
              }
              doNyseFallbackSwitch("postMessage error event");
            }
          }
        } catch (_err) {
          // ignore postMessage parse errors
        }
      };

      if (activeMessageHandler) {
        window.removeEventListener("message", activeMessageHandler);
      }
      activeMessageHandler = messageHandler;
      window.addEventListener("message", messageHandler);

      const mountWidget = () => {
        if (window.TradingView && window.TradingView.widget) {
          try {
            new window.TradingView.widget({
              container_id: innerId,
              symbol: currentTvSymbol,
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
            if (currentPrefix === "NASDAQ" && meta.isFallback) {
              doNyseFallbackSwitch("widget constructor exception");
            }
          }
        }
      };

      if (window.TradingView && window.TradingView.widget) {
        mountWidget();
      } else {
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
