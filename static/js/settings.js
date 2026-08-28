/**
 * Settings page controller.
 * Manages stock list CRUD, drag-and-drop reordering, and custom AI prompt.
 * All DOM manipulation uses safe APIs (textContent, createElement) - no innerHTML.
 */

// DEFAULT_SYMBOLS and APP_CONFIG are initialized by config_init.js

const dragInitialized = new Set();
const STOCKS_LOAD_RETRY_DELAY_MS = 1500;
const STOCKS_LOAD_MAX_RETRIES = 8;
let stocksLoadRetryTimer = null;
let stocksLoadAbortController = null;
let stocksLoadGeneration = 0;

// getSortOrderとsaveSortOrderはutils.jsで定義済み（全ページ共通）
// saveSortOrderはlocalStorageに保存するユーティリティ
function saveSortOrder(market, order) {
  localStorage.setItem(`sort_${market}`, JSON.stringify(order));
}

function sortIndex(order, symbol) {
  const idx = order.indexOf(symbol);
  return idx === -1 ? Number.MAX_SAFE_INTEGER : idx;
}

// escapeHtml関数は index.js から利用可能

function ensureDragContainer(container, market) {
  if (dragInitialized.has(market)) return;
  container.addEventListener("dragover", (e) => {
    e.preventDefault();
    const after = getDragAfterElement(container, e.clientY);
    const dragging = container.querySelector(".stock-item.dragging");
    if (!dragging) return;
    if (after == null) container.appendChild(dragging);
    else container.insertBefore(dragging, after);
  });
  dragInitialized.add(market);
}

function renderStocksLoading() {
  ["us", "jp", "idx"].forEach((market) => {
    const listEl = document.getElementById(`${market}-list`);
    if (!listEl) return;
    listEl.textContent = "";
    const loading = document.createElement("li");
    loading.className = "empty-message";
    loading.textContent = "銘柄データを準備中です...";
    listEl.appendChild(loading);
  });
}

async function loadStocks(retryAttempt = 0) {
  const requestGeneration = ++stocksLoadGeneration;
  if (stocksLoadAbortController) stocksLoadAbortController.abort();
  if (stocksLoadRetryTimer) {
    clearTimeout(stocksLoadRetryTimer);
    stocksLoadRetryTimer = null;
  }
  const abortController = new AbortController();
  stocksLoadAbortController = abortController;

  try {
    const { data } = await apiFetch(
      "/api/stocks",
      { signal: abortController.signal },
      { showToast: false },
    );
    if (requestGeneration !== stocksLoadGeneration) return;
    if (data?.fetching) {
      renderStocksLoading();
      if (retryAttempt >= STOCKS_LOAD_MAX_RETRIES) {
        showSettingsMessage(
          "銘柄データの準備に時間がかかっています。しばらくして再度お試しください。",
        );
        return;
      }
      stocksLoadRetryTimer = setTimeout(() => {
        if (requestGeneration === stocksLoadGeneration) {
          loadStocks(retryAttempt + 1);
        }
      }, STOCKS_LOAD_RETRY_DELAY_MS);
      return;
    }
    const stocksObj = data.stocks || data;
    const userUS = (stocksObj.us || []).filter(
      (s) => !DEFAULT_SYMBOLS.us.includes(s.symbol),
    );
    const userJP = (stocksObj.jp || []).filter(
      (s) => !DEFAULT_SYMBOLS.jp.includes(s.symbol),
    );
    const userIdx = (stocksObj.idx || []).filter(
      (s) => !DEFAULT_SYMBOLS.idx.includes(s.symbol),
    );
    renderList("us", userUS);
    renderList("jp", userJP);
    renderList("idx", userIdx);
  } catch (e) {
    if (
      requestGeneration !== stocksLoadGeneration ||
      e?.name === "AbortError"
    ) {
      return;
    }
    logger.error("Failed to load stocks:", e);
    showSettingsMessage(
      "銘柄リストの取得に失敗しました。しばらくして再度お試しください。",
    );
  }
}

function renderList(market, stocks) {
  const listEl = document.getElementById(`${market}-list`);
  listEl.textContent = "";
  ensureDragContainer(listEl, market);
  if (!stocks.length) {
    const empty = document.createElement("li");
    empty.className = "empty-message";
    empty.textContent = "追加銘柄はありません";
    listEl.appendChild(empty);
    return;
  }
  const order = getSortOrder(market);
  const sorted = [...stocks].sort(
    (a, b) => sortIndex(order, a.symbol) - sortIndex(order, b.symbol),
  );
  sorted.forEach((stock) => {
    const li = document.createElement("li");
    li.className = "stock-item";
    li.draggable = true;
    li.dataset.symbol = stock.symbol;

    const left = document.createElement("div");
    left.className = "stock-left";

    const handle = document.createElement("span");
    handle.className = "drag-handle";
    handle.textContent = "☰";
    left.appendChild(handle);

    const symbolEl = document.createElement("span");
    symbolEl.className = "stock-symbol";
    symbolEl.textContent = stock.symbol || "";
    left.appendChild(symbolEl);

    const nameEl = document.createElement("span");
    nameEl.className = "stock-name";
    nameEl.textContent = stock.name || "";
    left.appendChild(nameEl);

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "delete-btn";
    deleteBtn.type = "button";
    deleteBtn.textContent = "削除";

    const moveUpBtn = document.createElement("button");
    moveUpBtn.className = "stock-move-btn stock-move-up";
    moveUpBtn.type = "button";
    moveUpBtn.textContent = "▲";
    moveUpBtn.setAttribute("aria-label", "上に移動");

    const moveDownBtn = document.createElement("button");
    moveDownBtn.className = "stock-move-btn stock-move-down";
    moveDownBtn.type = "button";
    moveDownBtn.textContent = "▼";
    moveDownBtn.setAttribute("aria-label", "下に移動");

    const controls = document.createElement("div");
    controls.className = "stock-controls";
    controls.appendChild(moveUpBtn);
    controls.appendChild(moveDownBtn);
    controls.appendChild(deleteBtn);

    li.appendChild(left);
    li.appendChild(controls);

    addDragEvents(listEl, li, market);
    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      attachInlineDeleteConfirm(controls, deleteBtn, () =>
        executeDeleteStock(market, stock.symbol),
      );
    });
    moveUpBtn.addEventListener("click", () =>
      moveStock(market, stock.symbol, -1),
    );
    moveDownBtn.addEventListener("click", () =>
      moveStock(market, stock.symbol, 1),
    );
    listEl.appendChild(li);
  });
}

async function moveStock(market, symbol, direction) {
  const order = getSortOrder(market);
  const stocksObj = await fetchStocksForMarket(market);
  const userStocks = (stocksObj || []).filter(
    (s) => !DEFAULT_SYMBOLS[market].includes(s.symbol),
  );
  const symbols = [...userStocks.map((s) => s.symbol)];

  // order にないものは末尾に追加（既存の並びを尊重）
  const ordered = order.filter((s) => symbols.includes(s));
  symbols.forEach((s) => {
    if (!ordered.includes(s)) ordered.push(s);
  });

  const idx = ordered.indexOf(symbol);
  const target = idx + direction;
  if (idx === -1 || target < 0 || target >= ordered.length) return;

  [ordered[idx], ordered[target]] = [ordered[target], ordered[idx]];
  saveSortOrder(market, ordered);
  renderList(market, userStocks);
}

async function fetchStocksForMarket(market) {
  try {
    const { data } = await apiFetch(
      "/api/stocks",
      {},
      { showToast: false },
    ).catch(() => ({ data: {} }));
    const stocksObj = data.stocks || data;
    return stocksObj[market] || [];
  } catch (e) {
    logger.error("Failed to load stocks:", e);
    return [];
  }
}

function addDragEvents(container, item, market) {
  item.addEventListener("dragstart", (e) => {
    item.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  item.addEventListener("dragend", () => {
    item.classList.remove("dragging");
    const symbols = [...container.querySelectorAll(".stock-item")].map(
      (li) => li.dataset.symbol,
    );
    saveSortOrder(market, symbols);
  });
}

function getDragAfterElement(container, y) {
  const items = [...container.querySelectorAll(".stock-item:not(.dragging)")];
  return items.reduce(
    (closest, child) => {
      const box = child.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset)
        return { offset, element: child };
      return closest;
    },
    { offset: Number.NEGATIVE_INFINITY },
  ).element;
}

function attachInlineDeleteConfirm(container, originalBtn, onConfirm) {
  // Hide original delete button
  originalBtn.style.display = "none";

  // Create inline confirmation buttons
  const group = document.createElement("div");
  group.className = "inline-confirm-group";

  const yesBtn = document.createElement("button");
  yesBtn.type = "button";
  yesBtn.className = "inline-confirm-yes";
  yesBtn.textContent = "削除する";

  const noBtn = document.createElement("button");
  noBtn.type = "button";
  noBtn.className = "inline-confirm-no";
  noBtn.textContent = "✕";

  group.appendChild(yesBtn);
  group.appendChild(noBtn);
  container.appendChild(group);

  let cleanup = () => {
    group.remove();
    originalBtn.style.display = "";
  };

  yesBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    yesBtn.disabled = true;
    yesBtn.textContent = "削除中...";
    onConfirm().finally(() => cleanup());
  });

  noBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    cleanup();
  });

  // Auto restore after 6 seconds of inactivity (store timer so onConfirm can cancel it)
  const autoRestoreTimer = setTimeout(() => {
    if (group.parentElement) cleanup();
  }, 6000);
  const _origCleanup = cleanup;
  cleanup = () => {
    clearTimeout(autoRestoreTimer);
    _origCleanup();
  };
}

async function executeDeleteStock(market, symbol) {
  try {
    const res = await csrfFetch("/api/stocks/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, market }),
    });
    const payload = await res.text();
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    let data = {};
    try {
      data = payload ? JSON.parse(payload) : {};
    } catch (_parseErr) {
      throw new Error(`サーバー応答の解析に失敗しました: ${payload ? payload.slice(0, 200) : "(empty)"}`);
    }
    if (data.error) throw new Error(data.error);
    loadStocks();
    showToast(`✓ ${symbol} を削除しました`, "#10b981");
  } catch (e) {
    logger.error(e);
    showToast(`削除に失敗しました: ${e.message || "不明なエラー"}`, "#ff7d7d");
  }
}

async function deleteStock(market, symbol) {
  if (!confirm(`${symbol} を削除しますか？`)) return;
  return executeDeleteStock(market, symbol);
}

async function resetAllStocks() {
  const resetBtn = document.getElementById("reset-btn");
  if (resetBtn && !resetBtn.__isConfirming) {
    resetBtn.__isConfirming = true;
    const originalText = resetBtn.textContent;
    resetBtn.textContent = "⚠️ 本当に削除しますか？もう一度クリックで実行";
    resetBtn.style.background = "#e11d48";

    const revertTimer = setTimeout(() => {
      resetBtn.__isConfirming = false;
      resetBtn.textContent = originalText;
      resetBtn.style.background = "";
    }, 4500);

    resetBtn.__revertTimer = revertTimer;
    return;
  }

  if (resetBtn && resetBtn.__revertTimer) {
    clearTimeout(resetBtn.__revertTimer);
    resetBtn.__isConfirming = false;
  }

  try {
    const res = await csrfFetch("/api/stocks/reset", { method: "POST" });
    const payload = await res.text();
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    let data = {};
    try {
      data = payload ? JSON.parse(payload) : {};
    } catch (_parseErr) {
      throw new Error(`サーバー応答の解析に失敗しました: ${payload ? payload.slice(0, 200) : "(empty)"}`);
    }
    if (data.error) throw new Error(data.error);
    localStorage.removeItem("sort_us");
    localStorage.removeItem("sort_jp");
    localStorage.removeItem("sort_idx");
    loadStocks();
    showToast("✓ 銘柄リストを初期化しました", "#10b981");
  } catch (e) {
    logger.error(e);
    showToast(
      `初期化に失敗しました: ${e.message || "不明なエラー"}`,
      "#ff7d7d",
    );
  } finally {
    if (resetBtn) {
      resetBtn.textContent = "⚠️ すべての追加銘柄を削除して初期化";
      resetBtn.style.background = "";
    }
  }
}

function logout() {
  if (!confirm("APIキーを削除してログアウトしますか？")) return;

  // Clear browser storage immediately to ensure it's always removed
  clearLegacyApiKeyStorage();

  // Attempt to clear server-side credentials
  csrfFetch("/api/credentials", { method: "DELETE" })
    .then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        const detail =
          data.error || data.message || `Server error: ${response.status}`;
        throw new Error(detail);
      }
      // Server-side clear succeeded, navigate to setup
      location.href = "/setup";
    })
    .catch((error) => {
      logger.error("Server-side logout failed:", error);
      showToast(
        `ログアウトに失敗しました: ${error.message || "不明なエラー"}。一部の認証情報が端末に残っている可能性があります。`,
        "#ff7d7d",
      );
      // Do not redirect on failure: credentials may still exist server-side.
    });
}

document.addEventListener("DOMContentLoaded", () => {
  loadStocks();

  const backBtn = document.getElementById("back-btn");
  if (backBtn)
    backBtn.addEventListener("click", () => {
      location.href = "/main";
    });

  const screenerBtn = document.getElementById("screener-btn");
  if (screenerBtn)
    screenerBtn.addEventListener("click", () => {
      location.href = "/screener";
    });

  const resetBtn = document.getElementById("reset-btn");
  if (resetBtn) resetBtn.addEventListener("click", resetAllStocks);

  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", logout);

  // Load and bind Default View Mode radio options
  const viewModeRadios = document.querySelectorAll(
    'input[name="defaultViewMode"]',
  );
  const currentViewMode =
    localStorage.getItem("mns_default_view_mode") || "dashboard";
  viewModeRadios.forEach((radio) => {
    if (radio.value === currentViewMode) {
      radio.checked = true;
    }
    radio.addEventListener("change", (e) => {
      const selected = e.target.value;
      localStorage.setItem("mns_default_view_mode", selected);
      showSettingsMessage(
        `デフォルト表示モードを更新しました: ${selected === "observatory" ? "Market Observatory" : "標準ダッシュボード"}`,
        false,
      );
    });
  });

  // Load and bind Market Color Scheme radio options
  const colorSchemeRadios = document.querySelectorAll(
    'input[name="colorScheme"]',
  );
  const currentScheme = getColorSchemePreference
    ? getColorSchemePreference()
    : "us_standard";
  colorSchemeRadios.forEach((radio) => {
    if (radio.value === currentScheme) {
      radio.checked = true;
    }
    radio.addEventListener("change", (e) => {
      const selected = e.target.value;
      localStorage.setItem("mns_color_scheme", selected);
      if (typeof initThemeColorScheme === "function") {
        initThemeColorScheme();
      }
      showSettingsMessage(
        `カラーテーマを更新しました: ${selected === "jp_standard" ? "日本市場標準" : "米国標準"}`,
        false,
      );
    });
  });

  function renderModelOptions(container, models, currentModelName) {
    container.textContent = "";
    if (!Array.isArray(models) || models.length === 0) {
      const emptyMsg = document.createElement("div");
      emptyMsg.className = "model-loading";
      emptyMsg.textContent = "モデル一覧が取得できませんでした";
      container.appendChild(emptyMsg);
      return;
    }

    models.forEach((m) => {
      const label = document.createElement("label");
      label.className = "model-option-label";

      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "mistralModel";
      radio.value = m.name;
      if (
        m.name === currentModelName ||
        (currentModelName && currentModelName.includes(m.name))
      ) {
        radio.checked = true;
      }

      const card = document.createElement("span");
      card.className = "model-option-card";

      const header = document.createElement("div");
      header.className = "model-card-header";

      const title = document.createElement("strong");
      title.className = "model-card-title";
      title.textContent = m.label || m.name;
      header.appendChild(title);

      const badges = document.createElement("div");
      badges.className = "model-card-badges";

      if (m.recommended) {
        const recBadge = document.createElement("span");
        recBadge.className = "model-rec-tag";
        recBadge.textContent = "推奨";
        badges.appendChild(recBadge);
      }

      if (m.badge) {
        const badgeTag = document.createElement("span");
        badgeTag.className = "model-badge-tag";
        badgeTag.textContent = m.badge;
        badges.appendChild(badgeTag);
      }

      header.appendChild(badges);
      card.appendChild(header);

      if (m.description) {
        const desc = document.createElement("span");
        desc.className = "model-card-desc";
        desc.textContent = m.description;
        card.appendChild(desc);
      }

      label.appendChild(radio);
      label.appendChild(card);
      container.appendChild(label);
    });

    // If none is checked, select default or recommended
    const anyChecked = container.querySelector(
      'input[name="mistralModel"]:checked',
    );
    if (!anyChecked) {
      const defaultRadio =
        container.querySelector(
          'input[name="mistralModel"][value="mistral-medium-2604"]',
        ) || container.querySelector('input[name="mistralModel"]');
      if (defaultRadio) defaultRadio.checked = true;
    }
  }

  const modelGrid = document.getElementById("model-selection-grid");
  const saveModelBtn = document.getElementById("save-model-btn");
  const modelStatus = document.getElementById("model-save-status");

  if (modelGrid && saveModelBtn) {
    saveModelBtn.addEventListener("click", async () => {
      const selectedRadio = modelGrid.querySelector(
        'input[name="mistralModel"]:checked',
      );
      if (!selectedRadio) {
        showToast("モデルを選択してください", "#ff7d7d");
        return;
      }
      const chosenModel = selectedRadio.value;
      saveModelBtn.disabled = true;
      saveModelBtn.textContent = "保存中...";
      try {
        const res = await csrfFetch("/api/credentials", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mistral_model: chosenModel }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          throw new Error(
            data.details?.reason || data.error || "保存に失敗しました",
          );
        }
        modelStatus.textContent = "✓ モデル設定を保存しました";
        setTimeout(() => {
          modelStatus.textContent = "";
        }, 3000);
        const headerBadge = document.querySelector(".model-badge");
        if (headerBadge && data.model_badge) {
          headerBadge.textContent = data.model_badge;
        }
        showToast(
          `Mistralモデルを「${data.model_label || chosenModel}」に設定しました`,
          "#10b981",
        );
      } catch (err) {
        logger.error("Save model error:", err);
        showToast(`モデル設定の保存に失敗しました: ${err.message}`, "#ff7d7d");
      } finally {
        saveModelBtn.disabled = false;
        saveModelBtn.textContent = "モデル設定を保存";
      }
    });
  }

  const promptInput = document.getElementById("custom-prompt-input");
  const savePromptBtn = document.getElementById("save-prompt-btn");
  const promptStatus = document.getElementById("prompt-save-status");

  if (promptInput && savePromptBtn) {
    // Load existing credentials state (custom prompt, model catalog, etc.)
    apiFetch("/api/credentials", {}, { showToast: false })
      .then(({ data }) => {
        if (data && data.ok) {
          if (data.custom_ai_prompt) {
            promptInput.value = data.custom_ai_prompt;
          }
          if (modelGrid && data.available_models) {
            renderModelOptions(
              modelGrid,
              data.available_models,
              data.mistral_model,
            );
          }
          const alphaInput = document.getElementById(
            "alphavantage-api-key-input",
          );
          if (alphaInput && data.has_alphavantage_api_key) {
            alphaInput.placeholder = "設定済み (変更する場合のみ入力)";
          }
        }
      })
      .catch((err) => logger.error("Failed to load credentials state:", err));

    // Save prompt
    savePromptBtn.addEventListener("click", async () => {
      savePromptBtn.disabled = true;
      savePromptBtn.textContent = "保存中...";
      try {
        const res = await csrfFetch("/api/credentials", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ custom_ai_prompt: promptInput.value }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok)
          throw new Error(
            data.details?.reason || data.error || "保存に失敗しました",
          );

        promptStatus.textContent = "✓ 保存しました";
        setTimeout(() => {
          promptStatus.textContent = "";
        }, 3000);
      } catch (err) {
        logger.error("Save prompt error:", err);
        showToast(`プロンプトの保存に失敗しました: ${err.message}`, "#ff7d7d");
      } finally {
        savePromptBtn.disabled = false;
        savePromptBtn.textContent = "保存";
      }
    });
  }

  const alphaInput = document.getElementById("alphavantage-api-key-input");
  const saveAlphaBtn = document.getElementById("save-alpha-btn");
  const alphaStatus = document.getElementById("alpha-save-status");
  if (alphaInput && saveAlphaBtn) {
    saveAlphaBtn.addEventListener("click", async () => {
      saveAlphaBtn.disabled = true;
      saveAlphaBtn.textContent = "保存中...";
      try {
        const res = await csrfFetch("/api/credentials", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ alphavantage_api_key: alphaInput.value }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok)
          throw new Error(
            data.details?.reason || data.error || "保存に失敗しました",
          );

        alphaStatus.textContent = "✓ 保存しました";
        alphaInput.value = "";
        alphaInput.placeholder = "設定済み (変更する場合のみ入力)";
        setTimeout(() => {
          alphaStatus.textContent = "";
        }, 3000);
      } catch (err) {
        logger.error("Save alpha key error:", err);
        showToast(
          `Alpha Vantageキーの保存に失敗しました: ${err.message}`,
          "#ff7d7d",
        );
      } finally {
        saveAlphaBtn.disabled = false;
        saveAlphaBtn.textContent = "保存";
      }
    });
  }

  // Initialize tabs navigation
  initSettingsTabs();
});

function initSettingsTabs() {
  const tabBtns = document.querySelectorAll(".settings-tab-btn");
  const panels = document.querySelectorAll(".settings-tab-panel");
  if (!tabBtns.length) return;

  const activateTab = (activeBtn) => {
    const targetTab = activeBtn.dataset.tab;
    const targetPanelId = targetTab.replace("tab-", "panel-");
    tabBtns.forEach((b) => {
      const isActive = b === activeBtn;
      b.classList.toggle("active", isActive);
      b.setAttribute("aria-selected", String(isActive));
      b.setAttribute("tabindex", isActive ? "0" : "-1");
    });
    panels.forEach((p) => {
      const isActive = p.id === targetPanelId;
      p.classList.toggle("active", isActive);
      if (isActive && p.hasAttribute("hidden")) p.removeAttribute("hidden");
    });
    activeBtn.focus();
  };

  tabBtns.forEach((btn, idx) => {
    btn.setAttribute("tabindex", btn.classList.contains("active") ? "0" : "-1");
    btn.addEventListener("click", () => activateTab(btn));
    btn.addEventListener("keydown", (e) => {
      let targetIdx = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") targetIdx = (idx + 1) % tabBtns.length;
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") targetIdx = (idx - 1 + tabBtns.length) % tabBtns.length;
      else if (e.key === "Home") targetIdx = 0;
      else if (e.key === "End") targetIdx = tabBtns.length - 1;
      if (targetIdx !== null) {
        e.preventDefault();
        activateTab(tabBtns[targetIdx]);
      }
    });
  });
}

// showToastはutils.jsで定義済み（全ページ共通）

// Alias for backward compatibility with existing code
const showSettingsMessage = (message, isError = true) => {
  showToast(message, isError ? "#ff7d7d" : "#6bb6ff");
};
