// Mistral NeX Stocks - Simple Screener Logic

(function () {
  "use strict";

  let currentMarket = "all";
  let currentSector = "all";
  let currentPreset = "all";
  let searchQuery = "";
  let sortBy = "market_cap";
  let sortOrder = "desc";

  let fetchTimeout = null;
  let screenerRequestGeneration = 0;
  let screenerAbortController = null;

  function initScreener() {
    // Market Toggle Buttons
    const marketBtns = document.querySelectorAll(
      "#screenerMarketToggle .screener-pill",
    );
    marketBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        marketBtns.forEach((b) => {
          b.classList.remove("active");
          b.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        currentMarket = btn.dataset.market || "all";
        triggerFetch();
      });
    });

    // Preset Toggle Buttons
    const presetBtns = document.querySelectorAll(
      "#screenerChangePreset .preset-btn",
    );
    presetBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        presetBtns.forEach((b) => {
          b.classList.remove("active");
          b.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        currentPreset = btn.dataset.preset || "all";
        triggerFetch();
      });
    });

    // Sector Filter
    const sectorEl = document.getElementById("screenerSector");
    if (sectorEl) {
      sectorEl.addEventListener("change", (e) => {
        currentSector = e.target.value;
        triggerFetch();
      });
    }

    // Search Input (Debounced)
    const searchEl = document.getElementById("screenerSearch");
    if (searchEl) {
      searchEl.addEventListener("input", (e) => {
        searchQuery = e.target.value.trim();
        triggerFetchDebounced();
      });
    }

    // Sort Dropdown
    const sortEl = document.getElementById("screenerSort");
    if (sortEl) {
      sortEl.addEventListener("change", (e) => {
        sortBy = e.target.value;
        updateTableSortIndicators();
        triggerFetch();
      });
    }

    // Sort Order Toggle Button
    const sortOrderBtn = document.getElementById("screenerSortOrderBtn");
    function updateSortOrderBtn() {
      if (!sortOrderBtn) return;
      sortOrderBtn.textContent = sortOrder === "desc" ? "⬇️" : "⬆️";
      sortOrderBtn.setAttribute(
        "aria-label",
        sortOrder === "desc"
          ? "降順（クリックで昇順に切り替え）"
          : "昇順（クリックで降順に切り替え）",
      );
    }

    if (sortOrderBtn) {
      sortOrderBtn.addEventListener("click", () => {
        sortOrder = sortOrder === "desc" ? "asc" : "desc";
        updateSortOrderBtn();
        updateTableSortIndicators();
        triggerFetch();
      });
    }

    function handleSortableHeader(th) {
      const col = th.dataset.sort;
      if (!col) return;
      if (sortBy === col) {
        sortOrder = sortOrder === "desc" ? "asc" : "desc";
      } else {
        sortBy = col;
        sortOrder = "desc";
      }
      if (sortEl) sortEl.value = sortBy;
      updateSortOrderBtn();
      updateTableSortIndicators();
      triggerFetch();
    }

    // Table Header Click-to-Sort (mouse + keyboard)
    const sortableHeaders = document.querySelectorAll(".sortable-th");
    sortableHeaders.forEach((th) => {
      th.addEventListener("click", () => handleSortableHeader(th));
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleSortableHeader(th);
        }
      });
    });

    // Reset Button
    const resetBtn = document.getElementById("screenerResetBtn");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        currentMarket = "all";
        currentSector = "all";
        currentPreset = "all";
        searchQuery = "";
        sortBy = "market_cap";
        sortOrder = "desc";

        marketBtns.forEach((b) => {
          const isActive = b.dataset.market === "all";
          b.classList.toggle("active", isActive);
          b.setAttribute("aria-pressed", String(isActive));
        });
        presetBtns.forEach((b) => {
          const isActive = b.dataset.preset === "all";
          b.classList.toggle("active", isActive);
          b.setAttribute("aria-pressed", String(isActive));
        });
        if (sectorEl) sectorEl.value = "all";
        if (searchEl) searchEl.value = "";
        if (sortEl) sortEl.value = "market_cap";
        updateSortOrderBtn();

        triggerFetch();
      });
    }

    // Initial Fetch
    updateTableSortIndicators();
    fetchScreenerResults();
  }

  function updateTableSortIndicators() {
    document.querySelectorAll(".sortable-th").forEach((th) => {
      const col = th.dataset.sort;
      const indicator = th.querySelector(".sort-indicator");
      if (!indicator) return;
      if (col === sortBy) {
        th.classList.add("active-sort");
        th.setAttribute(
          "aria-sort",
          sortOrder === "desc" ? "descending" : "ascending",
        );
        indicator.textContent = sortOrder === "desc" ? " ▼" : " ▲";
      } else {
        th.classList.remove("active-sort");
        th.setAttribute("aria-sort", "none");
        indicator.textContent = "";
      }
    });
  }

  function renderActiveChips() {
    const chipsContainer = document.getElementById("screenerActiveChips");
    if (!chipsContainer) return;
    chipsContainer.textContent = "";

    const chips = [];
    if (currentMarket !== "all") {
      const label = currentMarket === "us" ? "🇺🇸 米国株" : "🇯🇵 日本株";
      chips.push({ key: "market", label: `市場: ${label}` });
    }
    if (currentSector !== "all") {
      chips.push({ key: "sector", label: `セクター: ${currentSector}` });
    }
    if (currentPreset !== "all") {
      const presetLabels = {
        gainers: "上昇 (>0%)",
        hot: "大幅高 (>3%)",
        losers: "下落 (<0%)",
      };
      chips.push({
        key: "preset",
        label: `条件: ${presetLabels[currentPreset] || currentPreset}`,
      });
    }
    if (searchQuery) {
      chips.push({ key: "search", label: `検索: "${searchQuery}"` });
    }

    if (chips.length === 0) {
      chipsContainer.classList.add("hidden");
      return;
    }

    chipsContainer.classList.remove("hidden");
    chips.forEach((c) => {
      const chipEl = document.createElement("span");
      chipEl.className = "screener-chip";
      chipEl.textContent = `${c.label} ×`;
      chipEl.setAttribute("tabindex", "0");
      chipEl.setAttribute("role", "button");
      chipEl.setAttribute("aria-label", `${c.label} フィルターを解除`);
      chipEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          chipEl.click();
        }
      });
      chipEl.addEventListener("click", () => {
        if (c.key === "market") {
          currentMarket = "all";
          document
            .querySelectorAll("#screenerMarketToggle .screener-pill")
            .forEach((b) => {
              const isActive = b.dataset.market === "all";
              b.classList.toggle("active", isActive);
              b.setAttribute("aria-pressed", String(isActive));
            });
        } else if (c.key === "sector") {
          currentSector = "all";
          const s = document.getElementById("screenerSector");
          if (s) s.value = "all";
        } else if (c.key === "preset") {
          currentPreset = "all";
          document
            .querySelectorAll("#screenerChangePreset .preset-btn")
            .forEach((b) => {
              const isActive = b.dataset.preset === "all";
              b.classList.toggle("active", isActive);
              b.setAttribute("aria-pressed", String(isActive));
            });
        } else if (c.key === "search") {
          searchQuery = "";
          const input = document.getElementById("screenerSearch");
          if (input) input.value = "";
        }
        triggerFetch();
      });
      chipsContainer.appendChild(chipEl);
    });
  }

  function triggerFetchDebounced() {
    if (fetchTimeout) clearTimeout(fetchTimeout);
    fetchTimeout = setTimeout(fetchScreenerResults, 300);
  }

  function triggerFetch() {
    if (fetchTimeout) clearTimeout(fetchTimeout);
    fetchScreenerResults();
  }

  async function fetchScreenerResults() {
    const requestGeneration = ++screenerRequestGeneration;
    if (screenerAbortController) screenerAbortController.abort();
    const abortController = new AbortController();
    screenerAbortController = abortController;
    renderActiveChips();
    const tbody = document.getElementById("screenerTableBody");
    const countEl = document.getElementById("screenerResultsCount");
    if (!tbody) return;

    tbody.replaceChildren();
    const trLoading = document.createElement("tr");
    const tdLoading = document.createElement("td");
    tdLoading.colSpan = 8;
    tdLoading.className = "text-center loading-cell";
    tdLoading.textContent = "データをロード中...";
    trLoading.appendChild(tdLoading);
    tbody.appendChild(trLoading);
    const params = new URLSearchParams({
      market: currentMarket,
      sector: currentSector,
      sort_by: sortBy,
      sort_order: sortOrder,
    });

    if (searchQuery) {
      params.append("q", searchQuery);
    }

    // Preset mapping to change_percent bounds
    if (currentPreset === "gainers") {
      params.append("min_change", "0.01");
    } else if (currentPreset === "hot") {
      params.append("min_change", "3.0");
    } else if (currentPreset === "losers") {
      params.append("max_change", "-0.01");
    }

    try {
      const { data } = await apiFetch(
        `/api/screener?${params.toString()}`,
        { signal: abortController.signal },
        { showToast: false },
      );
      if (requestGeneration !== screenerRequestGeneration) return;
      if (!data || !data.ok) {
        throw new Error(data?.error || "データの取得に失敗しました");
      }

      if (countEl) countEl.textContent = data.total || 0;
      renderTableRows(data.stocks || [], requestGeneration);
    } catch (err) {
      if (
        requestGeneration !== screenerRequestGeneration ||
        err?.name === "AbortError"
      ) {
        return;
      }
      console.error("Screener fetch error:", err);
      if (tbody) {
        tbody.replaceChildren();
        const trError = document.createElement("tr");
        const tdError = document.createElement("td");
        tdError.colSpan = 8;
        tdError.className = "text-center error-cell";
        tdError.textContent = `エラーが発生しました: ${err.message || String(err)}`;
        trError.appendChild(tdError);
        tbody.appendChild(trError);
      }
    }
  }

  function formatCurrency(val, market) {
    if (val == null || val === "" || Number.isNaN(Number(val))) return "--";
    const num = Number(val);
    if (!Number.isFinite(num)) return "--";
    if (market === "jp") {
      return `¥${Math.round(num).toLocaleString("ja-JP")}`;
    }
    return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function formatMarketCap(val, market) {
    if (
      val == null ||
      val === "" ||
      Number.isNaN(Number(val)) ||
      Number(val) <= 0
    )
      return "--";
    const num = Number(val);
    if (market === "jp") {
      if (num >= 1e12) return `¥${(num / 1e12).toFixed(1)}兆`;
      if (num >= 1e8) return `¥${(num / 1e8).toFixed(0)}億`;
      return `¥${num.toLocaleString()}`;
    }
    if (num >= 1e12) return `$${(num / 1e12).toFixed(2)}T`;
    if (num >= 1e9) return `$${(num / 1e9).toFixed(2)}B`;
    if (num >= 1e6) return `$${(num / 1e6).toFixed(1)}M`;
    return `$${num.toLocaleString()}`;
  }

  function renderTableRows(stocks, requestGeneration) {
    const tbody = document.getElementById("screenerTableBody");
    if (!tbody || requestGeneration !== screenerRequestGeneration) return;

    if (stocks.length === 0) {
      tbody.replaceChildren();
      const trEmpty = document.createElement("tr");
      const tdEmpty = document.createElement("td");
      tdEmpty.colSpan = 8;
      tdEmpty.className = "text-center empty-cell";
      tdEmpty.textContent = "指定した条件に該当する銘柄はありません。";
      trEmpty.appendChild(tdEmpty);
      tbody.appendChild(trEmpty);
      return;
    }

    const fragment = document.createDocumentFragment();
    stocks.forEach((stock) => {
      const tr = document.createElement("tr");
      tr.setAttribute("tabindex", "0");
      tr.setAttribute("role", "row");
      tr.style.cursor = "pointer";
      tr.setAttribute(
        "aria-label",
        `${stock.symbol} ${stock.name || ""} 価格: ${formatCurrency(stock.price, stock.market)}`,
      );
      tr.addEventListener("click", () => {
        if (stock.symbol) {
          window.location.href = `/main?q=${encodeURIComponent(stock.symbol)}`;
        }
      });
      tr.addEventListener("keydown", (e) => {
        if (e.target !== tr) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          tr.click();
        }
      });

      const changeVal = parseFloat(stock.change_percent);
      let changeClass = "neutral";
      let changeSign = "";
      if (!isNaN(changeVal)) {
        if (changeVal > 0) {
          changeClass = "plus";
          changeSign = "+";
        } else if (changeVal < 0) {
          changeClass = "minus";
        }
      }
      const pctStr = !isNaN(changeVal)
        ? `${changeSign}${changeVal.toFixed(2)}%`
        : "--%";

      // Symbol & Name Cell
      const nameTd = document.createElement("td");
      nameTd.className = "stock-symbol-cell";
      const flexDiv = document.createElement("div");
      flexDiv.className = "symbol-name-flex";
      const symCode = document.createElement("strong");
      symCode.className = "sym-code";
      symCode.textContent = stock.symbol;
      const symName = document.createElement("span");
      symName.className = "sym-name";
      symName.textContent = stock.name || "";
      flexDiv.appendChild(symCode);
      flexDiv.appendChild(symName);
      nameTd.appendChild(flexDiv);
      tr.appendChild(nameTd);

      // Market Badge Cell
      const mktTd = document.createElement("td");
      const mktBadge = document.createElement("span");
      mktBadge.className = `mkt-badge ${stock.market}`;
      mktBadge.textContent = stock.market === "jp" ? "🇯🇵 JP" : "🇺🇸 US";
      mktTd.appendChild(mktBadge);
      tr.appendChild(mktTd);

      // Sector Cell
      const sectorTd = document.createElement("td");
      sectorTd.className = "sector-cell";
      sectorTd.textContent = stock.sector || "Other";
      tr.appendChild(sectorTd);

      // Price Cell
      const priceTd = document.createElement("td");
      priceTd.className = "text-right price-cell";
      priceTd.textContent = formatCurrency(stock.price, stock.market);
      tr.appendChild(priceTd);

      // Change Cell
      const changeTd = document.createElement("td");
      changeTd.className = `text-right change-cell ${changeClass}`;
      changeTd.textContent = pctStr;
      tr.appendChild(changeTd);

      // High / Low Range Cell
      const rangeTd = document.createElement("td");
      rangeTd.className = "text-right range-cell";
      const isJp = stock.market === "jp";
      const highNum = Number(stock.high);
      const lowNum = Number(stock.low);
      const highStr =
        Number.isFinite(highNum) && highNum > 0
          ? isJp
            ? Math.round(highNum).toLocaleString()
            : highNum.toFixed(2)
          : "--";
      const lowStr =
        Number.isFinite(lowNum) && lowNum > 0
          ? isJp
            ? Math.round(lowNum).toLocaleString()
            : lowNum.toFixed(2)
          : "--";
      rangeTd.textContent = `${highStr} / ${lowStr}`;
      tr.appendChild(rangeTd);

      // Market Cap Cell
      const capTd = document.createElement("td");
      capTd.className = "text-right cap-cell";
      capTd.textContent = formatMarketCap(stock.market_cap, stock.market);
      tr.appendChild(capTd);

      // Action Cell
      const actTd = document.createElement("td");
      actTd.className = "text-center action-cell";

      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "screener-add-btn";
      addBtn.textContent = "➕ 追加";
      addBtn.setAttribute(
        "aria-label",
        `${stock.name || stock.symbol} をウォッチリストに追加`,
      );
      addBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        addBtn.disabled = true;
        addBtn.textContent = "追加中...";
        try {
          const fetchFn = typeof csrfFetch === "function" ? csrfFetch : fetch;
          const res = await fetchFn("/api/stocks/add", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              symbol: stock.symbol,
              market: stock.market,
              name: stock.name || stock.symbol,
            }),
          });
          const data = await res.json().catch(() => ({}));
          if (res.ok && (data?.ok || data?.success)) {
            addBtn.textContent = "✓ 追加済";
            addBtn.className = "screener-add-btn added";
            if (typeof showToast === "function") {
              showToast(
                "✓ " +
                  (stock.name || stock.symbol) +
                  " をウォッチリストに追加しました",
                "#7dffb0",
              );
            }
          } else if (data?.details?.reason === "既に追加済み") {
            addBtn.textContent = "✓ 追加済";
            addBtn.className = "screener-add-btn added";
            if (typeof showToast === "function") {
              showToast(stock.symbol + " は既に追加されています", "#ffb86c");
            }
          } else {
            const reason =
              data?.details?.reason ||
              data?.error ||
              "銘柄の追加に失敗しました";
            addBtn.textContent = reason;
            addBtn.disabled = false;
            if (typeof showToast === "function") {
              showToast(reason, "#ff5555");
            }
          }
        } catch (_err) {
          addBtn.textContent = "エラー";
          addBtn.disabled = false;
          if (typeof showToast === "function") {
            showToast("通信エラーが発生しました", "#ff5555");
          }
        }
      });

      actTd.appendChild(addBtn);
      tr.appendChild(actTd);

      fragment.appendChild(tr);
    });

    requestAnimationFrame(() => {
      if (requestGeneration !== screenerRequestGeneration) return;
      tbody.replaceChildren();
      tbody.appendChild(fragment);
    });
  }

  document.addEventListener("DOMContentLoaded", initScreener);
})();
