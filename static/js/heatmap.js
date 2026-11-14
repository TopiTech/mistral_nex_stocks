document.addEventListener("DOMContentLoaded", () => {
  const state = {
    currentMarket: "us",
    loading: false,
    controller: null,
    loadGeneration: 0,
    stockCount: 0,
    timeoutId: null,
    sizeMetric: "market_cap",
    rawStocks: [],
    lastErrorMessage: null,
    viewMode: "2d",
    three: {
      scene: null,
      camera: null,
      renderer: null,
      controls: null,
      stockMeshes: [],
      raycaster: null,
      mouse: null,
      hoveredMesh: null,
      isInit: false,
      animationFrameId: null,
      keyboardIndex: -1,
    },
  };

  const els = {
    canvas: document.getElementById("heatmap-canvas"),
    canvas3d: document.getElementById("heatmap-3d-canvas"),
    controls3d: document.getElementById("heatmap-3d-controls"),
    loading: document.getElementById("heatmap-loading"),
    updateTime: document.getElementById("update-time"),
    count: document.getElementById("heatmap-count"),
    tooltip: document.getElementById("heatmap-tooltip"),
    toggleUs: document.getElementById("toggle-us"),
    toggleJp: document.getElementById("toggle-jp"),
    view2d: document.getElementById("view-2d"),
    view3d: document.getElementById("view-3d"),
    camReset: document.getElementById("cam-reset"),
    camTop: document.getElementById("cam-top"),
    camIso: document.getElementById("cam-iso"),
    sizeMarketCap: document.getElementById("size-market-cap"),
    sizeVolume: document.getElementById("size-volume"),
    search: document.getElementById("heatmap-search"),
  };

  if (!els.canvas) return;

  const TREEMAP_SIZE = 1000;

  els.toggleUs?.addEventListener("click", () => switchMarket("us"));
  els.toggleJp?.addEventListener("click", () => switchMarket("jp"));
  els.view2d?.addEventListener("click", () => switchViewMode("2d"));
  els.view3d?.addEventListener("click", () => switchViewMode("3d"));
  els.camReset?.addEventListener("click", () => resetCamera("iso"));
  els.camTop?.addEventListener("click", () => resetCamera("top"));
  els.camIso?.addEventListener("click", () => resetCamera("iso"));
  els.sizeMarketCap?.addEventListener("click", () =>
    switchSizeMetric("market_cap"),
  );
  els.sizeVolume?.addEventListener("click", () => switchSizeMetric("volume"));
  els.search?.addEventListener("input", applySearchFilter);

  function setupButtonGroupKeyboardNav(buttons, onSelect) {
    const validButtons = buttons.filter(Boolean);
    validButtons.forEach((btn, index) => {
      btn.addEventListener("keydown", (event) => {
        if (event.isComposing || event.keyCode === 229) return;
        if (
          ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) ||
          event.key === "ArrowUp" ||
          event.key === "ArrowDown"
        ) {
          event.preventDefault();
          const direction =
            event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
          const nextIndex =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? validButtons.length - 1
                : (index + direction + validButtons.length) %
                  validButtons.length;
          const nextBtn = validButtons[nextIndex];
          validButtons.forEach((b, i) => {
            b.setAttribute("tabindex", i === nextIndex ? "0" : "-1");
          });
          nextBtn?.focus();
          onSelect?.(nextBtn, nextIndex);
        }
      });
    });
  }

  // Initialize roving tabindex for heatmap button groups
  [
    [els.toggleUs, els.toggleJp],
    [els.view2d, els.view3d],
    [els.sizeMarketCap, els.sizeVolume],
    [els.camReset, els.camTop, els.camIso],
  ].forEach((group) => {
    const valid = group.filter(Boolean);
    const hasActive = valid.some((b) => b.classList.contains("active"));
    valid.forEach((b, i) => {
      const isActive = hasActive ? b.classList.contains("active") : i === 0;
      b.setAttribute("tabindex", isActive ? "0" : "-1");
    });
  });

  setupButtonGroupKeyboardNav([els.toggleUs, els.toggleJp], (btn) => {
    if (btn === els.toggleUs) switchMarket("us");
    else if (btn === els.toggleJp) switchMarket("jp");
  });
  setupButtonGroupKeyboardNav([els.view2d, els.view3d], (btn) => {
    if (btn === els.view2d) switchViewMode("2d");
    else if (btn === els.view3d) switchViewMode("3d");
  });
  setupButtonGroupKeyboardNav([els.sizeMarketCap, els.sizeVolume], (btn) => {
    if (btn === els.sizeMarketCap) switchSizeMetric("market_cap");
    else if (btn === els.sizeVolume) switchSizeMetric("volume");
  });
  setupButtonGroupKeyboardNav([els.camReset, els.camTop, els.camIso], (btn) => {
    btn?.click();
  });

  // トグル再描画が現在表示中の市場と異なる市場のデータを描画しないための
  // データセット有効性チェック。読み込み失敗/切替途中の旧データを描画しない。
  function hasRenderableStocks() {
    return Boolean(
      state.rawStocks &&
      state.rawStocks.length &&
      state.rawStocksMarket === state.currentMarket,
    );
  }

  function switchMarket(market) {
    if (state.currentMarket === market) return;
    state.currentMarket = market;
    els.toggleUs?.classList.toggle("active", market === "us");
    els.toggleJp?.classList.toggle("active", market === "jp");
    els.toggleUs?.setAttribute("aria-pressed", String(market === "us"));
    els.toggleJp?.setAttribute("aria-pressed", String(market === "jp"));
    els.toggleUs?.setAttribute("tabindex", market === "us" ? "0" : "-1");
    els.toggleJp?.setAttribute("tabindex", market === "jp" ? "0" : "-1");
    if (els.search) els.search.value = "";
    loadHeatmap();
  }

  // init3DScene は #heatmap-3d-canvas が `hidden`（display:none）のまま実行される
  // ため clientWidth/clientHeight が 0 になり、フォールバックの 1000x600 で
  // レンダラとカメラが固定される。さらに 2D 表示中のリサイズは resize ハンドラが
  // 3D 時のみ寸法を更新するため反映されない。表示後に実寸へ同期する。
  function sync3DViewportSize() {
    const { renderer, camera } = state.three;
    if (!renderer || !camera || !els.canvas3d) return false;
    const width = els.canvas3d.clientWidth;
    const height = els.canvas3d.clientHeight;
    if (!(width > 0 && height > 0)) return false;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
    return true;
  }

  function switchViewMode(mode) {
    if (state.viewMode === mode) return;

    if (mode === "3d") {
      if (!els.canvas3d || typeof THREE === "undefined") {
        // (L21, 2026-11-07 review) #heatmap-3d-canvas carries `hidden` on
        // these paths, so writing the message there gave the user NO
        // feedback while the 3D button appeared to do nothing. Deliver to a
        // visible channel instead; the 2D fallback state is unchanged.
        const target = document.getElementById("view-mode-toggle-container");
        const channel = target || document.body;
        if (!channel.querySelector(".heatmap-3d-error")) {
          const msg = document.createElement("div");
          msg.className = "heatmap-3d-error";
          msg.setAttribute("role", "alert");
          msg.style.gridColumn = "1 / -1";
          msg.textContent =
            "3D表示を読み込めません。2D表示を使用してください。";
          channel.appendChild(msg);
        }
        return;
      }
      if (!state.three.isInit) {
        try {
          if (!init3DScene()) return;
        } catch (error) {
          console.error("3D heatmap initialization failed:", error);
          state.three.isInit = false;
          // (L21) same rationale: deliver the failure to a visible channel.
          const target = document.getElementById("view-mode-toggle-container");
          const channel = target || document.body;
          if (!channel.querySelector(".heatmap-3d-error")) {
            const msg = document.createElement("div");
            msg.className = "heatmap-3d-error";
            msg.setAttribute("role", "alert");
            msg.style.gridColumn = "1 / -1";
            msg.textContent =
              "3D表示を初期化できません。2D表示を使用してください。";
            channel.appendChild(msg);
          }
          return;
        }
      }
      state.viewMode = mode;
      els.view2d?.classList.toggle("active", false);
      els.view3d?.classList.toggle("active", true);
      els.view2d?.setAttribute("aria-pressed", "false");
      els.view3d?.setAttribute("aria-pressed", "true");
      els.view2d?.setAttribute("tabindex", "-1");
      els.view3d?.setAttribute("tabindex", "0");
      els.canvas?.classList.add("hidden");
      els.canvas3d?.classList.remove("hidden");
      els.controls3d?.classList.remove("hidden");
      sync3DViewportSize();
      start3DAnimation();
      if (hasRenderableStocks()) {
        const normalized = state.rawStocks
          .map(normalizeStock)
          .filter((stock) => stock.size > 0);
        render3DHeatmap(normalized);
      } else if (state.lastErrorMessage) {
        showError(state.lastErrorMessage);
      }
    } else {
      state.viewMode = mode;
      els.view2d?.classList.toggle("active", true);
      els.view3d?.classList.toggle("active", false);
      els.view2d?.setAttribute("aria-pressed", "true");
      els.view3d?.setAttribute("aria-pressed", "false");
      els.view2d?.setAttribute("tabindex", "0");
      els.view3d?.setAttribute("tabindex", "-1");
      stop3DAnimation();
      els.canvas?.classList.remove("hidden");
      els.canvas3d?.classList.add("hidden");
      els.controls3d?.classList.add("hidden");
      if (hasRenderableStocks()) {
        const normalized = state.rawStocks
          .map(normalizeStock)
          .filter((stock) => stock.size > 0);
        renderHeatmap(normalized);
      } else if (state.lastErrorMessage) {
        showError(state.lastErrorMessage);
      }
    }
  }

  function resetCamera(type = "iso") {
    if (!state.three.controls || !state.three.camera) return;
    if (type === "top") {
      state.three.camera.position.set(0, 140, 0.1);
      state.three.controls.target.set(0, 0, 0);
    } else {
      state.three.camera.position.set(0, 85, 95);
      state.three.controls.target.set(0, 0, 0);
    }
    state.three.controls.update();
  }

  function switchSizeMetric(metric) {
    if (state.sizeMetric === metric) return;
    state.sizeMetric = metric;
    els.sizeMarketCap?.classList.toggle("active", metric === "market_cap");
    els.sizeVolume?.classList.toggle("active", metric === "volume");
    els.sizeMarketCap?.setAttribute(
      "aria-pressed",
      String(metric === "market_cap"),
    );
    els.sizeVolume?.setAttribute("aria-pressed", String(metric === "volume"));
    els.sizeMarketCap?.setAttribute(
      "tabindex",
      metric === "market_cap" ? "0" : "-1",
    );
    els.sizeVolume?.setAttribute("tabindex", metric === "volume" ? "0" : "-1");

    if (hasRenderableStocks()) {
      const normalized = state.rawStocks
        .map(normalizeStock)
        .filter((stock) => stock.size > 0);
      if (state.viewMode === "3d") {
        render3DHeatmap(normalized);
      } else {
        renderHeatmap(normalized);
      }
    }
  }

  function applySearchFilter() {
    const query = els.search?.value.toLowerCase().trim() || "";
    const nodes = els.canvas?.querySelectorAll(".heatmap-node");
    if (nodes) {
      nodes.forEach((node) => {
        const label = node.getAttribute("aria-label")?.toLowerCase() || "";
        const title = node.title?.toLowerCase() || "";
        const matches = label.includes(query) || title.includes(query);
        const dimmed = query.length > 0 && !matches;
        node.classList.toggle("is-dimmed", dimmed);
        node.setAttribute("aria-hidden", String(dimmed));
        if (dimmed) {
          // フォーカス中のノードがaria-hiddenに落入るとスクリーンリーダーが
          // 非表示要素を読み上げ続けるため、フォーカスを検索入力へ退避する。
          if (node.contains(document.activeElement)) {
            els.search?.focus();
          }
          node.setAttribute("tabindex", "-1");
          node.setAttribute("aria-disabled", "true");
        } else {
          node.removeAttribute("tabindex");
          node.removeAttribute("aria-disabled");
        }
      });
    }

    if (state.three.stockMeshes && state.three.stockMeshes.length) {
      state.three.stockMeshes.forEach((mesh) => {
        const stockData = mesh.userData?.stock;
        if (!stockData) return;
        const symbol = (stockData.symbol || "").toLowerCase();
        const name = (stockData.name || "").toLowerCase();
        const sector = (stockData.sector || "").toLowerCase();
        const matches =
          !query ||
          symbol.includes(query) ||
          name.includes(query) ||
          sector.includes(query);

        if (mesh.material) {
          mesh.material.opacity = matches ? 0.9 : 0.15;
          mesh.material.transparent = true;
        }
      });
    }
  }

  function setLoading(isLoading) {
    state.loading = isLoading;
    els.loading?.classList.toggle("show", isLoading);
    els.loading?.setAttribute("aria-hidden", String(!isLoading));
    if (els.canvas) {
      els.canvas.classList.toggle("is-loading", isLoading);
    }
  }

  function buildErrorNode(message) {
    const error = document.createElement("div");
    error.className = "heatmap-error-state";
    error.setAttribute("role", "alert");
    const icon = document.createElement("div");
    icon.className = "heatmap-error-icon";
    icon.textContent = "!";
    const strong = document.createElement("strong");
    strong.textContent = message;
    const span = document.createElement("span");
    span.textContent =
      "市場が休場中、またはデータ取得に時間がかかっています。しばらくしてから再試行してください。";
    error.append(icon, strong, span);
    return error;
  }

  function showError(message) {
    state.lastErrorMessage = message;
    // 3D表示中は2Dキャンバスは display:none のため、エラーは表示中の
    // コンテナ(3Dキャンバス)へ描画しないとユーザーに伝わらない。
    if (state.viewMode === "3d") {
      if (!els.canvas3d) return;
      els.canvas3d
        .querySelectorAll(".heatmap-error-state")
        .forEach((node) => node.remove());
      els.canvas3d.appendChild(buildErrorNode(message));
      return;
    }
    if (!els.canvas) return;
    els.canvas.textContent = "";
    els.canvas.appendChild(buildErrorNode(message));
  }

  // escapeHtmlはutils.jsで定義済み（全ページ共通）

  // heatmap固有: 不正値はNaNで返す（utils.jsのtoFiniteNumberは0を返す）。
  // グローバルの toFiniteNumber をシャドウしないよう別名を付ける。
  function toFiniteOrNan(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : NaN;
  }

  function normalizeStock(stock) {
    const price = toFiniteOrNan(stock.price);
    const changePercent = toFiniteOrNan(stock.change_percent);
    const volume = toFiniteOrNan(stock.volume) || 0;
    const rawMarketCap = toFiniteOrNan(stock.market_cap);
    const safePrice = Number.isFinite(price) ? price : 1;
    const safeVolume = Number.isFinite(volume) ? volume : 0;
    const fallbackSize = Math.max(safePrice, 1) * Math.max(safeVolume, 1);

    // 出来高モードでもvolume欠落行は価格の代理値ではなく時価総額
    // (なければ従来どおりfallbackSize)でサイズする。出来高提供環境では
    // 従来と同じく volume * price を使う。
    const size =
      state.sizeMetric === "volume" && volume > 0
        ? volume * Math.max(safePrice, 1)
        : Number.isFinite(rawMarketCap) && rawMarketCap > 0
          ? rawMarketCap
          : Number.isFinite(fallbackSize)
            ? fallbackSize
            : 1;

    return {
      ...stock,
      price,
      change_percent: Number.isFinite(changePercent) ? changePercent : 0,
      volume,
      market_cap:
        Number.isFinite(rawMarketCap) && rawMarketCap > 0 ? rawMarketCap : 0,
      size,
    };
  }

  async function loadHeatmap(isRetry = false, expectedGeneration = null) {
    if (isRetry && expectedGeneration !== state.loadGeneration) return;

    const requestGeneration = isRetry
      ? state.loadGeneration
      : state.loadGeneration + 1;
    if (!isRetry) {
      state.loadGeneration = requestGeneration;
      if (state.timeoutId) {
        clearTimeout(state.timeoutId);
        state.timeoutId = null;
      }
      state.pollRetries = 0;
      // 前の市場のデータを新しい市場の読み込み中に再表示しないよう無効化する
      // (失敗/中断後のトグル操作が旧市場のタイルを再描画するのを防ぐ)。
      state.rawStocks = [];
      state.rawStocksMarket = null;
      state.lastErrorMessage = null;
      // 新規ロードのみキャンバスをクリアしてローディング表示
      setLoading(true);
      if (els.canvas) els.canvas.textContent = "";
      els.canvas3d
        ?.querySelectorAll(".heatmap-error-state")
        .forEach((node) => node.remove());
      if (els.updateTime) els.updateTime.textContent = "-";
      if (els.count) els.count.textContent = "--";
    }

    // ポーリングリトライ時も含め、前の要求が後発要求のUIを変更できないよう
    // controller、世代、市場をこの要求に固定する。
    state.controller?.abort();
    const controller = new AbortController();
    state.controller = controller;
    const requestMarket = state.currentMarket;
    const isCurrentRequest = () =>
      state.loadGeneration === requestGeneration &&
      state.controller === controller &&
      state.currentMarket === requestMarket;

    let isPolling = false;
    try {
      const { data } = await apiFetch(
        `/api/heatmap?market=${encodeURIComponent(requestMarket)}`,
        { signal: controller.signal },
        { showToast: false },
      );
      if (!isCurrentRequest()) return;
      if (data && data.fetching) {
        // バックエンドで非同期取得中。数秒待って再試行する。
        state.pollRetries = (state.pollRetries || 0) + 1;
        const maxRetries = 15; // 15 retries * 3000ms = 45 seconds total timeout
        if (state.pollRetries <= maxRetries) {
          isPolling = true;
          const retryTimerId = setTimeout(() => {
            if (state.timeoutId !== retryTimerId || !isCurrentRequest()) return;
            state.timeoutId = null;
            loadHeatmap(true, requestGeneration);
          }, 3000);
          state.timeoutId = retryTimerId;
          return;
        }
        showError(
          "ヒートマップデータの取得に時間がかかっています。再度お試しください。",
        );
        setLoading(false);
        return;
      }
      const stocks = Array.isArray(data.stocks) ? data.stocks : [];
      // ヒートマップAPIの行はmarketを含まないことがある(後方互換のため
      // バックエンドが提供する場合はそちらを優先)。書式整形
      // (formatNumber/formatCompact)が正しい通貨記号を使うよう要求市場で補完する。
      stocks.forEach((row) => {
        if (!row || typeof row !== "object") return;
        if (row.market !== "us" && row.market !== "jp") {
          row.market = requestMarket;
        }
      });
      state.rawStocks = stocks;
      state.rawStocksMarket = requestMarket;
      const normalized = stocks
        .map(normalizeStock)
        .filter((stock) => stock.size > 0);

      if (!normalized.length) {
        showError("表示できる銘柄データがありませんでした");
        setLoading(false);
        return;
      }

      state.lastErrorMessage = null;
      els.canvas3d
        ?.querySelectorAll(".heatmap-error-state")
        .forEach((node) => node.remove());
      state.stockCount = normalized.length;
      if (state.viewMode === "3d") {
        render3DHeatmap(normalized);
      } else {
        renderHeatmap(normalized);
      }
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
      if (!isCurrentRequest()) return;
      if (typeof logger !== "undefined" && logger.error) {
        logger.error("Heatmap fetch error:", err);
      } else {
        console.warn("Heatmap fetch error:", err);
      }
      showError("市場データの取得に失敗しました");
    } finally {
      if (!isPolling && isCurrentRequest()) {
        setLoading(false);
      }
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
    applySearchFilter();
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

    layoutTreemap(
      stockItems,
      0,
      0,
      100,
      100,
      width >= height,
      (stock, sx, sy, sw, sh) => placeNode(stock, sx, sy, sw, sh, group),
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

    const firstWeight = items
      .slice(0, splitIndex)
      .reduce((sum, item) => sum + item.weight, 0);
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
    const priceText = formatNumber(stock.price, stock.market);
    const changeText = `${changePercent > 0 ? "+" : ""}${changePercent.toFixed(2)}%`;

    node.style.backgroundColor = getColor(changePercent);
    const isJpTheme =
      typeof getColorSchemePreference === "function" &&
      getColorSchemePreference() === "jp_standard";
    const posGlow = isJpTheme
      ? "rgba(255, 125, 125, 0.95)"
      : "rgba(125, 255, 176, 0.95)";
    const negGlow = isJpTheme
      ? "rgba(125, 255, 176, 0.95)"
      : "rgba(255, 125, 125, 0.95)";

    node.style.setProperty(
      "--node-change",
      changePercent >= 0 ? posGlow : negGlow,
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
      `時価総額: ${formatCompact(stock.market_cap, stock.market)}`,
    ].join("\n");

    node.addEventListener("mouseenter", () =>
      showTooltip(node, stock, changePercent),
    );
    node.addEventListener("mousemove", (event) => moveTooltip(event));
    node.addEventListener("mouseleave", hideTooltip);
    node.addEventListener("focus", () =>
      showTooltip(node, stock, changePercent),
    );
    node.addEventListener("blur", hideTooltip);
    node.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        node.click();
      }
    });
    node.addEventListener("click", () => {
      if (stock.symbol) {
        const market = stock.market || state.currentMarket || "";
        const marketPart = market
          ? `&market=${encodeURIComponent(market)}`
          : "";
        window.location.href = `/main?q=${encodeURIComponent(stock.symbol)}${marketPart}`;
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

  // moveTooltipはmousemove毎にgetBoundingClientRectを呼ぶと読み書き競合で
  // レイアウト再計算を強制するため、ツールチップ表示時とウィンドウサイズ
  // 変更時に寸法をキャッシュしてイベント毎の測定を避ける。
  let tooltipSizeCache = null;
  let viewportSizeCache = {
    width: window.innerWidth,
    height: window.innerHeight,
  };

  function positionTooltipAt(clientX, clientY) {
    if (!els.tooltip) return;
    const padding = 16;
    const tooltipRect = tooltipSizeCache || els.tooltip.getBoundingClientRect();
    if (!tooltipSizeCache) {
      tooltipSizeCache = tooltipRect;
    }
    const viewWidth = viewportSizeCache.width || window.innerWidth;
    const viewHeight = viewportSizeCache.height || window.innerHeight;
    let left = clientX + padding;
    let top = clientY + padding;

    if (left + tooltipRect.width > viewWidth - padding) {
      left = clientX - tooltipRect.width - padding;
    }
    if (top + tooltipRect.height > viewHeight - padding) {
      top = clientY - tooltipRect.height - padding;
    }

    els.tooltip.style.left = `${Math.max(padding, left)}px`;
    els.tooltip.style.top = `${Math.max(padding, top)}px`;
  }

  function showTooltip(node, stock, changePercent) {
    if (!els.tooltip) return;
    const priceText = formatNumber(stock.price, stock.market);
    const changeText = `${changePercent > 0 ? "+" : ""}${changePercent.toFixed(2)}%`;
    const marketCap = formatCompact(stock.market_cap, stock.market);

    els.tooltip.textContent = "";
    const strong = document.createElement("strong");
    strong.textContent = stock.symbol || "";
    const nameSpan = document.createElement("span");
    nameSpan.textContent = stock.name || "";
    const detail = document.createElement("small");
    detail.textContent = `価格: ${priceText} / 前日比: ${changeText} / 時価総額: ${marketCap}`;
    els.tooltip.append(strong, nameSpan, detail);
    els.tooltip.classList.add("show");
    tooltipSizeCache = els.tooltip.getBoundingClientRect();
    if (node && node.classList) {
      node.classList.add("is-tooltip-open");
    }

    let anchorX = null;
    let anchorY = null;
    if (
      state.viewMode === "3d" &&
      state.three.renderer &&
      node === state.three.renderer.domElement &&
      typeof node.getBoundingClientRect === "function"
    ) {
      const rect = node.getBoundingClientRect();
      const hovered = state.three.hoveredMesh;
      const camera = state.three.camera;
      if (
        hovered?.position &&
        typeof hovered.position.clone === "function" &&
        camera
      ) {
        const projected = hovered.position.clone().project(camera);
        if (Number.isFinite(projected.x) && Number.isFinite(projected.y)) {
          anchorX = rect.left + (projected.x * 0.5 + 0.5) * rect.width;
          anchorY = rect.top + (-projected.y * 0.5 + 0.5) * rect.height;
        }
      }
      if (anchorX === null || anchorY === null) {
        anchorX = rect.left + rect.width / 2;
        anchorY = rect.top + rect.height / 2;
      }
    } else if (node && typeof node.getBoundingClientRect === "function") {
      const rect = node.getBoundingClientRect();
      if (
        rect.width > 0 ||
        rect.height > 0 ||
        rect.left !== 0 ||
        rect.top !== 0
      ) {
        anchorX = rect.left + rect.width / 2;
        anchorY = rect.top + rect.height / 2;
      }
    }
    if (Number.isFinite(anchorX) && Number.isFinite(anchorY)) {
      positionTooltipAt(anchorX, anchorY);
    }
  }

  function moveTooltip(event) {
    if (!els.tooltip) return;
    positionTooltipAt(event.clientX, event.clientY);
  }

  function hideTooltip() {
    tooltipSizeCache = null;
    if (!els.tooltip) return;
    els.tooltip.classList.remove("show");
    els.canvas
      ?.querySelectorAll(".heatmap-node.is-tooltip-open")
      .forEach((node) => {
        node.classList.remove("is-tooltip-open");
      });
  }

  /* --- 3D Scene Initialization & Rendering --- */
  function init3DScene() {
    if (!els.canvas3d || typeof THREE === "undefined") return false;

    const width = els.canvas3d.clientWidth || 1000;
    const height = els.canvas3d.clientHeight || 600;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0a0d14);
    scene.fog = new THREE.FogExp2(0x0a0d14, 0.0035);

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
    camera.position.set(0, 85, 95);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    els.canvas3d.replaceChildren();
    els.canvas3d.appendChild(renderer.domElement);
    renderer.domElement.tabIndex = 0;
    renderer.domElement.setAttribute("role", "application");
    renderer.domElement.setAttribute(
      "aria-label",
      "3D株式ヒートマップ。左右矢印で銘柄を選択し、Enterで詳細を開きます。",
    );

    let controls = null;
    if (typeof THREE.OrbitControls !== "undefined") {
      controls = new THREE.OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;
      controls.dampingFactor = 0.05;
      controls.maxPolarAngle = Math.PI / 2 - 0.05;
      controls.minDistance = 20;
      controls.maxDistance = 250;
    }

    // ライティング
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.65);
    scene.add(ambientLight);

    const dirLight1 = new THREE.DirectionalLight(0x00f2fe, 0.9);
    dirLight1.position.set(40, 80, 50);
    dirLight1.castShadow = true;
    scene.add(dirLight1);

    const dirLight2 = new THREE.DirectionalLight(0xc28bff, 0.5);
    dirLight2.position.set(-50, 40, -40);
    scene.add(dirLight2);

    // グリッド
    const gridHelper = new THREE.GridHelper(120, 30, 0x00f2fe, 0x1f293d);
    gridHelper.position.y = -0.1;
    scene.add(gridHelper);

    const raycaster = new THREE.Raycaster();
    const mouse = new THREE.Vector2();

    state.three.scene = scene;
    state.three.camera = camera;
    state.three.renderer = renderer;
    state.three.controls = controls;
    state.three.raycaster = raycaster;
    state.three.mouse = mouse;

    // Raycasting Events
    const onMouseMove = (event) => {
      if (state.viewMode !== "3d") return;
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

      raycaster.setFromCamera(mouse, camera);
      const intersects = raycaster.intersectObjects(state.three.stockMeshes);

      if (intersects.length > 0) {
        const hit = intersects[0].object;
        if (state.three.hoveredMesh !== hit) {
          if (state.three.hoveredMesh && state.three.hoveredMesh.material) {
            state.three.hoveredMesh.material.emissive.setHex(
              state.three.hoveredMesh.userData.origEmissive || 0x000000,
            );
          }
          state.three.hoveredMesh = hit;
          if (hit.material) {
            hit.userData.origEmissive = hit.material.emissive.getHex();
            hit.material.emissive.setHex(0x00f2fe);
          }
          const stock = hit.userData.stock;
          if (stock) {
            showTooltip(renderer.domElement, stock, stock.change_percent);
          }
        }
        moveTooltip(event);
      } else {
        if (state.three.hoveredMesh) {
          if (state.three.hoveredMesh.material) {
            state.three.hoveredMesh.material.emissive.setHex(
              state.three.hoveredMesh.userData.origEmissive || 0x000000,
            );
          }
          state.three.hoveredMesh = null;
          hideTooltip();
        }
      }
    };

    const onClick = () => {
      if (state.viewMode !== "3d" || !state.three.hoveredMesh) return;
      const stock = state.three.hoveredMesh.userData?.stock;
      if (stock && stock.symbol) {
        const market = stock.market || state.currentMarket || "";
        const marketPart = market
          ? `&market=${encodeURIComponent(market)}`
          : "";
        window.location.href = `/main?q=${encodeURIComponent(stock.symbol)}${marketPart}`;
      }
    };

    const selectKeyboardMesh = (index) => {
      const meshes = state.three.stockMeshes;
      if (!meshes.length) return;
      state.three.keyboardIndex = (index + meshes.length) % meshes.length;
      state.three.hoveredMesh = meshes[state.three.keyboardIndex];
      const stock = state.three.hoveredMesh.userData?.stock;
      if (stock) {
        renderer.domElement.setAttribute(
          "aria-label",
          `${stock.symbol} ${stock.name || ""} ${Number(stock.change_percent || 0).toFixed(2)}%。左右矢印で選択、Enterで詳細。`,
        );
        showTooltip(renderer.domElement, stock, stock.change_percent || 0);
      }
    };

    const onKeyDown = (event) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (state.viewMode !== "3d") return;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        event.preventDefault();
        selectKeyboardMesh(state.three.keyboardIndex + 1);
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        event.preventDefault();
        selectKeyboardMesh(state.three.keyboardIndex - 1);
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onClick();
      }
    };

    renderer.domElement.addEventListener("mousemove", onMouseMove);
    renderer.domElement.addEventListener("click", onClick);
    renderer.domElement.addEventListener("keydown", onKeyDown);
    renderer.domElement.addEventListener("mouseleave", hideTooltip);
    renderer.domElement.addEventListener("blur", hideTooltip);

    start3DAnimation();
    // 初期化が完了してから成功フラグを立てる。途中で例外が発生した場合
    // (WebGLコンテキスト取得失敗など)は呼び出し側のcatchでisInit=falseに
    // 戻し、次回再試行できるようにする。
    state.three.isInit = true;
    return true;
  }

  function start3DAnimation() {
    if (
      state.viewMode !== "3d" ||
      state.three.animationFrameId !== null ||
      !state.three.renderer ||
      !state.three.scene ||
      !state.three.camera
    ) {
      return;
    }

    const animate = () => {
      if (state.viewMode !== "3d") {
        state.three.animationFrameId = null;
        return;
      }
      state.three.animationFrameId = requestAnimationFrame(animate);
      state.three.controls?.update();
      state.three.renderer.render(state.three.scene, state.three.camera);
    };
    animate();
  }

  function stop3DAnimation() {
    if (state.three.animationFrameId !== null) {
      cancelAnimationFrame(state.three.animationFrameId);
      state.three.animationFrameId = null;
    }
  }

  function disposeObject(obj) {
    if (!obj) return;
    if (obj.children && obj.children.length > 0) {
      [...obj.children].forEach((child) => {
        disposeObject(child);
        obj.remove(child);
      });
    }
    if (obj.geometry) {
      obj.geometry.dispose();
    }
    if (obj.material) {
      if (Array.isArray(obj.material)) {
        obj.material.forEach((mat) => mat?.dispose());
      } else {
        obj.material.dispose();
      }
    }
  }

  function render3DHeatmap(stocks) {
    if (!state.three.isInit) {
      try {
        if (!init3DScene()) return;
      } catch (error) {
        console.error("3D heatmap initialization failed:", error);
        state.three.isInit = false;
        return;
      }
    }
    const { scene } = state.three;
    if (!scene || typeof THREE === "undefined") return;
    // 描画成功時は以前のエラー表示を除去する
    els.canvas3d
      ?.querySelectorAll(".heatmap-error-state")
      .forEach((node) => node.remove());

    if (state.three.stockMeshes) {
      state.three.stockMeshes.forEach((mesh) => {
        scene.remove(mesh);
        disposeObject(mesh);
      });
    }
    state.three.stockMeshes = [];
    state.three.hoveredMesh = null;
    state.three.keyboardIndex = -1;

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

    const layoutNodes = [];
    layoutTreemap(
      sectorItems,
      0,
      0,
      TREEMAP_SIZE,
      TREEMAP_SIZE,
      true,
      (sector, sx, sy, sw, sh) => {
        const stockItems = sector.stocks
          .map((stock) => ({ ...stock, weight: stock.size / sector.size }))
          .sort((a, b) => b.weight - a.weight);
        layoutTreemap(
          stockItems,
          0,
          0,
          100,
          100,
          sw >= sh,
          (stock, nx, ny, nw, nh) => {
            const absX = sx + (nx / 100) * sw;
            const absY = sy + (ny / 100) * sh;
            const absW = (nw / 100) * sw;
            const absH = (nh / 100) * sh;
            layoutNodes.push({ stock, x: absX, y: absY, w: absW, h: absH });
          },
        );
      },
    );

    layoutNodes.forEach(({ stock, x, y, w, h }) => {
      const worldW = Math.max((w / TREEMAP_SIZE) * 100 * 0.94, 0.8);
      const worldD = Math.max((h / TREEMAP_SIZE) * 100 * 0.94, 0.8);
      const posX = ((x + w / 2) / TREEMAP_SIZE) * 100 - 50;
      const posZ = ((y + h / 2) / TREEMAP_SIZE) * 100 - 50;

      const change = stock.change_percent || 0;
      const baseHeight = 1.5;
      const heightFactor =
        change >= 0 ? Math.min(change * 3.8, 32) : Math.max(change * 0.8, -3);
      const buildingHeight = Math.max(baseHeight + heightFactor, 0.6);
      const posY = buildingHeight / 2;

      const geometry = new THREE.BoxGeometry(worldW, buildingHeight, worldD);

      const isJpTheme =
        typeof getColorSchemePreference === "function" &&
        getColorSchemePreference() === "jp_standard";

      const greenColorStart = 0x0f5236;
      const greenColorEnd = 0x00ff87;
      const greenEmissiveEnd = 0x004d26;

      const redColorStart = 0x5c151b;
      const redColorEnd = 0xff4e50;
      const redEmissiveEnd = 0x4d000a;

      const posStart = isJpTheme ? redColorStart : greenColorStart;
      const posEnd = isJpTheme ? redColorEnd : greenColorEnd;
      const posEmissive = isJpTheme ? redEmissiveEnd : greenEmissiveEnd;

      const negStart = isJpTheme ? greenColorStart : redColorStart;
      const negEnd = isJpTheme ? greenColorEnd : redColorEnd;
      const negEmissive = isJpTheme ? greenEmissiveEnd : redEmissiveEnd;

      let colorHex = 0x1e293b;
      let emissiveHex = 0x000000;
      if (change > 0) {
        const ratio = Math.min(change / 3, 1);
        colorHex = new THREE.Color()
          .lerpColors(new THREE.Color(posStart), new THREE.Color(posEnd), ratio)
          .getHex();
        emissiveHex = new THREE.Color()
          .lerpColors(
            new THREE.Color(0x000000),
            new THREE.Color(posEmissive),
            ratio,
          )
          .getHex();
      } else if (change < 0) {
        const ratio = Math.min(Math.abs(change) / 3, 1);
        colorHex = new THREE.Color()
          .lerpColors(new THREE.Color(negStart), new THREE.Color(negEnd), ratio)
          .getHex();
        emissiveHex = new THREE.Color()
          .lerpColors(
            new THREE.Color(0x000000),
            new THREE.Color(negEmissive),
            ratio,
          )
          .getHex();
      }

      const material = new THREE.MeshStandardMaterial({
        color: colorHex,
        emissive: emissiveHex,
        roughness: 0.2,
        metalness: 0.5,
        transparent: true,
        opacity: 0.94,
      });

      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(posX, posY, posZ);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      mesh.userData = { stock };

      const edges = new THREE.EdgesGeometry(geometry);
      const wireColor =
        change >= 0
          ? isJpTheme
            ? 0xff4e50
            : 0x00f2fe
          : isJpTheme
            ? 0x00f2fe
            : 0xff4e50;
      const lineMat = new THREE.LineBasicMaterial({
        color: wireColor,
        linewidth: 1,
        transparent: true,
        opacity: 0.45,
      });
      const wireframe = new THREE.LineSegments(edges, lineMat);
      mesh.add(wireframe);

      scene.add(mesh);
      state.three.stockMeshes.push(mesh);
    });

    applySearchFilter();
  }

  function getColor(value) {
    const limit = 3;
    const ratio = Math.min(Math.abs(value) / limit, 1);
    const base = [38, 50, 56];
    const green = [0, 230, 118];
    const red = [213, 0, 0];
    const isJpTheme =
      typeof getColorSchemePreference === "function" &&
      getColorSchemePreference() === "jp_standard";
    const positive = isJpTheme ? red : green;
    const negative = isJpTheme ? green : red;

    const target = value >= 0 ? positive : negative;
    const rgb = target.map((channel, index) =>
      Math.round(base[index] + (channel - base[index]) * ratio),
    );
    return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
  }

  function formatNumber(value, market) {
    if (value == null || typeof value === "boolean" || value === "")
      return "--";
    const num = Number(value);
    if (!Number.isFinite(num)) return "--";
    const symbol = market === "jp" ? "¥" : "$";
    const decimals = market === "jp" ? 0 : 2;
    return `${symbol}${num.toLocaleString("ja-JP", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })}`;
  }

  function formatCompact(value, market) {
    const num = Number(value);
    if (typeof value === "boolean" || !Number.isFinite(num) || num <= 0)
      return "--";
    if (market === "jp") {
      if (num >= 1e12) return `¥${(num / 1e12).toFixed(1)}兆`;
      if (num >= 1e8) return `¥${(num / 1e8).toFixed(0)}億`;
      return `¥${num.toLocaleString("ja-JP")}`;
    }
    if (num >= 1e12) return `$${(num / 1e12).toFixed(2)}T`;
    if (num >= 1e9) return `$${(num / 1e9).toFixed(2)}B`;
    if (num >= 1e6) return `$${(num / 1e6).toFixed(1)}M`;
    return `$${num.toLocaleString()}`;
  }

  const _resizeHandler = () => {
    viewportSizeCache = {
      width: window.innerWidth,
      height: window.innerHeight,
    };
    hideTooltip();
    if (state.viewMode === "3d") {
      sync3DViewportSize();
    }
  };
  window.addEventListener("resize", _resizeHandler);

  window.addEventListener("beforeunload", () => {
    window.removeEventListener("resize", _resizeHandler);
    stop3DAnimation();
    if (state.timeoutId) {
      clearTimeout(state.timeoutId);
    }
    state.controller?.abort();
    state.three.controls?.dispose?.();
    if (state.three.stockMeshes) {
      state.three.stockMeshes.forEach((mesh) => {
        if (state.three.scene) state.three.scene.remove(mesh);
        disposeObject(mesh);
      });
      state.three.stockMeshes = [];
    }
    if (state.three.renderer) {
      state.three.renderer.dispose();
    }
  });

  loadHeatmap();
});
