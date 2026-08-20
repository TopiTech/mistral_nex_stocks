/**
 * ai-dive-controller.js - Layered AI Deep Dive controller for Market Observatory.
 *
 * Implements 5 structured information tiers:
 * Tier 1: Core Financials & Live Quote
 * Tier 2: Technicals & Historical Trends
 * Tier 3: Market News Headlines
 * Tier 4: Structured AI Analysis (on-demand explicit execution only)
 * Tier 5: Risk Factors, Contrarian Views, and Disclaimers
 */

(function (global) {
  "use strict";

  class AiDiveController {
    constructor(state, elements) {
      this.state = state;
      this.els = elements || {};
      this._abortController = null;
      this._newsAbortController = null;
      this._returnFocusTarget = null;

      this.bindEvents();
      this.bindState();
    }

    bindEvents() {
      if (this.els.closeBtn) {
        this.els.closeBtn.addEventListener("click", () => {
          this.state.closeAiDive();
        });
      }

      if (this.els.overlay) {
        this.els.overlay.addEventListener("click", (e) => {
          if (e.target === this.els.overlay) {
            this.state.closeAiDive();
          }
        });
      }

      if (this.els.startAiBtn) {
        this.els.startAiBtn.addEventListener("click", () => {
          this.runDeepAiAnalysis();
        });
      }
    }

    bindState() {
      this.state.subscribe((key, val, data) => {
        if (key === "aiDiveOpen" || key === "aiDiveSymbol") {
          if (data.aiDiveOpen && data.aiDiveSymbol) {
            this.openModal(data.aiDiveSymbol, data);
          } else {
            this.closeModal();
          }
        }
      });
    }

    destroy() {
      if (this._abortController) {
        this._abortController.abort();
      }
      if (this._newsAbortController) {
        this._newsAbortController.abort();
      }
    }

    openModal(symbol, data) {
      const modal = this.els.overlay;
      if (!modal) return;

      if (!modal.contains(document.activeElement)) {
        this._returnFocusTarget = document.activeElement;
      }

      modal.classList.remove("hidden");
      modal.setAttribute("aria-hidden", "false");
      modal.removeAttribute("inert");
      const closeBtn =
        this.els.closeBtn || modal.querySelector("#ai-dive-close-btn");
      if (closeBtn) closeBtn.focus();

      const stock = data.stocks.get(symbol) || {
        symbol,
        name: symbol,
        price: 0,
        changePercent: 0,
      };

      // Set header
      if (this.els.symbolTitle) {
        this.els.symbolTitle.textContent = stock.symbol;
      }
      if (this.els.nameSubtitle) {
        this.els.nameSubtitle.textContent =
          stock.displayName || stock.name || stock.symbol;
      }

      // Render Tier 1: Financials
      this.renderTier1Financials(stock);

      // Render Tier 2: Technicals
      this.renderTier2Technicals(stock);

      // Render Tier 3: News
      this.fetchAndRenderNews(stock);

      // Reset Tier 4: AI Analysis to idle
      this.resetTier4Ai();
    }

    closeModal() {
      const modal = this.els.overlay;
      if (!modal) return;

      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
      modal.setAttribute("inert", "");

      if (this._abortController) {
        this._abortController.abort();
        this._abortController = null;
      }
      if (this._newsAbortController) {
        this._newsAbortController.abort();
        this._newsAbortController = null;
      }
      const returnFocusTarget = this._returnFocusTarget;
      this._returnFocusTarget = null;
      if (returnFocusTarget && document.contains(returnFocusTarget)) {
        returnFocusTarget.focus();
      }
    }

    renderTier1Financials(stock) {
      const container = this.els.tier1Container;
      if (!container) return;

      container.textContent = "";

      const grid = document.createElement("div");
      grid.className = "metric-stat-grid";

      const chgSign = stock.changePercent >= 0 ? "+" : "";
      const chgClass = stock.changePercent >= 0 ? "text-pos" : "text-neg";
      const priceStr =
        stock.price > 0
          ? global.ObservatoryDataAdapter.formatPrice(stock.price, stock)
          : "--";

      const items = [
        { label: "現在株価", value: priceStr },
        {
          label: "前日比",
          value: `${chgSign}${stock.changePercent.toFixed(2)}%`,
          cls: chgClass,
        },
        {
          label: "時価総額",
          value:
            stock.marketCap > 0
              ? global.ObservatoryDataAdapter.formatMarketCap(
                  stock.marketCap,
                  stock,
                )
              : "--",
        },
        {
          label: "PER (株価収益率)",
          value: stock.peRatio ? `${stock.peRatio.toFixed(1)}x` : "--",
        },
        {
          label: "52週レンジ",
          value:
            stock.high52 > 0 && stock.low52 > 0
              ? `${global.ObservatoryDataAdapter.formatPrice(stock.low52, stock)} - ${global.ObservatoryDataAdapter.formatPrice(stock.high52, stock)}`
              : "--",
        },
        { label: "セクター", value: stock.sector || "--" },
      ];

      for (const item of items) {
        const card = document.createElement("div");
        card.className = "stat-card";
        const lbl = document.createElement("span");
        lbl.className = "stat-label";
        lbl.textContent = item.label;
        const val = document.createElement("span");
        val.className = `stat-value ${item.cls || ""}`;
        val.textContent = item.value;
        card.appendChild(lbl);
        card.appendChild(val);
        grid.appendChild(card);
      }

      container.appendChild(grid);
    }

    renderTier2Technicals(stock) {
      const container = this.els.tier2Container;
      if (!container) return;

      container.textContent = "";

      const techDiv = document.createElement("div");
      techDiv.className = "technical-summary-card";

      const vol = (stock.volatility || 1.0).toFixed(1);
      const mom =
        stock.momentum !== undefined
          ? stock.momentum >= 0
            ? `+${stock.momentum.toFixed(1)}`
            : stock.momentum.toFixed(1)
          : "--";

      const p1 = document.createElement("p");
      p1.textContent = `📊 ボラティリティ係数: ${vol}x | 短期モメンタム: ${mom}`;
      const p2 = document.createElement("p");
      p2.className = "text-secondary";
      p2.textContent =
        stock.changePercent >= 0
          ? "📈 上昇基調: 移動平均線に対して堅調な買い圧力が観測されています。"
          : "📉 調整局面: 短期的な売り圧力が観測されています。";

      techDiv.appendChild(p1);
      techDiv.appendChild(p2);
      container.appendChild(techDiv);
    }

    async fetchAndRenderNews(stock) {
      const container = this.els.tier3Container;
      if (!container) return;

      container.textContent = "";
      const loading = document.createElement("div");
      loading.className = "news-loading";
      loading.textContent = "最新ニュースを取得中...";
      container.appendChild(loading);

      if (this._newsAbortController) {
        this._newsAbortController.abort();
      }
      this._newsAbortController = new AbortController();
      const newsAbortController = this._newsAbortController;

      try {
        let data = null;
        for (let attempt = 0; attempt < 6; attempt += 1) {
          const res = await (global.apiFetch || fetch)("/api/news", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: newsAbortController.signal,
          });
          data =
            res && typeof res.json === "function"
              ? await res.json().catch(() => null)
              : (res?.data ?? res);
          if (!data?.fetching) break;
          await this.waitForNewsRetry(1500, newsAbortController.signal);
        }

        if (
          newsAbortController !== this._newsAbortController ||
          !this.isCurrentAiDiveSymbol(stock.symbol)
        ) {
          return;
        }

        container.textContent = "";
        const newsList = this.getMarketNewsItems(data, stock);

        if (!newsList.length) {
          const empty = document.createElement("div");
          empty.className = "news-empty";
          empty.textContent = "市場ニュースはまだ利用できません。";
          container.appendChild(empty);
          return;
        }

        const ul = document.createElement("ul");
        ul.className = "news-list";

        for (const n of newsList) {
          const li = document.createElement("li");
          li.className = "news-item";

          const titleSpan = document.createElement("span");
          titleSpan.className = "news-title";
          titleSpan.textContent = n.title || "ニュース";

          const metaSpan = document.createElement("span");
          metaSpan.className = "news-meta";
          metaSpan.textContent = ` - ${n.source || "Market"} (${n.time || n.date || "最近"})`;

          const summary = document.createElement("p");
          summary.className = "news-summary";
          summary.textContent = n.summary || "要約は提供されていません。";

          li.appendChild(titleSpan);
          li.appendChild(metaSpan);
          li.appendChild(summary);
          ul.appendChild(li);
        }

        container.appendChild(ul);
      } catch (err) {
        if (err.name !== "AbortError") {
          container.textContent = "";
          const errDiv = document.createElement("div");
          errDiv.className = "news-empty";
          errDiv.textContent = "ニュースの取得に失敗しました。";
          container.appendChild(errDiv);
        }
      }
    }

    isCurrentAiDiveSymbol(symbol) {
      return (
        this.state.state.aiDiveOpen && this.state.state.aiDiveSymbol === symbol
      );
    }

    waitForNewsRetry(delayMs, signal) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, delayMs);
        signal.addEventListener(
          "abort",
          () => {
            clearTimeout(timer);
            reject(new DOMException("News request cancelled", "AbortError"));
          },
          { once: true },
        );
      });
    }

    getMarketNewsItems(data, stock) {
      if (!data || data.fetching) return [];
      const marketKey = stock.market === "jp" ? "jp" : "us";
      const marketLabel = marketKey === "jp" ? "日本市場" : "米国市場";
      const sections = [
        { key: marketKey, title: `${marketLabel}ニュース` },
        { key: "trends", title: "市場トレンド" },
      ];
      return sections
        .map(({ key, title }) => {
          const section = data[key];
          const summary = section?.content;
          if (typeof summary !== "string" || !summary.trim()) return null;
          return {
            title,
            summary,
            source: section.status || "Market",
            time: section.timestamp || "最近",
          };
        })
        .filter(Boolean);
    }

    resetTier4Ai() {
      const container = this.els.tier4Container;
      const startBtn = this.els.startAiBtn;
      if (startBtn) {
        startBtn.disabled = false;
        startBtn.textContent = "✨ AI深層分析を開始";
      }
      if (container) {
        container.textContent = "";
        const placeholder = document.createElement("div");
        placeholder.className = "ai-dive-placeholder";
        placeholder.textContent =
          "上のボタンを押すと、Mistral AI による構造化ファンダメンタル・テクニカル深層分析を実行します。";
        container.appendChild(placeholder);
      }
    }

    async runDeepAiAnalysis() {
      const symbol = this.state.state.aiDiveSymbol;
      const stock = this.state.state.stocks.get(symbol);
      if (!symbol || !stock) return;

      const container = this.els.tier4Container;
      const startBtn = this.els.startAiBtn;

      if (startBtn) {
        startBtn.disabled = true;
        startBtn.textContent = "⏳ AI分析実行中...";
      }

      if (container) {
        container.textContent = "";
        const loadingDiv = document.createElement("div");
        loadingDiv.className = "ai-loading-box";
        const spinner = document.createElement("span");
        spinner.className = "loading-spinner";
        loadingDiv.appendChild(spinner);
        loadingDiv.appendChild(
          document.createTextNode(" Mistral AI が銘柄構造化データを分析中..."),
        );
        container.appendChild(loadingDiv);
      }

      if (this._abortController) {
        this._abortController.abort();
      }
      this._abortController = new AbortController();

      const payload = {
        symbol: stock.symbol,
        name: stock.displayName || stock.name || stock.symbol,
        price: stock.price,
        chart_data: stock.chartData || [],
        sector: stock.sector,
        industry: stock.industry,
        market_cap: stock.marketCap,
        pe_ratio: stock.peRatio,
        market: stock.market || "us",
        request_token:
          typeof global.createRequestToken === "function"
            ? global.createRequestToken()
            : String(Date.now()),
      };

      try {
        let data = {};
        let resOk = false;
        const maxAttempts = 6;

        for (let attempt = 0; attempt <= maxAttempts; attempt++) {
          const res = await (global.apiFetch || fetch)("/api/analyze-v2", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            signal: this._abortController.signal,
          });

          data =
            (res && typeof res.json === "function"
              ? await res.json().catch(() => null)
              : (res?.data ?? res)) || {};
          const resObj = res?.response || res;
          if (resObj && !resObj.ok) {
            throw new Error(data.error || `HTTP ${resObj.status}`);
          }
          if (!data.fetching) {
            resOk = true;
            break;
          }
          await new Promise((r) => setTimeout(r, 2000));
        }

        if (!resOk) {
          throw new Error("AI分析がタイムアウトしました");
        }

        this.renderAiAnalysisResult(data);
      } catch (err) {
        if (err.name !== "AbortError") {
          if (container) {
            container.textContent = "";
            const errDiv = document.createElement("div");
            errDiv.className = "ai-error-banner";
            errDiv.textContent = `❌ AI分析エラー: ${err.message || "分析に失敗しました"}`;
            container.appendChild(errDiv);
          }
        }
      } finally {
        if (startBtn) {
          startBtn.disabled = false;
          startBtn.textContent = "🔄 AI分析を再実行";
        }
      }
    }

    renderAiAnalysisResult(data) {
      const container = this.els.tier4Container;
      if (!container) return;

      container.textContent = "";

      const resultBox = document.createElement("div");
      resultBox.className = "ai-analysis-result-box";

      // 1. Recommendation Header
      const recHeader = document.createElement("div");
      recHeader.className = "ai-rec-header";

      const recBadge = document.createElement("span");
      const rec = String(data.recommendation || "HOLD").toUpperCase();
      recBadge.className = `ai-rec-badge rec-${rec.toLowerCase().replace(/\s+/g, "-")}`;
      recBadge.textContent = rec;
      recHeader.appendChild(recBadge);

      if (data.sentiment) {
        const sentSpan = document.createElement("span");
        sentSpan.className = "ai-sent-tag";
        sentSpan.textContent = `センチメント: ${data.sentiment}`;
        recHeader.appendChild(sentSpan);
      }

      const stock = this.state?.state?.stocks?.get(
        this.state?.state?.aiDiveSymbol,
      );
      const targetPrice = data.target_price_3m ?? data.target_price;
      if (Number.isFinite(Number(targetPrice)) && Number(targetPrice) > 0) {
        const tgtSpan = document.createElement("span");
        tgtSpan.className = "ai-target-tag";
        tgtSpan.textContent = `目標株価: ${global.ObservatoryDataAdapter.formatPrice(targetPrice, stock)}`;
        recHeader.appendChild(tgtSpan);
      }

      resultBox.appendChild(recHeader);

      // 2. Summary / Catalyst
      const summary = data.analysis_summary || data.summary || data.catalyst;
      if (summary) {
        const sumDiv = document.createElement("div");
        sumDiv.className = "ai-summary-text";
        const strong = document.createElement("strong");
        strong.textContent = "【主要カタリスト & 要約】: ";
        sumDiv.appendChild(strong);
        sumDiv.appendChild(document.createTextNode(summary));
        resultBox.appendChild(sumDiv);
      }

      // 3. Key Points / Factors
      const keyFactors = Array.isArray(data.key_catalysts)
        ? data.key_catalysts
        : Array.isArray(data.key_factors)
          ? data.key_factors
          : [];
      if (keyFactors.length) {
        const factorsUl = document.createElement("ul");
        factorsUl.className = "ai-factors-list";
        for (const factor of keyFactors) {
          const li = document.createElement("li");
          li.textContent = factor;
          factorsUl.appendChild(li);
        }
        resultBox.appendChild(factorsUl);
      }

      // 4. Risks & Uncertainties (Tier 5)
      const riskFactors = Array.isArray(data.risk_factors)
        ? data.risk_factors
        : data.risk_factors
          ? [data.risk_factors]
          : [];
      const riskText =
        riskFactors.filter(Boolean).join(" / ") || data.uncertainties || "";
      if (riskText) {
        const riskBox = document.createElement("div");
        riskBox.className = "ai-risk-box";
        const riskTitle = document.createElement("div");
        riskTitle.className = "risk-title";
        riskTitle.textContent = "⚠️ リスク & 不確実性要因";
        const riskBody = document.createElement("p");
        riskBody.textContent = riskText;
        riskBox.appendChild(riskTitle);
        riskBox.appendChild(riskBody);
        resultBox.appendChild(riskBox);
      }

      container.appendChild(resultBox);
    }
  }

  global.AiDiveController = AiDiveController;
})(typeof window !== "undefined" ? window : this);
