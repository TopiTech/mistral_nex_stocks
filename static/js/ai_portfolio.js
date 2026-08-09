/* global showToast, fetchInitialStocks, loadPortfolioSnapshot, state, Chart */

(function () {
  "use strict";

  let currentAiPortfolio = null;
  let activeAiPreset = "tech";
  let aiSummaryChartInstance = null;
  let aiSectorChartInstance = null;

  // Capital Baseline for virtual calculation
  const VIRTUAL_BASE_CAPITAL_JPY = 10000000;

  document.addEventListener("DOMContentLoaded", () => {
    initAiPortfolio();
  });

  function initAiPortfolio() {
    setupModeSwitcher();
    setupPresetBar();
    setupCustomPanel();
    setupActionButtons();
  }

  function setupModeSwitcher() {
    const myTab = document.getElementById("pf-mode-my");
    const aiTab = document.getElementById("pf-mode-ai");
    const myView = document.getElementById("my-portfolio-view");
    const aiView = document.getElementById("ai-portfolio-view");

    if (!myTab || !aiTab || !myView || !aiView) return;

    myTab.addEventListener("click", () => {
      myTab.classList.add("active");
      myTab.setAttribute("aria-selected", "true");
      aiTab.classList.remove("active");
      aiTab.setAttribute("aria-selected", "false");

      myView.classList.remove("hidden");
      aiView.classList.add("hidden");
    });

    aiTab.addEventListener("click", () => {
      aiTab.classList.add("active");
      aiTab.setAttribute("aria-selected", "true");
      myTab.classList.remove("active");
      myTab.setAttribute("aria-selected", "false");

      aiView.classList.remove("hidden");
      myView.classList.add("hidden");

      if (!currentAiPortfolio) {
        loadAiPortfolio(activeAiPreset);
      } else {
        renderAiPortfolio(currentAiPortfolio);
      }
    });
  }

  function setupPresetBar() {
    const presetPills = document.querySelectorAll(".ai-preset-pill");
    const customPanel = document.getElementById("ai-pf-custom-panel");

    presetPills.forEach((pill) => {
      pill.addEventListener("click", () => {
        presetPills.forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");

        const presetKey = pill.dataset.preset;
        activeAiPreset = presetKey;

        if (presetKey === "custom") {
          if (customPanel) customPanel.classList.remove("hidden");
        } else {
          if (customPanel) customPanel.classList.add("hidden");
          loadAiPortfolio(presetKey);
        }
      });
    });
  }

  function setupCustomPanel() {
    const genBtn = document.getElementById("ai-theme-generate-btn");
    const themeInput = document.getElementById("ai-theme-input");
    const chips = document.querySelectorAll(".ai-theme-chip");

    if (genBtn && themeInput) {
      genBtn.addEventListener("click", () => {
        const text = themeInput.value.trim();
        if (!text) {
          if (typeof showToast === "function") {
            showToast("⚠️ テーマを入力してください", "#ff9800");
          }
          return;
        }
        loadAiPortfolio(text);
      });

      themeInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          genBtn.click();
        }
      });
    }

    chips.forEach((chip) => {
      chip.addEventListener("click", () => {
        if (themeInput) {
          themeInput.value = chip.dataset.theme || chip.textContent;
          if (genBtn) genBtn.click();
        }
      });
    });
  }

  function setupActionButtons() {
    const rebalanceBtn = document.getElementById("ai-pf-rebalance-btn");
    const saveBtn = document.getElementById("ai-pf-save-btn");
    const copyBtn = document.getElementById("ai-pf-copy-btn");

    if (rebalanceBtn) {
      rebalanceBtn.addEventListener("click", () => {
        if (!currentAiPortfolio) return;
        const theme = currentAiPortfolio.theme || activeAiPreset;
        rebalanceAiPortfolio(theme);
      });
    }

    if (saveBtn) {
      saveBtn.addEventListener("click", () => {
        if (!currentAiPortfolio) return;
        saveAiPortfolio(currentAiPortfolio);
      });
    }

    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        if (!currentAiPortfolio || !currentAiPortfolio.items) return;
        copyAiPortfolioToMy(currentAiPortfolio.items);
      });
    }
  }

  async function loadAiPortfolio(presetOrTheme) {
    showLoadingState(true);
    try {
      const resp = await fetch("/api/ai-portfolio/generate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
        body: JSON.stringify({ theme: presetOrTheme }),
      });

      if (!resp.ok) throw new Error(`API error: ${resp.status}`);
      const data = await resp.json();

      if (data.ok && data.portfolio) {
        currentAiPortfolio = data.portfolio;
        renderAiPortfolio(currentAiPortfolio);
      } else {
        throw new Error(data.error || "Failed to generate AI portfolio");
      }
    } catch (err) {
      console.error("loadAiPortfolio error:", err);
      if (typeof showToast === "function") {
        showToast("⚠️ AIポートフォリオの取得に失敗しました", "#ff4d4d");
      }
    } finally {
      showLoadingState(false);
    }
  }

  async function rebalanceAiPortfolio(theme) {
    showLoadingState(true);
    try {
      const resp = await fetch("/api/ai-portfolio/rebalance", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
        body: JSON.stringify({ theme: theme }),
      });

      const data = await resp.json();
      if (data.ok && data.portfolio) {
        currentAiPortfolio = data.portfolio;
        renderAiPortfolio(currentAiPortfolio);
        if (typeof showToast === "function") {
          showToast("🤖 AIリバランスが完了しました！", "#7dffb0");
        }
      }
    } catch (err) {
      console.error("rebalanceAiPortfolio error:", err);
    } finally {
      showLoadingState(false);
    }
  }

  async function saveAiPortfolio(portfolio) {
    try {
      const resp = await fetch("/api/ai-portfolio/save", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
        body: JSON.stringify({ portfolio: portfolio }),
      });

      const data = await resp.json();
      if (data.ok) {
        if (typeof showToast === "function") {
          showToast("💾 カスタムテーマを保存しました", "#7dffb0");
        }
      }
    } catch (err) {
      console.error("saveAiPortfolio error:", err);
    }
  }

  async function copyAiPortfolioToMy(items) {
    try {
      const resp = await fetch("/api/ai-portfolio/copy-to-my", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
        body: JSON.stringify({ items: items }),
      });

      const data = await resp.json();
      if (data.ok) {
        if (typeof showToast === "function") {
          showToast(
            `📥 ${data.added_count || items.length} 銘柄をマイポートフォリオに反映しました`,
            "#7dffb0",
          );
        }
        // GET /api/stocks intentionally omits portfolio fields. Refresh the
        // regular market list first, then restore holdings from the protected
        // snapshot endpoint so the active portfolio tab updates immediately.
        if (typeof fetchInitialStocks === "function") {
          await fetchInitialStocks();
        }
        if (typeof loadPortfolioSnapshot === "function") {
          await loadPortfolioSnapshot();
        }
      }
    } catch (err) {
      console.error("copyAiPortfolioToMy error:", err);
    }
  }

  function renderAiPortfolio(portfolio) {
    if (!portfolio) return;

    const titleEl = document.getElementById("ai-pf-title");
    const descEl = document.getElementById("ai-pf-desc");
    const riskEl = document.getElementById("ai-pf-risk");
    const returnEl = document.getElementById("ai-pf-return");
    const commentaryEl = document.getElementById("ai-pf-commentary-text");
    const countEl = document.getElementById("ai-pf-count");

    if (titleEl) titleEl.textContent = portfolio.title || "🤖 AIポートフォリオ";
    if (descEl) descEl.textContent = portfolio.description || "";
    if (riskEl) riskEl.textContent = portfolio.risk_level || "中リスク";
    if (returnEl)
      returnEl.textContent = `期待年間リターン: ${portfolio.expected_return || "8-12%"}`;
    if (commentaryEl)
      commentaryEl.textContent =
        portfolio.commentary || "AIによる最適化戦略を適用中。";
    if (countEl)
      countEl.textContent = `${portfolio.items ? portfolio.items.length : 0} 銘柄`;

    renderAiKpiGrid(portfolio);
    drawAiPortfolioCharts(portfolio);
    renderAiStockCards(portfolio.items || []);
  }

  function renderAiKpiGrid(portfolio) {
    const totalValueEl = document.getElementById("ai-pf-total-value");
    const totalPlEl = document.getElementById("ai-pf-total-pl");
    const todayPlEl = document.getElementById("ai-pf-today-pl");

    if (totalValueEl) totalValueEl.textContent = "¥10,000,000";

    // Simulate P&L based on market items
    let simulatedPlJpy = 0;
    let simulatedTodayPlJpy = 0;

    const items = portfolio.items || [];
    items.forEach((item) => {
      const stock = findStockInState(item.symbol, item.market);
      const allocJpy =
        VIRTUAL_BASE_CAPITAL_JPY * ((item.weight_pct || 20) / 100);
      if (stock && stock.change_pct != null) {
        simulatedTodayPlJpy += allocJpy * (stock.change_pct / 100);
      }
      simulatedPlJpy += allocJpy * 0.042; // Base baseline performance +4.2%
    });

    if (totalPlEl) {
      const plPct = (simulatedPlJpy / VIRTUAL_BASE_CAPITAL_JPY) * 100;
      totalPlEl.textContent = `+¥${Math.round(simulatedPlJpy).toLocaleString()} (+${plPct.toFixed(1)}%)`;
      totalPlEl.style.color = simulatedPlJpy >= 0 ? "#4caf50" : "#ff4d4d";
    }

    if (todayPlEl) {
      const sign = simulatedTodayPlJpy >= 0 ? "+" : "";
      todayPlEl.textContent = `${sign}¥${Math.round(simulatedTodayPlJpy).toLocaleString()}`;
      todayPlEl.style.color = simulatedTodayPlJpy >= 0 ? "#4caf50" : "#ff4d4d";
    }
  }

  function renderAiStockCards(items) {
    const container = document.getElementById("ai-portfolio-stocks");
    if (!container) return;

    // Build cards with DOM APIs + textContent so AI-generated or user-saved
    // text (rationale, risk_level, symbol) is never parsed as HTML.
    container.textContent = "";

    items.forEach((item) => {
      const stock = findStockInState(item.symbol, item.market) || {
        symbol: item.symbol,
        name: item.symbol,
        current_price: item.target_price || 100,
        change_pct: 0.5,
      };

      const card = document.createElement("div");
      card.className = "ai-stock-card stock-card";

      // US quotes are priced in USD, JP quotes in JPY; label them with the
      // matching currency symbol instead of mislabeling USD as yen.
      const isJp = item.market === "jp";
      const marketFlag = isJp ? "🇯🇵" : "🇺🇸";
      const currencySymbol = isJp ? "¥" : "$";
      const weightPct = item.weight_pct != null ? Number(item.weight_pct) : 20;
      const allocJpy = VIRTUAL_BASE_CAPITAL_JPY * (weightPct / 100);

      const rawPrice =
        stock.current_price != null ? Number(stock.current_price) : null;
      const priceText =
        rawPrice != null && Number.isFinite(rawPrice)
          ? `${currencySymbol}${rawPrice.toLocaleString()}`
          : "--";
      const changePct =
        stock.change_pct != null ? Number(stock.change_pct) : null;
      const changeText =
        changePct != null && Number.isFinite(changePct)
          ? `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`
          : "--%";
      const changeColor =
        changePct != null && changePct >= 0 ? "#4caf50" : "#ff4d4d";
      const targetPrice =
        item.target_price != null ? Number(item.target_price) : null;
      const targetPriceText =
        targetPrice != null && Number.isFinite(targetPrice) && targetPrice > 0
          ? `${currencySymbol}${targetPrice.toLocaleString()}`
          : "--";
      const riskLevel = ["low", "mid", "high"].includes(item.risk_level)
        ? item.risk_level
        : "mid";
      const rationale =
        item.rationale || "業界成長性と財務基盤を評価して組み入れ。";

      // --- header ---
      const header = document.createElement("div");
      header.className = "ai-stock-header";

      const symbolBox = document.createElement("div");
      symbolBox.className = "ai-stock-symbol";
      const flagSpan = document.createElement("span");
      flagSpan.textContent = marketFlag + " ";
      const strong = document.createElement("strong");
      strong.textContent = item.symbol;
      flagSpan.appendChild(strong);
      symbolBox.appendChild(flagSpan);
      const weightTag = document.createElement("span");
      weightTag.className = "ai-weight-tag";
      weightTag.textContent = `${weightPct}% 構成`;
      symbolBox.appendChild(weightTag);

      const priceBox = document.createElement("div");
      priceBox.className = "ai-stock-price";
      priceBox.style.color = changeColor;
      priceBox.textContent = priceText;
      const changeSpan = document.createElement("span");
      changeSpan.className = "ai-change";
      changeSpan.textContent = `(${changeText})`;
      priceBox.appendChild(changeSpan);

      header.appendChild(symbolBox);
      header.appendChild(priceBox);

      // --- body ---
      const body = document.createElement("div");
      body.className = "ai-stock-body";

      const metrics = document.createElement("div");
      metrics.className = "ai-stock-metrics";
      const metricRows = [
        ["仮想割当額", `¥${Math.round(allocJpy).toLocaleString()}`],
        ["AI目標株価", targetPriceText],
        ["リスク評価", riskLevel],
      ];
      metricRows.forEach(([label, value]) => {
        const row = document.createElement("div");
        const labelSpan = document.createElement("span");
        labelSpan.className = "label";
        labelSpan.textContent = `${label}:`;
        row.appendChild(labelSpan);
        const valueStrong = document.createElement("strong");
        valueStrong.textContent = value;
        if (label === "リスク評価") {
          valueStrong.className = `risk-pill ${riskLevel}`;
        }
        row.appendChild(valueStrong);
        metrics.appendChild(row);
      });

      const rationaleBox = document.createElement("div");
      rationaleBox.className = "ai-rationale-box";
      const rationaleTitle = document.createElement("span");
      rationaleTitle.className = "ai-rationale-title";
      rationaleTitle.textContent = "🤖 AI選定理由:";
      const rationaleText = document.createElement("p");
      rationaleText.className = "ai-rationale-text";
      rationaleText.textContent = rationale;
      rationaleBox.appendChild(rationaleTitle);
      rationaleBox.appendChild(rationaleText);

      body.appendChild(metrics);
      body.appendChild(rationaleBox);

      // --- footer ---
      const footer = document.createElement("div");
      footer.className = "ai-stock-footer";

      const favBtn = document.createElement("button");
      favBtn.type = "button";
      favBtn.className = "ai-card-btn fav-btn";
      favBtn.textContent = "★ お気に入り";
      favBtn.setAttribute("data-symbol", item.symbol);

      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "ai-card-btn add-btn";
      addBtn.textContent = "💼 マイポートフォリオへ追加";
      addBtn.setAttribute("data-symbol", item.symbol);
      addBtn.setAttribute("data-market", item.market);
      addBtn.setAttribute(
        "data-target",
        targetPrice != null && targetPrice > 0 ? String(targetPrice) : "100",
      );
      addBtn.setAttribute("data-weight", String(weightPct));
      addBtn.addEventListener("click", () => {
        copyAiPortfolioToMy([item]);
      });

      footer.appendChild(favBtn);
      footer.appendChild(addBtn);

      card.appendChild(header);
      card.appendChild(body);
      card.appendChild(footer);
      container.appendChild(card);
    });
  }

  function drawAiPortfolioCharts(portfolio) {
    if (typeof Chart === "undefined") return;

    // 1. Sector/Stock Distribution Doughnut Chart
    const sectorCanvas = document.getElementById("ai-pf-sector-canvas");
    if (sectorCanvas) {
      if (aiSectorChartInstance) aiSectorChartInstance.destroy();

      const items = portfolio.items || [];
      const labels = items.map((it) => it.symbol);
      const dataValues = items.map((it) => it.weight_pct);
      const colors = [
        "#8b5cf6",
        "#06b6d4",
        "#3b82f6",
        "#10b981",
        "#f59e0b",
        "#ec4899",
        "#6366f1",
      ];

      aiSectorChartInstance = new Chart(sectorCanvas, {
        type: "doughnut",
        data: {
          labels: labels,
          datasets: [
            {
              data: dataValues,
              backgroundColor: colors.slice(0, items.length),
              borderWidth: 2,
              borderColor: "#1e293b",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: "right",
              labels: { color: "#cbd5e1", font: { size: 11 } },
            },
          },
        },
      });
    }

    // 2. Performance Simulation Line Chart
    const summaryCanvas = document.getElementById("ai-pf-summary-canvas");
    if (summaryCanvas) {
      if (aiSummaryChartInstance) aiSummaryChartInstance.destroy();

      const labels = [
        "1ヶ月前",
        "3週間前",
        "2週間前",
        "1週間前",
        "現在 (シミュレーション)",
      ];
      const simData = [10000000, 10120000, 10250000, 10180000, 10420000];

      aiSummaryChartInstance = new Chart(summaryCanvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "AI仮想運用評価額 (円)",
              data: simData,
              borderColor: "#a855f7",
              backgroundColor: "rgba(168, 85, 247, 0.15)",
              fill: true,
              tension: 0.3,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
          },
          scales: {
            x: { ticks: { color: "#94a3b8" }, grid: { display: false } },
            y: {
              ticks: { color: "#94a3b8" },
              grid: { color: "rgba(255,255,255,0.05)" },
            },
          },
        },
      });
    }
  }

  function findStockInState(symbol, market) {
    if (typeof state === "undefined" || !state.stocks) return null;
    const list = state.stocks[market] || [];
    return list.find((s) => s.symbol === symbol);
  }

  function showLoadingState(loading) {
    const container = document.getElementById("ai-portfolio-stocks");
    if (!container) return;
    if (loading) {
      container.innerHTML = `
        <div class="ai-loading-box">
          <div class="ai-spinner"></div>
          <p>🤖 AIが市場データを分析して最適ポートフォリオを構成中...</p>
        </div>
      `;
    }
  }

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }
})();
