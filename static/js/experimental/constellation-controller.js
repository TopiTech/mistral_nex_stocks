/**
 * constellation-controller.js - Multi-stock comparison and AI comparative synthesis for Market Observatory.
 *
 * Links 2-3 stocks, displays a comparative metrics HUD, and provides
 * an on-demand AI comparative analysis via /api/chat.
 */

(function (global) {
  "use strict";

  class ConstellationController {
    constructor(state, elements) {
      this.state = state;
      this.els = elements || {};
      this._abortController = null;

      this.bindEvents();
      this.bindState();
    }

    bindEvents() {
      if (this.els.toggleBtn) {
        this.els.toggleBtn.addEventListener("click", () => {
          this.state.toggleConnectMode();
        });
      }

      if (this.els.closeBtn) {
        this.els.closeBtn.addEventListener("click", () => {
          this.state.clearConnectedSymbols();
          this.state.toggleConnectMode(false);
        });
      }

      if (this.els.aiCompareBtn) {
        this.els.aiCompareBtn.addEventListener("click", () => {
          this.runAiComparison();
        });
      }
    }

    bindState() {
      this.state.subscribe((key, val, data) => {
        if (key === "isConnectMode" || key === "connectedSymbols") {
          this.renderHUD(data);
        }
      });
    }

    destroy() {
      if (this._abortController) {
        this._abortController.abort();
        this._abortController = null;
      }
    }

    renderHUD(data) {
      const isConnect = data.isConnectMode;
      const symbols = data.connectedSymbols || [];
      const drawer = this.els.drawer;
      const toggleBtn = this.els.toggleBtn;

      if (toggleBtn) {
        toggleBtn.classList.toggle("active", isConnect);
        toggleBtn.setAttribute("aria-pressed", String(isConnect));
      }

      if (!drawer) return;

      if (!isConnect && symbols.length === 0) {
        if (drawer.contains(document.activeElement)) {
          document.activeElement.blur();
        }
        drawer.classList.add("hidden");
        drawer.setAttribute("aria-hidden", "true");
        drawer.setAttribute("inert", "");
        return;
      }

      drawer.classList.remove("hidden");
      drawer.setAttribute("aria-hidden", "false");
      drawer.removeAttribute("inert");

      const countEl = this.els.countText;
      if (countEl) {
        countEl.textContent = `${symbols.length} / 3 銘柄接続中`;
      }

      const listContainer = this.els.listContainer;
      if (!listContainer) return;

      listContainer.textContent = "";

      if (symbols.length === 0) {
        const hint = document.createElement("div");
        hint.className = "constellation-hint";
        hint.textContent =
          "軌道上の銘柄ノードをクリックして、最大3つの比較ノードを接続してください。";
        listContainer.appendChild(hint);
        if (this.els.aiCompareBtn) this.els.aiCompareBtn.disabled = true;
        return;
      }

      if (this.els.aiCompareBtn) {
        this.els.aiCompareBtn.disabled = symbols.length < 2;
      }

      const table = document.createElement("table");
      table.className = "constellation-table";

      // Header row
      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      const thMetric = document.createElement("th");
      thMetric.textContent = "指標";
      headRow.appendChild(thMetric);

      const stockObjs = symbols.map(
        (sym) =>
          data.stocks.get(sym) || { symbol: sym, price: 0, changePercent: 0 },
      );

      for (const st of stockObjs) {
        const th = document.createElement("th");
        th.textContent = st.symbol;
        headRow.appendChild(th);
      }
      thead.appendChild(headRow);
      table.appendChild(thead);

      // Body rows
      const tbody = document.createElement("tbody");

      const rows = [
        { label: "名称", get: (s) => s.displayName || s.name || s.symbol },
        {
          label: "株価",
          get: (s) =>
            s.price > 0
              ? global.formatPrice
                ? global.formatPrice(s.price, s.market)
                : `$${s.price.toFixed(2)}`
              : "--",
        },
        {
          label: "騰落率",
          get: (s) => {
            const sign = s.changePercent >= 0 ? "+" : "";
            return `${sign}${s.changePercent.toFixed(2)}%`;
          },
          isChange: true,
        },
        {
          label: "時価総額",
          get: (s) =>
            s.marketCap > 0
              ? global.formatMarketCap
                ? global.formatMarketCap(s.marketCap)
                : `${(s.marketCap / 1e9).toFixed(1)}B`
              : "--",
        },
        { label: "セクター", get: (s) => s.sector || "--" },
        {
          label: "PER",
          get: (s) => (s.peRatio ? `${s.peRatio.toFixed(1)}x` : "--"),
        },
        {
          label: "ボラティリティ",
          get: (s) => `${(s.volatility || 1.0).toFixed(1)}x`,
        },
      ];

      for (const r of rows) {
        const tr = document.createElement("tr");
        const tdLabel = document.createElement("td");
        tdLabel.className = "metric-label";
        tdLabel.textContent = r.label;
        tr.appendChild(tdLabel);

        for (const st of stockObjs) {
          const tdVal = document.createElement("td");
          tdVal.textContent = r.get(st);
          if (r.isChange) {
            tdVal.className = st.changePercent >= 0 ? "text-pos" : "text-neg";
          }
          tr.appendChild(tdVal);
        }
        tbody.appendChild(tr);
      }

      table.appendChild(tbody);
      listContainer.appendChild(table);

      // Render relative performance mini chart if 2+ stocks connected
      this.renderRelativeReturnChart(symbols, data);
    }

    renderRelativeReturnChart(symbols, data) {
      const wrapper =
        this.els.chartWrapper ||
        document.getElementById("constellation-chart-wrapper");
      const canvas =
        this.els.chartCanvas ||
        document.getElementById("constellation-chart-canvas");
      if (!wrapper || !canvas) return;

      if (!symbols || symbols.length < 2) {
        wrapper.classList.add("hidden");
        return;
      }

      const stockObjs = symbols.map((s) => data.stocks.get(s)).filter(Boolean);
      const chartColors = ["#6366f1", "#38bdf8", "#f59e0b"];

      const ctx = canvas.getContext("2d");
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      // Baseline center line (0%)
      const midY = h / 2;
      ctx.strokeStyle = "rgba(255, 255, 255, 0.15)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(0, midY);
      ctx.lineTo(w, midY);
      ctx.stroke();
      ctx.setLineDash([]);

      let hasValidData = false;

      stockObjs.forEach((st, idx) => {
        const rawChart = st.chartData || [];
        if (!rawChart || rawChart.length < 2) return;

        const baseVal = Number(
          rawChart[0].close || rawChart[0].price || rawChart[0] || 0,
        );
        if (baseVal <= 0) return;

        hasValidData = true;
        const color = chartColors[idx % chartColors.length];
        const stepX = w / Math.max(1, rawChart.length - 1);

        ctx.strokeStyle = color;
        ctx.lineWidth = 2.0;
        ctx.beginPath();

        rawChart.forEach((pt, pIdx) => {
          const val = Number(pt.close || pt.price || pt || baseVal);
          const ret = ((val - baseVal) / baseVal) * 100; // Return %
          // Scale -20% to +20% to canvas height
          const clampedRet = Math.max(-25, Math.min(25, ret));
          const y = midY - (clampedRet / 25) * (h * 0.42);
          const x = pIdx * stepX;

          if (pIdx === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });

        ctx.stroke();

        // Legend label on top-right
        ctx.fillStyle = color;
        ctx.font = "bold 10px 'Orbitron', sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(st.symbol, 8 + idx * 55, 14);
      });

      if (hasValidData) {
        wrapper.classList.remove("hidden");
      } else {
        wrapper.classList.add("hidden");
      }
    }

    async runAiComparison() {
      const symbols = this.state.state.connectedSymbols;
      if (!symbols || symbols.length < 2) {
        if (typeof global.showToast === "function") {
          global.showToast(
            "⚠️ 比較には2つ以上の銘柄を接続してください",
            "#f59e0b",
          );
        }
        return;
      }

      const aiResultContainer = this.els.aiResultContainer;
      const aiCompareBtn = this.els.aiCompareBtn;
      if (!aiResultContainer) return;

      aiResultContainer.classList.remove("hidden");
      aiResultContainer.textContent = "";

      const loadingDiv = document.createElement("div");
      loadingDiv.className = "ai-loading-state";
      const spinner = document.createElement("span");
      spinner.className = "loading-spinner";
      loadingDiv.appendChild(spinner);
      loadingDiv.appendChild(
        document.createTextNode(" Mistral AI が星座比較分析を生成中..."),
      );
      aiResultContainer.appendChild(loadingDiv);

      if (aiCompareBtn) {
        aiCompareBtn.disabled = true;
      }

      if (this._abortController) {
        this._abortController.abort();
      }
      this._abortController = new AbortController();

      try {
        const stockObjs = symbols
          .map((s) => this.state.state.stocks.get(s))
          .filter(Boolean);
        const dataSummary = stockObjs
          .map(
            (s) =>
              `- ${s.symbol} (${s.name || ""}): 価格=${s.price}, 騰落率=${s.changePercent.toFixed(2)}%, セクター=${s.sector}, PER=${s.peRatio || "N/A"}, 時価総額=${s.marketCap}`,
          )
          .join("\n");

        const prompt = `以下の銘柄群の市場データに基づいて、相対的な強み・弱み・セクター内でのポジショニングを簡潔に比較分析してください。\n\n【対象銘柄データ】\n${dataSummary}\n\n投資助言ではなく、客観的な財務指標と市場パフォーマンスの違いに焦点を当てて200字程度でまとめてください。`;

        const primaryStock = stockObjs[0] || {
          symbol: symbols[0],
          market: "us",
        };
        const payload = {
          symbol: primaryStock.symbol,
          market: primaryStock.market || "us",
          message: prompt,
          request_token:
            typeof global.createRequestToken === "function"
              ? global.createRequestToken()
              : String(Date.now()),
        };

        const abortController = this._abortController;
        let data = null;
        for (let attempt = 0; attempt < 6; attempt += 1) {
          const res = await (global.apiFetch || fetch)("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            signal: abortController.signal,
          });

          data =
            res && typeof res.json === "function"
              ? await res.json().catch(() => null)
              : (res?.data ?? res);
          if (!data?.fetching) break;
          await this.waitForAiPoll(1500, abortController.signal);
        }
        if (abortController !== this._abortController) return;
        if (data?.fetching) {
          throw new Error(
            "AI分析の準備に時間がかかっています。しばらくして再試行してください。",
          );
        }
        aiResultContainer.textContent = "";

        if (data && (data.reply || data.message)) {
          const replyText = data.reply || data.message;
          const resultCard = document.createElement("div");
          resultCard.className = "ai-comparison-card";

          const title = document.createElement("h4");
          title.textContent = `✨ AI 星座比較分析 (${symbols.join(" vs ")})`;
          resultCard.appendChild(title);

          const body = document.createElement("p");
          body.textContent = replyText;
          resultCard.appendChild(body);

          const disclaimer = document.createElement("div");
          disclaimer.className = "ai-disclaimer-note";
          disclaimer.textContent =
            "※ AIによる比較分析は客観的データに基づく情報提供のみを目的としており、特定の投資行動を推奨するものではありません。";
          resultCard.appendChild(disclaimer);

          aiResultContainer.appendChild(resultCard);
        } else {
          throw new Error(data?.error || "AI応答を取得できませんでした");
        }
      } catch (err) {
        if (err.name !== "AbortError") {
          aiResultContainer.textContent = "";
          const errDiv = document.createElement("div");
          errDiv.className = "ai-error-banner";
          errDiv.textContent = `❌ 比較分析エラー: ${err.message || "通信に失敗しました"}`;
          aiResultContainer.appendChild(errDiv);
        }
      } finally {
        if (aiCompareBtn) {
          aiCompareBtn.disabled = false;
        }
      }
    }

    waitForAiPoll(delayMs, signal) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, delayMs);
        signal.addEventListener(
          "abort",
          () => {
            clearTimeout(timer);
            reject(new DOMException("AI comparison cancelled", "AbortError"));
          },
          { once: true },
        );
      });
    }
  }

  global.ConstellationController = ConstellationController;
})(typeof window !== "undefined" ? window : this);
