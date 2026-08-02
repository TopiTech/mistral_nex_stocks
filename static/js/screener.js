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

  function initScreener() {
    // Market Toggle Buttons
    const marketBtns = document.querySelectorAll(
      "#screenerMarketToggle .screener-pill",
    );
    marketBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        marketBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
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
        presetBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
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
        triggerFetch();
      });
    }

    // Sort Order Toggle Button
    const sortOrderBtn = document.getElementById("screenerSortOrderBtn");
    if (sortOrderBtn) {
      sortOrderBtn.addEventListener("click", () => {
        sortOrder = sortOrder === "desc" ? "asc" : "desc";
        sortOrderBtn.textContent = sortOrder === "desc" ? "⬇️" : "⬆️";
        triggerFetch();
      });
    }

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

        marketBtns.forEach((b) =>
          b.classList.toggle("active", b.dataset.market === "all"),
        );
        presetBtns.forEach((b) =>
          b.classList.toggle("active", b.dataset.preset === "all"),
        );
        if (sectorEl) sectorEl.value = "all";
        if (searchEl) searchEl.value = "";
        if (sortEl) sortEl.value = "market_cap";
        if (sortOrderBtn) sortOrderBtn.textContent = "⬇️";

        triggerFetch();
      });
    }

    // Initial Fetch
    fetchScreenerResults();
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
    const tbody = document.getElementById("screenerTableBody");
    const countEl = document.getElementById("screenerResultsCount");
    if (!tbody) return;

    tbody.innerHTML =
      '<tr><td colspan="8" class="text-center loading-cell">データをロード中...</td></tr>';

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
      const res = await fetch(`/api/screener?${params.toString()}`);
      if (!res.ok) {
        throw new Error(`HTTP error ${res.status}`);
      }
      const data = await res.json();
      if (!data || !data.ok) {
        throw new Error(data?.error || "データの取得に失敗しました");
      }

      if (countEl) countEl.textContent = data.total || 0;
      renderTableRows(data.stocks || []);
    } catch (err) {
      console.error("Screener fetch error:", err);
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="8" class="text-center error-cell">エラーが発生しました: ${err.message || String(err)}</td></tr>`;
      }
    }
  }

  function formatCurrency(val, market) {
    if (!val || isNaN(val)) return "--";
    const symbol = market === "jp" ? "¥" : "$";
    return `${symbol}${val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function formatMarketCap(val, market) {
    if (!val || isNaN(val) || val <= 0) return "--";
    if (market === "jp") {
      if (val >= 1e12) return `¥${(val / 1e12).toFixed(1)}兆`;
      if (val >= 1e8) return `¥${(val / 1e8).toFixed(0)}億`;
      return `¥${val.toLocaleString()}`;
    }
    if (val >= 1e12) return `$${(val / 1e12).toFixed(2)}T`;
    if (val >= 1e9) return `$${(val / 1e9).toFixed(2)}B`;
    if (val >= 1e6) return `$${(val / 1e6).toFixed(1)}M`;
    return `$${val.toLocaleString()}`;
  }

  function renderTableRows(stocks) {
    const tbody = document.getElementById("screenerTableBody");
    if (!tbody) return;

    if (stocks.length === 0) {
      tbody.innerHTML =
        '<tr><td colspan="8" class="text-center empty-cell">指定した条件に該当する銘柄はありません。</td></tr>';
      return;
    }

    tbody.innerHTML = "";
    stocks.forEach((stock) => {
      const tr = document.createElement("tr");

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
      nameTd.innerHTML = `
        <div class="symbol-name-flex">
          <strong class="sym-code">${stock.symbol}</strong>
          <span class="sym-name">${stock.name || ""}</span>
        </div>
      `;
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
      const highStr = stock.high ? stock.high.toFixed(2) : "--";
      const lowStr = stock.low ? stock.low.toFixed(2) : "--";
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
          const data = await res.json();
          if (res.ok && data?.success) {
            addBtn.textContent = "✓ 追加済";
            addBtn.className = "screener-add-btn added";
          } else {
            addBtn.textContent = data?.error || "追加済み";
            addBtn.disabled = false;
          }
        } catch (_err) {
          addBtn.textContent = "エラー";
          addBtn.disabled = false;
        }
      });

      actTd.appendChild(addBtn);
      tr.appendChild(actTd);

      tbody.appendChild(tr);
    });
  }

  document.addEventListener("DOMContentLoaded", initScreener);
})();
