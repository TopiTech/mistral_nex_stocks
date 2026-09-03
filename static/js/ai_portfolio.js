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

  // Colors used across the AI portfolio charts (kept in sync with the CSS).
  const AI_CHART_COLORS = [
    "#a855f7",
    "#06b6d4",
    "#3b82f6",
    "#10b981",
    "#f59e0b",
    "#ec4899",
    "#6366f1",
  ];

  const RISK_LABELS = { low: "低リスク", mid: "中リスク", high: "高リスク" };

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

    const switchToMy = () => {
      cancelAiPortfolioRequest();
      myTab.classList.add("active");
      myTab.setAttribute("aria-selected", "true");
      myTab.setAttribute("tabindex", "0");
      aiTab.classList.remove("active");
      aiTab.setAttribute("aria-selected", "false");
      aiTab.setAttribute("tabindex", "-1");

      myView.classList.remove("hidden");
      myView.removeAttribute("hidden");
      aiView.classList.add("hidden");
      aiView.setAttribute("hidden", "");
    };

    const switchToAi = () => {
      aiTab.classList.add("active");
      aiTab.setAttribute("aria-selected", "true");
      aiTab.setAttribute("tabindex", "0");
      myTab.classList.remove("active");
      myTab.setAttribute("aria-selected", "false");
      myTab.setAttribute("tabindex", "-1");

      aiView.classList.remove("hidden");
      aiView.removeAttribute("hidden");
      myView.classList.add("hidden");
      myView.setAttribute("hidden", "");

      if (!savedAiPortfoliosLoaded && !savedAiPortfoliosLoading) {
        loadSavedAiPortfolios();
      }
      if (!currentAiPortfolio) {
        loadAiPortfolio(activeAiPreset);
      } else {
        renderAiPortfolio(currentAiPortfolio);
      }
    };

    myTab.addEventListener("click", switchToMy);
    aiTab.addEventListener("click", switchToAi);

    [myTab, aiTab].forEach((tab) => {
      tab.addEventListener("keydown", (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          if (tab === myTab) {
            switchToAi();
            aiTab.focus();
          } else {
            switchToMy();
            myTab.focus();
          }
        }
      });
    });
  }

  function setupPresetBar() {
    const presetPills = document.querySelectorAll(".ai-preset-pill");
    const customPanel = document.getElementById("ai-pf-custom-panel");

    presetPills.forEach((pill) => {
      pill.setAttribute(
        "aria-pressed",
        String(pill.classList.contains("active")),
      );
      pill.addEventListener("click", () => {
        presetPills.forEach((p) => {
          p.classList.remove("active");
          p.setAttribute("aria-pressed", "false");
        });
        pill.classList.add("active");
        pill.setAttribute("aria-pressed", "true");

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
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === "Enter") {
          e.preventDefault();
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
      const meta = document.createElement("span");
      meta.className = "ai-saved-portfolio-meta";
      meta.textContent = `${portfolio.items ? portfolio.items.length : 0}銘柄・${
        portfolio.expected_return || "---"
      }`;
      selectButton.appendChild(title);
      selectButton.appendChild(theme);
      selectButton.appendChild(meta);
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
      const data = await resp.json().catch(() => ({}));
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
      const data = await resp.json().catch(() => ({}));
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

  function delayWithAbort(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      const timer = setTimeout(resolve, ms);
      signal?.addEventListener(
        "abort",
        () => {
          clearTimeout(timer);
          reject(new DOMException("Aborted", "AbortError"));
        },
        { once: true },
      );
    });
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

      let data = await resp.json().catch(() => ({}));
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
          await delayWithAbort(backoff, abortController.signal);
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
          const pollData = await pollResp.json().catch(() => ({}));
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

      let data = await resp.json().catch(() => ({}));
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
          await delayWithAbort(backoff, abortController.signal);
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
          const pollData = await pollResp.json().catch(() => ({}));
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

      const data = await resp.json().catch(() => ({}));
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

      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(
          getApiErrorMessage(data, `反映に失敗しました (${resp.status})`),
        );
      }
      if (data.ok) {
        if (typeof showToast === "function") {
          const toastMsg =
            data.message ||
            `${data.added_count ?? items.length} 銘柄をマイポートフォリオに反映しました`;
          showToast(
            toastMsg.startsWith("📥") ? toastMsg : `📥 ${toastMsg}`,
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

  // ---------------------------------------------------------------------------
  // Deterministic AI performance simulation
  //
  // The virtual performance curve used to be a hard-coded array, so every
  // portfolio rendered the exact same "AI simulation" chart. It is now derived
  // deterministically from the portfolio's own identity and composition: the
  // same portfolio always produces the same curve (stable across re-renders),
  // while different portfolios get visibly different trajectories driven by
  // their holdings, weights, target prices and live market data.
  // ---------------------------------------------------------------------------

  // FNV-1a 32-bit string hash (stable across JS engines).
  function hashString(str) {
    let hash = 0x811c9dc5;
    for (let i = 0; i < str.length; i += 1) {
      hash ^= str.charCodeAt(i);
      hash = Math.imul(hash, 0x01000193);
    }
    return hash >>> 0;
  }

  // Deterministic PRNG (mulberry32) seeded from a portfolio identity hash.
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function next() {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function toNumber(value) {
    if (value === null || value === undefined) return null;
    const n = Number(String(value).replace(/,/g, ""));
    return Number.isFinite(n) ? n : null;
  }

  function computeAiSimulation(portfolio) {
    const items = Array.isArray(portfolio?.items) ? portfolio.items : [];
    const identity = [
      String(portfolio?.id || ""),
      String(portfolio?.theme || ""),
      String(portfolio?.title || ""),
      ...items.map(
        (it) =>
          `${it?.market || "us"}:${String(it?.symbol || "")}:${toNumber(it?.weight_pct) ?? 0}:${toNumber(it?.target_price) ?? ""}`,
      ),
    ].join("|");
    const rng = mulberry32(hashString(identity));

    const weights = items.map((it) => {
      const w = toNumber(it?.weight_pct);
      return w !== null && w > 0 ? w : 0;
    });
    const totalWeight = weights.reduce((acc, w) => acc + w, 0) || 1;

    let weightedReturn = 0;
    items.forEach((it, index) => {
      const stock = findStockInState(it.symbol, it.market);
      const currentPrice = toNumber(stock?.price);
      const targetPrice = toNumber(it.target_price);

      // Stable per-symbol bias: the same ticker behaves similarly across
      // portfolios, so curves stay believable while still differing.
      const symbolHash = hashString(String(it.symbol || "").toLowerCase());
      const symbolBias = ((symbolHash % 2000) / 2000 - 0.5) * 0.03;

      // Portfolio-specific noise from the seeded PRNG.
      const portfolioNoise = (rng() - 0.5) * 0.04;

      // Gentle anchor toward the AI target price. Target prices are AI
      // estimates (often stale), so only a small, capped tilt is applied
      // instead of aggressively re-rating the position.
      let targetPull = 0;
      if (
        currentPrice !== null &&
        currentPrice > 0 &&
        targetPrice !== null &&
        targetPrice > 0
      ) {
        const gap = (targetPrice - currentPrice) / currentPrice;
        targetPull = Math.max(-0.04, Math.min(0.06, gap * 0.08));
      }

      // Today's real move nudges the month-end estimate slightly.
      const todayChange =
        toNumber(stock?.change_percent) !== null
          ? toNumber(stock?.change_percent) / 100
          : 0;

      // Small structural drift so simulations are not biased toward zero.
      const baseDrift = 0.005;

      const itemReturn =
        baseDrift +
        symbolBias +
        portfolioNoise +
        targetPull +
        todayChange * 0.15;
      weightedReturn += (weights[index] / totalWeight) * itemReturn;
    });

    // Clamp to a plausible monthly band (±15%) so the simulation stays credible.
    const monthlyReturn = Math.max(-0.15, Math.min(0.15, weightedReturn));

    // Build the 5-point curve ending exactly at the computed final value.
    const steps = 4;
    const points = [];
    for (let i = 0; i <= steps; i += 1) {
      const t = i / steps;
      // Ease-in-out so most movement happens mid-month (more natural than a
      // straight line).
      const eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      const wobble = (rng() - 0.5) * 0.002 * (1 - t);
      points.push(
        Math.round(
          VIRTUAL_BASE_CAPITAL_JPY * (1 + monthlyReturn * eased + wobble),
        ),
      );
    }
    points[steps] = Math.round(VIRTUAL_BASE_CAPITAL_JPY * (1 + monthlyReturn));

    return { points, monthlyReturn };
  }

  function renderAiKpiGrid(portfolio) {
    const totalValueEl = document.getElementById("ai-pf-total-value");
    const totalPlEl = document.getElementById("ai-pf-total-pl");
    const todayPlEl = document.getElementById("ai-pf-today-pl");

    if (totalValueEl) totalValueEl.textContent = "¥10,000,000";

    // The total P&L is the same simulation that drives the chart, so the KPI
    // always matches the end point of the performance curve.
    const sim = computeAiSimulation(portfolio);
    const simulatedPlJpy = VIRTUAL_BASE_CAPITAL_JPY * sim.monthlyReturn;

    // Today's P&L comes from real per-stock moves weighted by allocation.
    let simulatedTodayPlJpy = 0;
    const items = portfolio.items || [];
    items.forEach((item) => {
      const stock = findStockInState(item.symbol, item.market);
      const allocJpy =
        VIRTUAL_BASE_CAPITAL_JPY * ((item.weight_pct || 20) / 100);
      const changePct = toNumber(stock?.change_percent);
      if (changePct !== null) {
        simulatedTodayPlJpy += allocJpy * (changePct / 100);
      }
    });

    if (totalPlEl) {
      const plPct = (simulatedPlJpy / VIRTUAL_BASE_CAPITAL_JPY) * 100;
      const sign = simulatedPlJpy >= 0 ? "+" : "";
      totalPlEl.textContent = `${sign}¥${Math.round(Math.abs(simulatedPlJpy)).toLocaleString()} (${sign}${plPct.toFixed(1)}%)`;
      totalPlEl.classList.toggle("pos", simulatedPlJpy >= 0);
      totalPlEl.classList.toggle("neg", simulatedPlJpy < 0);
    }

    if (todayPlEl) {
      const sign = simulatedTodayPlJpy >= 0 ? "+" : "";
      todayPlEl.textContent = `${sign}¥${Math.round(Math.abs(simulatedTodayPlJpy)).toLocaleString()}`;
      todayPlEl.classList.toggle("pos", simulatedTodayPlJpy >= 0);
      todayPlEl.classList.toggle("neg", simulatedTodayPlJpy < 0);
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
      const weightPct = toNumber(item.weight_pct) ?? 20;
      const allocJpy = VIRTUAL_BASE_CAPITAL_JPY * (weightPct / 100);

      const rawPrice = toNumber(stock.price);
      const priceText =
        rawPrice !== null
          ? `${currencySymbol}${rawPrice.toLocaleString()}`
          : "--";
      const changePct = toNumber(stock.change_percent);
      const changeText =
        changePct !== null
          ? `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`
          : "--%";
      const changeColor =
        changePct !== null && changePct >= 0
          ? "var(--color-positive)"
          : "var(--color-negative)";
      const targetPrice = toNumber(item.target_price);
      const targetPriceText =
        targetPrice !== null && targetPrice > 0
          ? `${currencySymbol}${targetPrice.toLocaleString()}`
          : "--";
      const riskLevel = ["low", "mid", "high"].includes(item.risk_level)
        ? item.risk_level
        : "mid";
      const rationale =
        item.rationale || "業界成長性と財務基盤を評価して組み入れ。";

      // 目標株価との乖離 (upside): positive when current < target.
      let gapText = null;
      let gapPositive = true;
      if (
        rawPrice !== null &&
        rawPrice > 0 &&
        targetPrice !== null &&
        targetPrice > 0
      ) {
        const gapPct = ((targetPrice - rawPrice) / rawPrice) * 100;
        gapPositive = gapPct >= 0;
        gapText = `${gapPct >= 0 ? "+" : ""}${gapPct.toFixed(1)}%`;
      }

      // --- header ---
      const header = document.createElement("div");
      header.className = "ai-stock-header";

      const symbolBox = document.createElement("div");
      symbolBox.className = "ai-stock-symbol";
      const flagSpan = document.createElement("span");
      flagSpan.className = "ai-stock-flag";
      flagSpan.textContent = marketFlag;
      const strong = document.createElement("strong");
      strong.textContent = item.symbol;
      symbolBox.appendChild(flagSpan);
      symbolBox.appendChild(strong);

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

      // --- weight progress bar ---
      const weightBar = document.createElement("div");
      weightBar.className = "ai-weight-bar";
      weightBar.setAttribute("role", "img");
      weightBar.setAttribute(
        "aria-label",
        `${item.symbol} の構成比率 ${weightPct}%`,
      );
      const weightFill = document.createElement("span");
      weightFill.className = "ai-weight-bar-fill";
      weightFill.style.width = `${Math.min(100, weightPct)}%`;
      weightBar.appendChild(weightFill);

      // --- body ---
      const body = document.createElement("div");
      body.className = "ai-stock-body";

      const metrics = document.createElement("div");
      metrics.className = "ai-stock-metrics";
      const metricRows = [
        ["仮想割当額", `¥${Math.round(allocJpy).toLocaleString()}`, ""],
        ["AI目標株価", targetPriceText, ""],
        [
          "リスク評価",
          RISK_LABELS[riskLevel] || riskLevel,
          `risk-pill ${riskLevel}`,
        ],
      ];
      if (gapText !== null) {
        metricRows.push([
          "目標株価乖離",
          gapText,
          gapPositive ? "gap-pos" : "gap-neg",
        ]);
      }
      metricRows.forEach(([label, value, valueClass]) => {
        const row = document.createElement("div");
        row.className = "ai-metric-row";
        const labelSpan = document.createElement("span");
        labelSpan.className = "label";
        labelSpan.textContent = `${label}:`;
        row.appendChild(labelSpan);
        const valueStrong = document.createElement("strong");
        valueStrong.textContent = value;
        if (valueClass) valueStrong.className = valueClass;
        row.appendChild(valueStrong);
        metrics.appendChild(row);
      });

      const rationaleBox = document.createElement("div");
      rationaleBox.className = "ai-rationale-box";
      const rationaleTitle = document.createElement("span");
      rationaleTitle.className = "ai-rationale-title";
      rationaleTitle.textContent = "🤖 AI選定理由";
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
        targetPrice !== null && targetPrice > 0 ? String(targetPrice) : "100",
      );
      addBtn.setAttribute("data-weight", String(weightPct));
      addBtn.addEventListener("click", () => {
        copyAiPortfolioToMy([item]);
      });

      footer.appendChild(favBtn);
      footer.appendChild(addBtn);

      card.appendChild(header);
      card.appendChild(weightBar);
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
      const dataValues = items.map((it) => it.weight_pct || 0);
      const centerPlugin = {
        id: "aiSectorCenterText",
        afterDraw(chart) {
          const meta = chart.getDatasetMeta(0);
          if (!meta.data || meta.data.length === 0) return;
          const { ctx } = chart;
          const x = meta.data[0].x;
          const y = meta.data[0].y;
          ctx.save();
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillStyle = "#e2e8f0";
          ctx.font = "700 15px 'Segoe UI', sans-serif";
          ctx.fillText(`${items.length}銘柄`, x, y - 7);
          ctx.fillStyle = "#94a3b8";
          ctx.font = "500 10px 'Segoe UI', sans-serif";
          ctx.fillText("配分構成", x, y + 11);
          ctx.restore();
        },
      };

      aiSectorChartInstance = new Chart(sectorCanvas, {
        type: "doughnut",
        data: {
          labels: labels,
          datasets: [
            {
              data: dataValues,
              backgroundColor: AI_CHART_COLORS.slice(0, items.length),
              borderWidth: 2,
              borderColor: "rgba(11, 16, 32, 0.9)",
              hoverOffset: 6,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "62%",
          plugins: {
            legend: {
              position: "right",
              labels: {
                color: "#cbd5e1",
                font: { size: 11 },
                boxWidth: 10,
                boxHeight: 10,
                usePointStyle: true,
                pointStyle: "circle",
                padding: 12,
              },
            },
            tooltip: {
              callbacks: {
                label: (tooltipCtx) => {
                  const value = tooltipCtx.raw || 0;
                  const total = tooltipCtx.dataset.data.reduce(
                    (acc, v) => acc + (Number(v) || 0),
                    0,
                  );
                  const pct =
                    total > 0 ? ((value / total) * 100).toFixed(1) : "0.0";
                  const weight = Number(value).toFixed(1);
                  return ` ${tooltipCtx.label}: ${weight}% (構成比 ${pct}%)`;
                },
              },
              backgroundColor: "rgba(15, 23, 42, 0.95)",
              borderColor: "rgba(168, 85, 247, 0.4)",
              borderWidth: 1,
              padding: 10,
              titleColor: "#e2e8f0",
              bodyColor: "#cbd5e1",
            },
          },
        },
        plugins: [centerPlugin],
      });
    }

    // 2. Performance Simulation Line Chart
    const summaryCanvas = document.getElementById("ai-pf-summary-canvas");
    if (summaryCanvas) {
      if (aiSummaryChartInstance) aiSummaryChartInstance.destroy();

      const sim = computeAiSimulation(portfolio);
      const labels = [
        "1ヶ月前",
        "3週間前",
        "2週間前",
        "1週間前",
        "現在 (シミュレーション)",
      ];
      const fillGradient = summaryCanvas
        .getContext("2d")
        .createLinearGradient(0, 0, 0, summaryCanvas.clientHeight || 240);
      fillGradient.addColorStop(0, "rgba(168, 85, 247, 0.35)");
      fillGradient.addColorStop(1, "rgba(168, 85, 247, 0.02)");

      const formatYen = (value) =>
        value == null ||
        typeof value === "boolean" ||
        !Number.isFinite(Number(value))
          ? "¥--"
          : `¥${Number(value).toLocaleString()}`;

      aiSummaryChartInstance = new Chart(summaryCanvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "AI仮想運用評価額",
              data: sim.points,
              borderColor: "#a855f7",
              borderWidth: 2.5,
              backgroundColor: fillGradient,
              fill: true,
              tension: 0.4,
              pointRadius: 3,
              pointHoverRadius: 6,
              pointBackgroundColor: "#a855f7",
              pointBorderColor: "#fff",
              pointBorderWidth: 1.5,
            },
            {
              label: "投資元本 (ベース)",
              data: labels.map(() => VIRTUAL_BASE_CAPITAL_JPY),
              borderColor: "rgba(148, 163, 184, 0.45)",
              borderDash: [6, 6],
              borderWidth: 1.5,
              pointRadius: 0,
              fill: false,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { intersect: false, mode: "index" },
          plugins: {
            legend: {
              display: true,
              position: "top",
              align: "end",
              labels: {
                color: "#94a3b8",
                font: { size: 10 },
                boxWidth: 14,
                boxHeight: 2,
                usePointStyle: true,
                pointStyle: "line",
              },
            },
            tooltip: {
              backgroundColor: "rgba(15, 23, 42, 0.95)",
              borderColor: "rgba(168, 85, 247, 0.4)",
              borderWidth: 1,
              padding: 10,
              titleColor: "#e2e8f0",
              bodyColor: "#cbd5e1",
              callbacks: {
                label: (tooltipCtx) => {
                  const value = tooltipCtx.raw;
                  if (tooltipCtx.datasetIndex === 1) {
                    return ` ${tooltipCtx.dataset.label}: ${formatYen(value)}`;
                  }
                  const pl = value - VIRTUAL_BASE_CAPITAL_JPY;
                  const plPct = (pl / VIRTUAL_BASE_CAPITAL_JPY) * 100;
                  const sign = pl >= 0 ? "+" : "";
                  return ` ${tooltipCtx.dataset.label}: ${formatYen(value)} (${sign}¥${Math.abs(pl).toLocaleString()} / ${sign}${plPct.toFixed(1)}%)`;
                },
              },
            },
          },
          scales: {
            x: {
              ticks: { color: "#94a3b8", font: { size: 11 } },
              grid: { display: false },
            },
            y: {
              ticks: {
                color: "#94a3b8",
                font: { size: 11 },
                callback: (value) => `¥${(Number(value) / 10000).toFixed(0)}万`,
              },
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
      // Skeleton cards give a more polished loading experience than a bare
      // spinner while the AI portfolio is being generated.
      const skeletonCount = 3;
      for (let i = 0; i < skeletonCount; i += 1) {
        const skeleton = document.createElement("div");
        skeleton.className = "ai-stock-card ai-skeleton-card";
        skeleton.setAttribute("aria-hidden", "true");
        const header = document.createElement("div");
        header.className = "ai-skeleton ai-skeleton-header";
        const bar = document.createElement("div");
        bar.className = "ai-skeleton ai-skeleton-bar";
        const line1 = document.createElement("div");
        line1.className = "ai-skeleton ai-skeleton-line";
        const line2 = document.createElement("div");
        line2.className = "ai-skeleton ai-skeleton-line short";
        const line3 = document.createElement("div");
        line3.className = "ai-skeleton ai-skeleton-line";
        skeleton.appendChild(header);
        skeleton.appendChild(bar);
        skeleton.appendChild(line1);
        skeleton.appendChild(line2);
        skeleton.appendChild(line3);
        container.appendChild(skeleton);
      }
      const statusBox = document.createElement("div");
      statusBox.className = "ai-loading-box";
      const spinner = document.createElement("div");
      spinner.className = "ai-spinner";
      const text = document.createElement("p");
      text.textContent =
        "🤖 AIが市場データを分析して最適ポートフォリオを構成中...";
      statusBox.appendChild(spinner);
      statusBox.appendChild(text);
      container.appendChild(statusBox);
    } else {
      container
        .querySelectorAll(".ai-skeleton-card")
        .forEach((el) => el.remove());
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

    const icon = document.createElement("div");
    icon.className = "ai-error-icon";
    icon.textContent = "🤖";
    box.appendChild(icon);

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
