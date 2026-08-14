/* global showToast, fetchInitialStocks, loadPortfolioSnapshot, state, Chart, csrfFetch */

(function () {
  "use strict";

  let currentAiPortfolio = null;
  let activeAiPreset = "tech";
  let aiSummaryChartInstance = null;
  let aiSectorChartInstance = null;
  let aiPortfolioRequestGeneration = 0;
  let aiPortfolioAbortController = null;
  let savedAiPortfolios = [];
  let savedAiPortfoliosLoaded = false;
  let savedAiPortfoliosLoading = false;
  let savedAiPortfoliosError = "";
  let savedAiPortfoliosRequestGeneration = 0;
  let savedAiPortfoliosAbortController = null;
  const deletingSavedAiPortfolioIds = new Set();
  const AI_PORTFOLIO_PRESET_IDS = new Set(["tech", "dividend", "balanced"]);

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
    loadSavedAiPortfolios();
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

      if (!savedAiPortfoliosLoaded && !savedAiPortfoliosLoading) {
        loadSavedAiPortfolios();
      }
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

  function isCustomSavedAiPortfolio(portfolio) {
    const id = String(portfolio?.id || "").trim();
    return Boolean(id) && !AI_PORTFOLIO_PRESET_IDS.has(id);
  }

  function isCurrentSavedAiPortfolioRequest(
    requestGeneration,
    abortController,
  ) {
    return (
      requestGeneration === savedAiPortfoliosRequestGeneration &&
      abortController === savedAiPortfoliosAbortController &&
      !abortController.signal.aborted
    );
  }

  function setSavedAiPortfolioStatus(message) {
    const status = document.getElementById("ai-saved-portfolios-status");
    if (status) status.textContent = message;
  }

  function renderSavedAiPortfolios() {
    const container = document.getElementById("ai-saved-portfolios");
    if (!container) return;

    container.replaceChildren();
    container.setAttribute(
      "aria-busy",
      savedAiPortfoliosLoading ? "true" : "false",
    );

    if (savedAiPortfoliosLoading && savedAiPortfolios.length === 0) {
      const loading = document.createElement("p");
      loading.className = "ai-saved-portfolio-empty";
      loading.textContent = "保存済みテーマを読み込み中...";
      container.appendChild(loading);
      return;
    }

    if (savedAiPortfoliosError && savedAiPortfolios.length === 0) {
      const box = document.createElement("div");
      box.className = "ai-saved-portfolio-error";
      box.setAttribute("role", "alert");
      const text = document.createElement("p");
      text.textContent = savedAiPortfoliosError;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "再試行";
      retry.addEventListener("click", loadSavedAiPortfolios);
      box.appendChild(text);
      box.appendChild(retry);
      container.appendChild(box);
      return;
    }

    if (savedAiPortfolios.length === 0) {
      const empty = document.createElement("p");
      empty.className = "ai-saved-portfolio-empty";
      empty.textContent = "保存済みのカスタムテーマはありません。";
      container.appendChild(empty);
      return;
    }

    savedAiPortfolios.forEach((portfolio) => {
      const id = String(portfolio.id || "");
      const item = document.createElement("div");
      item.className = "ai-saved-portfolio-item";
      item.setAttribute("role", "listitem");

      const selectButton = document.createElement("button");
      selectButton.type = "button";
      selectButton.className = "ai-saved-portfolio-select";
      selectButton.classList.toggle("active", currentAiPortfolio?.id === id);
      selectButton.setAttribute(
        "aria-label",
        `${portfolio.title || portfolio.theme || "保存済みテーマ"} を開く`,
      );
      const title = document.createElement("strong");
      title.textContent =
        portfolio.title || portfolio.theme || "保存済みテーマ";
      const theme = document.createElement("span");
      theme.textContent = portfolio.theme || "カスタムテーマ";
      selectButton.appendChild(title);
      selectButton.appendChild(theme);
      selectButton.addEventListener("click", () => selectSavedAiPortfolio(id));

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "ai-saved-portfolio-delete";
      deleteButton.textContent = "削除";
      deleteButton.disabled = deletingSavedAiPortfolioIds.has(id);
      deleteButton.setAttribute(
        "aria-label",
        `${portfolio.title || portfolio.theme || "保存済みテーマ"} を削除`,
      );
      deleteButton.addEventListener("click", () => deleteSavedAiPortfolio(id));

      item.appendChild(selectButton);
      item.appendChild(deleteButton);
      container.appendChild(item);
    });
  }

  function showSavedAiPortfolioError(message) {
    setSavedAiPortfolioStatus(message);
    savedAiPortfoliosError = message;
    renderSavedAiPortfolios();
  }

  async function loadSavedAiPortfolios() {
    if (savedAiPortfoliosAbortController) {
      savedAiPortfoliosAbortController.abort();
    }
    const abortController = new AbortController();
    const requestGeneration = ++savedAiPortfoliosRequestGeneration;
    savedAiPortfoliosAbortController = abortController;
    savedAiPortfoliosLoading = true;
    savedAiPortfoliosError = "";
    setSavedAiPortfolioStatus("保存済みテーマを読み込み中...");
    renderSavedAiPortfolios();

    try {
      const resp = await csrfFetch("/api/ai-portfolio", {
        signal: abortController.signal,
      });
      const data = await resp.json();
      if (
        !isCurrentSavedAiPortfolioRequest(requestGeneration, abortController)
      ) {
        return;
      }
      if (!resp.ok || !data?.ok) {
        throw new Error(
          getApiErrorMessage(
            data,
            `保存済みテーマの取得に失敗しました (${resp.status})`,
          ),
        );
      }
      savedAiPortfolios = (Array.isArray(data.saved) ? data.saved : []).filter(
        isCustomSavedAiPortfolio,
      );
      savedAiPortfoliosLoaded = true;
      savedAiPortfoliosError = "";
      setSavedAiPortfolioStatus(
        savedAiPortfolios.length > 0
          ? `${savedAiPortfolios.length}件の保存済みテーマ`
          : "保存済みのカスタムテーマはありません。",
      );
      renderSavedAiPortfolios();
    } catch (err) {
      if (
        !isCurrentSavedAiPortfolioRequest(requestGeneration, abortController) ||
        err?.name === "AbortError"
      ) {
        return;
      }
      console.error("loadSavedAiPortfolios error:", err);
      savedAiPortfoliosLoaded = false;
      showSavedAiPortfolioError("保存済みテーマの取得に失敗しました。");
    } finally {
      if (savedAiPortfoliosAbortController === abortController) {
        savedAiPortfoliosAbortController = null;
        savedAiPortfoliosLoading = false;
        renderSavedAiPortfolios();
      }
    }
  }

  function refreshSavedAiPortfolios() {
    void loadSavedAiPortfolios();
  }

  function upsertSavedAiPortfolio(portfolio) {
    if (!isCustomSavedAiPortfolio(portfolio)) return;
    const next = { ...portfolio };
    const existingIndex = savedAiPortfolios.findIndex(
      (item) => item.id === next.id,
    );
    if (existingIndex >= 0) {
      savedAiPortfolios.splice(existingIndex, 1, next);
    } else {
      savedAiPortfolios.push(next);
    }
    renderSavedAiPortfolios();
    refreshSavedAiPortfolios();
  }

  function selectSavedAiPortfolio(portfolioId) {
    const portfolio = savedAiPortfolios.find((item) => item.id === portfolioId);
    if (!portfolio) return;

    cancelAiPortfolioRequest();
    currentAiPortfolio = portfolio;
    activeAiPreset = "custom";
    document.querySelectorAll(".ai-preset-pill").forEach((pill) => {
      pill.classList.toggle("active", pill.dataset.preset === "custom");
    });
    document.getElementById("ai-pf-custom-panel")?.classList.remove("hidden");
    const themeInput = document.getElementById("ai-theme-input");
    if (themeInput) themeInput.value = portfolio.theme || "";
    renderAiPortfolio(currentAiPortfolio);
    renderSavedAiPortfolios();
    setSavedAiPortfolioStatus("保存済みテーマを表示しています。");
  }

  async function deleteSavedAiPortfolio(portfolioId) {
    if (
      !portfolioId ||
      deletingSavedAiPortfolioIds.has(portfolioId) ||
      !savedAiPortfolios.some((item) => item.id === portfolioId)
    ) {
      return;
    }

    deletingSavedAiPortfolioIds.add(portfolioId);
    renderSavedAiPortfolios();
    try {
      const resp = await csrfFetch("/api/ai-portfolio/custom", {
        method: "DELETE",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ id: portfolioId }),
      });
      const data = await resp.json();
      if (!resp.ok || !data?.ok) {
        throw new Error(
          getApiErrorMessage(data, `削除に失敗しました (${resp.status})`),
        );
      }

      savedAiPortfolios = savedAiPortfolios.filter(
        (item) => item.id !== portfolioId,
      );
      if (currentAiPortfolio?.id === portfolioId) {
        currentAiPortfolio = null;
        activeAiPreset = "tech";
        document.querySelectorAll(".ai-preset-pill").forEach((pill) => {
          pill.classList.toggle("active", pill.dataset.preset === "tech");
        });
        document.getElementById("ai-pf-custom-panel")?.classList.add("hidden");
        loadAiPortfolio(activeAiPreset);
      }
      setSavedAiPortfolioStatus("保存済みテーマを削除しました。");
      if (typeof showToast === "function") {
        showToast("🗑️ 保存済みテーマを削除しました", "#7dffb0");
      }
      refreshSavedAiPortfolios();
    } catch (err) {
      console.error("deleteSavedAiPortfolio error:", err);
      setSavedAiPortfolioStatus("保存済みテーマの削除に失敗しました。");
      showAiPortfolioFailure("保存済みテーマの削除に失敗しました");
    } finally {
      deletingSavedAiPortfolioIds.delete(portfolioId);
      renderSavedAiPortfolios();
    }
  }

  function cancelAiPortfolioRequest() {
    aiPortfolioRequestGeneration += 1;
    if (aiPortfolioAbortController) aiPortfolioAbortController.abort();
    aiPortfolioAbortController = null;
  }

  function beginAiPortfolioRequest() {
    if (aiPortfolioAbortController) aiPortfolioAbortController.abort();
    const requestGeneration = ++aiPortfolioRequestGeneration;
    aiPortfolioAbortController = new AbortController();
    return { requestGeneration, abortController: aiPortfolioAbortController };
  }

  function isCurrentAiPortfolioRequest(requestGeneration) {
    return requestGeneration === aiPortfolioRequestGeneration;
  }

  async function loadAiPortfolio(presetOrTheme) {
    const { requestGeneration, abortController } = beginAiPortfolioRequest();
    showLoadingState(true);
    try {
      const resp = await csrfFetch("/api/ai-portfolio/generate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ theme: presetOrTheme }),
        signal: abortController.signal,
      });

      let data = await resp.json();
      if (!isCurrentAiPortfolioRequest(requestGeneration)) return;
      if (!resp.ok) {
        throw new Error(
          getApiErrorMessage(data, `取得に失敗しました (${resp.status})`),
        );
      }

      if (data && data.fetching) {
        const maxAttempts = 15;
        let attempt = 0;
        let finished = false;
        while (attempt < maxAttempts) {
          if (!isCurrentAiPortfolioRequest(requestGeneration)) return;
          attempt += 1;
          const backoff = Math.min(1000 * attempt, 3000);
          await new Promise((resolve) => setTimeout(resolve, backoff));
          if (!isCurrentAiPortfolioRequest(requestGeneration)) return;

          const pollResp = await csrfFetch("/api/ai-portfolio/generate", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ theme: presetOrTheme }),
            signal: abortController.signal,
          });
          if (!pollResp.ok) {
            const pollErrData = await pollResp.json().catch(() => ({}));
            throw new Error(
              getApiErrorMessage(
                pollErrData,
                `取得に失敗しました (${pollResp.status})`,
              ),
            );
          }
          const pollData = await pollResp.json();
          if (!isCurrentAiPortfolioRequest(requestGeneration)) return;
          if (pollData && !pollData.fetching) {
            data = pollData;
            finished = true;
            break;
          }
        }
        if (!finished) {
          throw new Error("AIポートフォリオの生成がタイムアウトしました");
        }
      }

      if (data.ok && data.portfolio) {
        currentAiPortfolio = data.portfolio;
        renderAiPortfolio(currentAiPortfolio);
        upsertSavedAiPortfolio(currentAiPortfolio);
      } else {
        throw new Error(data.error || "Failed to generate AI portfolio");
      }
    } catch (err) {
      if (
        !isCurrentAiPortfolioRequest(requestGeneration) ||
        err?.name === "AbortError"
      ) {
        return;
      }
      console.error("loadAiPortfolio error:", err);
      showAiPortfolioError("AIポートフォリオの取得に失敗しました。", () =>
        loadAiPortfolio(presetOrTheme),
      );
      showAiPortfolioFailure("AIポートフォリオの取得に失敗しました");
    } finally {
      if (isCurrentAiPortfolioRequest(requestGeneration)) {
        showLoadingState(false);
        if (aiPortfolioAbortController === abortController) {
          aiPortfolioAbortController = null;
        }
      }
    }
  }

  async function rebalanceAiPortfolio(theme) {
    const { requestGeneration, abortController } = beginAiPortfolioRequest();
    showLoadingState(true);
    try {
      const resp = await csrfFetch("/api/ai-portfolio/rebalance", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ theme: theme }),
        signal: abortController.signal,
      });

      let data = await resp.json();
      if (!isCurrentAiPortfolioRequest(requestGeneration)) return;
      if (!resp.ok) {
        throw new Error(
          getApiErrorMessage(data, `リバランスに失敗しました (${resp.status})`),
        );
      }

      if (data && data.fetching) {
        const maxAttempts = 15;
        let attempt = 0;
        let finished = false;
        while (attempt < maxAttempts) {
          if (!isCurrentAiPortfolioRequest(requestGeneration)) return;
          attempt += 1;
          const backoff = Math.min(1000 * attempt, 3000);
          await new Promise((resolve) => setTimeout(resolve, backoff));
          if (!isCurrentAiPortfolioRequest(requestGeneration)) return;

          const pollResp = await csrfFetch("/api/ai-portfolio/rebalance", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ theme: theme }),
            signal: abortController.signal,
          });
          if (!pollResp.ok) {
            const pollErrData = await pollResp.json().catch(() => ({}));
            throw new Error(
              getApiErrorMessage(
                pollErrData,
                `リバランスに失敗しました (${pollResp.status})`,
              ),
            );
          }
          const pollData = await pollResp.json();
          if (!isCurrentAiPortfolioRequest(requestGeneration)) return;
          if (pollData && !pollData.fetching) {
            data = pollData;
            finished = true;
            break;
          }
        }
        if (!finished) {
          throw new Error("AIリバランスがタイムアウトしました");
        }
      }

      if (data.ok && data.portfolio) {
        currentAiPortfolio = data.portfolio;
        renderAiPortfolio(currentAiPortfolio);
        upsertSavedAiPortfolio(currentAiPortfolio);
        if (typeof showToast === "function") {
          showToast("🤖 AIリバランスが完了しました！", "#7dffb0");
        }
      } else {
        throw new Error(getApiErrorMessage(data, "リバランスに失敗しました"));
      }
    } catch (err) {
      if (
        !isCurrentAiPortfolioRequest(requestGeneration) ||
        err?.name === "AbortError"
      ) {
        return;
      }
      console.error("rebalanceAiPortfolio error:", err);
      showAiPortfolioError("AIリバランスに失敗しました。", () =>
        rebalanceAiPortfolio(theme),
      );
      showAiPortfolioFailure("AIリバランスに失敗しました");
    } finally {
      if (isCurrentAiPortfolioRequest(requestGeneration)) {
        showLoadingState(false);
        if (aiPortfolioAbortController === abortController) {
          aiPortfolioAbortController = null;
        }
      }
    }
  }

  async function saveAiPortfolio(portfolio) {
    try {
      const resp = await csrfFetch("/api/ai-portfolio/save", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ portfolio: portfolio }),
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(
          getApiErrorMessage(data, `保存に失敗しました (${resp.status})`),
        );
      }
      if (data.ok) {
        if (data.portfolio) {
          currentAiPortfolio = data.portfolio;
          upsertSavedAiPortfolio(currentAiPortfolio);
        }
        if (typeof showToast === "function") {
          showToast("💾 カスタムテーマを保存しました", "#7dffb0");
        }
      } else {
        throw new Error(getApiErrorMessage(data, "保存に失敗しました"));
      }
    } catch (err) {
      console.error("saveAiPortfolio error:", err);
      showAiPortfolioFailure("カスタムテーマの保存に失敗しました");
    }
  }

  async function copyAiPortfolioToMy(items) {
    try {
      const resp = await csrfFetch("/api/ai-portfolio/copy-to-my", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ items: items }),
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(
          getApiErrorMessage(data, `反映に失敗しました (${resp.status})`),
        );
      }
      if (data.ok) {
        if (typeof showToast === "function") {
          showToast(
            `📥 ${data.added_count || items.length} 銘柄をマイポートフォリオに反映しました`,
            "#7dffb0",
          );
          if (data.stale_warning) {
            showToast(`⚠️ ${data.stale_warning}`, "#ffd166");
          }
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
      } else {
        throw new Error(
          getApiErrorMessage(data, "マイポートフォリオへの反映に失敗しました"),
        );
      }
    } catch (err) {
      console.error("copyAiPortfolioToMy error:", err);
      showAiPortfolioFailure("マイポートフォリオへの反映に失敗しました");
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
      if (stock && stock.change_percent != null) {
        simulatedTodayPlJpy +=
          allocJpy *
          (Number(String(stock.change_percent).replace(/,/g, "")) / 100);
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
        price: item.target_price || 100,
        change_percent: 0.5,
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
        stock.price != null
          ? Number(String(stock.price).replace(/,/g, ""))
          : null;
      const priceText =
        rawPrice != null && Number.isFinite(rawPrice)
          ? `${currencySymbol}${rawPrice.toLocaleString()}`
          : "--";
      const changePct =
        stock.change_percent != null
          ? Number(String(stock.change_percent).replace(/,/g, ""))
          : null;
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
      const favKey = `${(item.market || "us").toLowerCase()}:${item.symbol}`;
      if (
        typeof state !== "undefined" &&
        typeof state.isFavorite === "function" &&
        state.isFavorite(favKey)
      ) {
        favBtn.classList.add("active");
        favBtn.textContent = "★ お気に入り済";
      }
      favBtn.addEventListener("click", () => {
        if (
          typeof state !== "undefined" &&
          typeof state.toggleFavorite === "function"
        ) {
          state.toggleFavorite(favKey);
          const isFav = state.isFavorite(favKey);
          favBtn.classList.toggle("active", isFav);
          favBtn.textContent = isFav ? "★ お気に入り済" : "★ お気に入り";
          if (typeof renderFavorites === "function") {
            renderFavorites();
          }
          if (typeof showToast === "function") {
            showToast(
              isFav
                ? `⭐ ${item.symbol} をお気に入りに追加しました`
                : `🗑️ ${item.symbol} をお気に入りから解除しました`,
              isFav ? "#7dffb0" : "#ffcc66",
            );
          }
        }
      });

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
      container.replaceChildren();
      const box = document.createElement("div");
      box.className = "ai-loading-box";
      const spinner = document.createElement("div");
      spinner.className = "ai-spinner";
      const text = document.createElement("p");
      text.textContent =
        "🤖 AIが市場データを分析して最適ポートフォリオを構成中...";
      box.appendChild(spinner);
      box.appendChild(text);
      container.appendChild(box);
    } else {
      container.querySelector(".ai-loading-box")?.remove();
    }
  }

  function getApiErrorMessage(data, fallback) {
    const candidate =
      data?.details?.reason ||
      data?.message ||
      data?.error?.message ||
      data?.error;
    return typeof candidate === "string" && candidate.trim()
      ? candidate
      : fallback;
  }

  function showAiPortfolioFailure(message) {
    if (typeof showToast === "function") {
      showToast(`⚠️ ${message}`, "#ff4d4d");
    }
  }

  function showAiPortfolioError(message, retry) {
    const container = document.getElementById("ai-portfolio-stocks");
    if (!container) return;
    container.replaceChildren();

    const box = document.createElement("div");
    box.className = "ai-error-box";
    box.setAttribute("role", "alert");

    const text = document.createElement("p");
    text.textContent = message;
    box.appendChild(text);

    const retryButton = document.createElement("button");
    retryButton.type = "button";
    retryButton.textContent = "再試行";
    retryButton.addEventListener("click", retry);
    box.appendChild(retryButton);
    container.appendChild(box);
  }
})();
