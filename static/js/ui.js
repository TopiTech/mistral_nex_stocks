// M-8: Declare the IntersectionObserver as a module-level variable that is
// initialized lazily on first use (or explicitly via initCardIntersectionObserver).
// This avoids top-level browser API side effects that run before DOMContentLoaded,
// which can cause issues with script load ordering.
let cardIntersectionObserver = null;

function _createCardIntersectionObserver() {
  return new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        const wrapper = entry.target;
        const isVisible = entry.isIntersecting;
        wrapper.dataset.visible = isVisible ? "true" : "false";

        if (isVisible) {
          // Draw deferred sparkline if data is pending
          if (wrapper.__pendingSparklineData) {
            drawSparkline(wrapper, wrapper.__pendingSparklineData);
            wrapper.__pendingSparklineData = null;
          }
          // Trigger lazy details if the panel is open
          const detailPanel = wrapper.querySelector(".detail-panel");
          if (detailPanel && detailPanel.classList.contains("open")) {
            const stockKey = wrapper.dataset.stockKey;
            const stock = wrapper.__stockData || getStockByKey(stockKey);
            if (stock) {
              refreshStockChart(
                wrapper,
                getChartPref(stockKey, "period", "3mo"),
              );
            }
          }
        }
      });
    },
    {
      root: null,
      rootMargin: "100px",
      threshold: 0.01,
    },
  );
}

function initCardIntersectionObserver() {
  if (!cardIntersectionObserver) {
    cardIntersectionObserver = _createCardIntersectionObserver();
  }
  return cardIntersectionObserver;
}

// Cleanup all observers on page unload to prevent memory leaks
document.addEventListener(
  "beforeunload",
  function cleanupIntersectionObserver() {
    if (cardIntersectionObserver) {
      cardIntersectionObserver.disconnect();
      cardIntersectionObserver = null;
    }
  },
);

// #region Detail Panel Management
async function ensureStockDetails(wrapper) {
  const stockKey = wrapper.dataset.stockKey;
  if (stockDetailsCache.has(stockKey)) {
    renderDetailExtras(wrapper, stockDetailsCache.get(stockKey));
    return;
  }

  const detailInner = wrapper.querySelector(".detail-inner");
  const sectorEl = wrapper.querySelector(".detail-sector");
  const industryEl = wrapper.querySelector(".detail-industry");
  const mcapEl = wrapper.querySelector(".detail-mcap");
  const peEl = wrapper.querySelector(".detail-pe");

  // Remove existing error banner if any
  const existingBanner = wrapper.querySelector(".detail-error-banner");
  if (existingBanner) {
    existingBanner.remove();
  }

  // Show loading state visual feedback
  if (sectorEl) sectorEl.textContent = "取得中...";
  if (industryEl) industryEl.textContent = "取得中...";
  if (mcapEl) mcapEl.textContent = "取得中...";
  if (peEl) peEl.textContent = "取得中...";

  const symbol = wrapper.dataset.symbol;
  const market = wrapper.dataset.market || "us";

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 4000);

  const showErrorBanner = (text) => {
    // Reset fields to failure placeholder
    if (sectorEl) sectorEl.textContent = "取得失敗";
    if (industryEl) industryEl.textContent = "取得失敗";
    if (mcapEl) mcapEl.textContent = "取得失敗";
    if (peEl) peEl.textContent = "取得失敗";

    if (!detailInner) return;

    // Create single error banner at the bottom of detail panel info
    const banner = document.createElement("div");
    banner.className = "detail-error-banner";

    const label = document.createElement("span");
    label.textContent = `詳細データの取得失敗: ${text}`;
    banner.appendChild(label);

    const retryBtn = document.createElement("button");
    retryBtn.textContent = "再試行";
    retryBtn.className = "detail-error-retry-btn";
    retryBtn.addEventListener("click", (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
      ensureStockDetails(wrapper);
    });
    banner.appendChild(retryBtn);

    // Append to detail panel inner container
    detailInner.appendChild(banner);
  };

  try {
    // H-2: the backend may return {fetching:true} on a cold cache miss while
    // it fetches fundamentals off-thread. Poll briefly until real data arrives.
    const MAX_DETAILS_POLLS = 8;
    const pollOnce = async () => {
      const url = new URL("/api/stock-details", window.location.origin);
      url.search = new URLSearchParams({ symbol, market }).toString();
      const res = await fetch(url.toString(), { signal: controller.signal });
      return res.json();
    };

    let data = null;
    for (let attempt = 0; attempt <= MAX_DETAILS_POLLS; attempt++) {
      data = await pollOnce();
      if (!data || !data.fetching) break;
      if (controller.signal.aborted) break;
      await new Promise((r) => setTimeout(r, 700));
    }

    clearTimeout(timeoutId);
    if (data && !data.error && !data.fetching) {
      stockDetailsCache.set(stockKey, data);
      renderDetailExtras(wrapper, data);
    } else if (data && data.fetching) {
      // Still pending after polling window: keep "取得中..." placeholder;
      // reopening the detail panel (or a user refresh) will re-poll.
      logger.info("stock-details still fetching after poll window; deferring");
    } else {
      const errMsg = data?.error || "データ取得失敗";
      showErrorBanner(errMsg);
    }
  } catch (e) {
    clearTimeout(timeoutId);
    const isTimeout = e.name === "AbortError";
    const statusText = isTimeout ? "タイムアウト" : "取得失敗";
    showErrorBanner(statusText);
    logger.warn("Details fetch error:", e);
  }
}
function renderFavorites() {
  document.querySelectorAll(".favorite-star").forEach((star) => {
    const wrapper = star.closest(".stock-wrapper");
    const stockKey = wrapper?.dataset?.stockKey;
    star.classList.toggle("active", !!stockKey && state.isFavorite(stockKey));
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Unified UI Update for a stock card.
 */
function updateStockUI(wrapper, stock) {
  const stockKey = wrapper.dataset.stockKey;
  const oldPrice = wrapper.__stockData?.price;
  const newPrice = stock.price;
  const isPortfolioTab = wrapper.dataset.marketContext === "portfolio";

  // Skip update if identical to avoid DOM thrashing
  // Check price AND chart freshness (for sparkline updates when price is static)
  const lastChartTs =
    Array.isArray(stock.chart_data) && stock.chart_data.length > 0
      ? stock.chart_data[stock.chart_data.length - 1].x
      : "";
  const oldDataStr = wrapper.dataset.lastDataHash;
  const newDataStr = `${stock.price}|${stock.change}|${stock.change_percent}|${stock.shares}|${stock.avg_price}|${lastChartTs}`;

  if (oldDataStr === newDataStr) return;
  wrapper.dataset.lastDataHash = newDataStr;

  const hasSparklinePoints =
    Array.isArray(stock.chart_data) && stock.chart_data.length > 0;
  const freshSparklineData = hasFreshSparklineData(stock) && hasSparklinePoints;

  wrapper.__stockData = { ...stock };
  stockRealtimeUpdateAt.set(stockKey, Date.now());
  checkAlerts(stock, oldPrice);

  // Update Compact View
  const priceEl = wrapper.querySelector(".compact-price");
  if (priceEl) {
    const formattedPrice = formatPrice(newPrice, stock);
    if (priceEl.textContent !== formattedPrice) {
      priceEl.textContent = formattedPrice;
      if (oldPrice != null && newPrice !== oldPrice && oldPrice !== "--") {
        const flashClass = newPrice > oldPrice ? "flash-up" : "flash-down";
        triggerPriceFlash(priceEl, flashClass);
      }
    }
    // Add brief 'updating' effect for every data arrival to feel "live"
    priceEl.classList.add("updating");
    if (priceEl.__updateTimer) clearTimeout(priceEl.__updateTimer);
    priceEl.__updateTimer = setTimeout(
      () => priceEl.classList.remove("updating"),
      600,
    );
  }

  const changeEl = wrapper.querySelector(".compact-change");
  if (changeEl) {
    const sign = stock.change >= 0 ? "+" : "";
    // ▲▼ は色覚多様性に配慮し、色だけでなく記号でも増減を伝えるためのアクセシビリティ記号
    const arrow = stock.change >= 0 ? "▲" : "▼";
    const ariaPrefix = stock.change >= 0 ? "上昇" : "下落";
    const nextCls = `compact-change ${stock.change >= 0 ? "pos" : "neg"}`;
    const nextText = `${arrow}${sign}${stock.change} (${sign}${stock.change_percent}%)`;
    if (changeEl.className !== nextCls) changeEl.className = nextCls;
    if (changeEl.textContent !== nextText) {
      changeEl.textContent = nextText;
      changeEl.setAttribute(
        "aria-label",
        `${ariaPrefix} ${sign}${stock.change} (${sign}${stock.change_percent}%)`,
      );
    }
  }

  if (isPortfolioTab) {
    updatePortfolioInfoElements(wrapper, stock);
  }

  // スパークラインの更新
  if (!hasSparklinePoints) {
    setSparklineVisibility(wrapper, false);
  } else {
    // 表示可否と再描画判定を分離し、再描画スキップ時も表示状態は安定させる
    const wasHidden = isSparklineHidden(wrapper);
    setSparklineVisibility(wrapper, true);

    // 鮮度が低いデータは可視状態を維持し、不要な再描画だけ抑制する
    if (!freshSparklineData && !wasHidden) {
      return;
    }

    // リロード直後の差し替えジャンプを抑えるため、初回ライブ更新の1回目は再描画を抑制
    const isFirstLiveRefresh =
      stock.__live_update && wrapper.dataset.liveSparkSeen !== "1";
    if (stock.__live_update) {
      wrapper.dataset.liveSparkSeen = "1";
    }
    if (isFirstLiveRefresh && !wasHidden) {
      return;
    }

    const needsInitialDraw = wasHidden;
    if (
      needsInitialDraw ||
      shouldUpdateSparkline(wrapper, stockKey, stock.chart_data)
    ) {
      if (isElementInViewport(wrapper)) {
        drawSparkline(wrapper, stock.chart_data);
      } else {
        wrapper.__pendingSparklineData = stock.chart_data;
      }
    }
  }

  // Update Detail Panel (only if not open to avoid jitter)
  const detail = wrapper.querySelector(".detail-panel");
  if (detail && !detail.classList.contains("open")) {
    const elMap = {
      ".detail-current": formatPrice(stock.price, stock),
      ".detail-high": formatPrice(stock.high, stock),
      ".detail-low": formatPrice(stock.low, stock),
      ".detail-volume":
        stock.volume != null ? Number(stock.volume).toLocaleString() : "--",
    };
    for (const [sel, val] of Object.entries(elMap)) {
      const el = wrapper.querySelector(sel);
      if (el && el.textContent !== String(val)) {
        el.textContent = val;
      }
    }
  }

  // Real-time Chart Update if open
  // ガード条件: ローディング中、または読み込み完了直後（0.8秒間）は更新をスキップしてアニメーションの衝突を防ぐ
  const container = wrapper.querySelector(".chart-container");
  const lastRefresh = parseInt(wrapper.dataset.lastRefresh || "0");
  const isCooldown = Date.now() - lastRefresh < 800;

  if (
    detail?.classList.contains("open") &&
    container &&
    !container.classList.contains("loading") &&
    !isCooldown
  ) {
    requestAnimationFrame(() => {
      if (isPortfolioTab) {
        const pnlCanvas = wrapper.querySelector(".chart-canvas-pnl");
        if (pnlCanvas)
          drawPnLChart(pnlCanvas, stock.chart_data || [], stock.avg_price, {
            animate: false,
          });
      } else {
        // 期間保護: ユーザーが3moまたは1d以外の期間を選択中の場合、SSEデータでチャートを上書きしない（ガタつき防止）
        const currentPeriod = getChartPref(stockKey, "period", "3mo");
        if (currentPeriod === "3mo" || currentPeriod === "1d") {
          // 3mo デフォルト: 出来高アニメーションのみ残しつつ低遅延で更新
          const hasHistory =
            Array.isArray(stock.chart_data) && stock.chart_data.length >= 2;
          if (hasHistory) {
            const showVolume = getChartPref(stockKey, "volume", "on") !== "off";
            drawChart(wrapper, stock.chart_data || [], stock.ohlc_data || [], {
              animate: false,
              animateVolumeOnly: showVolume,
            });
          } else {
            // SSE軽量ペイロード時は既存チャートの末尾だけ追従させる
            const canvas = wrapper.querySelector(".chart-canvas");
            const chart = canvas ? chartInstances.get(canvas) : null;
            const isLine =
              getChartPref(stockKey, "type", "line") !== "candlestick";
            if (
              chart &&
              chart.data.datasets?.[0]?.data?.length > 0 &&
              stock.price != null
            ) {
              const lastPoint = chart.data.datasets[0].data.at(-1);
              if (lastPoint && lastPoint.y !== undefined) {
                lastPoint.y = stock.price;
              } else if (lastPoint && lastPoint.c !== undefined) {
                lastPoint.c = stock.price;
                if (stock.price > lastPoint.h) lastPoint.h = stock.price;
                if (stock.price < lastPoint.l) lastPoint.l = stock.price;
              }
              chart.update("none");
            }
          }
        } else {
          // 他期間選択中: 既存チャートの最終データポイントのみ現在価格に更新 (SSEデータで上書きしない)
          const canvas = wrapper.querySelector(".chart-canvas");
          if (canvas) {
            const chart = chartInstances.get(canvas);
            const isLine =
              getChartPref(stockKey, "type", "line") !== "candlestick";
            if (chart && chart.data.datasets?.[0]?.data?.length > 0) {
              const lastPoint = chart.data.datasets[0].data.at(-1);
              if (lastPoint && stock.price != null) {
                if (lastPoint.y !== undefined) {
                  // ラインチャート: y プロパティを更新
                  lastPoint.y = stock.price;
                } else if (lastPoint.c !== undefined) {
                  // ローソク足チャート: close を更新し、high/low も補正
                  lastPoint.c = stock.price;
                  if (stock.price > lastPoint.h) lastPoint.h = stock.price;
                  if (stock.price < lastPoint.l) lastPoint.l = stock.price;
                }

                chart.update("none");
              }
            }
          }
        }
      }
    });
    if (stockDetailsCache.has(stockKey))
      renderDetailExtras(wrapper, stockDetailsCache.get(stockKey));
  }
}

function updatePortfolioInfoElements(wrapper, stock) {
  const pfInfoEl = wrapper.querySelector(".compact-pf-info");
  const shares = toFiniteNumber(stock.shares, 0);
  const avgPrice = toFiniteNumber(stock.avg_price, 0);
  const currentPrice = toFiniteNumber(stock.price, 0);

  if (pfInfoEl && shares > 0) {
    const plVal = (currentPrice - avgPrice) * shares;
    const plSign = plVal >= 0 ? "+" : "";
    const plClass = plVal >= 0 ? "pos" : "neg";
    pfInfoEl.textContent = `保有: ${shares} | 損益: `;

    const plSpan = document.createElement("span");
    plSpan.className = plClass;
    plSpan.textContent = `${plSign}${plVal.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
    pfInfoEl.appendChild(plSpan);
  } else if (pfInfoEl) {
    pfInfoEl.textContent = "\u00A0";
  }

  // Detailed PF block
  const pfBlock = wrapper.querySelector(".pf-detail-block");
  if (pfBlock && shares > 0) {
    const plVal = (currentPrice - avgPrice) * shares;
    const plPct =
      avgPrice > 0 ? ((currentPrice - avgPrice) / avgPrice) * 100 : 0;
    const pfShares = pfBlock.querySelector(".pf-shares");
    const pfAvgprice = pfBlock.querySelector(".pf-avgprice");
    const pfValue = pfBlock.querySelector(".pf-value");
    const plEl = pfBlock.querySelector(".pf-pl");
    if (pfShares) pfShares.textContent = shares;
    if (pfAvgprice)
      pfAvgprice.textContent = avgPrice.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    if (pfValue)
      pfValue.textContent = (currentPrice * shares).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    if (plEl) {
      plEl.className = `pf-pl ${plVal >= 0 ? "pos" : "neg"}`;
      const sign = plVal >= 0 ? "+" : "";
      plEl.textContent = `${sign}${plVal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} (${sign}${plPct.toFixed(2)}%)`;
    }
  }
}

const updateExistingCard = (wrapper, stock) => updateStockUI(wrapper, stock);

// #region DOM Component Creation

/**
 * detail-panel をDOM APIで構築（innerHTML 不使用）
 */
function buildDetailPanel(
  stock,
  marketContext,
  uniqueId,
  savedColor,
  isPortfolio,
) {
  const safeColor = sanitizeHexColor(savedColor || "#6bb6ff");

  const detail = document.createElement("div");
  detail.className = "detail-panel";

  const inner = createEl("div", "detail-inner");

  // Expand toggle button
  inner.appendChild(createEl("button", "expand-toggle-btn"));

  // Portfolio detail block
  if (isPortfolio) {
    const shares = toFiniteNumber(stock.shares, 0);
    const avgPrice = toFiniteNumber(stock.avg_price, 0);
    const currentPrice = toFiniteNumber(stock.price, 0);
    const plVal = (currentPrice - avgPrice) * shares;
    const plPct =
      avgPrice > 0 ? ((currentPrice - avgPrice) / avgPrice) * 100 : 0;
    const plClass = plVal >= 0 ? "pos" : "neg";
    const plSign = plVal >= 0 ? "+" : "";

    const pfBlock = createEl("div", "pf-detail-block");

    const row1 = createEl("div", "pf-detail-row margin-sm");
    const s1 = document.createElement("span");
    s1.textContent = "保有株数: ";
    const s1Strong = document.createElement("strong");
    s1Strong.className = "pf-shares";
    s1Strong.textContent = String(shares);
    s1.appendChild(s1Strong);
    const s2 = document.createElement("span");
    s2.textContent = "平均取得単価: ";
    const s2Strong = document.createElement("strong");
    s2Strong.className = "pf-avgprice";
    s2Strong.textContent = avgPrice.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    s2.appendChild(s2Strong);
    row1.appendChild(s1);
    row1.appendChild(s2);

    const row2 = createEl("div", "pf-detail-row");
    const s3 = document.createElement("span");
    s3.textContent = "評価額: ";
    const s3Strong = document.createElement("strong");
    s3Strong.className = "pf-value";
    s3Strong.textContent = (currentPrice * shares).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    s3.appendChild(s3Strong);
    const s4 = document.createElement("span");
    s4.textContent = "評価損益: ";
    const s4Strong = document.createElement("strong");
    s4Strong.className = `pf-pl ${plClass}`;
    s4Strong.textContent = `${plSign}${plVal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} (${plSign}${plPct.toFixed(2)}%)`;
    s4.appendChild(s4Strong);
    row2.appendChild(s3);
    row2.appendChild(s4);

    pfBlock.appendChild(row1);
    pfBlock.appendChild(row2);
    inner.appendChild(pfBlock);
  }

  // Detail info section
  const info = createEl("div", "detail-info");
  const infoItems = [
    {
      label: "現在値:",
      cls: "detail-current",
      val: formatPrice(stock.price, stock),
    },
    { label: "高値:", cls: "detail-high", val: formatPrice(stock.high, stock) },
    { label: "安値:", cls: "detail-low", val: formatPrice(stock.low, stock) },
    {
      label: "出来高:",
      cls: "detail-volume",
      val: stock.volume != null ? Number(stock.volume).toLocaleString() : "--",
    },
    {
      label: "セクター:",
      cls: "detail-sector extra",
      val: "--",
      extraCls: "detail-item-sector",
    },
    {
      label: "業種:",
      cls: "detail-industry extra",
      val: "--",
      extraCls: "detail-item-industry",
    },
    {
      label: "時価総額:",
      cls: "detail-mcap extra",
      val: "--",
      extraCls: "detail-item-mcap",
    },
    {
      label: "PER:",
      cls: "detail-pe extra",
      val: "--",
      extraCls: "detail-item-pe",
    },
  ];
  infoItems.forEach(({ label, cls, val, extraCls }) => {
    const item = createEl("div", `detail-item ${extraCls || ""}`.trim());
    const strong = document.createElement("strong");
    strong.textContent = label;
    const span = createEl("span", cls, val);
    item.appendChild(strong);
    item.appendChild(span);
    info.appendChild(item);
  });

  // Color picker
  const colorItem = createEl("div", "detail-item");
  const colorLabel = document.createElement("strong");
  colorLabel.textContent = "カード色:";
  const colorInput = document.createElement("input");
  colorInput.id = `card-color-picker-${uniqueId}`;
  colorInput.name = "card-color-picker";
  colorInput.className = "card-color-picker";
  colorInput.type = "color";
  colorInput.value = safeColor;
  colorInput.setAttribute("aria-label", "メインカラー設定");
  colorItem.appendChild(colorLabel);
  colorItem.appendChild(colorInput);
  info.appendChild(colorItem);
  inner.appendChild(info);

  // Detail actions
  const actions = createEl("div", "detail-actions");
  const pfBtn = createEl(
    "button",
    "pf-edit-btn detail-action-btn portfolio",
    "💼 ポートフォリオ設定",
  );
  const alertBtn = createEl(
    "button",
    "alert-edit-btn detail-action-btn alert",
    "🔔 アラート設定",
  );
  actions.appendChild(pfBtn);
  actions.appendChild(alertBtn);
  inner.appendChild(actions);

  // Chart controls (hidden for portfolio)
  const chartControls = createEl("div", "chart-controls");
  if (isPortfolio) {
    chartControls.classList.add("portfolio-hidden");
  }

  // Type controls
  const typeGroup = createEl("div", "control-group type-controls");
  const isLine =
    getChartPref(
      makeStockKey(stock.market || "us", stock.symbol),
      "type",
      "line",
    ) !== "candlestick";
  const lineBtn = createEl(
    "button",
    `control-btn ${isLine ? "active" : ""}`,
    "ライン",
  );
  lineBtn.dataset.type = "line";
  const candleBtn = createEl(
    "button",
    `control-btn ${!isLine ? "active" : ""}`,
    "ロウソク足",
  );
  candleBtn.dataset.type = "candlestick";
  typeGroup.appendChild(lineBtn);
  typeGroup.appendChild(candleBtn);
  chartControls.appendChild(typeGroup);

  // Volume controls
  const volGroup = createEl("div", "control-group volume-controls");
  const volOn =
    getChartPref(
      makeStockKey(stock.market || "us", stock.symbol),
      "volume",
      "on",
    ) === "on";
  const volOnBtn = createEl(
    "button",
    `control-btn ${volOn ? "active" : ""}`,
    "出来高ON",
  );
  volOnBtn.dataset.volume = "on";
  const volOffBtn = createEl(
    "button",
    `control-btn ${!volOn ? "active" : ""}`,
    "出来高OFF",
  );
  volOffBtn.dataset.volume = "off";
  volGroup.appendChild(volOnBtn);
  volGroup.appendChild(volOffBtn);
  chartControls.appendChild(volGroup);

  // Period controls
  const periodGroup = createEl("div", "control-group period-controls");
  const stockKey = makeStockKey(stock.market || "us", stock.symbol);
  CONSTANTS.PERIODS.forEach((p) => {
    const btn = createEl(
      "button",
      `control-btn ${getChartPref(stockKey, "period", "3mo") === p ? "active" : ""}`,
      p.toUpperCase(),
    );
    btn.dataset.period = p;
    periodGroup.appendChild(btn);
  });
  chartControls.appendChild(periodGroup);
  inner.appendChild(chartControls);

  // Chart container
  const chartContainer = createEl("div", "chart-container");
  if (isPortfolio) {
    chartContainer.classList.add("portfolio-hidden");
  }
  const chartCanvas = createEl("canvas", "chart-canvas");
  chartContainer.appendChild(chartCanvas);
  inner.appendChild(chartContainer);

  // PnL chart for portfolio
  if (isPortfolio) {
    const pnlContainer = createEl("div", "chart-container pnl-chart-container");
    const pnlLabel = createEl("div", "pnl-chart-label");
    pnlLabel.textContent = "損益率推移 (3ヶ月)";
    const pnlCanvas = createEl("canvas", "chart-canvas-pnl");
    pnlContainer.appendChild(pnlLabel);
    pnlContainer.appendChild(pnlCanvas);
    inner.appendChild(pnlContainer);
  }

  // Analyze button
  inner.appendChild(createEl("button", "analyze-btn", "🔍 AI分析実行"));

  // AI section
  const aiSection = createEl("div", "ai-section");
  const aiTitle = document.createElement("div");
  aiTitle.className = "ai-title";
  aiTitle.textContent = "📈 分析結果 ";
  const aiBadge = createEl("span", "ai-badge", "AI");
  aiTitle.appendChild(aiBadge);
  aiSection.appendChild(aiTitle);

  const aiSlider = createEl("div", "ai-slider");
  const aiCards = [
    { title: "推奨", cls: "ai-rec" },
    { title: "センチメント", cls: "ai-sent" },
    { title: "目標価格 / 3ヶ月", cls: "ai-target", hasUpside: true },
    { title: "注目ポイント", cls: "ai-cat" },
    { title: "リスク要因", cls: "ai-risk" },
  ];
  aiCards.forEach(({ title, cls, hasUpside }) => {
    const card = createEl("div", "ai-card");
    card.appendChild(createEl("div", "ai-card-title", title));
    card.appendChild(createEl("div", `${cls} ai-card-content`, "分析中..."));
    if (hasUpside) {
      const upside = createEl("div", "ai-upside ai-card-content", "");
      card.appendChild(upside);
    }
    aiSlider.appendChild(card);
  });
  aiSection.appendChild(aiSlider);
  inner.appendChild(aiSection);

  // Chat section
  inner.appendChild(createEl("button", "chat-toggle-btn", "💡 AIに質問する"));
  const chatSection = createEl("div", "chat-section");
  const chatTitle = document.createElement("div");
  chatTitle.className = "ai-title";
  chatTitle.textContent = "💬 AIに質問 ";
  const chatBadge = createEl("span", "ai-badge", "AI");
  chatTitle.appendChild(chatBadge);
  chatSection.appendChild(chatTitle);
  chatSection.appendChild(createEl("div", "chat-log", ""));
  chatSection.lastChild.setAttribute("role", "log");
  chatSection.lastChild.setAttribute("aria-live", "polite");
  const chatInputWrapper = createEl("div", "chat-input-wrapper");
  const chatInput = document.createElement("input");
  chatInput.id = `chat-input-${uniqueId}`;
  chatInput.name = "chat-input";
  chatInput.className = "chat-input";
  chatInput.placeholder = "業績の見通しは？";
  chatInput.setAttribute("aria-label", "AIへの質問");
  const chatSendBtn = createEl("button", "chat-send-btn", "送信");
  chatSendBtn.type = "button";
  chatInputWrapper.appendChild(chatInput);
  chatInputWrapper.appendChild(chatSendBtn);
  chatSection.appendChild(chatInputWrapper);
  inner.appendChild(chatSection);

  detail.appendChild(inner);
  return detail;
}

function createStockCard(stock, marketContext) {
  const market = stock.market || "us";
  const stockKey = makeStockKey(market, stock.symbol);
  const domKey = makeDomSafeKey(stockKey);
  const uniqueId = `${marketContext}-${domKey}`;
  const savedColor = isValidHexColor(getStockColor(stockKey))
    ? getStockColor(stockKey).trim()
    : "";

  const wrapper = document.createElement("div");
  wrapper.className = "stock-wrapper";
  wrapper.dataset.symbol = stock.symbol;
  wrapper.dataset.market = market;
  wrapper.dataset.stockKey = stockKey;
  wrapper.dataset.marketContext = marketContext;
  wrapper.__stockData = { ...stock, market };

  const sign = stock.change >= 0 ? "+" : "";
  const isPortfolio = marketContext === "portfolio";

  // Compact Card Inner - DOM APIで構築
  // カスタムカラーが保存されている場合のみインラインで border-left-color を設定。
  // 未保存の場合はCSSの market 別スタイル（us→primary / jp→acc-purple / idx→acc-orange）が適用されるようにする。
  const safeColor = savedColor ? sanitizeHexColor(savedColor, "") : "";
  const compact = document.createElement("div");
  compact.className = `compact-card ${market}`;
  if (safeColor) compact.style.borderLeftColor = safeColor;
  // キーボード操作でカードを開けるよう role/tabindex を付与（マウスのみの click からの改善）
  compact.setAttribute("role", "button");
  compact.setAttribute("tabindex", "0");
  compact.setAttribute(
    "aria-label",
    `${stock.symbol} ${stock.name} の詳細を開く`,
  );

  const favStar = createEl("div", "favorite-star", "★");
  favStar.setAttribute("role", "button");
  favStar.setAttribute("aria-label", "お気に入り");
  compact.appendChild(favStar);

  const symEl = createEl("div", "compact-symbol", stock.symbol);
  // カスタムカラーが保存されている場合のみシンボル色をインライン設定（CSS未設定時は .compact-symbol の --text-accent が適用）
  if (safeColor) symEl.style.color = safeColor;
  compact.appendChild(symEl);

  compact.appendChild(createEl("div", "compact-name", stock.name));

  const right = createEl("div", "compact-right");
  right.appendChild(
    createEl(
      "div",
      "compact-price price-live-pulse",
      formatPrice(stock.price, stock),
    ),
  );
  const changeClass = stock.change >= 0 ? "pos" : "neg";
  // ▲▼ は色覚多様性に配慮し、色だけでなく記号でも増減を伝えるためのアクセシビリティ記号
  const arrow = stock.change >= 0 ? "▲" : "▼";
  const ariaPrefix = stock.change >= 0 ? "上昇" : "下落";
  const changeEl = createEl(
    "div",
    `compact-change ${changeClass}`,
    `${arrow}${sign}${stock.change} (${sign}${stock.change_percent}%)`,
  );
  changeEl.setAttribute(
    "aria-label",
    `${ariaPrefix} ${sign}${stock.change} (${sign}${stock.change_percent}%)`,
  );
  right.appendChild(changeEl);
  right.appendChild(createEl("div", "compact-pf-info"));
  const sparkline = createEl("div", "sparkline");
  sparkline.setAttribute("aria-hidden", "true");
  const sparkCanvas = createEl("canvas", "spark-canvas");
  sparkline.appendChild(sparkCanvas);
  right.appendChild(sparkline);
  compact.appendChild(right);

  // Detail Panel - DOM APIで構築（innerHTML不使用）
  const detail = buildDetailPanel(
    stock,
    marketContext,
    uniqueId,
    savedColor,
    isPortfolio,
  );

  // Events setup
  compact.addEventListener("click", (e) => {
    if (e.target.classList.contains("favorite-star")) return;
    toggleDetail(wrapper);
  });
  compact.addEventListener("keydown", (e) => {
    if (e.target.classList.contains("favorite-star")) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggleDetail(wrapper);
    }
  });
  compact.querySelector(".favorite-star")?.addEventListener("click", (e) => {
    e.stopPropagation();
    state.toggleFavorite(stockKey);
    renderFavorites();
  });

  const setupBtn = (sel, cb) =>
    detail.querySelector(sel)?.addEventListener("click", cb);
  setupBtn(".analyze-btn", function () {
    const aiSection = detail.querySelector(".ai-section");
    const listContainer = wrapper.closest(".stocks-list");
    aiSection?.classList.add("show");
    scheduleCompactLayoutAfterTransition(aiSection, listContainer);
    analyzeStock(this, wrapper);
  });
  setupBtn(".chat-toggle-btn", () => {
    const chatSection = detail.querySelector(".chat-section");
    if (!chatSection) return;
    const listContainer = wrapper.closest(".stocks-list");
    chatSection.classList.toggle("show");
    scheduleCompactLayoutAfterTransition(
      chatSection,
      listContainer,
      "max-height",
      false,
    );
  });
  setupBtn(".chat-send-btn", () => sendChat(wrapper));
  setupBtn(".pf-edit-btn", () => openPortfolioModal(stockKey));
  setupBtn(".alert-edit-btn", () => openAlertModal(stockKey));

  detail
    .querySelector(".chat-input")
    ?.addEventListener(
      "keypress",
      (e) => e.key === "Enter" && sendChat(wrapper),
    );
  detail
    .querySelector(".card-color-picker")
    ?.addEventListener("input", function () {
      updateStockColor(stockKey, this.value);
    });

  detail.querySelectorAll(".control-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const isPeriod = !!btn.dataset.period;
      const isVolume = btn.dataset.volume !== undefined;
      const val = isPeriod
        ? btn.dataset.period
        : isVolume
          ? btn.dataset.volume
          : btn.dataset.type;
      setChartPref(
        stockKey,
        isPeriod ? "period" : isVolume ? "volume" : "type",
        val,
      );
      btn.parentElement
        .querySelectorAll(".control-btn")
        .forEach((b) => b.classList.toggle("active", b === btn));
      refreshStockChart(wrapper, getChartPref(stockKey, "period", "3mo"));
    });
  });

  setupBtn(".expand-toggle-btn", function () {
    wrapper.classList.toggle("is-expanded");
    // チャートのリサイズをトリガー（幅が変わるため）
    const canvas = wrapper.querySelector(".chart-canvas");
    if (canvas) {
      const chart = chartInstances.get(canvas);
      if (chart) chart.resize();
    }
  });

  wrapper.appendChild(compact);
  wrapper.appendChild(detail);
  updatePortfolioInfoElements(wrapper, stock);

  // Initialize visibility state for IntersectionObserver
  wrapper.dataset.visible = "false";
  initCardIntersectionObserver().observe(wrapper);

  const hasSparklinePoints =
    Array.isArray(stock.chart_data) && stock.chart_data.length > 0;
  setSparklineVisibility(wrapper, hasSparklinePoints);
  if (hasSparklinePoints) {
    if (isElementInViewport(wrapper)) {
      requestAnimationFrame(() =>
        drawSparkline(wrapper, stock.chart_data || []),
      );
    } else {
      wrapper.__pendingSparklineData = stock.chart_data;
    }
  }
  // Register in O(1) lookup registry
  registerWrapper(stockKey, wrapper);
  return wrapper;
}

/**
 * 指定された市場の銘柄カードをレンダリングまたは更新します。
 * @param {string} market - 市場種別 ("us", "jp", "idx")。
 * @param {Array<Object>} stocks - レンダリング対象の銘柄データ配列。
 */
// #region Main Stock List Rendering
function renderStocks(market, stocks) {
  const container = document.getElementById(`${market}-stocks`);
  if (!container) return;

  // 初回ロードのスケルトン残留を防ぐ
  container.querySelectorAll(".skeleton-card").forEach((el) => el.remove());
  container.querySelectorAll(".no-results").forEach((el) => el.remove());

  const existingCards = new Map();
  container.querySelectorAll(".stock-wrapper").forEach((w) => {
    const key = w.dataset.stockKey;
    if (key) existingCards.set(key, w);
  });

  const sortedStocks = applySortOrder(market, stocks);
  const orderedWrappers = [];
  let createdCount = 0;
  let updatedCount = 0;
  sortedStocks.forEach((stock) => {
    const latestStock = { ...stock, market };
    const stockKey = makeStockKey(market, stock.symbol);
    let wrapper = existingCards.get(stockKey);
    if (wrapper) {
      updatedCount += 1;
      updateExistingCard(wrapper, latestStock);
      existingCards.delete(stockKey);
    } else {
      createdCount += 1;
      wrapper = createStockCard(latestStock, market);
    }
    orderedWrappers.push(wrapper);
  });

  existingCards.forEach((wrapper) => {
    wrapper
      .querySelectorAll("canvas")
      .forEach((canvas) => destroyChart(canvas));
    cardIntersectionObserver
      ? cardIntersectionObserver.unobserve(wrapper)
      : void 0;
    unregisterWrapper(wrapper.dataset.stockKey, wrapper);
    wrapper.remove();
  });

  if (document.querySelector(".tab.active")?.id === "tab-portfolio") {
    renderPortfolio();
  }

  // Reorder in-place without innerHTML="" to preserve Chart.js canvas state
  orderedWrappers.forEach((wrapper, i) => {
    if (wrapper.parentNode !== container) {
      container.appendChild(wrapper);
    } else {
      const currentAtIdx = container.children[i];
      if (currentAtIdx !== wrapper) {
        container.insertBefore(wrapper, currentAtIdx || null);
      }
    }
  });
  renderFavorites();
}

function toggleDetail(wrapper) {
  const detail = wrapper.querySelector(".detail-panel");
  if (!detail) return;
  const stockKey = wrapper.dataset.stockKey;
  const stock = wrapper.__stockData || getStockByKey(stockKey);
  const isOpen = detail.classList.contains("open");
  if (!isOpen) {
    cancelScheduledDestroy(detail);
    const openPanels = document.querySelectorAll(".detail-panel.open");
    if (openPanels.length >= 3) {
      closeDetailPanel(openPanels[0]);
    }

    // close->open の競合時に古い close コールバックを失効させる
    const generation = (detailCloseGeneration.get(detail) || 0) + 1;
    detailCloseGeneration.set(detail, generation);
    const isCurrentOpen = () =>
      detailCloseGeneration.get(detail) === generation;

    detail.classList.add("open");

    // 展開したカードが画面内に収まるようにスムーズスクロール
    setTimeout(() => {
      wrapper.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 100);
    const listContainer = detail.closest(".stocks-list");

    const syncOpenLayout = () => {
      if (!isCurrentOpen() || !detail.classList.contains("open")) return;
      compactStockCardLayout(listContainer);
    };

    const onOpenTransitionEnd = (event) => {
      if (event.target !== detail || !isCurrentOpen()) return;
      if (event.propertyName !== "max-height") return;
      clearTimeout(openFallbackTimer);
      detail.removeEventListener("transitionend", onOpenTransitionEnd);
      syncOpenLayout();
    };

    detail.addEventListener("transitionend", onOpenTransitionEnd);
    const openFallbackTimer = setTimeout(() => {
      if (!isCurrentOpen()) return;
      detail.removeEventListener("transitionend", onOpenTransitionEnd);
      syncOpenLayout();
    }, getTransitionFallbackMs(detail));

    if (stock) {
      const isPortfolio = wrapper.dataset.marketContext === "portfolio";
      const period = isPortfolio
        ? "3mo"
        : getChartPref(stockKey, "period", "3mo");
      refreshStockChart(wrapper, period);
      ensureStockDetails(wrapper);
    }
  } else {
    closeDetailPanel(detail);
  }
}

function closeDetailPanel(detail) {
  if (!detail) return;
  cancelScheduledDestroy(detail);
  const listContainer = detail.closest(".stocks-list");
  // 閉じ始めに固定された minHeight を解放し、折りたたみアニメーションの視認性を維持する
  clearStockCardMinHeights(listContainer);
  detail.classList.remove("open");
  const wrapper = detail.closest(".stock-wrapper");
  if (wrapper) wrapper.classList.remove("is-expanded");
  const fallbackMs = getTransitionFallbackMs(detail);

  const generation = (detailCloseGeneration.get(detail) || 0) + 1;
  detailCloseGeneration.set(detail, generation);
  const isCurrentClose = () => detailCloseGeneration.get(detail) === generation;

  const finalize = () => {
    if (!isCurrentClose() || detail.classList.contains("open")) return;
    detail.querySelectorAll("canvas").forEach((c) => {
      if (c.isConnected) destroyChart(c);
    });
  };

  const onTransitionEnd = (event) => {
    if (event.target !== detail || !isCurrentClose()) return;
    if (event.propertyName !== "max-height") return;
    clearTimeout(fallbackTimer);
    detail.removeEventListener("transitionend", onTransitionEnd);
    finalize();
    if (!detail.classList.contains("open")) {
      compactStockCardLayout(listContainer);
    }
  };

  // Ensure and register listener
  detail.addEventListener("transitionend", onTransitionEnd);
  const fallbackTimer = setTimeout(() => {
    if (!isCurrentClose()) return;
    detail.removeEventListener("transitionend", onTransitionEnd);
    finalize();
    if (!detail.classList.contains("open")) {
      compactStockCardLayout(listContainer);
    }
  }, fallbackMs);
}
// #endregion Detail Panel Management

function renderSkeletons() {
  sseState.skeletonShownAt = Date.now();
  const markets = ["us", "jp", "idx"];
  markets.forEach((m) => {
    const container = document.getElementById(`${m}-stocks`);
    if (!container) return;

    // 見栄えのために8個程度スケルトンを表示
    container.textContent = "";
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < 8; i++) {
      const card = createEl("div", "skeleton-card");
      card.appendChild(createEl("div", "skeleton skeleton-text"));
      card.appendChild(createEl("div", "skeleton skeleton-name"));
      card.appendChild(createEl("div", "skeleton skeleton-price"));
      fragment.appendChild(card);
    }
    container.appendChild(fragment);
  });
}

function renderInitialLoadingTimeoutState() {
  ["us", "jp", "idx"].forEach((m) => {
    const container = document.getElementById(`${m}-stocks`);
    if (!container) return;
    container.textContent = "";
    container.appendChild(
      createEl(
        "div",
        "no-results",
        "データ取得待機中です。接続状態を確認し、しばらく待っても表示されない場合は更新してください。",
      ),
    );
  });
}

// #region Portfolio Management
/**
 * ポートフォリオ全体のレンダリングを実行します。
 * 為替レートの適用や損益計算も含みます。
 */
function renderPortfolio() {
  const container = DOM.get("portfolio-stocks");
  const summaryContainer = document.getElementById(
    "portfolio-summary-container",
  );
  if (!container) return;

  const allStocks = getAllStocks();
  const holdings = allStocks.filter((s) => {
    const sh = toFiniteNumber(s.shares, NaN);
    return Number.isFinite(sh) && sh > 0;
  });

  if (holdings.length === 0) {
    if (summaryContainer) summaryContainer.style.display = "none";
    container.textContent = "";
    const empty = document.createElement("div");
    empty.className = "no-results";
    empty.style.gridColumn = "1/-1";
    empty.style.padding = "40px";
    empty.style.textAlign = "center";
    empty.style.color = "#9ca3af";
    empty.textContent =
      "保有銘柄がありません。銘柄詳細からポートフォリオ設定を行ってください。";
    container.appendChild(empty);
    return;
  }

  // 既存のカードを保持したまま更新する (全削除によるチラつきを防止)
  const existingKeys = new Set();
  holdings.forEach((stock) => {
    const stockKey = makeStockKey(stock.market, stock.symbol);
    existingKeys.add(stockKey);
    const registeredSet = wrapperRegistryMap.get(stockKey);
    const wrapper = registeredSet
      ? Array.from(registeredSet).find((w) => w.closest("#portfolio-stocks"))
      : null;

    if (wrapper) {
      updateExistingCard(wrapper, stock);
    } else {
      container.appendChild(createStockCard(stock, "portfolio"));
    }
  });

  // 不要になったカードを削除
  Array.from(container.querySelectorAll(".stock-wrapper")).forEach((w) => {
    if (!existingKeys.has(w.dataset.stockKey)) {
      w.querySelectorAll("canvas").forEach((canvas) => destroyChart(canvas));
      unregisterWrapper(w.dataset.stockKey, w);
      w.remove();
    }
  });

  renderFavorites();

  if (summaryContainer) {
    summaryContainer.style.display = "block";
    drawPortfolioSummaryChart(holdings);
  }
}
// #endregion Portfolio Management

// #region Portfolio Logic
let lastPfChartSignature = "";
const portfolioChartCache = new Map();

function computeHoldingsHash(holdings) {
  return holdings
    .map((h) => `${h.market}:${h.symbol}:${h.shares}:${h.avg_price}`)
    .join("|");
}

function drawPortfolioSummaryChart(holdings) {
  const canvas = DOM.get("pf-summary-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const chartAnimationControl = resolvePortfolioChartAnimationControl();

  // 最新レートを取得 (state経由)
  const usdJpyRate = portfolioFixedExchangeRate || state.exchangeRate || null;
  const isMixedCurrency =
    holdings.some((s) => s.currency === "USD") &&
    holdings.some((s) => s.currency === "JPY");

  if (holdings.some((s) => s.currency === "USD") && !usdJpyRate) {
    document
      .getElementById("pf-summary-loading")
      ?.style.setProperty("display", "block");
    canvas.style.display = "none";
    updatePortfolioHeader(
      holdings,
      null,
      isMixedCurrency,
      chartAnimationControl,
    );
    return;
  }
  document
    .getElementById("pf-summary-loading")
    ?.style.setProperty("display", "none");
  canvas.style.display = "block";

  // 1. 全銘柄からユニークな「日付文字列 (YYYY-MM-DD)」を抽出 (時差によるズレを防止)
  const allDates = new Set();
  const stockHistoryMap = new Map(); // stockKey -> Map<dateStr, price>

  holdings.forEach((stock) => {
    const stockKey = makeStockKey(stock.market, stock.symbol);
    const dayMap = new Map();
    if (stock.chart_data?.length) {
      stock.chart_data.forEach((d) => {
        const dObj = new Date(d.x);
        if (isNaN(dObj.getTime())) return;
        const dateStr = buildLocalDateKey(dObj);
        if (!dateStr) return;
        allDates.add(dateStr);
        dayMap.set(dateStr, d.price ?? d.c ?? 0);
      });
    }
    stockHistoryMap.set(stockKey, {
      days: dayMap,
      shares: toFiniteNumber(stock.shares, 0),
      avgPrice: toFiniteNumber(stock.avg_price, 0),
      rate: stock.currency === "USD" ? usdJpyRate : 1.0,
    });
  });

  const sortedDates = Array.from(allDates).sort();
  if (sortedDates.length < 2) {
    updatePortfolioHeader(
      holdings,
      usdJpyRate,
      isMixedCurrency,
      chartAnimationControl,
    );
    return;
  }

  // 2. 日付ごとに全銘柄の「時価 - コスト」を合算。データ欠損時は前方補填。
  const lastPrices = new Map(); // stockKey -> price

  const dataPoints = sortedDates.map((dateStr) => {
    let totalValue = 0;
    let totalCost = 0;

    holdings.forEach((stock) => {
      const stockKey = makeStockKey(stock.market, stock.symbol);
      const info = stockHistoryMap.get(stockKey);

      let price = info.days.get(dateStr);
      if (price === undefined) {
        price = lastPrices.get(stockKey) || 0; // 前方の有効な値を採用
      } else {
        lastPrices.set(stockKey, price);
      }

      totalValue += price * info.shares * info.rate;
    });
    return { x: new Date(dateStr).getTime(), y: totalValue };
  });

  // 3. データの変更がない場合は再描画をスキップ (SSEなどでのチラつき防止)
  const currentSignature = JSON.stringify(
    dataPoints.map((p) => p.y.toFixed(0)),
  );
  if (pfSummaryChartInstance && currentSignature === lastPfChartSignature) {
    updatePortfolioHeader(
      holdings,
      usdJpyRate,
      isMixedCurrency,
      chartAnimationControl,
    );
    return;
  }
  lastPfChartSignature = currentSignature;

  // ヘッダー表示を更新
  updatePortfolioHeader(
    holdings,
    usdJpyRate,
    isMixedCurrency,
    chartAnimationControl,
  );

  // 描画処理 (既存チャートの更新または新規作成)

  if (pfSummaryChartInstance) {
    // 既存のチャートデータを更新 (アニメーションなしまたはスムース)
    pfSummaryChartInstance.data.datasets[0].data = dataPoints;
    pfSummaryChartInstance.data.datasets[0].borderColor = "#6bb6ff";
    pfSummaryChartInstance.options.animation = chartAnimationControl.animation;
    pfSummaryChartInstance.update(chartAnimationControl.updateMode);
    return;
  }

  pfSummaryChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "合計評価額",
          data: dataPoints,
          borderColor: "#6bb6ff",
          borderWidth: 2,
          fill: {
            target: "origin",
            above: "rgba(107, 182, 255, 0.2)",
          },
          tension: 0.3,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: chartAnimationControl.animation,
      interaction: { intersect: false, mode: "index" },
      plugins: { legend: { display: false } },
      scales: {
        x: {
          type: "time",
          time: { unit: "day", displayFormats: { day: "MM/dd" } },
          ticks: { color: "#ccc", maxTicksLimit: 10 },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
        y: {
          ticks: {
            color: "#ccc",
            callback: (val) => Number(val).toLocaleString(),
          },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
      },
    },
  });
}

function calculatePortfolioMetrics(holdings, currentFxRate, prevFxRate) {
  let totalCurrentValueJPY = 0;
  let totalCostJPY = 0;
  let totalTodayPlJPY = 0;

  holdings.forEach((stock) => {
    const shares = toFiniteNumber(stock.shares, 0);
    const avgPrice = toFiniteNumber(stock.avg_price, 0);
    const currentPrice = toFiniteNumber(stock.price, 0);
    const changeLocal = toFiniteNumber(stock.change, 0);

    const isUSD = stock.currency === "USD" || stock.market === "us";
    const curRate = isUSD ? currentFxRate : 1.0;
    const prvRate = isUSD ? prevFxRate : 1.0;

    // avg_fx_rate が null/undefined/0 の場合は現在の為替レートをデフォルトとする
    const rawAvgFx = stock.avg_fx_rate;
    const avgFxRate =
      rawAvgFx !== null && rawAvgFx !== undefined && Number(rawAvgFx) > 0
        ? Number(rawAvgFx)
        : curRate;

    const costRate = isUSD ? avgFxRate : 1.0;

    totalCurrentValueJPY += shares * currentPrice * curRate;
    totalCostJPY += shares * avgPrice * costRate;

    const prevPriceLocal = currentPrice - changeLocal;
    totalTodayPlJPY +=
      shares * (currentPrice * curRate - prevPriceLocal * prvRate);
  });

  return {
    totalValue: totalCurrentValueJPY,
    totalCost: totalCostJPY,
    totalPl: totalCurrentValueJPY - totalCostJPY,
    todayPl: totalTodayPlJPY,
  };
}

function updatePortfolioHeader(
  holdings,
  usdJpyRate,
  isMixedCurrency,
  chartAnimationControl,
) {
  if (usdJpyRate === null && isMixedCurrency) {
    const valEl = DOM.get("pf-total-value");
    if (valEl) valEl.textContent = "為替データ取得中...";
    return;
  }

  const currentFxRate = usdJpyRate || 1.0;
  const usdJpyChange = toFiniteNumber(state.indices?.USDJPY?.change, 0);
  const prevFxRate = currentFxRate - usdJpyChange;

  const metrics = calculatePortfolioMetrics(
    holdings,
    currentFxRate,
    prevFxRate,
  );

  const plClass = metrics.totalPl >= 0 ? "pos" : "neg";
  const plSign = metrics.totalPl >= 0 ? "+" : "";
  const todayPlClass = metrics.todayPl >= 0 ? "pos" : "neg";
  const todayPlSign = metrics.todayPl >= 0 ? "+" : "";
  const unitLabel = isMixedCurrency ? " (JPY換算)" : " (JPY)";

  const valEl = DOM.get("pf-total-value");
  if (valEl)
    valEl.textContent =
      metrics.totalValue.toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
      }) + unitLabel;
  const plEl = DOM.get("pf-total-pl");
  if (plEl) {
    plEl.textContent = "";
    const span = document.createElement("span");
    span.className = plClass;
    span.textContent = `${plSign}${metrics.totalPl.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
    plEl.appendChild(span);
  }
  const tdayEl = DOM.get("pf-today-pl");
  if (tdayEl) {
    tdayEl.textContent = "";
    const span = document.createElement("span");
    span.className = todayPlClass;
    span.textContent = `${todayPlSign}${metrics.todayPl.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
    tdayEl.appendChild(span);
  }

  // Draw sector chart
  drawSectorPieChart(holdings, currentFxRate, chartAnimationControl);
}

let pfSummaryChartInstance = null;
let pfSectorChartInstance = null;
function drawSectorPieChart(holdings, usdJpyRate, chartAnimationControl) {
  const canvas = DOM.get("pf-sector-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  const sectorMap = {};
  holdings.forEach((stock) => {
    const sector = stock.sector || "Other";
    const shares = toFiniteNumber(stock.shares, 0);
    const price = toFiniteNumber(stock.price, 0);
    const rate = stock.currency === "USD" ? usdJpyRate : 1.0;
    const value = shares * price * rate;

    sectorMap[sector] = (sectorMap[sector] || 0) + value;
  });

  const sortedSectors = Object.entries(sectorMap).sort((a, b) => b[1] - a[1]);
  const labels = sortedSectors.map((s) => s[0]);
  const data = sortedSectors.map((s) => s[1]);

  const colors = [
    "#6bb6ff",
    "#7dffb0",
    "#ff7d7d",
    "#ffcc66",
    "#ff7daa",
    "#9bc9ff",
    "#a3e635",
    "#f87171",
    "#fbbf24",
    "#f472b6",
    "#818cf8",
    "#34d399",
    "#fb7185",
    "#eab308",
    "#c084fc",
  ];

  if (pfSectorChartInstance) {
    pfSectorChartInstance.data.labels = labels;
    pfSectorChartInstance.data.datasets[0].data = data;
    pfSectorChartInstance.options.animation =
      chartAnimationControl?.animation ?? false;
    pfSectorChartInstance.update(chartAnimationControl?.updateMode ?? "none");
    return;
  }

  pfSectorChartInstance = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: labels,
      datasets: [
        {
          data: data,
          backgroundColor: colors,
          borderColor: "rgba(11, 16, 32, 0.8)",
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: chartAnimationControl?.animation ?? false,
      plugins: {
        legend: {
          position: "right",
          labels: { color: "#ccc", font: { size: 10 }, boxWidth: 10 },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const val = ctx.raw;
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const pct = ((val / total) * 100).toFixed(1);
              return ` ${ctx.label}: ${val.toLocaleString(undefined, { maximumFractionDigits: 0 })} JPY (${pct}%)`;
            },
          },
        },
      },
      cutout: "60%",
    },
  });
}

function applySortOrder(market, stocks) {
  const order = getSortOrder(market);
  const defaultSymbols = DEFAULT_SYMBOLS[market] || [];
  const userStocks = stocks.filter((s) => !defaultSymbols.includes(s.symbol));
  const defaultStocks = stocks.filter((s) => defaultSymbols.includes(s.symbol));
  const sortedUser = [...userStocks].sort(
    (a, b) => orderIndex(order, a.symbol) - orderIndex(order, b.symbol),
  );
  const sortedDefault = [...defaultStocks].sort(
    (a, b) =>
      defaultSymbols.indexOf(a.symbol) - defaultSymbols.indexOf(b.symbol),
  );
  return [...sortedUser, ...sortedDefault];
}

function getAllStocks() {
  return [...state.stocks.us, ...state.stocks.jp, ...state.stocks.idx];
}

const formatMarketCap = (value) => {
  const num = Number(value);
  if (Number.isNaN(num)) return value ?? "--";
  return num.toLocaleString();
};

function isBlankDetailValue(value, field) {
  if (value === null || value === undefined) return true;
  if (typeof value === "string" && value.trim() === "") return true;
  if (field === "pe_ratio" || field === "market_cap") {
    const num = Number(value);
    return !Number.isFinite(num) || num <= 0;
  }
  return false;
}

function setDetailItemVisibility(wrapper, field, visible) {
  const row = wrapper.querySelector(`.detail-item-${field}`);
  if (!row) return;
  row.classList.toggle("detail-item-hidden", !visible);
  row.style.display = visible ? "" : "none";
  row.setAttribute("aria-hidden", visible ? "false" : "true");
}

function hideBulkAnalyzeStatus() {
  const box = DOM.get("bulkAnalyzeStatus");
  if (!box) return;
  box.classList.remove("show", "running", "success", "error");
  setTimeout(() => {
    if (!box.classList.contains("show")) box.textContent = "";
  }, 350);
}

function setBulkAnalyzeStatus(message = "", type = "") {
  const box = DOM.get("bulkAnalyzeStatus");
  if (!box) return;
  if (!message) {
    hideBulkAnalyzeStatus();
    return;
  }
  box.textContent = message;
  box.className = "bulk-analyze-status show";
  if (type) box.classList.add(type);
}

function destroyChart(el) {
  if (!el) return;
  if (el.__destroyTimer) {
    clearTimeout(el.__destroyTimer);
    el.__destroyTimer = null;
  }
  const chart = chartInstances.get(el);
  if (chart) {
    try {
      chart.destroy();
    } catch (e) {
      console.warn("Chart destruction failed:", e);
    } finally {
      chartInstances.delete(el);
      if (el.__chart) el.__chart = null;
    }
  }
}

function cancelScheduledDestroy(root) {
  if (!root) return;
  root.querySelectorAll("canvas").forEach((canvas) => {
    if (canvas.__destroyTimer) {
      clearTimeout(canvas.__destroyTimer);
      canvas.__destroyTimer = null;
    }
  });
}

function triggerPriceFlash(priceEl, flashClass) {
  if (!priceEl) return;
  if (!priceEl.__flashCleanupHandler) {
    priceEl.__flashCleanupHandler = (event) => {
      if (
        event.animationName === "flash-green" ||
        event.animationName === "flash-red"
      ) {
        priceEl.classList.remove("flash-up", "flash-down");
      }
    };
    priceEl.addEventListener("animationend", priceEl.__flashCleanupHandler);
  }
  priceEl.classList.remove("flash-up", "flash-down");
  void priceEl.offsetWidth;
  priceEl.classList.add(flashClass);
}

function clearChartError(wrapper) {
  const container =
    wrapper.querySelector(".chart-container") ||
    wrapper.querySelector(".chart-canvas-container");
  if (!container) return;
  const err = container.querySelector(".chart-error");
  if (err) err.remove();
}

function showChartError(wrapper, msg, type = "error") {
  const container =
    wrapper.querySelector(".chart-container") ||
    wrapper.querySelector(".chart-canvas-container");
  if (!container) return;

  destroyChart(wrapper.querySelector(".chart-canvas"));
  clearChartError(wrapper);

  const errDiv = document.createElement("div");
  errDiv.className = `chart-error ${type}`;
  const icon = type === "info" ? "ℹ️" : "⚠️";
  const iconDiv = createEl("div", "chart-error-icon", icon);
  const msgDiv = createEl("div", "chart-error-msg", msg);
  errDiv.appendChild(iconDiv);
  errDiv.appendChild(msgDiv);
  container.appendChild(errDiv);
}

const drawSparkline = (wrapper, data) => {
  const canvas = wrapper.querySelector(".spark-canvas");
  if (!canvas || !data?.length) return;
  setSparklineVisibility(wrapper, true);
  const ctx = canvas.getContext("2d");
  const stockKey = wrapper.dataset.stockKey;
  if (stockKey) {
    const signature = getSparklineSignature(data);
    if (signature) sparklineSignatureMap.set(stockKey, signature);
  }

  const existingChart = chartInstances.get(canvas);
  if (existingChart) {
    existingChart.data.labels = data.map((_, i) => i);
    existingChart.data.datasets[0].data = data.map((d) => d.price);
    existingChart.update("none");
    return;
  }

  destroyChart(canvas);

  const chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: data.map((_, i) => i),
      datasets: [
        {
          data: data.map((d) => d.price),
          borderColor: "#6bb6ff",
          borderWidth: 1.5,
          fill: false,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      elements: { line: { tension: 0.3 } },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } },
    },
  });
  chartInstances.set(canvas, chart);
};

function getDatasetHiddenStateByLabel(chart) {
  const hiddenByLabel = new Map();
  if (!chart?.data?.datasets) return hiddenByLabel;
  chart.data.datasets.forEach((ds, index) => {
    if (!ds?.label) return;
    hiddenByLabel.set(ds.label, !chart.isDatasetVisible(index));
  });
  return hiddenByLabel;
}

function applyDatasetHiddenStateByLabel(chart, hiddenByLabel) {
  if (!chart?.data?.datasets || !hiddenByLabel) return;
  chart.data.datasets.forEach((ds, index) => {
    if (!ds?.label) return;
    if (hiddenByLabel.has(ds.label)) {
      const shouldBeHidden = !!hiddenByLabel.get(ds.label);
      chart.setDatasetVisibility(index, !shouldBeHidden);
    }
  });
}

// #endregion API Status & Formatting Helpers
