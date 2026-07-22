document.addEventListener("DOMContentLoaded", () => {
  const state = {
    currentMarket: "us",
    loading: false,
    controller: null,
    stockCount: 0,
    timeoutId: null,
    sizeMetric: "market_cap",
    rawStocks: [],
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

  function switchMarket(market) {
    if (state.currentMarket === market) return;
    state.currentMarket = market;
    els.toggleUs?.classList.toggle("active", market === "us");
    els.toggleJp?.classList.toggle("active", market === "jp");
    els.toggleUs?.setAttribute("aria-pressed", String(market === "us"));
    els.toggleJp?.setAttribute("aria-pressed", String(market === "jp"));
    if (els.search) els.search.value = "";
    loadHeatmap();
  }

  function switchViewMode(mode) {
    if (state.viewMode === mode) return;
    state.viewMode = mode;
    els.view2d?.classList.toggle("active", mode === "2d");
    els.view3d?.classList.toggle("active", mode === "3d");
    els.view2d?.setAttribute("aria-pressed", String(mode === "2d"));
    els.view3d?.setAttribute("aria-pressed", String(mode === "3d"));

    if (mode === "3d") {
      els.canvas?.classList.add("hidden");
      els.canvas3d?.classList.remove("hidden");
      els.controls3d?.classList.remove("hidden");
      if (!state.three.isInit) {
        init3DScene();
      }
      if (state.rawStocks && state.rawStocks.length) {
        const normalized = state.rawStocks
          .map(normalizeStock)
          .filter((stock) => stock.size > 0);
        render3DHeatmap(normalized);
      }
    } else {
      els.canvas?.classList.remove("hidden");
      els.canvas3d?.classList.add("hidden");
      els.controls3d?.classList.add("hidden");
      if (state.rawStocks && state.rawStocks.length) {
        const normalized = state.rawStocks
          .map(normalizeStock)
          .filter((stock) => stock.size > 0);
        renderHeatmap(normalized);
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

    if (state.rawStocks && state.rawStocks.length) {
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
        node.classList.toggle("is-dimmed", query.length > 0 && !matches);
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
    span.textContent =
      "市場が休場中、またはデータ取得に時間がかかっています。しばらくしてから再試行してください。";
    error.append(icon, strong, span);
    els.canvas.appendChild(error);
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
    const fallbackSize = Math.max(price, 1) * Math.max(volume, 1);

    const size =
      state.sizeMetric === "volume"
        ? volume > 0
          ? volume * Math.max(price, 1)
          : fallbackSize
        : Number.isFinite(rawMarketCap) && rawMarketCap > 0
          ? rawMarketCap
          : fallbackSize;

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

  async function loadHeatmap(isRetry = false) {
    if (!isRetry) {
      if (state.timeoutId) {
        clearTimeout(state.timeoutId);
        state.timeoutId = null;
      }
      state.pollRetries = 0;
      // 新規ロードのみキャンバスをクリアしてローディング表示
      state.controller?.abort();
      state.controller = new AbortController();
      setLoading(true);
      if (els.canvas) els.canvas.textContent = "";
      if (els.updateTime) els.updateTime.textContent = "-";
      if (els.count) els.count.textContent = "--";
    } else {
      // ポーリングリトライ時はAbortControllerだけ更新する（ローディング表示は維持）
      state.controller?.abort();
      state.controller = new AbortController();
    }

    let isPolling = false;
    try {
      const resp = await fetch(
        `/api/heatmap?market=${encodeURIComponent(state.currentMarket)}`,
        {
          signal: state.controller.signal,
        },
      );
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const data = await resp.json();
      if (data && data.fetching) {
        // バックエンドで非同期取得中。数秒待って再試行する。
        state.pollRetries = (state.pollRetries || 0) + 1;
        const maxRetries = 15; // 15 retries * 3000ms = 45 seconds total timeout
        if (state.pollRetries <= maxRetries) {
          isPolling = true;
          state.timeoutId = setTimeout(() => {
            state.timeoutId = null;
            loadHeatmap(true);
          }, 3000);
          return;
        }
        showError(
          "ヒートマップデータの取得に時間がかかっています。再度お試しください。",
        );
        setLoading(false);
        return;
      }
      const stocks = Array.isArray(data.stocks) ? data.stocks : [];
      state.rawStocks = stocks;
      const normalized = stocks
        .map(normalizeStock)
        .filter((stock) => stock.size > 0);

      if (!normalized.length) {
        showError("表示できる銘柄データがありませんでした");
        setLoading(false);
        return;
      }

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
      if (typeof logger !== "undefined" && logger.error) {
        logger.error("Heatmap fetch error:", err);
      } else {
        console.warn("Heatmap fetch error:", err);
      }
      showError("市場データの取得に失敗しました");
    } finally {
      if (!isPolling) {
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
    const priceText = Number.isFinite(stock.price)
      ? formatNumber(stock.price)
      : "--";
    const changeText = `${changePercent > 0 ? "+" : ""}${changePercent.toFixed(2)}%`;

    node.style.backgroundColor = getColor(changePercent);
    node.style.setProperty(
      "--node-change",
      changePercent >= 0
        ? "rgba(125, 255, 176, 0.95)"
        : "rgba(255, 125, 125, 0.95)",
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

    node.addEventListener("mouseenter", () =>
      showTooltip(node, stock, changePercent),
    );
    node.addEventListener("mousemove", (event) => moveTooltip(event));
    node.addEventListener("mouseleave", hideTooltip);
    node.addEventListener("focus", () =>
      showTooltip(node, stock, changePercent),
    );
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
    const priceText = Number.isFinite(stock.price)
      ? formatNumber(stock.price)
      : "--";
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
    if (node && node.classList) {
      node.classList.add("is-tooltip-open");
    }
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
    els.canvas
      ?.querySelectorAll(".heatmap-node.is-tooltip-open")
      .forEach((node) => {
        node.classList.remove("is-tooltip-open");
      });
  }

  /* --- 3D Scene Initialization & Rendering --- */
  function init3DScene() {
    if (!els.canvas3d || typeof THREE === "undefined") return;
    state.three.isInit = true;

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

    els.canvas3d.innerHTML = "";
    els.canvas3d.appendChild(renderer.domElement);

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
        window.location.href = `/main?q=${encodeURIComponent(stock.symbol)}`;
      }
    };

    renderer.domElement.addEventListener("mousemove", onMouseMove);
    renderer.domElement.addEventListener("click", onClick);
    renderer.domElement.addEventListener("mouseleave", hideTooltip);

    const animate = () => {
      state.three.animationFrameId = requestAnimationFrame(animate);
      if (controls) controls.update();
      renderer.render(scene, camera);
    };
    animate();
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
      init3DScene();
    }
    const { scene } = state.three;
    if (!scene || typeof THREE === "undefined") return;

    if (state.three.stockMeshes) {
      state.three.stockMeshes.forEach((mesh) => {
        scene.remove(mesh);
        disposeObject(mesh);
      });
    }
    state.three.stockMeshes = [];
    state.three.hoveredMesh = null;

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

      let colorHex = 0x1e293b;
      let emissiveHex = 0x000000;
      if (change > 0) {
        const ratio = Math.min(change / 3, 1);
        colorHex = new THREE.Color()
          .lerpColors(
            new THREE.Color(0x0f5236),
            new THREE.Color(0x00ff87),
            ratio,
          )
          .getHex();
        emissiveHex = new THREE.Color()
          .lerpColors(
            new THREE.Color(0x000000),
            new THREE.Color(0x004d26),
            ratio,
          )
          .getHex();
      } else if (change < 0) {
        const ratio = Math.min(Math.abs(change) / 3, 1);
        colorHex = new THREE.Color()
          .lerpColors(
            new THREE.Color(0x5c151b),
            new THREE.Color(0xff4e50),
            ratio,
          )
          .getHex();
        emissiveHex = new THREE.Color()
          .lerpColors(
            new THREE.Color(0x000000),
            new THREE.Color(0x4d000a),
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
      const lineMat = new THREE.LineBasicMaterial({
        color: change >= 0 ? 0x00f2fe : 0xff4e50,
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

  const _resizeHandler = () => {
    hideTooltip();
    if (
      state.viewMode === "3d" &&
      state.three.renderer &&
      state.three.camera &&
      els.canvas3d
    ) {
      const w = els.canvas3d.clientWidth;
      const h = els.canvas3d.clientHeight;
      if (w > 0 && h > 0) {
        state.three.camera.aspect = w / h;
        state.three.camera.updateProjectionMatrix();
        state.three.renderer.setSize(w, h);
      }
    }
  };
  window.addEventListener("resize", _resizeHandler);

  document.addEventListener("beforeunload", () => {
    window.removeEventListener("resize", _resizeHandler);
    if (state.three.animationFrameId) {
      cancelAnimationFrame(state.three.animationFrameId);
    }
    if (state.timeoutId) {
      clearTimeout(state.timeoutId);
    }
    state.controller?.abort();
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
