document.addEventListener("DOMContentLoaded", () => {
  const state = {
    currentMarket: "us",
    loading: false,
    controller: null,
    stockCount: 0,
  };

  const els = {
    canvas: document.getElementById("heatmap-canvas"),
    loading: document.getElementById("heatmap-loading"),
    updateTime: document.getElementById("update-time"),
    count: document.getElementById("heatmap-count"),
    tooltip: document.getElementById("heatmap-tooltip"),
    toggleUs: document.getElementById("toggle-us"),
    toggleJp: document.getElementById("toggle-jp"),
  };

  if (!els.canvas) return;

  const TREEMAP_SIZE = 1000;

  els.toggleUs?.addEventListener("click", () => switchMarket("us"));
  els.toggleJp?.addEventListener("click", () => switchMarket("jp"));

  function switchMarket(market) {
    if (state.currentMarket === market) return;
    state.currentMarket = market;
    els.toggleUs?.classList.toggle("active", market === "us");
    els.toggleJp?.classList.toggle("active", market === "jp");
    els.toggleUs?.setAttribute("aria-pressed", String(market === "us"));
    els.toggleJp?.setAttribute("aria-pressed", String(market === "jp"));
    loadHeatmap();
  }

  function setLoading(isLoading) {
    state.loading = isLoading;
    els.loading?.classList.toggle("show", isLoading);
    els.loading?.setAttribute("aria-hidden", String(!isLoading));
    if (els.canvas) {
      els.canvas.classList.toggle("is-loading", isLoading);
    }
  }

  function showError(message) {
    if (!els.canvas) return;
    els.canvas.textContent = "";
    const error = document.createElement("div");
    error.className = "heatmap-error-state";
    const icon = document.createElement("div");
    icon.className = "heatmap-error-icon";
    icon.textContent = "!";
    const strong = document.createElement("strong");
    strong.textContent = message;
    const span = document.createElement("span");
    span.textContent = "市場が休場中、またはデータ取得に時間がかかっています。しばらくしてから再試行してください。";
    error.append(icon, strong, span);
    els.canvas.appendChild(error);
  }

// escapeHtmlはutils.jsで定義済み（全ページ共通）

  function normalizeStock(stock) {
    const price = toFiniteNumber(stock.price);
    const changePercent = toFiniteNumber(stock.change_percent);
    const volume = toFiniteNumber(stock.volume) || 0;
    const rawMarketCap = toFiniteNumber(stock.market_cap);
    const fallbackSize = Math.max(price, 1) * Math.max(volume, 1);
    const size = Number.isFinite(rawMarketCap) && rawMarketCap > 0 ? rawMarketCap : fallbackSize;

    return {
      ...stock,
      price,
      change_percent: Number.isFinite(changePercent) ? changePercent : 0,
      volume,
      market_cap: Number.isFinite(rawMarketCap) && rawMarketCap > 0 ? rawMarketCap : 0,
      size,
    };
  }

  function toFiniteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : NaN;
  }

  async function loadHeatmap() {
    if (state.loading) return;

    state.controller?.abort();
    state.controller = new AbortController();
    setLoading(true);
    if (els.canvas) els.canvas.textContent = "";
    if (els.updateTime) els.updateTime.textContent = "-";
    if (els.count) els.count.textContent = "--";

    try {
      const resp = await fetch(`/api/heatmap?market=${encodeURIComponent(state.currentMarket)}`, {
        signal: state.controller.signal,
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const data = await resp.json();
      const stocks = Array.isArray(data.stocks) ? data.stocks : [];
      const normalized = stocks.map(normalizeStock).filter((stock) => stock.size > 0);

      if (!normalized.length) {
        showError("表示できる銘柄データがありませんでした");
        return;
      }

      state.stockCount = normalized.length;
      renderHeatmap(normalized);
      if (els.updateTime) {
        els.updateTime.textContent = new Date().toLocaleTimeString("ja-JP", {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        });
      }
      if (els.count) els.count.textContent = normalized.length;
    } catch (err) {
      if (err.name === "AbortError") return;
      if (typeof logger !== 'undefined' && logger.error) {
        logger.error("Heatmap fetch error:", err);
      } else {
        console.warn("Heatmap fetch error:", err);
      }
      showError("市場データの取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }

  function renderHeatmap(stocks) {
    if (!els.canvas) return;
    els.canvas.textContent = "";

    const sectorsMap = new Map();
    let totalSize = 0;

    stocks.forEach((stock) => {
      const sectorName = stock.sector || "Other";
      const sector = sectorsMap.get(sectorName) || {
        name: sectorName,
        stocks: [],
        size: 0,
      };
      sector.stocks.push(stock);
      sector.size += stock.size;
      totalSize += stock.size;
      sectorsMap.set(sectorName, sector);
    });

    const sectorItems = Array.from(sectorsMap.values())
      .map((sector) => ({ ...sector, weight: sector.size / totalSize }))
      .sort((a, b) => b.weight - a.weight);

    // Initial split direction based on canvas aspect ratio
    const canvasRect = els.canvas.getBoundingClientRect();
    const isHorizontal = canvasRect.width >= canvasRect.height;

    layoutTreemap(
      sectorItems,
      0,
      0,
      TREEMAP_SIZE,
      TREEMAP_SIZE,
      isHorizontal,
      (sector, x, y, width, height) => {
        renderSectorGroup(sector, x, y, width, height);
      },
    );
  }

  function renderSectorGroup(sector, x, y, width, height) {
    const group = document.createElement("div");
    group.className = "heatmap-sector-group";
    group.style.left = `${(x / TREEMAP_SIZE) * 100}%`;
    group.style.top = `${(y / TREEMAP_SIZE) * 100}%`;
    group.style.width = `${(width / TREEMAP_SIZE) * 100}%`;
    group.style.height = `${(height / TREEMAP_SIZE) * 100}%`;

    if (width > 85 && height > 55) {
      const label = document.createElement("div");
      label.className = "sector-label";
      label.textContent = sector.name;
      label.title = `${sector.name} (${sector.stocks.length}銘柄)`;
      group.appendChild(label);
    }

    els.canvas.appendChild(group);

    const stockItems = sector.stocks
      .map((stock) => ({ ...stock, weight: stock.size / sector.size }))
      .sort((a, b) => b.weight - a.weight);

    layoutTreemap(stockItems, 0, 0, 100, 100, width >= height, (stock, sx, sy, sw, sh) =>
      placeNode(stock, sx, sy, sw, sh, group),
    );
  }

  /**
   * Recursive squarified-ish treemap layout algorithm.
   * Splits items along the longer axis to maintain near-square aspect ratios.
   * Uses binary split by weight for balanced layout.
   *
   * @param {Array<{weight: number}>} items - Items to lay out, each with a weight property
   * @param {number} x - Top-left X coordinate of current box
   * @param {number} y - Top-left Y coordinate of current box
   * @param {number} width - Width of current box
   * @param {number} height - Height of current box
   * @param {boolean} horizontal - Whether to split horizontally or vertically
   * @param {Function} callback - Called per item with (item, x, y, w, h)
   */
  function layoutTreemap(items, x, y, width, height, horizontal, callback) {
    if (!items.length || width <= 0 || height <= 0) return;

    if (items.length === 1) {
      callback(items[0], x, y, width, height);
      return;
    }

    // Dynamic orientation adjustment: split the longer side
    const isActuallyHorizontal = width >= height;

    const totalWeight = items.reduce((sum, item) => sum + item.weight, 0) || 1;
    let splitIndex = 1;
    let accumulatedWeight = 0;

    // Binary split with improved heuristic (find mid-point by weight)
    for (let index = 0; index < items.length - 1; index += 1) {
      const w = items[index].weight;
      if (accumulatedWeight + w > totalWeight / 2 && index > 0) {
        // Decide whether to include this item or not based on which gets closer to 50/50
        const diffWith = Math.abs(accumulatedWeight + w - totalWeight / 2);
        const diffWithout = Math.abs(accumulatedWeight - totalWeight / 2);
        if (diffWithout < diffWith) {
          splitIndex = index;
        } else {
          splitIndex = index + 1;
        }
        break;
      }
      accumulatedWeight += w;
      splitIndex = index + 1;
    }

    const firstWeight = items.slice(0, splitIndex).reduce((sum, item) => sum + item.weight, 0);
    const ratio = firstWeight / totalWeight;

    if (isActuallyHorizontal) {
      const splitWidth = width * ratio;
      layoutTreemap(
        items.slice(0, splitIndex),
        x,
        y,
        splitWidth,
        height,
        height >= splitWidth,
        callback,
      );
      layoutTreemap(
        items.slice(splitIndex),
        x + splitWidth,
        y,
        width - splitWidth,
        height,
        height >= width - splitWidth,
        callback,
      );
    } else {
      const splitHeight = height * ratio;
      layoutTreemap(
        items.slice(0, splitIndex),
        x,
        y,
        width,
        splitHeight,
        splitHeight >= width,
        callback,
      );
      layoutTreemap(
        items.slice(splitIndex),
        x,
        y + splitHeight,
        width,
        height - splitHeight,
        height - splitHeight >= width,
        callback,
      );
    }
  }

  function placeNode(stock, x, y, width, height, parent) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "heatmap-node";
    node.style.left = `${x}%`;
    node.style.top = `${y}%`;
    node.style.width = `${width}%`;
    node.style.height = `${height}%`;

    const changePercent = toFiniteNumber(stock.change_percent) || 0;
    const priceText = Number.isFinite(stock.price) ? formatNumber(stock.price) : "--";
    const changeText = `${changePercent > 0 ? "+" : ""}${changePercent.toFixed(2)}%`;

    node.style.backgroundColor = getColor(changePercent);
    node.style.setProperty(
      "--node-change",
      changePercent >= 0 ? "rgba(125, 255, 176, 0.95)" : "rgba(255, 125, 125, 0.95)",
    );
    node.setAttribute(
      "aria-label",
      `${stock.symbol || ""} ${stock.name || ""} ${priceText} ${changeText}`,
    );
    node.title = [
      stock.name || stock.symbol || "",
      `価格: ${priceText}`,
      `前日比: ${changeText}`,
      `セクター: ${stock.sector || "Other"}`,
      `時価総額: ${formatCompact(stock.market_cap)}`,
    ].join("\n");

    node.addEventListener("mouseenter", () => showTooltip(node, stock, changePercent));
    node.addEventListener("mousemove", (event) => moveTooltip(event));
    node.addEventListener("mouseleave", hideTooltip);
    node.addEventListener("focus", () => showTooltip(node, stock, changePercent));
    node.addEventListener("blur", hideTooltip);
    node.addEventListener("click", () => {
      if (stock.symbol) {
        window.location.href = `/main?q=${encodeURIComponent(stock.symbol)}`;
      }
    });

    if (width > 18 && height > 18) {
      const symbol = document.createElement("span");
      symbol.className = "node-symbol";
      symbol.textContent = stock.symbol || "";

      const change = document.createElement("span");
      change.className = "node-change";
      change.textContent = changeText;

      const name = document.createElement("span");
      name.className = "node-name";
      name.textContent = stock.name || "";

      node.append(symbol, change, name);
    }

    parent.appendChild(node);
  }

  function showTooltip(node, stock, changePercent) {
    if (!els.tooltip) return;
    const priceText = Number.isFinite(stock.price) ? formatNumber(stock.price) : "--";
    const changeText = `${changePercent > 0 ? "+" : ""}${changePercent.toFixed(2)}%`;
    const marketCap = formatCompact(stock.market_cap);

    els.tooltip.textContent = "";
    const strong = document.createElement("strong");
    strong.textContent = stock.symbol || "";
    const nameSpan = document.createElement("span");
    nameSpan.textContent = stock.name || "";
    const detail = document.createElement("small");
    detail.textContent = `価格: ${priceText} / 前日比: ${changeText} / 時価総額: ${marketCap}`;
    els.tooltip.append(strong, nameSpan, detail);
    els.tooltip.classList.add("show");
    els.tooltip.style.opacity = "1";
    els.tooltip.style.transform = "translateY(0)";
    node.classList.add("is-tooltip-open");
  }

  function moveTooltip(event) {
    if (!els.tooltip) return;
    const padding = 16;
    const tooltipRect = els.tooltip.getBoundingClientRect();
    let left = event.clientX + padding;
    let top = event.clientY + padding;

    if (left + tooltipRect.width > window.innerWidth - padding) {
      left = event.clientX - tooltipRect.width - padding;
    }
    if (top + tooltipRect.height > window.innerHeight - padding) {
      top = event.clientY - tooltipRect.height - padding;
    }

    els.tooltip.style.left = `${Math.max(padding, left)}px`;
    els.tooltip.style.top = `${Math.max(padding, top)}px`;
  }

  function hideTooltip() {
    if (!els.tooltip) return;
    els.tooltip.classList.remove("show");
    els.tooltip.style.opacity = "";
    els.tooltip.style.transform = "";
    els.canvas?.querySelectorAll(".heatmap-node.is-tooltip-open").forEach((node) => {
      node.classList.remove("is-tooltip-open");
    });
  }

  function getColor(value) {
    const limit = 3;
    const ratio = Math.min(Math.abs(value) / limit, 1);
    const base = [38, 50, 56];
    const positive = [0, 230, 118];
    const negative = [213, 0, 0];
    const target = value >= 0 ? positive : negative;
    const rgb = target.map((channel, index) =>
      Math.round(base[index] + (channel - base[index]) * ratio),
    );
    return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
  }

  function formatNumber(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "--";
    return num.toLocaleString("ja-JP", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function formatCompact(value) {
    if (!Number.isFinite(value) || value <= 0) return "--";
    return new Intl.NumberFormat("ja-JP", {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(value);
  }

  const _resizeHandler = () => hideTooltip();
  window.addEventListener("resize", _resizeHandler);

  document.addEventListener("beforeunload", () => {
    window.removeEventListener("resize", _resizeHandler);
    state.controller?.abort();
  });

  loadHeatmap();
});
