/* global NodeFilter */
// Mistral NeX Stocks - Content Script for Ticker Detection

(function () {
  "use strict";

  // Common English words / stop-words to exclude from 2-5 letter uppercase ticker matches
  const EXCLUDED_WORDS = new Set([
    "A",
    "I",
    "IT",
    "IN",
    "ON",
    "AT",
    "TO",
    "IF",
    "IS",
    "OF",
    "OR",
    "BY",
    "DO",
    "BE",
    "ME",
    "MY",
    "HE",
    "WE",
    "NO",
    "SO",
    "UP",
    "US",
    "AM",
    "AN",
    "AS",
    "GO",
    "AND",
    "THE",
    "FOR",
    "NOT",
    "BUT",
    "CAN",
    "ALL",
    "ANY",
    "MAY",
    "NEW",
    "NOW",
    "OUT",
    "PER",
    "SEE",
    "TOP",
    "WAY",
    "WHO",
    "BOY",
    "CAT",
    "DOG",
    "DAY",
    "MAN",
    "CEO",
    "CFO",
    "CTO",
    "COO",
    "IPO",
    "SEC",
    "FED",
    "GDP",
    "CPI",
    "PPI",
    "PMI",
    "USD",
    "EUR",
    "JPY",
    "GBP",
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "HKD",
    "NZD",
    "KRW",
    "API",
    "AI",
    "ML",
    "CPU",
    "GPU",
    "RAM",
    "HDD",
    "SSD",
    "URL",
    "URI",
    "PDF",
    "CSV",
    "PNG",
    "JPG",
    "GIF",
    "SVG",
    "XML",
    "HTML",
    "CSS",
    "JS",
    "JSON",
    "REST",
    "HTTP",
    "HTTPS",
    "NYSE",
    "NASDAQ",
    "AMEX",
    "TOKYO",
    "INDEX",
    "STOCK",
    "SHARE",
    "EDIT",
    "VIEW",
    "HOME",
    "PAGE",
    "DATE",
    "TIME",
    "BLOG",
    "NEWS",
    "POST",
    "READ",
    "LIKE",
    "MORE",
    "LESS",
    "HELP",
    "DONE",
    "FAIL",
    "TRUE",
    "FALSE",
    "NULL",
    "MAIN",
    "USER",
    "DATA",
    "CODE",
    "TEXT",
    "FILE",
    "TYPE",
    "FORM",
    "LINK",
    "ITEM",
    "INFO",
    "MENU",
    "ICON",
    "LOGO",
    "JOIN",
    "SEND",
    "SAVE",
    "COPY",
    "PASTE",
    "LOAD",
    "OPEN",
    "SHOW",
    "HIDE",
    "PLAY",
    "STOP",
    "NEXT",
    "PREV",
    "BACK",
    "WORK",
    "SITE",
    "THIS",
    "THAT",
    "FROM",
    "WITH",
    "HAVE",
    "WILL",
    "YOUR",
    "THEY",
    "KNOW",
    "WANT",
    "BEEN",
    "GOOD",
    "MUCH",
    "SOME",
    "VERY",
    "WHEN",
    "COME",
    "HERE",
    "JUST",
    "LONG",
    "MAKE",
    "MANY",
    "ONLY",
    "OVER",
    "SUCH",
    "TAKE",
    "THAN",
    "THEM",
    "WELL",
    "WERE",
    "WHAT",
    "YEAR",
    "BOTH",
    "EACH",
    "MOST",
    "SAID",
    "SAYS",
    "ALSO",
    "INTO",
    "THEN",
    "EVEN",
    "MADE",
    "MUST",
    "PART",
  ]);

  // Specific well-known tickers to prioritize or always match
  const KNOWN_US_TICKERS = new Set([
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "GOOG",
    "META",
    "TSLA",
    "BRK.B",
    "AVGO",
    "JPM",
    "ELI",
    "LLY",
    "V",
    "UNH",
    "MA",
    "PG",
    "HD",
    "JNJ",
    "MRK",
    "ABBV",
    "CVX",
    "CRM",
    "BAC",
    "AMD",
    "KO",
    "PEP",
    "NFLX",
    "WMT",
    "COST",
    "TMO",
    "CSCO",
    "ACN",
    "MCD",
    "DIS",
    "ABT",
    "ORCL",
    "INTC",
    "CMCSA",
    "PFE",
    "VZ",
    "NKE",
    "TXN",
    "QCOM",
    "PM",
    "DHR",
    "SPGI",
    "IBM",
    "NOW",
    "GE",
    "HON",
    "AMAT",
    "UNP",
    "AMGN",
    "CAT",
    "LOW",
    "BA",
    "GS",
    "PLTR",
    "ARM",
    "SMCI",
    "MU",
    "COIN",
    "HOOD",
    "MSTR",
    "RDDT",
    "SNOW",
    "CRWD",
    "PANW",
    "NET",
    "A",
    "T",
    "F",
    "MO",
    "GM",
  ]);

  /**
   * Traverse text nodes in document body while ignoring non-content elements
   */
  function extractPageTextSnippets() {
    const textBlocks = [];
    const walker = document.createTreeWalker(
      document.body || document.documentElement,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          if (!node || !node.parentElement) return NodeFilter.FILTER_REJECT;
          const tag = node.parentElement.tagName.toUpperCase();
          if (
            [
              "SCRIPT",
              "STYLE",
              "NOSCRIPT",
              "TEXTAREA",
              "INPUT",
              "SELECT",
              "OPTION",
              "CODE",
              "PRE",
              "SVG",
            ].includes(tag)
          ) {
            return NodeFilter.FILTER_REJECT;
          }
          if (node.parentElement.isContentEditable) {
            return NodeFilter.FILTER_REJECT;
          }
          const val = node.nodeValue ? node.nodeValue.trim() : "";
          return val.length >= 2
            ? NodeFilter.FILTER_ACCEPT
            : NodeFilter.FILTER_SKIP;
        },
      },
    );

    let currentNode;
    while ((currentNode = walker.nextNode())) {
      textBlocks.push({
        text: currentNode.nodeValue,
        element: currentNode.parentElement,
      });
    }

    return textBlocks;
  }

  /**
   * Scan page text for stock tickers
   */
  function detectTickers() {
    const textBlocks = extractPageTextSnippets();
    const foundMap = new Map(); // symbol -> { symbol, market, count, snippet }

    // Regex 1: Explicit $ symbols (e.g. $AAPL, $TSLA, $7203)
    const dollarRegex = /\$([A-Z]{1,5}|[1-9]\d{3})\b/g;

    // Regex 2: Japanese stock codes (e.g. 7203, 9984, 6758.T)
    const jpRegex = /\b([1-9]\d{3})(?:\.T)?\b/g;

    // Regex 3: US stock ticker symbols (2 to 5 uppercase letters)
    const usRegex = /\b([A-Z]{2,5})\b/g;

    for (const block of textBlocks) {
      const text = block.text;

      // 1. Check dollar prefixed tickers (High Confidence)
      let match;
      dollarRegex.lastIndex = 0;
      while ((match = dollarRegex.exec(text)) !== null) {
        const raw = match[1];
        const isJp = /^\d{4}$/.test(raw);
        const symbol = isJp ? `${raw}.T` : raw;
        const market = isJp ? "jp" : "us";

        addMatch(foundMap, symbol, market, text, match.index, 3); // weight 3
      }

      // 2. Check Japanese stock codes
      jpRegex.lastIndex = 0;
      while ((match = jpRegex.exec(text)) !== null) {
        const code = match[1];
        // Ensure not part of a standard year or date or price like 2024, 2025, 2026, 1000
        // Use a sliding window of plausible years so the rule does not go stale.
        const yearVal = parseInt(code, 10);
        const currentYear = new Date().getFullYear();
        if (yearVal >= 1990 && yearVal <= currentYear + 1) continue; // skip year numbers

        // Check surrounding context for Japanese stock indicators or just numeric stock code
        const symbol = `${code}.T`;
        addMatch(foundMap, symbol, "jp", text, match.index, 2);
      }

      // 3. Check US Ticker symbols
      usRegex.lastIndex = 0;
      while ((match = usRegex.exec(text)) !== null) {
        const sym = match[1];
        const isKnown = KNOWN_US_TICKERS.has(sym);
        if (!isKnown && EXCLUDED_WORDS.has(sym)) continue;
        if (!isKnown && sym.length < 3) continue; // Skip unknown 2-letter words

        addMatch(foundMap, sym, "us", text, match.index, isKnown ? 2 : 1);
      }
    }

    // Convert map to sorted array
    const results = Array.from(foundMap.values())
      .filter(
        (item) =>
          item.score >= 2 ||
          KNOWN_US_TICKERS.has(item.symbol) ||
          item.market === "jp",
      )
      .sort((a, b) => b.score - a.score || b.count - a.count)
      .slice(0, 20); // Top 20 detected tickers

    return {
      ok: true,
      title: document.title || "Web Page",
      url: window.location.href,
      tickers: results,
    };
  }

  function addMatch(map, symbol, market, fullText, matchIndex, weight = 1) {
    const existing = map.get(symbol);
    const snippetStart = Math.max(0, matchIndex - 25);
    const snippetEnd = Math.min(
      fullText.length,
      matchIndex + symbol.length + 25,
    );
    const snippet = fullText.substring(snippetStart, snippetEnd).trim();

    if (existing) {
      existing.count += 1;
      existing.score += weight;
      if (!existing.snippet && snippet) {
        existing.snippet = snippet;
      }
    } else {
      map.set(symbol, {
        symbol,
        market,
        count: 1,
        score: weight,
        snippet: snippet || "",
      });
    }
  }

  // Runtime message listener
  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request && request.action === "detectTickers") {
      try {
        const data = detectTickers();
        sendResponse(data);
      } catch (err) {
        console.error("Ticker detection error:", err);
        sendResponse({ ok: false, error: String(err) });
      }
      return true;
    }
  });
})();
