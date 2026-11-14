/* global NodeFilter */
// Mistral NeX Stocks - Content Script for Ticker Detection

(function () {
  "use strict";

  if (window.__MNS_CONTENT_LOADED__) return;
  window.__MNS_CONTENT_LOADED__ = true;

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

  // Maximum text nodes to scan to prevent freezing the main thread on large DOMs
  const MAX_EXTRACT_TEXT_NODES = 2500;

  const IGNORED_ANCESTOR_SELECTOR =
    "script,style,noscript,textarea,input,select,option,code,pre,svg";

  /**
   * Traverse text nodes in document body while ignoring non-content elements
   */
  function extractPageTextSnippets() {
    const textBlocks = [];
    const root = document.body || document.documentElement;
    if (!root) return textBlocks;

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node || !node.parentElement) return NodeFilter.FILTER_REJECT;
        if (
          typeof node.parentElement.closest === "function" &&
          node.parentElement.closest(IGNORED_ANCESTOR_SELECTOR)
        ) {
          return NodeFilter.FILTER_REJECT;
        }
        if (
          node.parentElement.isContentEditable ||
          (typeof node.parentElement.closest === "function" &&
            node.parentElement.closest('[contenteditable="true"]'))
        ) {
          return NodeFilter.FILTER_REJECT;
        }
        const val = node.nodeValue ? node.nodeValue.trim() : "";
        return val.length >= 2
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_SKIP;
      },
    });

    let currentNode;
    while ((currentNode = walker.nextNode())) {
      textBlocks.push({
        text: currentNode.nodeValue,
        element: currentNode.parentElement,
      });
      if (textBlocks.length >= MAX_EXTRACT_TEXT_NODES) {
        break;
      }
    }

    return textBlocks;
  }

  /**
   * Scan page text for stock tickers
   */
  function detectTickers() {
    const textBlocks = extractPageTextSnippets();
    const foundMap = new Map(); // `${market}:${symbol}` -> { symbol, market, count, score, snippet }

    // Regex 1: Explicit $ symbols (e.g. $AAPL, $TSLA, $BRK.B, $V) — exclude numeric amounts like $1000
    const dollarRegex =
      /\$([A-Z]{2,5}\.[A-Z]|[A-Z]{1,5})(?![A-Za-z0-9])(?!\.[A-Za-z])/g;

    // Regex 2: Japanese stock codes (e.g. 7203, 9984, 6758.T, and TSE alphanumeric like 130A / 130A.T)
    const jpRegex = /\b([1-9]\d{2}[0-9A-Z])(?:\.T)?\b/g;

    // Regex 3: US stock ticker symbols (dotted BRK.B, 1-letter known tickers, or 2-5 uppercase letters, excluding $ prefixed / .T suffixes)
    const usRegex =
      /(?:^|[^A-Za-z0-9.$])([A-Z]{2,5}\.[A-Z]|[A-Z]{1,5})(?![A-Za-z0-9])(?!\.[A-Za-z])/g;

    for (const block of textBlocks) {
      const text = block.text;

      // 1. Check dollar prefixed tickers (High Confidence)
      let match;
      dollarRegex.lastIndex = 0;
      while ((match = dollarRegex.exec(text)) !== null) {
        const symbol = match[1];
        addMatch(foundMap, symbol, "us", text, match.index, 3); // weight 3
      }

      // 2. Check Japanese stock codes
      jpRegex.lastIndex = 0;
      while ((match = jpRegex.exec(text)) !== null) {
        const code = match[1];
        const hasTSuffix = match[0].toUpperCase().endsWith(".T");
        const beforeChar = match.index > 0 ? text[match.index - 1] : "";
        if (!hasTSuffix) {
          if (beforeChar === "$") {
            continue; // skip USD currency amounts like $1000
          }
          // 素の4文字コード(価格・数量・ID等と同形)は年度窓の内外を問わず
          // 株式文脈必須にする(FE-ext-8)。文脈 = 前後20文字の
          // 株/コード/東証 等キーワード、または括弧付き表記。
          const contextStart = Math.max(0, match.index - 20);
          const contextEnd = Math.min(
            text.length,
            match.index + match[0].length + 20,
          );
          const surrounding = text.slice(contextStart, contextEnd);

          const afterChar =
            match.index + match[0].length < text.length
              ? text[match.index + match[0].length]
              : "";
          const isParenthesized =
            (beforeChar === "(" && afterChar === ")") ||
            (beforeChar === "（" && afterChar === "）") ||
            new RegExp(`[（(]\\s*${code}(?:\\.T)?\\s*[)）]`).test(surrounding);

          const hasStockContext =
            isParenthesized ||
            /(?:株|コード|銘柄|東証|証券|プライム|スタンダード|グロース|ホールディングス)/.test(
              surrounding,
            );

          if (!hasStockContext) {
            continue; // skip bare 4-char codes without stock context
          }
        }

        const symbol = `${code}.T`;
        addMatch(foundMap, symbol, "jp", text, match.index, hasTSuffix ? 3 : 2);
      }

      // 3. Check US Ticker symbols
      usRegex.lastIndex = 0;
      while ((match = usRegex.exec(text)) !== null) {
        const sym = match[1];
        const symIndex = match.index + (match[0].length - sym.length);
        if (symIndex > 0 && text[symIndex - 1] === "$") continue;
        const isKnown = KNOWN_US_TICKERS.has(sym);
        if (!isKnown && EXCLUDED_WORDS.has(sym)) continue;
        if (!isKnown && sym.length < 3) continue; // Skip unknown 1-2 letter words

        addMatch(foundMap, sym, "us", text, symIndex, isKnown ? 2 : 1);
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
    const mapKey = `${market}:${symbol}`;
    const existing = map.get(mapKey);
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
      map.set(mapKey, {
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
    // 同一拡張機能コンテキスト以外からの要求は無視する(ディフェンスイン
    // ディープ。バックグラウンド側の sender チェックの鏡像)。
    if (sender && sender.id !== chrome.runtime.id) {
      return false;
    }
    if (request && request.action === "detectTickers") {
      try {
        const data = detectTickers();
        sendResponse(data);
      } catch (err) {
        console.error("Ticker detection error:", err);
        sendResponse({ ok: false, error: String(err) });
      }
      // sendResponse は同期呼び出しのためチャネルは開いたままにしない。
      return false;
    }
    return false;
  });
})();
