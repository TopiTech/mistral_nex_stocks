// M-8: Declare the IntersectionObserver as a module-level variable that is
// initialized lazily on first use (or explicitly via initCardIntersectionObserver).
// This avoids top-level browser API side effects that run before DOMContentLoaded,
// which can cause issues with script load ordering.
let cardIntersectionObserver = null;

function _createCardIntersectionObserver() {
  if (typeof IntersectionObserver === "undefined") return null;
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
  if (!cardIntersectionObserver) {
    return { observe() {}, unobserve() {}, disconnect() {} };
  }
  return cardIntersectionObserver;
}

function getSupportedIntervalsForPeriod(period) {
  switch (period) {
    case "1d":
    case "5d":
      return ["auto", "1m", "5m", "15m", "1h", "1d"];
    case "1mo":
      return ["auto", "5m", "15m", "1h", "1d", "1wk"];
    case "3mo":
    case "6mo":
      return ["auto", "1h", "1d", "1wk", "1mo"];
    case "1y":
    case "2y":
      return ["auto", "1h", "1d", "1wk", "1mo"];
    case "5y":
    case "max":
      return ["auto", "1d", "1wk", "1mo"];
    default:
      return ["auto", "1m", "5m", "15m", "1h", "1d", "1wk", "1mo"];
  }
}

function updateIntervalControlsVisibility(parentEl, stockKey, currentPeriod) {
  if (!parentEl) return;
  const intervalGroups = parentEl.querySelectorAll(".interval-controls");
  if (!intervalGroups.length) return;

  const supported = getSupportedIntervalsForPeriod(currentPeriod);
  let currentInterval = getChartPref(stockKey, "interval", "auto");

  if (!supported.includes(currentInterval)) {
    currentInterval = "auto";
    setChartPref(stockKey, "interval", "auto");
  }

  intervalGroups.forEach((group) => {
    group.querySelectorAll("[data-interval]").forEach((btn) => {
      const inv = btn.dataset.interval;
      const isSupported = supported.includes(inv);
      btn.style.display = isSupported ? "" : "none";
      btn.classList.toggle("active", inv === currentInterval);
    });
  });
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
  const timeoutId = setTimeout(() => controller.abort(), 15000);

  const showErrorBanner = (text) => {
    // Reset fields to failure placeholder
    if (sectorEl) sectorEl.textContent = "取得失敗";
    if (industryEl) industryEl.textContent = "取得失敗";
    if (mcapEl) mcapEl.textContent = "取得失敗";
    if (peEl) peEl.textContent = "取得失敗";

    if (!detailInner) return;
    // Prevent stacking multiple banners on repeated failures
    const staleBanner = detailInner.querySelector(".detail-error-banner");
    if (staleBanner) staleBanner.remove();

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
      let url;
      try {
        url = new URL("/api/stock-details", window.location.origin);
      } catch (_urlErr) {
        return { error: "URL構築に失敗しました" };
      }
      url.search = new URLSearchParams({ symbol, market }).toString();
      const res = await fetch(url.toString(), { signal: controller.signal });
      try {
        return await res.json();
      } catch (_jsonErr) {
        return {
          error: `サーバー応答の解析に失敗しました (HTTP ${res.status})`,
        };
      }
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
    const active = !!stockKey && state.isFavorite(stockKey);
    star.classList.toggle("active", active);
    star.setAttribute("aria-pressed", String(active));
    star.setAttribute(
      "aria-label",
      active ? "お気に入りから削除" : "お気に入りに追加",
    );
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
  updateOpenStockDetailDrawerHeader(wrapper, stock);

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
    const isScrapedOrRealtime =
      stock.is_realtime ||
      ["tradingview", "yahoojp", "sbi", "alphavantage", "yahoous"].includes(
        (stock.source || "").toLowerCase(),
      );
    if (isScrapedOrRealtime) {
      priceEl.classList.add("scraping-success");
    }
    priceEl.classList.add("updating");
    if (priceEl.__updateTimer) clearTimeout(priceEl.__updateTimer);
    priceEl.__updateTimer = setTimeout(
      () => priceEl.classList.remove("updating"),
      1200,
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

  const ptsEl = wrapper.querySelector(".compact-pts");
  if (ptsEl) {
    let ptsTxt = "";
    if (
      stock.pts_price != null &&
      typeof stock.pts_price === "number" &&
      stock.pts_price > 0
    ) {
      ptsTxt =
        typeof formatPrice === "function"
          ? `PTS ${formatPrice(stock.pts_price, stock)}`
          : `PTS ${stock.pts_price}`;
    } else if (
      stock.pts_price != null &&
      String(stock.pts_price).trim() !== ""
    ) {
      ptsTxt = `PTS ${stock.pts_price}`;
    }
    if (ptsEl.textContent !== ptsTxt) {
      ptsEl.textContent = ptsTxt;
    }
    ptsEl.hidden = !ptsTxt;
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
  let container = wrapper.querySelector(".chart-container");
  if (
    !container &&
    typeof currentDrawerActiveWrapper !== "undefined" &&
    currentDrawerActiveWrapper === wrapper
  ) {
    const drawerChart = document.getElementById("drawerTabChartContent");
    if (drawerChart) {
      container = drawerChart.querySelector(".chart-container");
    }
  }
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
            const _isLine =
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
            const _isLine =
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
window.updateStockUI = updateStockUI;
window.updateExistingCard = updateExistingCard;

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
  const expandButton = createEl("button", "expand-toggle-btn");
  expandButton.type = "button";
  expandButton.id = `expand-${uniqueId}`;
  expandButton.setAttribute("aria-expanded", "false");
  expandButton.setAttribute("aria-label", `${stock.symbol} の詳細を開く`);
  expandButton.setAttribute("aria-controls", `detail-content-${uniqueId}`);
  inner.appendChild(expandButton);
  detail.id = `detail-content-${uniqueId}`;

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

  const stockKey = makeStockKey(stock.market || "us", stock.symbol);
  const currentType = getChartPref(stockKey, "type", "candlestick");

  // 1. Chart Style Selector
  const typeGroup = createEl("div", "control-group type-controls");
  const chartStyles = [
    { id: "candlestick", label: "ローソク足" },
    { id: "line", label: "ライン" },
    { id: "area", label: "エリア" },
    { id: "heikin_ashi", label: "平均足" },
  ];
  chartStyles.forEach((style) => {
    const btn = createEl(
      "button",
      `control-btn ${currentType === style.id ? "active" : ""}`,
      style.label,
    );
    btn.dataset.type = style.id;
    typeGroup.appendChild(btn);
  });
  chartControls.appendChild(typeGroup);

  // 2. Technical Indicator Chips
  const indGroup = createEl("div", "control-group ind-controls");
  const indicators = [
    { key: "ind_ma5", label: "MA5", defaultVal: "on" },
    { key: "ind_ma25", label: "MA25", defaultVal: "on" },
    { key: "ind_ma75", label: "MA75", defaultVal: "off" },
    { key: "ind_ma200", label: "MA200", defaultVal: "off" },
    { key: "ind_bollinger", label: "ボリンジャー", defaultVal: "off" },
    { key: "ind_rsi", label: "RSI", defaultVal: "off" },
    { key: "ind_macd", label: "MACD", defaultVal: "off" },
  ];
  indicators.forEach((ind) => {
    const isOn = getChartPref(stockKey, ind.key, ind.defaultVal) === "on";
    const btn = createEl(
      "button",
      `chip-btn ${isOn ? "active" : ""}`,
      ind.label,
    );
    btn.dataset.ind = ind.key;
    indGroup.appendChild(btn);
  });
  chartControls.appendChild(indGroup);

  // 3. Period controls
  const periodGroup = createEl("div", "control-group period-controls");
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

  // 3.5. Interval (時間足) controls
  const currentInterval = getChartPref(stockKey, "interval", "auto");
  const intervalGroup = createEl("div", "control-group interval-controls");
  const intervalDefs = [
    { id: "auto", label: "Auto" },
    { id: "1m", label: "1分" },
    { id: "5m", label: "5分" },
    { id: "15m", label: "15分" },
    { id: "1h", label: "1時間" },
    { id: "1d", label: "日足" },
    { id: "1wk", label: "週足" },
    { id: "1mo", label: "月足" },
  ];
  intervalDefs.forEach((item) => {
    const btn = createEl(
      "button",
      `control-btn ${currentInterval === item.id ? "active" : ""}`,
      item.label,
    );
    btn.dataset.interval = item.id;
    intervalGroup.appendChild(btn);
  });
  chartControls.appendChild(intervalGroup);
  updateIntervalControlsVisibility(
    chartControls,
    stockKey,
    getChartPref(stockKey, "period", "3mo"),
  );

  // 4. Action Buttons (AI Technical Line Drawing & Fullscreen View)
  const actionGroup = createEl("div", "control-group tool-actions-group");
  const isEligible = window.APP_CONFIG?.is_ai_technical_lines_eligible ?? false;
  const aiBtnText = isEligible
    ? "✨ AIテクニカル描画"
    : "🔒 AIテクニカル描画 (Medium/Large限定)";
  const aiBtnCls = isEligible
    ? "ai-tech-lines-btn"
    : "ai-tech-lines-btn locked";

  const aiTechBtn = createEl("button", aiBtnCls, aiBtnText);
  aiTechBtn.type = "button";
  aiTechBtn.title = isEligible
    ? "AIがサポート線・抵抗線・トレンドラインを動的描画"
    : "Mistral Medium/Largeモデル選択時のみ利用可能";
  actionGroup.appendChild(aiTechBtn);

  const fsBtn = createEl("button", "fs-chart-btn", "⛶ 全画面表示");
  fsBtn.type = "button";
  actionGroup.appendChild(fsBtn);

  chartControls.appendChild(actionGroup);
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

  const symEl = createEl("div", "compact-symbol", stock.symbol);
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
  let ptsTxt = "";
  if (
    stock.pts_price != null &&
    typeof stock.pts_price === "number" &&
    stock.pts_price > 0
  ) {
    ptsTxt =
      typeof formatPrice === "function"
        ? `PTS ${formatPrice(stock.pts_price, stock)}`
        : `PTS ${stock.pts_price}`;
  } else if (stock.pts_price != null && String(stock.pts_price).trim() !== "") {
    ptsTxt = `PTS ${stock.pts_price}`;
  }
  const ptsEl = createEl("div", "compact-pts", ptsTxt);
  if (!ptsTxt) {
    ptsEl.setAttribute("aria-hidden", "true");
  }
  right.appendChild(ptsEl);
  right.appendChild(createEl("div", "compact-pf-info"));
  const sparkline = createEl("div", "sparkline");
  sparkline.setAttribute("aria-hidden", "true");
  const sparkCanvas = createEl("canvas", "spark-canvas");
  sparkline.appendChild(sparkCanvas);
  right.appendChild(sparkline);
  compact.appendChild(right);

  const compactActions = createEl("div", "compact-actions");

  // Keyboard-accessible detail expansion for the card body (R3): the card
  // itself is a <div> whose click handler opens the drawer, which is
  // pointer-only. This explicit button provides the same action via
  // Enter/Space while keeping the fullscreen/favorite descendants distinct.
  const expandBtn = createEl("button", "compact-expand-btn", "詳細");
  expandBtn.type = "button";
  expandBtn.setAttribute("aria-expanded", "false");
  expandBtn.setAttribute("aria-controls", "stock-detail-drawer-overlay");
  expandBtn.setAttribute("aria-haspopup", "dialog");
  expandBtn.setAttribute(
    "aria-label",
    `${stock.symbol}（${market}）の詳細を開く`,
  );
  compactActions.appendChild(expandBtn);

  const cardFsBtn = createEl("button", "card-fs-btn fs-chart-btn", "⛶");
  cardFsBtn.type = "button";
  cardFsBtn.title = "全画面テクニカルチャートを開く";
  cardFsBtn.setAttribute(
    "aria-label",
    `${stock.symbol} の全画面チャートを表示`,
  );
  compactActions.appendChild(cardFsBtn);

  const favStar = createEl("button", "favorite-star", "★");
  favStar.type = "button";
  favStar.setAttribute("aria-pressed", "false");
  favStar.setAttribute("aria-label", "お気に入りに追加");
  compactActions.appendChild(favStar);

  compact.appendChild(compactActions);

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
    if (
      e.target.closest(".favorite-star") ||
      e.target.closest(".card-fs-btn") ||
      e.target.closest(".compact-expand-btn")
    )
      return;
    openStockDetailDrawer(getLatestStockForDrawer(stock, wrapper), wrapper);
  });
  expandBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openStockDetailDrawer(getLatestStockForDrawer(stock, wrapper), wrapper);
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
    openAiDrawer(stock.symbol, stock.name, stock.market);
  });
  setupBtn(".chat-send-btn", () => sendChat(wrapper));
  setupBtn(".pf-edit-btn", () => openPortfolioModal(stockKey));
  setupBtn(".alert-edit-btn", () => openAlertModal(stockKey));

  detail.querySelector(".chat-input")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      sendChat(wrapper);
    }
  });
  detail
    .querySelector(".card-color-picker")
    ?.addEventListener("input", function () {
      updateStockColor(stockKey, this.value);
    });

  detail
    .querySelectorAll(
      "[data-type], [data-period], [data-interval], [data-ind], [data-volume]",
    )
    .forEach((btn) => {
      if (btn.dataset.ind !== undefined || btn.dataset.volume !== undefined) {
        const isOn =
          btn.dataset.ind !== undefined
            ? getChartPref(
                stockKey,
                btn.dataset.ind,
                btn.dataset.ind === "ind_ma5" || btn.dataset.ind === "ind_ma25"
                  ? "on"
                  : "off",
              ) === "on"
            : getChartPref(stockKey, "volume", "on") === "on";
        btn.setAttribute("aria-pressed", String(isOn));
      }
      btn.addEventListener("click", () => {
        if (btn.dataset.type) {
          setChartPref(stockKey, "type", btn.dataset.type);
          btn.parentElement.querySelectorAll("[data-type]").forEach((b) => {
            const on = b === btn;
            b.classList.toggle("active", on);
            b.setAttribute("aria-pressed", String(on));
          });
        } else if (btn.dataset.period) {
          setChartPref(stockKey, "period", btn.dataset.period);
          btn.parentElement.querySelectorAll("[data-period]").forEach((b) => {
            const on = b === btn;
            b.classList.toggle("active", on);
            b.setAttribute("aria-pressed", String(on));
          });
          updateIntervalControlsVisibility(
            wrapper,
            stockKey,
            btn.dataset.period,
          );
        } else if (btn.dataset.interval) {
          setChartPref(stockKey, "interval", btn.dataset.interval);
          btn.parentElement.querySelectorAll("[data-interval]").forEach((b) => {
            const on = b === btn;
            b.classList.toggle("active", on);
            b.setAttribute("aria-pressed", String(on));
          });
        } else if (btn.dataset.ind) {
          const key = btn.dataset.ind;
          const defaultVal =
            key === "ind_ma5" || key === "ind_ma25" ? "on" : "off";
          const curr = getChartPref(stockKey, key, defaultVal);
          const next = curr === "on" ? "off" : "on";
          setChartPref(stockKey, key, next);
          btn.classList.toggle("active", next === "on");
          btn.setAttribute("aria-pressed", String(next === "on"));
        } else if (btn.dataset.volume !== undefined) {
          const curr = getChartPref(stockKey, "volume", "on");
          const next = curr === "on" ? "off" : "on";
          setChartPref(stockKey, "volume", next);
          btn.classList.toggle("active", next === "on");
          btn.setAttribute("aria-pressed", String(next === "on"));
        }
        refreshStockChart(
          wrapper,
          getChartPref(stockKey, "period", "3mo"),
          getChartPref(stockKey, "interval", "auto"),
        );
      });
    });

  setupBtn(".expand-toggle-btn", function () {
    const isExpanded = !wrapper.classList.contains("is-expanded");
    wrapper.classList.toggle("is-expanded", isExpanded);
    this.setAttribute("aria-expanded", String(isExpanded));
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
  if (sortedStocks.length === 0) {
    const empty = createEl(
      "div",
      "no-results market-empty-state",
      `${market === "us" ? "米国" : market === "jp" ? "日本" : "インデックス/ETF"}市場の銘柄データがありません。検索から銘柄を追加できます。`,
    );
    empty.style.gridColumn = "1 / -1";
    container.appendChild(empty);
  }
  const orderedWrappers = [];
  let _createdCount = 0;
  let _updatedCount = 0;
  sortedStocks.forEach((stock) => {
    const latestStock = { ...stock, market };
    const stockKey = makeStockKey(market, stock.symbol);
    let wrapper = existingCards.get(stockKey);
    if (wrapper) {
      _updatedCount += 1;
      updateExistingCard(wrapper, latestStock);
      existingCards.delete(stockKey);
    } else {
      _createdCount += 1;
      wrapper = createStockCard(latestStock, market);
    }
    orderedWrappers.push(wrapper);
  });

  existingCards.forEach((wrapper) => {
    if (currentDrawerActiveWrapper === wrapper) {
      closeStockDetailDrawer();
    }
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
  updateTabCounts();
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
    wrapper.classList.add("is-expanded");
    const sym = stock?.symbol || "";
    const openExpandBtn = wrapper.querySelector(".expand-toggle-btn");
    if (openExpandBtn) {
      openExpandBtn.setAttribute("aria-expanded", "true");
      openExpandBtn.setAttribute("aria-label", `${sym} の詳細を閉じる`);
    }

    // 展開したカードが画面内に収まるようにスムーズスクロール（reduced-motion配慮）
    const _prefersReduced =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setTimeout(() => {
      wrapper.scrollIntoView({
        behavior: _prefersReduced ? "auto" : "smooth",
        block: "nearest",
      });
    }, 100);
    // Expand時にフォーカスを詳細パネル先頭へ移動（キーボード操作性）
    const _firstFocusable = detail.querySelector(
      "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
    );
    if (_firstFocusable) {
      try {
        _firstFocusable.focus({ preventScroll: true });
      } catch (_e) {
        _firstFocusable.focus();
      }
    }
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
  if (wrapper) {
    wrapper.classList.remove("is-expanded");
    const stockKey = wrapper.dataset.stockKey || "";
    const stockSymbol = stockKey.includes(":")
      ? stockKey.split(":").slice(1).join(":")
      : stockKey;
    const expandBtn = wrapper.querySelector(".expand-toggle-btn");
    if (expandBtn) {
      expandBtn.setAttribute("aria-expanded", "false");
      expandBtn.setAttribute("aria-label", `${stockSymbol} の詳細を開く`);
    }
  }
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
  // 既にスケルトン表示中（タイマー起動済み）なら期限を延長しない。
  // connectSSE が stocks 空のたびに再呼び出しするため、毎回 Date.now() を
  // 上書きすると 8 秒タイムアウトが永遠にリセットされ続ける。
  if (!sseState.skeletonShownAt) {
    sseState.skeletonShownAt = Date.now();
  }
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
    updateTabCounts();
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
      if (cardIntersectionObserver) {
        cardIntersectionObserver.unobserve(w);
      }
      unregisterWrapper(w.dataset.stockKey, w);
      w.remove();
    }
  });

  renderFavorites();
  updateTabCounts();

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
    let _totalCost = 0;

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
          labels: { color: "#e8f0ff", font: { size: 10 }, boxWidth: 10 },
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

function setBulkAnalyzeStatus(message = "", type = "", structuredData = null) {
  const box = DOM.get("bulkAnalyzeStatus");
  if (!box) return;
  if (!message && !structuredData) {
    hideBulkAnalyzeStatus();
    return;
  }

  box.className = "bulk-analyze-status show";
  if (type) box.classList.add(type);

  // If structuredData is supplied, render a rich interactive result card UI
  if (
    structuredData &&
    (Array.isArray(structuredData.success) ||
      Array.isArray(structuredData.failed))
  ) {
    box.replaceChildren();

    const container = document.createElement("div");
    container.className = "bulk-result-container";

    // Header
    const header = document.createElement("div");
    header.className = "bulk-result-header";

    const titleGroup = document.createElement("div");
    titleGroup.className = "bulk-result-title-group";

    const icon = document.createElement("span");
    icon.className = "bulk-result-icon";
    icon.textContent = structuredData.isCancelled ? "⚠️" : "✨";
    titleGroup.appendChild(icon);

    const title = document.createElement("strong");
    title.className = "bulk-result-title";
    const sCount = structuredData.success?.length || 0;
    const fCount = structuredData.failed?.length || 0;
    title.textContent = structuredData.isCancelled
      ? `一括AI分析がキャンセルされました (完了分 成功: ${sCount}件 / 失敗: ${fCount}件)`
      : `一括AI分析が完了しました (成功: ${sCount}件 / 失敗: ${fCount}件)`;
    titleGroup.appendChild(title);
    header.appendChild(titleGroup);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "bulk-result-close-btn";
    closeBtn.setAttribute("aria-label", "分析結果を閉じる");
    closeBtn.textContent = "✕";
    closeBtn.addEventListener("click", () => hideBulkAnalyzeStatus());
    header.appendChild(closeBtn);

    container.appendChild(header);

    // Cards Grid
    const grid = document.createElement("div");
    grid.className = "bulk-result-grid";

    // Success items
    (structuredData.success || []).forEach((item) => {
      const card = document.createElement("div");
      card.className = "bulk-result-card";

      const topRow = document.createElement("div");
      topRow.className = "bulk-card-top";

      const symEl = document.createElement("strong");
      symEl.className = "bulk-card-symbol";
      symEl.textContent = item.symbol;
      topRow.appendChild(symEl);

      const badges = document.createElement("div");
      badges.className = "bulk-card-badges";

      if (item.recommendation && item.recommendation !== "--") {
        const recBadge = document.createElement("span");
        const recNorm = String(item.recommendation).toLowerCase();
        const isBuy = recNorm.includes("買") || recNorm.includes("buy");
        const isSell = recNorm.includes("売") || recNorm.includes("sell");
        recBadge.className = `bulk-badge rec ${isBuy ? "buy" : isSell ? "sell" : "neutral"}`;
        recBadge.textContent = item.recommendation;
        badges.appendChild(recBadge);
      }

      if (item.sentiment && item.sentiment !== "--") {
        const sentBadge = document.createElement("span");
        const sentNorm = String(item.sentiment).toLowerCase();
        const isBull = sentNorm.includes("強気") || sentNorm.includes("bull");
        const isBear = sentNorm.includes("弱気") || sentNorm.includes("bear");
        sentBadge.className = `bulk-badge sent ${isBull ? "bull" : isBear ? "bear" : "neutral"}`;
        sentBadge.textContent = item.sentiment;
        badges.appendChild(sentBadge);
      }

      topRow.appendChild(badges);
      card.appendChild(topRow);

      // Action button to open drawer for this stock
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "bulk-card-detail-btn";
      btn.textContent = "詳細・チャートを見る →";
      btn.addEventListener("click", () => {
        const stockKey =
          item.stockKey ||
          (item.market ? makeStockKey(item.market, item.symbol) : null);
        const matchingWrapper =
          (stockKey
            ? document.querySelector(
                `.stock-wrapper[data-stock-key="${stockKey}"]`,
              )
            : null) ||
          document.querySelector(
            `.stock-wrapper[data-symbol="${item.symbol}"]`,
          );
        const stockData = matchingWrapper?.__stockData ||
          (stockKey ? getStockByKey(stockKey) : null) || {
            symbol: item.symbol,
            market: item.market,
            name: item.name,
          };
        openStockDetailDrawer(stockData, matchingWrapper);
      });
      card.appendChild(btn);

      grid.appendChild(card);
    });

    // Failed items
    (structuredData.failed || []).forEach((item) => {
      const card = document.createElement("div");
      card.className = "bulk-result-card failed";

      const topRow = document.createElement("div");
      topRow.className = "bulk-card-top";

      const symEl = document.createElement("strong");
      symEl.className = "bulk-card-symbol";
      symEl.textContent = item.symbol;
      topRow.appendChild(symEl);

      const errBadge = document.createElement("span");
      errBadge.className = "bulk-badge rec sell";
      errBadge.textContent = "失敗";
      topRow.appendChild(errBadge);
      card.appendChild(topRow);

      const errMsg = document.createElement("div");
      errMsg.className = "bulk-card-err-msg";
      errMsg.textContent = item.error || "データ取得失敗";
      card.appendChild(errMsg);

      grid.appendChild(card);
    });

    container.appendChild(grid);
    box.appendChild(container);
  } else {
    // Legacy / simple text status during progress
    box.textContent = message;
  }
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
  errDiv.setAttribute("role", type === "info" ? "status" : "alert");
  errDiv.appendChild(iconDiv);
  errDiv.appendChild(msgDiv);
  if (type !== "info") {
    const retry = createEl("button", "chart-error-retry", "再試行");
    retry.type = "button";
    retry.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      refreshStockChart(
        wrapper,
        getChartPref(wrapper.dataset.stockKey, "period", "3mo"),
      );
    });
    errDiv.appendChild(retry);
  }
  container.appendChild(errDiv);
}

const drawSparkline = (wrapper, data) => {
  const canvas = wrapper.querySelector(".spark-canvas");
  if (!canvas || !data?.length) return;
  setSparklineVisibility(wrapper, true);

  const stockKey = wrapper.dataset.stockKey;
  if (stockKey) {
    const signature = getSparklineSignature(data);
    if (signature) sparklineSignatureMap.set(stockKey, signature);
  }

  // Clean up any legacy Chart.js instance on this canvas
  destroyChart(canvas);

  const prices = data
    .map((d) => (typeof d === "number" ? d : d?.price))
    .filter((p) => typeof p === "number" && !isNaN(p));
  if (prices.length < 2) return;

  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width || 0, 100);
  const height = Math.max(rect.height || 0, 32);
  const dpr = window.devicePixelRatio || 1;

  if (
    canvas.width !== Math.round(width * dpr) ||
    canvas.height !== Math.round(height * dpr)
  ) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }

  const ctx = canvas.getContext("2d");
  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  // Determine trend & color based on stock change and color theme
  const stock = wrapper.__stockData;
  const change =
    stock?.change != null
      ? Number(stock.change)
      : prices[prices.length - 1] - prices[0];
  const isJpScheme =
    typeof getColorSchemePreference === "function" &&
    getColorSchemePreference() === "jp_standard";

  let strokeColor = "#6bb6ff";
  let fillColorTop = "rgba(107, 182, 255, 0.25)";
  let fillColorBottom = "rgba(107, 182, 255, 0.0)";

  if (change > 0) {
    const hex = isJpScheme ? "#f43f5e" : "#10b981";
    const rgb = isJpScheme ? "244, 63, 94" : "16, 185, 129";
    strokeColor = hex;
    fillColorTop = `rgba(${rgb}, 0.28)`;
    fillColorBottom = `rgba(${rgb}, 0.0)`;
  } else if (change < 0) {
    const hex = isJpScheme ? "#10b981" : "#f43f5e";
    const rgb = isJpScheme ? "16, 185, 129" : "244, 63, 94";
    strokeColor = hex;
    fillColorTop = `rgba(${rgb}, 0.28)`;
    fillColorBottom = `rgba(${rgb}, 0.0)`;
  }

  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = max - min || 1;
  const padTop = 3;
  const padBottom = 3;
  const drawHeight = Math.max(height - padTop - padBottom, 1);

  const points = prices.map((p, i) => {
    const x = (i / (prices.length - 1)) * width;
    const y = padTop + drawHeight - ((p - min) / range) * drawHeight;
    return { x, y };
  });

  // Area fill with vertical gradient
  const gradient = ctx.createLinearGradient(0, padTop, 0, height);
  gradient.addColorStop(0, fillColorTop);
  gradient.addColorStop(1, fillColorBottom);

  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const curr = points[i];
    const cpX = (prev.x + curr.x) / 2;
    ctx.bezierCurveTo(cpX, prev.y, cpX, curr.y, curr.x, curr.y);
  }
  ctx.lineTo(width, height);
  ctx.lineTo(0, height);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // Smooth line stroke
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const curr = points[i];
    const cpX = (prev.x + curr.x) / 2;
    ctx.bezierCurveTo(cpX, prev.y, cpX, curr.y, curr.x, curr.y);
  }
  ctx.strokeStyle = strokeColor;
  ctx.lineWidth = 1.8;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke();

  ctx.restore();
};

let currentDrawerSymbol = "";
let currentDrawerName = "";
let currentDrawerMarket = "us";
let aiDrawerTrigger = null;

function openAiDrawer(symbol, name, market) {
  aiDrawerTrigger =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  currentDrawerSymbol = symbol || "MNS";
  currentDrawerName = name || symbol || "銘柄";
  currentDrawerMarket = market || "us";

  const overlay = document.getElementById("ai-drawer-overlay");
  const symEl = document.getElementById("ai-drawer-symbol");
  const nameEl = document.getElementById("ai-drawer-name");
  const messagesEl = document.getElementById("ai-drawer-chat-messages");
  const inputEl = document.getElementById("aiDrawerInput");

  if (symEl) symEl.textContent = currentDrawerSymbol;
  if (nameEl) nameEl.textContent = currentDrawerName;

  if (messagesEl) {
    // Use safe DOM API instead of innerHTML to prevent XSS from symbol/name injection.
    // The symbols/names come from user input and backend data; rendering them must
    // never produce HTML that could execute scripts.
    const container = document.createElement("div");
    container.className = "ai-msg assistant";

    const iconText = document.createTextNode("\u{1F916}\u{FE0F} ");
    const strong = document.createElement("strong");
    const nameSpan = document.createTextNode(
      `${currentDrawerSymbol} (${currentDrawerName})`,
    );
    const label = document.createTextNode(
      " についてAIアナリストに質問できます。",
    );

    strong.appendChild(nameSpan);
    container.appendChild(iconText);
    container.appendChild(strong);
    container.appendChild(label);
    container.appendChild(document.createElement("br"));

    const example = document.createElement("span");
    example.textContent =
      "(例: 「直近の業績評価は？」「競合と比較した優位性は？」)";
    container.appendChild(example);

    messagesEl.replaceChildren(container);
  }

  if (overlay) {
    overlay.removeAttribute("inert");
    overlay.classList.remove("hidden");
    overlay.setAttribute("aria-hidden", "false");
    if (typeof lockBodyScroll === "function") {
      lockBodyScroll();
    }
  }
  if (inputEl) {
    inputEl.value = "";
    setTimeout(() => {
      // Do not restore focus into an inert, already-closed drawer when the
      // user opens and immediately dismisses it.
      if (
        overlay &&
        !overlay.classList.contains("hidden") &&
        overlay.getAttribute("aria-hidden") === "false"
      ) {
        inputEl.focus();
      }
    }, 200);
  }
}

function closeAiDrawer() {
  const overlay = document.getElementById("ai-drawer-overlay");
  if (overlay && !overlay.classList.contains("hidden")) {
    if (overlay.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    overlay.classList.add("hidden");
    overlay.setAttribute("aria-hidden", "true");
    overlay.setAttribute("inert", "");
    if (typeof unlockBodyScroll === "function") {
      unlockBodyScroll();
    }
  }
  aiDrawerTrigger?.focus?.();
  aiDrawerTrigger = null;
}

async function sendAiDrawerMessage() {
  const inputEl = document.getElementById("aiDrawerInput");
  const messagesEl = document.getElementById("ai-drawer-chat-messages");
  if (!inputEl || !messagesEl) return;

  const text = inputEl.value.trim();
  if (!text) return;

  const userMsg = document.createElement("div");
  userMsg.className = "ai-msg user";
  userMsg.textContent = text;
  messagesEl.appendChild(userMsg);
  inputEl.value = "";
  messagesEl.scrollTop = messagesEl.scrollHeight;

  const loadingMsg = document.createElement("div");
  loadingMsg.className = "ai-msg assistant";
  loadingMsg.textContent = "考え中...";
  messagesEl.appendChild(loadingMsg);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  try {
    const genToken = () => {
      if (typeof createRequestToken === "function") return createRequestToken();
      if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
      const bytes = new Uint8Array(24);
      globalThis.crypto.getRandomValues(bytes);
      return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    };

    const payload = {
      symbol: currentDrawerSymbol,
      market: currentDrawerMarket || "us",
      message: text,
      request_token: genToken(),
    };

    // SECURITY: Use DOM API instead of innerHTML to prevent XSS from unsanitized
    // AI replies. Split on real newlines and render as separate text nodes so
    // \n becomes visual line breaks without ever passing through HTML parsing.
    const renderReply = (replyText) => {
      loadingMsg.replaceChildren();
      const headerStrong = document.createElement("strong");
      headerStrong.textContent = "【AI回答】";
      loadingMsg.appendChild(headerStrong);
      const br = document.createElement("br");
      loadingMsg.appendChild(br);
      const lines = String(replyText || "").split("\n");
      for (let i = 0; i < lines.length; i++) {
        if (i > 0) {
          const lineBreak = document.createElement("br");
          loadingMsg.appendChild(lineBreak);
        }
        loadingMsg.appendChild(document.createTextNode(lines[i]));
      }
    };

    // C-2: ストリーミングを試み、非対応/失敗時はポーリングへフォールバック。
    let reply = null;
    let streamApiError = null;
    if (typeof streamChatReply === "function") {
      try {
        reply = await streamChatReply(payload, (partial) => {
          renderReply(partial);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        });
      } catch (streamErr) {
        if (streamErr && streamErr.isMistralError) {
          // サーバーからの明示的なAPIエラー：メッセージをそのまま表示。
          streamApiError = streamErr;
        } else {
          reply = null;
        }
      }
    }
    if (streamApiError) throw streamApiError;

    if (reply === null) {
      if (typeof chatPollingReply === "function") {
        reply = await chatPollingReply(payload);
      } else {
        // フォールバック: 従来のインライン・ポーリング
        let data = {};
        let resOk = false;
        const pollMax =
          typeof CHAT_POLL_MAX_ATTEMPTS !== "undefined"
            ? CHAT_POLL_MAX_ATTEMPTS
            : 6;
        const pollInterval =
          typeof CHAT_POLL_INTERVAL_MS !== "undefined"
            ? CHAT_POLL_INTERVAL_MS
            : 2000;
        const fetchFn =
          typeof apiFetch === "function"
            ? apiFetch
            : typeof csrfFetch === "function"
              ? async (url, opts) => {
                  const res = await csrfFetch(url, opts);
                  const json = await res.json().catch(() => ({}));
                  return { response: res, data: json };
                }
              : async (url, opts) => {
                  const res = await fetch(url, opts);
                  const json = await res.json().catch(() => ({}));
                  return { response: res, data: json };
                };
        for (let attempt = 0; attempt <= pollMax; attempt++) {
          const { response: res, data: fetched } = await fetchFn("/api/chat", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
          });
          data = fetched || {};
          if (!res.ok) {
            const detailReason = data?.details?.reason
              ? String(data.details.reason)
              : "";
            const errMsg =
              detailReason ||
              String(data.message || data.error || `HTTP ${res.status}`);
            throw new Error(errMsg);
          }
          if (!data.fetching) {
            resOk = true;
            break;
          }
          if (typeof sleep === "function") {
            await sleep(pollInterval);
          } else {
            await new Promise((resolve) => setTimeout(resolve, pollInterval));
          }
        }
        if (!resOk) {
          throw new Error("AI応答の生成がタイムアウトしました");
        }
        reply = data.reply || data.summary || "応答を取得できませんでした";
      }
    }
    renderReply(reply);
  } catch (err) {
    loadingMsg.textContent = `エラー: ${err.message || "接続エラーが発生しました。"}`;
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function updateTabCounts() {
  const usCount =
    typeof state !== "undefined" &&
    state?.stocks?.us &&
    Array.isArray(state.stocks.us)
      ? state.stocks.us.length
      : document.querySelectorAll("#us-stocks .stock-wrapper").length;

  const jpCount =
    typeof state !== "undefined" &&
    state?.stocks?.jp &&
    Array.isArray(state.stocks.jp)
      ? state.stocks.jp.length
      : document.querySelectorAll("#jp-stocks .stock-wrapper").length;

  const idxCount =
    typeof state !== "undefined" &&
    state?.stocks?.idx &&
    Array.isArray(state.stocks.idx)
      ? state.stocks.idx.length
      : document.querySelectorAll("#idx-stocks .stock-wrapper").length;

  let pfCount = 0;
  if (typeof getAllStocks === "function") {
    const allStocks = getAllStocks();
    const holdings = allStocks.filter((s) => {
      const sh =
        typeof toFiniteNumber === "function"
          ? toFiniteNumber(s?.shares, NaN)
          : Number(s?.shares);
      return Number.isFinite(sh) && sh > 0;
    });
    pfCount = holdings.length;
  } else {
    pfCount = document.querySelectorAll(
      "#portfolio-stocks .stock-wrapper",
    ).length;
  }

  const usEl = document.getElementById("tab-us-count");
  const jpEl = document.getElementById("tab-jp-count");
  const idxEl = document.getElementById("tab-idx-count");
  const pfEl = document.getElementById("tab-portfolio-count");
  const holdingEl = document.getElementById("pf-holding-count");

  if (usEl) usEl.textContent = String(usCount);
  if (jpEl) jpEl.textContent = String(jpCount);
  if (idxEl) idxEl.textContent = String(idxCount);
  if (pfEl) pfEl.textContent = String(pfCount);
  if (holdingEl) holdingEl.textContent = `${pfCount} 銘柄`;
}

function trapDrawerFocus(event, drawerOverlay) {
  if (!drawerOverlay || event.key !== "Tab") return;
  const focusable = Array.from(
    drawerOverlay.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((el) => !el.hasAttribute("inert") && el.offsetParent !== null);
  if (!focusable.length) {
    event.preventDefault();
    drawerOverlay.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function initAiDrawerEvents() {
  const closeBtn = document.getElementById("closeAiDrawerBtn");
  const overlay = document.getElementById("ai-drawer-overlay");
  const sendBtn = document.getElementById("sendAiDrawerBtn");
  const inputEl = document.getElementById("aiDrawerInput");

  closeBtn?.addEventListener("click", closeAiDrawer);
  overlay?.addEventListener("click", (e) => {
    if (e.target === overlay) closeAiDrawer();
  });
  overlay?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeAiDrawer();
    } else if (e.key === "Tab") {
      trapDrawerFocus(e, overlay);
    }
  });
  sendBtn?.addEventListener("click", sendAiDrawerMessage);
  inputEl?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      sendAiDrawerMessage();
    }
  });
}

function initStockDetailDrawerEvents() {
  const closeBtn = document.getElementById("closeStockDetailDrawerBtn");
  const overlay = document.getElementById("stock-detail-drawer-overlay");
  closeBtn?.addEventListener("click", closeStockDetailDrawer);
  overlay?.addEventListener("click", (e) => {
    if (e.target === overlay) closeStockDetailDrawer();
  });
  overlay?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeStockDetailDrawer();
    } else if (e.key === "Tab") {
      trapDrawerFocus(e, overlay);
    }
  });

  const chartTabBtn = document.getElementById("drawerTabChartBtn");
  const aiTabBtn = document.getElementById("drawerTabAiBtn");
  const chartContent = document.getElementById("drawerTabChartContent");
  const aiContent = document.getElementById("drawerTabAiContent");

  function selectChartTab() {
    chartTabBtn?.classList.add("active");
    chartTabBtn?.setAttribute("aria-selected", "true");
    aiTabBtn?.classList.remove("active");
    aiTabBtn?.setAttribute("aria-selected", "false");
    chartContent?.classList.remove("hidden");
    chartContent?.removeAttribute("hidden");
    aiContent?.classList.add("hidden");
    aiContent?.setAttribute("hidden", "");
    chartTabBtn?.focus();
  }

  function selectAiTab() {
    aiTabBtn?.classList.add("active");
    aiTabBtn?.setAttribute("aria-selected", "true");
    chartTabBtn?.classList.remove("active");
    chartTabBtn?.setAttribute("aria-selected", "false");
    aiContent?.classList.remove("hidden");
    aiContent?.removeAttribute("hidden");
    chartContent?.classList.add("hidden");
    chartContent?.setAttribute("hidden", "");
    aiTabBtn?.focus();
  }

  chartTabBtn?.addEventListener("click", selectChartTab);
  aiTabBtn?.addEventListener("click", selectAiTab);

  const tabBar = document.querySelector(".drawer-tab-bar");
  tabBar?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      if (chartTabBtn?.classList.contains("active")) {
        selectAiTab();
      } else {
        selectChartTab();
      }
    }
  });
}

let currentDrawerActiveWrapper = null;
let stockDetailDrawerTrigger = null;

/**
 * Cards are updated in place for SSE and polling updates, so their click
 * handler's creation-time stock object can be stale.  Prefer the wrapper's
 * latest data, while retaining state/fallback fields needed by older cards.
 */
function getLatestStockForDrawer(stock, wrapper) {
  const stockKey = wrapper?.dataset?.stockKey;
  const stateStock = stockKey ? getStockByKey(stockKey) : null;
  const wrapperStock = wrapper?.__stockData || null;
  if (!stock && !stateStock && !wrapperStock) return null;
  const market =
    wrapperStock?.market ||
    stateStock?.market ||
    stock?.market ||
    wrapper?.dataset?.market;
  return {
    ...(stock || {}),
    ...(stateStock || {}),
    ...(wrapperStock || {}),
    ...(market ? { market } : {}),
  };
}

function formatDrawerPrice(stock) {
  if (typeof stock?.price === "boolean") return "--";
  if (typeof formatPrice === "function") {
    return formatPrice(stock?.price, stock);
  }
  const price = Number(stock?.price);
  return Number.isFinite(price)
    ? price.toLocaleString()
    : (stock?.price ?? "--");
}

function renderStockDetailDrawerHeader(stock) {
  const symbolEl = document.getElementById("drawer-stock-symbol");
  const nameEl = document.getElementById("drawer-stock-name");
  const priceBadge = document.getElementById("drawer-stock-price-badge");
  const sym = stock.symbol || "";
  const name = stock.name || stock.companyName || "";
  const priceStr = formatDrawerPrice(stock);
  const changeVal = parseFloat(stock.change_percent || 0);
  const isPos = changeVal >= 0;
  const sign = isPos ? "+" : "";

  if (symbolEl) symbolEl.textContent = sym;
  if (nameEl) nameEl.textContent = name;
  if (priceBadge) {
    priceBadge.textContent = `${priceStr} (${sign}${changeVal.toFixed(2)}%)`;
    priceBadge.className = `drawer-price-badge ${isPos ? "pos" : "neg"}`;
  }
}

function updateOpenStockDetailDrawerHeader(wrapper, stock) {
  if (currentDrawerActiveWrapper !== wrapper) return;
  const overlay = document.getElementById("stock-detail-drawer-overlay");
  if (!overlay || overlay.classList.contains("hidden")) return;
  renderStockDetailDrawerHeader(getLatestStockForDrawer(stock, wrapper));
}

function openStockDetailDrawer(stock, wrapper) {
  stock = getLatestStockForDrawer(stock, wrapper);
  if (!stock) return;
  const overlay = document.getElementById("stock-detail-drawer-overlay");
  if (!overlay) return;

  if (currentDrawerActiveWrapper && currentDrawerActiveWrapper !== wrapper) {
    closeStockDetailDrawer();
  }
  currentDrawerActiveWrapper = wrapper;
  stockDetailDrawerTrigger =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;

  renderStockDetailDrawerHeader(stock);

  const chartContent = document.getElementById("drawerTabChartContent");
  const aiContent = document.getElementById("drawerTabAiContent");

  const detailPanel = wrapper ? wrapper.querySelector(".detail-panel") : null;

  if (detailPanel) {
    const info = detailPanel.querySelector(".detail-info");
    const actions = detailPanel.querySelector(".detail-actions");
    const chartControls = detailPanel.querySelector(".chart-controls");
    const chartContainer = detailPanel.querySelector(".chart-container");
    const pnlContainer = detailPanel.querySelector(".pnl-chart-container");

    const analyzeBtn = detailPanel.querySelector(".analyze-btn");
    const aiSection = detailPanel.querySelector(".ai-section");
    const chatToggleBtn = detailPanel.querySelector(".chat-toggle-btn");
    const chatSection = detailPanel.querySelector(".chat-section");

    if (chartContent) {
      chartContent.replaceChildren();
      if (info) chartContent.appendChild(info);
      if (actions) chartContent.appendChild(actions);
      if (chartControls) chartContent.appendChild(chartControls);
      if (chartContainer) chartContent.appendChild(chartContainer);
      if (pnlContainer) chartContent.appendChild(pnlContainer);
    }

    if (aiContent) {
      aiContent.replaceChildren();
      if (analyzeBtn) {
        analyzeBtn.style.display = "block";
        analyzeBtn.style.width = "100%";
        analyzeBtn.style.margin = "12px 0";
        aiContent.appendChild(analyzeBtn);
      }
      if (aiSection) {
        aiContent.appendChild(aiSection);
      }
      if (chatToggleBtn) {
        chatToggleBtn.style.display = "none";
      }
      if (chatSection) {
        chatSection.classList.add("show");
        chatSection.style.marginTop = "20px";
        aiContent.appendChild(chatSection);
      }
    }
  } else {
    if (chartContent) {
      chartContent.replaceChildren();
      const emptyMsg = createEl(
        "div",
        "drawer-empty-msg",
        "銘柄カードが現在の表示領域に見つかりません。一覧画面をご確認ください。",
      );
      emptyMsg.style.padding = "24px";
      emptyMsg.style.textAlign = "center";
      emptyMsg.style.color = "var(--text-muted, #888)";
      chartContent.appendChild(emptyMsg);
    }
    if (aiContent) {
      aiContent.replaceChildren();
    }
  }

  overlay.removeAttribute("inert");
  overlay.classList.remove("hidden");
  overlay.setAttribute("aria-hidden", "false");
  if (typeof lockBodyScroll === "function") {
    lockBodyScroll();
  }

  // Move focus into the modal drawer so keyboard users do not remain on the
  // trigger outside an aria-modal dialog. The keydown trap only becomes
  // effective after focus has entered the drawer.
  const firstDrawerFocusable = overlay.querySelector(
    'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  );
  firstDrawerFocusable?.focus?.();

  // Reflect the open drawer on the card's keyboard-accessible expand button.
  if (wrapper) {
    const drawerExpandBtn = wrapper.querySelector(".compact-expand-btn");
    if (drawerExpandBtn) {
      drawerExpandBtn.setAttribute("aria-expanded", "true");
      drawerExpandBtn.setAttribute(
        "aria-label",
        `${stock.symbol}（${stock.market || "us"}）の詳細を閉じる`,
      );
    }
  }

  if (wrapper && stock) {
    const stockKey =
      wrapper.dataset.stockKey ||
      makeStockKey(stock.market || "us", stock.symbol);
    const isPortfolio = wrapper.dataset.marketContext === "portfolio";
    const period = isPortfolio
      ? "3mo"
      : getChartPref(stockKey, "period", "3mo");

    setTimeout(() => {
      refreshStockChart(wrapper, period);
      ensureStockDetails(wrapper);
    }, 50);
  }
}

function closeStockDetailDrawer() {
  const overlay = document.getElementById("stock-detail-drawer-overlay");
  if (overlay && !overlay.classList.contains("hidden")) {
    if (overlay.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    overlay.classList.add("hidden");
    overlay.setAttribute("aria-hidden", "true");
    overlay.setAttribute("inert", "");
    if (typeof unlockBodyScroll === "function") {
      unlockBodyScroll();
    }
  }

  if (currentDrawerActiveWrapper) {
    const drawerExpandBtn = currentDrawerActiveWrapper.querySelector(
      ".compact-expand-btn",
    );
    if (drawerExpandBtn) {
      drawerExpandBtn.setAttribute("aria-expanded", "false");
      const key = currentDrawerActiveWrapper.dataset.stockKey || "";
      const symbol = key.includes(":")
        ? key.split(":").slice(1).join(":")
        : key;
      const market = currentDrawerActiveWrapper.dataset.market || "us";
      drawerExpandBtn.setAttribute(
        "aria-label",
        `${symbol}（${market}）の詳細を開く`,
      );
    }
    const detailInner = currentDrawerActiveWrapper.querySelector(
      ".detail-panel .detail-inner",
    );
    const chartContent = document.getElementById("drawerTabChartContent");
    const aiContent = document.getElementById("drawerTabAiContent");

    if (detailInner) {
      if (chartContent) {
        Array.from(chartContent.children).forEach((child) =>
          detailInner.appendChild(child),
        );
      }
      if (aiContent) {
        Array.from(aiContent.children).forEach((child) =>
          detailInner.appendChild(child),
        );
      }
      const analyzeBtn = detailInner.querySelector(".analyze-btn");
      if (analyzeBtn) {
        analyzeBtn.style.display = "";
        analyzeBtn.style.width = "";
        analyzeBtn.style.margin = "";
      }
      const chatToggleBtn = detailInner.querySelector(".chat-toggle-btn");
      if (chatToggleBtn) {
        chatToggleBtn.style.display = "";
      }
      const chatSection = detailInner.querySelector(".chat-section");
      if (chatSection) {
        chatSection.style.marginTop = "";
      }
    }
    currentDrawerActiveWrapper = null;
  }
  stockDetailDrawerTrigger?.focus?.();
  stockDetailDrawerTrigger = null;
}

if (typeof state !== "undefined" && state?.subscribe) {
  state.subscribe("stocks", updateTabCounts);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    initAiDrawerEvents();
    initStockDetailDrawerEvents();
    updateTabCounts();
  });
} else {
  initAiDrawerEvents();
  initStockDetailDrawerEvents();
  updateTabCounts();
}

// #region AI Technical Lines & Fullscreen Modal
function showAiTechnicalLoading(container) {
  if (!container) return;
  let overlay = container.querySelector(".ai-tech-loading-overlay");
  if (!overlay) {
    overlay = createEl("div", "ai-tech-loading-overlay");
    // DOM APIで構築（innerHTML不使用・静的文言のみのため挿入リスクなし）
    overlay.appendChild(createEl("div", "ai-tech-spinner"));
    overlay.appendChild(
      createEl("div", "ai-tech-loading-title", "✨ AI テクニカル分析中"),
    );
    overlay.appendChild(
      createEl(
        "div",
        "ai-tech-loading-subtitle",
        "Mistral AI がサポート線・抵抗線・トレンドラインを動的解析中...",
      ),
    );
    container.style.position = "relative";
    container.appendChild(overlay);
  }
  overlay.classList.remove("hidden");
}

function hideAiTechnicalLoading(container) {
  if (!container) return;
  const overlay = container.querySelector(".ai-tech-loading-overlay");
  if (overlay) overlay.remove();
}

async function triggerAiTechnicalLines(wrapper) {
  const targetWrapper = wrapper || currentDrawerActiveWrapper;
  if (!targetWrapper) return;

  const isEligible = window.APP_CONFIG?.is_ai_technical_lines_eligible ?? false;
  if (!isEligible) {
    showToast(
      "🔒 AIテクニカル線描画機能は Mistral Medium または Large モデルでのみご利用いただけます。設定画面（⚙）よりモデルを変更してください。",
      "warning",
      7000,
    );
    return;
  }

  const stockKey = targetWrapper.dataset.stockKey;
  const stock = targetWrapper.__stockData || getStockByKey(stockKey);
  if (!stock) return;

  const btns = targetWrapper.querySelectorAll(".ai-tech-lines-btn");
  const fsBtn = document.getElementById("fs-ai-tech-lines-btn");
  const allBtns = [...Array.from(btns), fsBtn].filter(Boolean);

  allBtns.forEach((b) => {
    b.disabled = true;
    b.classList.add("loading");
    b.textContent = "⏳ AI分析中...";
  });

  const chartContainer =
    targetWrapper.querySelector(".chart-container") ||
    document.querySelector("#stock-detail-drawer .chart-container");
  const fsCanvasWrapper = document.getElementById("fs-chart-canvas-wrapper");
  showAiTechnicalLoading(chartContainer);
  if (
    fsCanvasWrapper &&
    !document
      .getElementById("chart-fullscreen-modal")
      ?.classList.contains("hidden")
  ) {
    showAiTechnicalLoading(fsCanvasWrapper);
  }

  try {
    const period = getChartPref(stockKey, "period", "3mo");
    const prefetch = getFreshPrefetchedHistory(stockKey, period);
    const historyData = prefetch ? prefetch.formattedData : [];

    const fetchFn = typeof csrfFetch === "function" ? csrfFetch : fetch;
    const res = await fetchFn("/api/ai-technical-lines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: stock.symbol,
        market: stock.market,
        period: period,
        history_data: historyData,
      }),
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data?.ok) {
      if (data?.model_restricted) {
        showToast(`🔒 ${data.error}`, "warning", 8000);
      } else {
        showToast(
          data?.error ||
            `AIテクニカル線の生成に失敗しました (HTTP ${res.status})`,
          "error",
        );
      }
      return;
    }

    targetWrapper.__aiTechnicalLines = data;
    showToast("✨ AIテクニカル線の描画を適用しました", "success");

    renderAiTechnicalLinesSummary(targetWrapper, data);
    refreshStockChart(targetWrapper, period);

    // If Fullscreen modal is open, redraw it too
    const fsModal = document.getElementById("chart-fullscreen-modal");
    if (fsModal && !fsModal.classList.contains("hidden")) {
      const fsCanvas = document.getElementById("fs-chart-canvas");
      if (fsCanvas) {
        const freshHistory =
          getFreshPrefetchedHistory(stockKey, period) || prefetch;
        if (freshHistory) {
          drawChart(
            targetWrapper,
            freshHistory.formattedData,
            freshHistory.ohlcData,
            {
              targetCanvas: fsCanvas,
              aiTechnicalLines: data,
            },
          );
        }
      }
    }
  } catch (err) {
    logger.error("AI Technical Lines error:", err);
    showToast("通信エラーが発生しました。", "error");
  } finally {
    hideAiTechnicalLoading(chartContainer);
    hideAiTechnicalLoading(fsCanvasWrapper);
    allBtns.forEach((b) => {
      b.disabled = false;
      b.classList.remove("loading");
      b.textContent = "✨ AIテクニカル描画";
    });
  }
}

function renderAiTechnicalLinesSummary(wrapper, data) {
  if (!wrapper) return;
  let summaryBox = wrapper.querySelector(".ai-tech-lines-summary");
  if (!summaryBox) {
    summaryBox = createEl("div", "ai-tech-lines-summary hidden");
    const chartContainer = wrapper.querySelector(".chart-container");
    if (chartContainer && chartContainer.parentNode) {
      chartContainer.parentNode.insertBefore(
        summaryBox,
        chartContainer.nextSibling,
      );
    }
  }

  if (!data || !data.summary) {
    summaryBox.classList.add("hidden");
    return;
  }

  summaryBox.classList.remove("hidden");
  summaryBox.replaceChildren();

  const header = createEl("div", "ai-tech-header");
  const badge = createEl("span", "ai-tech-badge", "✨ AIテクニカル分析要約");
  const biasCls = (data.trend_bias || "").toLowerCase();
  const bias = createEl(
    "span",
    `ai-tech-bias ${biasCls}`,
    data.trend_bias || "Neutral",
  );
  header.appendChild(badge);
  header.appendChild(bias);
  summaryBox.appendChild(header);

  const summaryText = createEl("div", "ai-tech-summary-text", data.summary);
  summaryBox.appendChild(summaryText);

  if (Array.isArray(data.lines) && data.lines.length > 0) {
    const list = createEl("div", "ai-tech-lines-list");
    data.lines.forEach((line) => {
      const item = createEl("div", `ai-tech-line-item ${line.type || ""}`);
      const colorDot = createEl("span", "ai-line-dot");
      colorDot.style.backgroundColor = line.color || "#00ff88";
      const labelSpan = createEl(
        "strong",
        "ai-line-label",
        line.label || line.type,
      );
      const descSpan = createEl("span", "ai-line-desc", line.description || "");

      item.appendChild(colorDot);
      item.appendChild(labelSpan);
      item.appendChild(descSpan);
      list.appendChild(item);
    });
    summaryBox.appendChild(list);
  }
}

function openFullscreenChart(wrapper) {
  const targetWrapper = wrapper || currentDrawerActiveWrapper;
  if (!targetWrapper) return;
  const stockKey = targetWrapper.dataset.stockKey;
  const stock = targetWrapper.__stockData || getStockByKey(stockKey);
  if (!stock) return;

  const modal = document.getElementById("chart-fullscreen-modal");
  if (!modal) return;
  modal.dataset.stockKey = stockKey;
  modal.removeAttribute("inert");
  modal.setAttribute("aria-hidden", "false");
  modal.classList.remove("hidden");
  modal.classList.add("show");
  modal.style.display = "flex";
  if (typeof lockBodyScroll === "function") {
    lockBodyScroll();
  }
  modal._previousFocus = document.activeElement;

  const symbolEl = document.getElementById("fs-stock-symbol");
  const nameEl = document.getElementById("fs-stock-name");
  const priceEl = document.getElementById("fs-stock-price");
  const toolbar = document.getElementById("fs-chart-toolbar");
  const canvas = document.getElementById("fs-chart-canvas");

  if (symbolEl) symbolEl.textContent = stock.symbol;
  if (nameEl) nameEl.textContent = stock.name || stock.companyName || "";
  if (priceEl) priceEl.textContent = formatPrice(stock.price, stock);

  if (toolbar) {
    toolbar.replaceChildren();
    const isEligible =
      window.APP_CONFIG?.is_ai_technical_lines_eligible ?? false;
    const aiBtnText = isEligible
      ? "✨ AIテクニカル描画"
      : "🔒 AIテクニカル描画 (Medium/Large限定)";
    const aiBtnCls = isEligible
      ? "fs-btn ai-tech-lines-btn"
      : "fs-btn ai-tech-lines-btn locked";

    const currentType = getChartPref(stockKey, "type", "candlestick");
    const currentPeriod = getChartPref(stockKey, "period", "3mo");
    const currentInterval = getChartPref(stockKey, "interval", "auto");

    // DOM APIで構築（innerHTML不使用）— チャート設定値はクラス属性にのみ反映
    const typeGroup = createEl("div", "control-group type-controls");
    [
      ["candlestick", "ローソク足"],
      ["line", "ライン"],
      ["area", "エリア"],
      ["heikin_ashi", "平均足"],
    ].forEach(([type, label]) => {
      const btn = createEl(
        "button",
        `control-btn ${currentType === type ? "active" : ""}`,
        label,
      );
      btn.type = "button";
      btn.dataset.type = type;
      typeGroup.appendChild(btn);
    });
    toolbar.appendChild(typeGroup);

    const indGroup = createEl("div", "control-group ind-controls");
    const indDefs = [
      ["ind_ma5", "MA5", "on"],
      ["ind_ma25", "MA25", "on"],
      ["ind_ma75", "MA75", "off"],
      ["ind_ma200", "MA200", "off"],
      ["ind_bollinger", "ボリンジャー", "off"],
      ["ind_rsi", "RSI", "off"],
      ["ind_macd", "MACD", "off"],
      ["volume", "出来高", "on"],
    ];
    indDefs.forEach(([key, label, def]) => {
      const isOn = getChartPref(stockKey, key, def) === "on";
      const btn = createEl("button", `chip-btn ${isOn ? "active" : ""}`, label);
      btn.type = "button";
      if (key === "volume") {
        btn.dataset.volume = "on";
      } else {
        btn.dataset.ind = key;
      }
      indGroup.appendChild(btn);
    });
    toolbar.appendChild(indGroup);

    const periodGroup = createEl("div", "control-group period-controls");
    ["1d", "5d", "1mo", "3mo", "6mo", "1y"].forEach((period) => {
      const btn = createEl(
        "button",
        `control-btn ${currentPeriod === period ? "active" : ""}`,
        period.toUpperCase(),
      );
      btn.type = "button";
      btn.dataset.period = period;
      periodGroup.appendChild(btn);
    });
    toolbar.appendChild(periodGroup);

    const intervalGroup = createEl("div", "control-group interval-controls");
    const intervalDefs = [
      { id: "auto", label: "Auto" },
      { id: "1m", label: "1分" },
      { id: "5m", label: "5分" },
      { id: "15m", label: "15分" },
      { id: "1h", label: "1時間" },
      { id: "1d", label: "日足" },
      { id: "1wk", label: "週足" },
      { id: "1mo", label: "月足" },
    ];
    intervalDefs.forEach((item) => {
      const btn = createEl(
        "button",
        `control-btn ${currentInterval === item.id ? "active" : ""}`,
        item.label,
      );
      btn.type = "button";
      btn.dataset.interval = item.id;
      intervalGroup.appendChild(btn);
    });
    toolbar.appendChild(intervalGroup);
    updateIntervalControlsVisibility(toolbar, stockKey, currentPeriod);

    const aiTechBtn = createEl("button", aiBtnCls, aiBtnText);
    aiTechBtn.type = "button";
    aiTechBtn.id = "fs-ai-tech-lines-btn";
    toolbar.appendChild(aiTechBtn);

    toolbar
      .querySelectorAll(
        "[data-type], [data-period], [data-interval], [data-ind], [data-volume]",
      )
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          if (btn.dataset.type) {
            setChartPref(stockKey, "type", btn.dataset.type);
            toolbar
              .querySelectorAll("[data-type]")
              .forEach((b) => b.classList.toggle("active", b === btn));
          } else if (btn.dataset.period) {
            setChartPref(stockKey, "period", btn.dataset.period);
            toolbar
              .querySelectorAll("[data-period]")
              .forEach((b) => b.classList.toggle("active", b === btn));
            updateIntervalControlsVisibility(
              toolbar,
              stockKey,
              btn.dataset.period,
            );
          } else if (btn.dataset.interval) {
            setChartPref(stockKey, "interval", btn.dataset.interval);
            toolbar
              .querySelectorAll("[data-interval]")
              .forEach((b) => b.classList.toggle("active", b === btn));
          } else if (btn.dataset.ind) {
            const key = btn.dataset.ind;
            const curr = getChartPref(
              stockKey,
              key,
              key === "ind_ma5" || key === "ind_ma25" ? "on" : "off",
            );
            const next = curr === "on" ? "off" : "on";
            setChartPref(stockKey, key, next);
            btn.classList.toggle("active", next === "on");
          } else if (btn.dataset.volume !== undefined) {
            const curr = getChartPref(stockKey, "volume", "on");
            const next = curr === "on" ? "off" : "on";
            setChartPref(stockKey, "volume", next);
            btn.classList.toggle("active", next === "on");
          }
          const p = getChartPref(stockKey, "period", "3mo");
          const inv = getChartPref(stockKey, "interval", "auto");
          refreshStockChart(targetWrapper, p, inv);
          setTimeout(() => {
            const prefetch = getFreshPrefetchedHistory(stockKey, p, inv);
            if (prefetch) {
              drawChart(
                targetWrapper,
                prefetch.formattedData,
                prefetch.ohlcData,
                {
                  targetCanvas: canvas,
                  aiTechnicalLines: targetWrapper.__aiTechnicalLines,
                  period: p,
                  interval: inv,
                },
              );
            }
          }, 50);
        });
      });

    toolbar
      .querySelector("#fs-ai-tech-lines-btn")
      ?.addEventListener("click", () => {
        triggerAiTechnicalLines(targetWrapper);
      });
  }

  modal._previousFocus = document.activeElement;
  modal.removeAttribute("inert");
  modal.classList.remove("hidden");
  modal.classList.add("show");
  modal.style.display = "flex";
  modal.setAttribute("aria-hidden", "false");

  const currentMode = typeof getSseMode === "function" ? getSseMode() : 2;
  const tvContainer = document.getElementById("tradingview-chart-container");
  const canvasWrapper = document.getElementById("fs-chart-canvas-wrapper");
  const viewToggle = document.getElementById("fs-chart-view-toggle");

  const isJpOrIdx =
    stock.market === "jp" ||
    stock.market === "idx" ||
    String(stock.symbol).endsWith(".T") ||
    String(stock.symbol).startsWith("^");

  let activeFsViewMode = "builtin";
  if (isJpOrIdx) {
    activeFsViewMode = "builtin";
  } else {
    const savedPref = getChartPref(stockKey, "fs_view_mode", null);
    if (savedPref === "tradingview" || savedPref === "builtin") {
      activeFsViewMode = savedPref;
    } else {
      activeFsViewMode = currentMode === 2 ? "tradingview" : "builtin";
    }
  }

  const applyFsViewMode = (mode) => {
    activeFsViewMode = mode;
    setChartPref(stockKey, "fs_view_mode", mode);

    if (viewToggle) {
      viewToggle.querySelectorAll("[data-fs-view]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.fsView === mode);
      });
    }

    if (mode === "tradingview" && window.TradingViewManager && tvContainer) {
      if (canvasWrapper) canvasWrapper.classList.add("hidden");
      if (toolbar) toolbar.classList.add("hidden");
      tvContainer.classList.remove("hidden");
      window.TradingViewManager.renderAdvancedChart(
        "tradingview-chart-container",
        stock.tv_symbol || stock.symbol,
        stock.exchange,
      );
    } else {
      if (canvasWrapper) canvasWrapper.classList.remove("hidden");
      if (toolbar) toolbar.classList.remove("hidden");
      if (tvContainer) {
        tvContainer.classList.add("hidden");
        if (window.TradingViewManager) {
          window.TradingViewManager.clearContainer(tvContainer);
        }
      }

      const period = getChartPref(stockKey, "period", "3mo");
      const interval = getChartPref(stockKey, "interval", "auto");
      const prefetch = getFreshPrefetchedHistory(stockKey, period, interval);
      if (prefetch) {
        drawChart(targetWrapper, prefetch.formattedData, prefetch.ohlcData, {
          targetCanvas: canvas,
          aiTechnicalLines: targetWrapper.__aiTechnicalLines,
          period: period,
          interval: interval,
        });
      } else {
        refreshStockChart(targetWrapper, period, interval).then(() => {
          const fresh = getFreshPrefetchedHistory(stockKey, period, interval);
          if (fresh) {
            drawChart(targetWrapper, fresh.formattedData, fresh.ohlcData, {
              targetCanvas: canvas,
              aiTechnicalLines: targetWrapper.__aiTechnicalLines,
              period: period,
              interval: interval,
            });
          }
        });
      }
    }
  };

  if (viewToggle) {
    if (isJpOrIdx) {
      viewToggle.classList.add("hidden");
    } else {
      viewToggle.classList.remove("hidden");
    }

    viewToggle.querySelectorAll("[data-fs-view]").forEach((btn) => {
      btn.onclick = () => {
        if (isJpOrIdx) return;
        const selectedView = btn.dataset.fsView;
        if (selectedView && selectedView !== activeFsViewMode) {
          applyFsViewMode(selectedView);
        }
      };
    });
  }

  applyFsViewMode(activeFsViewMode);

  const closeBtn = document.getElementById("closeFsChartModal");
  if (closeBtn) {
    closeBtn.onclick = closeFsChartModal;
    closeBtn.focus();
  }
  modal.onclick = (e) => {
    if (e.target === modal) closeFsChartModal();
  };

  if (modal._keydownHandler) {
    modal.removeEventListener("keydown", modal._keydownHandler);
    modal._keydownHandler = null;
  }
  modal._keydownHandler = (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeFsChartModal();
      return;
    }
    if (e.key !== "Tab") return;
    const focusable = Array.from(
      modal.querySelectorAll(
        'button:not([disabled]):not([style*="display: none"]):not(.hidden), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };
  modal.addEventListener("keydown", modal._keydownHandler);
}

function closeFsChartModal() {
  const modal = document.getElementById("chart-fullscreen-modal");
  if (modal && !modal.classList.contains("hidden")) {
    if (modal._keydownHandler) {
      modal.removeEventListener("keydown", modal._keydownHandler);
      modal._keydownHandler = null;
    }
    if (typeof unlockBodyScroll === "function") {
      unlockBodyScroll();
    }
    if (modal.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    modal.classList.remove("show");
    modal.classList.add("hidden");
    modal.style.display = "none";
    modal.setAttribute("aria-hidden", "true");
    modal.setAttribute("inert", "");
    delete modal.dataset.stockKey;
    const tvContainer = document.getElementById("tradingview-chart-container");
    if (tvContainer && window.TradingViewManager) {
      window.TradingViewManager.clearContainer(tvContainer);
    }
    if (
      modal._previousFocus &&
      typeof modal._previousFocus.focus === "function"
    ) {
      modal._previousFocus.focus();
      modal._previousFocus = null;
    }
  }
}

// Global Event Delegation for Fullscreen Chart and AI Technical Lines
document.addEventListener("click", (e) => {
  const fsBtn = e.target.closest(".fs-chart-btn");
  if (fsBtn) {
    e.stopPropagation();
    const wrapper =
      fsBtn.closest(".stock-wrapper") || currentDrawerActiveWrapper;
    if (wrapper) openFullscreenChart(wrapper);
    return;
  }
  const aiLinesBtn = e.target.closest(
    ".ai-tech-lines-btn:not(#fs-ai-tech-lines-btn)",
  );
  if (aiLinesBtn) {
    e.stopPropagation();
    const wrapper =
      aiLinesBtn.closest(".stock-wrapper") || currentDrawerActiveWrapper;
    if (wrapper) triggerAiTechnicalLines(wrapper);
    return;
  }
});
// #endregion AI Technical Lines & Fullscreen Modal
