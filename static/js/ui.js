// M-8: Declare the IntersectionObserver as a module-level variable that is
// initialized lazily on first use (or explicitly via initCardIntersectionObserver).
// This avoids top-level browser API side effects that run before DOMContentLoaded,
// which can cause issues with script load ordering.
// offsetParent is null for position:fixed descendants, so resolve focus-trap
// visibility via computed style + bounding rect (shared with utils.js).
function isUiElementVisible(el) {
  if (typeof isEffectivelyVisible === "function")
    return isEffectivelyVisible(el);
  if (!el || typeof el.getBoundingClientRect !== "function") return false;
  if (
    typeof window !== "undefined" &&
    typeof window.getComputedStyle === "function"
  ) {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
  } else if (el.offsetParent === null && el !== document.body) {
    return false;
  }
  const rect = el.getBoundingClientRect();
  return rect.width > 0 || rect.height > 0;
}

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
if (
  typeof window !== "undefined" &&
  typeof window.addEventListener === "function"
) {
  window.addEventListener(
    "beforeunload",
    function cleanupIntersectionObserver() {
      if (cardIntersectionObserver) {
        cardIntersectionObserver.disconnect();
        cardIntersectionObserver = null;
      }
    },
  );
}

// #region Detail Panel Management
function isStockDetailsCacheFresh(stockKey) {
  if (!stockDetailsCache.has(stockKey)) return false;
  const cachedAt = stockDetailsCacheAt.get(stockKey);
  if (!Number.isFinite(cachedAt)) return false;
  return Date.now() - cachedAt < STOCK_DETAILS_CACHE_TTL_MS;
}

async function ensureStockDetails(wrapper) {
  const stockKey = wrapper.dataset.stockKey;
  if (isStockDetailsCacheFresh(stockKey)) {
    renderDetailExtras(wrapper, stockDetailsCache.get(stockKey));
    return;
  }
  if (stockDetailsCache.has(stockKey)) {
    stockDetailsCache.delete(stockKey);
    stockDetailsCacheAt.delete(stockKey);
  }

  // 進行中の取得があれば破棄してから再実行する(リトライボタン/再オープンで
  // 重複ポーリングループが走り、遅い側の失敗が新しい描画を上書きするのを防ぐ)。
  wrapper.__detailsReqController?.abort();
  const requestController = new AbortController();
  wrapper.__detailsReqController = requestController;
  const isCurrentRequest = () =>
    wrapper.__detailsReqController === requestController;

  // (L16, 2026-11-07 review) ドロワーがこの銘柄で開いている間は .detail-info
  // が #stock-detail-drawer 配下へ移動済みのため、ラッパー直下の問い合わせは
  // null を返し、ローディング/失敗プレースホルダとエラーバナーが不可視の
  // インラインパネルにしか出なかった。renderDetailExtras(chart.js) と同じ
  // 「ラッパー → 開いているドロワー」フォールバック契約に揃える。
  const findDetailEl = (sel) =>
    wrapper.querySelector(sel) ||
    document.querySelector(`#stock-detail-drawer ${sel}`);

  const detailInner = wrapper.querySelector(".detail-inner");
  const sectorEl = findDetailEl(".detail-sector");
  const industryEl = findDetailEl(".detail-industry");
  const mcapEl = findDetailEl(".detail-mcap");
  const peEl = findDetailEl(".detail-pe");

  // Remove existing error banner if any (both locations, in case the drawer
  // moved between renders and a stale banner survived in the inline panel).
  const staleBanners = document.querySelectorAll(".detail-error-banner");
  staleBanners.forEach((el) => el.remove());

  // Show loading state visual feedback
  if (sectorEl) sectorEl.textContent = "取得中...";
  if (industryEl) industryEl.textContent = "取得中...";
  if (mcapEl) mcapEl.textContent = "取得中...";
  if (peEl) peEl.textContent = "取得中...";

  const symbol = wrapper.dataset.symbol;
  const market = wrapper.dataset.market || "us";

  const controller = requestController;
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 15000);

  const showErrorBanner = (text) => {
    // Reset fields to failure placeholder
    if (sectorEl) sectorEl.textContent = "取得失敗";
    if (industryEl) industryEl.textContent = "取得失敗";
    if (mcapEl) mcapEl.textContent = "取得失敗";
    if (peEl) peEl.textContent = "取得失敗";

    // (L16) バナーは「今見えている側」のコンテナへ出す: ドロワーがこの銘柄で
    // 開いていれば #drawerTabChartContent(可視)、閉じていれば元の
    // .detail-inner に入れる。detailInner はドロワー移動後は不可視のため。
    const drawerEl = document.getElementById("stock-detail-drawer");
    const isDrawerActive =
      currentDrawerActiveWrapper === wrapper ||
      Boolean(
        drawerEl?.classList.contains("open") &&
        drawerEl.dataset.stockKey === stockKey,
      );
    const bannerHost = isDrawerActive
      ? document.getElementById("drawerTabChartContent")
      : detailInner;
    if (!bannerHost) return;
    // Prevent stacking multiple banners on repeated failures
    const staleBanner = bannerHost.querySelector(".detail-error-banner");
    if (staleBanner) staleBanner.remove();

    // Create single error banner at the bottom of detail panel info
    const banner = document.createElement("div");
    banner.className = "detail-error-banner";

    const label = document.createElement("span");
    label.textContent = `詳細データの取得失敗: ${text}`;
    banner.appendChild(label);

    const retryBtn = document.createElement("button");
    retryBtn.type = "button";
    retryBtn.textContent = "再試行";
    retryBtn.className = "detail-error-retry-btn";
    retryBtn.addEventListener("click", (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
      ensureStockDetails(wrapper);
    });
    banner.appendChild(retryBtn);

    // Append to the visible host container (see bannerHost above)
    bannerHost.appendChild(banner);
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
      // HTTPステータスを明示的に確認する(プロキシ等の502 JSONボディを
      // 成功扱いにしないため)。両分岐が body を代入するため初期化不要。
      let body;
      try {
        body = await res.json();
      } catch (_jsonErr) {
        body = null;
      }
      if (!res.ok) {
        return {
          error:
            body?.error ||
            body?.details?.reason ||
            `サーバー応答エラー (HTTP ${res.status})`,
        };
      }
      return body ?? { error: "サーバー応答の解析に失敗しました" };
    };

    let data = null;
    for (let attempt = 0; attempt <= MAX_DETAILS_POLLS; attempt++) {
      data = await pollOnce();
      if (!data || !data.fetching) break;
      if (controller.signal.aborted) break;
      await new Promise((r) => setTimeout(r, 700));
    }

    clearTimeout(timeoutId);
    if (!isCurrentRequest()) return;
    // 成功は「明示的な ok:true」のみとし、失敗フラグの不在を成功と見なさない
    // (空オブジェクトやプロキシの502ボディをキャッシュで恒久汚染しない)。
    if (data && data.ok === true && !data.fetching) {
      if (!stockDetailsCache.has(stockKey)) _enforceStockDetailsCacheLimit();
      stockDetailsCache.set(stockKey, data);
      stockDetailsCacheAt.set(stockKey, Date.now());
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
    if (!isCurrentRequest()) return;
    const reason = e?.details?.reason || e?.data?.details?.reason || "";
    if (timedOut) {
      showErrorBanner(reason ? `タイムアウト（${reason}）` : "タイムアウト");
    } else if (e.name === "AbortError") {
      showErrorBanner(reason || "取得失敗");
    } else {
      showErrorBanner(reason || (e?.message ? String(e.message) : "取得失敗"));
    }
    logger.warn("Details fetch error:", e);
  } finally {
    clearTimeout(timeoutId);
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
 * Format compact change metrics (arrow, sign, value, percent, class, aria-label)
 * ensuring proper accessibility, neutral zero/missing states, and NaN safety.
 */
function formatStockChangeMetrics(stock) {
  let changeCls = "";
  let arrow = "";
  let sign = "";
  let ariaPrefix = "変動なし";
  let changeText = "--";
  let pctText = "--%";
  // Market-aware display precision must live inside this function: the
  // regression harness extracts this function alone via regex, so a
  // top-level helper would be undefined in that context.
  const _isJpForChange = (() => {
    if (stock?.market === "jp") return true;
    const symbol = stock?.symbol;
    if (typeof symbol !== "string") return false;
    const upper = symbol.toUpperCase();
    if (upper.endsWith(".T")) return true;
    const bare = upper.includes(":")
      ? upper.slice(upper.lastIndexOf(":") + 1)
      : upper;
    const code = bare.endsWith(".T") ? bare.slice(0, -2) : bare;
    return /^[0-9][A-Z0-9]{3}$/.test(code);
  })();
  const changeDecimals = _isJpForChange ? 2 : 4;
  const changeFactor = 10 ** changeDecimals;

  const changeNum =
    typeof stock?.change === "number"
      ? stock.change
      : stock?.change != null && stock?.change !== ""
        ? Number(stock.change)
        : NaN;

  if (Number.isFinite(changeNum)) {
    let rawChange =
      Math.round((changeNum + Number.EPSILON) * changeFactor) / changeFactor;
    if (Object.is(rawChange, -0)) rawChange = 0;
    changeText = String(rawChange);
    if (rawChange > 0) {
      changeCls = "pos";
      arrow = "▲";
      sign = "+";
      ariaPrefix = "上昇";
    } else if (rawChange < 0) {
      changeCls = "neg";
      arrow = "▼";
      sign = "";
      ariaPrefix = "下落";
    } else {
      ariaPrefix = "変わらず";
    }
  }

  const pctNum =
    typeof stock?.change_percent === "number"
      ? stock.change_percent
      : stock?.change_percent != null && stock?.change_percent !== ""
        ? Number(stock.change_percent)
        : NaN;

  if (Number.isFinite(pctNum)) {
    let rawPct = Math.round((pctNum + Number.EPSILON) * 100) / 100;
    if (Object.is(rawPct, -0)) rawPct = 0;
    const pctSign = rawPct > 0 ? "+" : "";
    pctText = `${pctSign}${rawPct}%`;
  }

  const className = changeCls
    ? `compact-change ${changeCls}`
    : "compact-change";
  const text = Number.isFinite(changeNum)
    ? `${arrow}${sign}${changeText} (${pctText})`
    : `-- (${pctText})`;
  const ariaLabel = Number.isFinite(changeNum)
    ? `${ariaPrefix} ${sign}${changeText} (${pctText})`
    : `データなし (${pctText})`;

  return { className, text, ariaLabel };
}

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
  const oldDataStr = wrapper.dataset.lastDataHash;
  // 変更検知ハッシュは描画する全フィールドを覆う(価格が静止している間も
  // volume/high/low/pts_price の変化でドローワー等が更新されるように)。
  // 組成は utils.js の stockRenderHash に一元化し、api.js の SSE 差分ゲートと
  // 共有する(時々で片方だけ狭くなると更新の取りこぼしが再発する)。
  const newDataStr = stockRenderHash(stock);

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
      // 価格欠損(null/undefined)時に flash-down を誤再生しないよう、
      // 両値が有限数値のときのみフラッシュする。
      if (
        Number.isFinite(newPrice) &&
        Number.isFinite(oldPrice) &&
        newPrice !== oldPrice
      ) {
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
    // 前回の更新がライブ由来でも、今回の更新がポーリング由来なら緑パルスを
    // 維持しない(データ鮮度を誤って示さないよう毎回トグルする)。
    priceEl.classList.toggle("scraping-success", isScrapedOrRealtime);
    priceEl.classList.add("updating");
    if (priceEl.__updateTimer) clearTimeout(priceEl.__updateTimer);
    priceEl.__updateTimer = setTimeout(
      () => priceEl.classList.remove("updating"),
      1200,
    );
  }

  const changeEl = wrapper.querySelector(".compact-change");
  if (changeEl) {
    const {
      className: nextCls,
      text: nextText,
      ariaLabel,
    } = formatStockChangeMetrics(stock);
    if (changeEl.className !== nextCls) changeEl.className = nextCls;
    if (changeEl.textContent !== nextText) {
      changeEl.textContent = nextText;
      changeEl.setAttribute("aria-label", ariaLabel);
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
    if (ptsTxt) {
      ptsEl.hidden = false;
      ptsEl.removeAttribute("aria-hidden");
    } else {
      ptsEl.hidden = true;
      ptsEl.setAttribute("aria-hidden", "true");
    }
  }

  if (isPortfolioTab) {
    updatePortfolioInfoElements(wrapper, stock);
  }

  // スパークラインの更新
  // 注意: 以下の「再描画抑制」判断はスパークライン描画のみを対象とする。
  // 関数全体を return すると、以降に続くドローワー詳細項目の数値更新と
  // リアルタイムチャート追従が実行されず、切断中のスナップショット更新で
  // ドローワーが停止するため、return は使わず描画ブロックを条件分岐する。
  if (!hasSparklinePoints) {
    setSparklineVisibility(wrapper, false);
  } else {
    // 表示可否と再描画判定を分離し、再描画スキップ時も表示状態は安定させる
    const wasHidden = isSparklineHidden(wrapper);
    setSparklineVisibility(wrapper, true);

    // 鮮度が低いデータは可視状態を維持し、不要な再描画だけ抑制する
    const skipStaleRedraw = !freshSparklineData && !wasHidden;

    // リロード直後の差し替えジャンプを抑えるため、初回ライブ更新の1回目は再描画を抑制
    const isFirstLiveRefresh =
      stock.__live_update && wrapper.dataset.liveSparkSeen !== "1";
    if (stock.__live_update) {
      wrapper.dataset.liveSparkSeen = "1";
    }
    const skipFirstLiveRefresh = isFirstLiveRefresh && !wasHidden;

    if (!skipStaleRedraw && !skipFirstLiveRefresh) {
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
  }

  const drawerEl = document.getElementById("stock-detail-drawer");
  const isDrawerOpen =
    (typeof currentDrawerActiveWrapper !== "undefined" &&
      currentDrawerActiveWrapper === wrapper) ||
    Boolean(
      drawerEl?.classList.contains("open") &&
      drawerEl.dataset.stockKey === stockKey,
    );

  // Update Detail Panel (only if not open to avoid jitter)
  const detail = wrapper.querySelector(".detail-panel");
  if (detail && !detail.classList.contains("open")) {
    const elMap = {
      ".detail-current": formatPrice(stock.price, stock),
      ".detail-high": formatPrice(stock.high, stock),
      ".detail-low": formatPrice(stock.low, stock),
      ".detail-volume":
        stock.volume != null && Number(stock.volume) > 0
          ? Number(stock.volume).toLocaleString()
          : "--",
    };
    for (const [sel, val] of Object.entries(elMap)) {
      const el = wrapper.querySelector(sel);
      if (el && el.textContent !== String(val)) {
        el.textContent = val;
      }
    }
  }

  // Update drawer detail numerical values if open for this stock
  if (isDrawerOpen) {
    const drawerChartContent = document.getElementById("drawerTabChartContent");
    const drawerTarget = drawerChartContent || drawerEl;
    if (drawerTarget) {
      const elMap = {
        ".detail-current": formatPrice(stock.price, stock),
        ".detail-high": formatPrice(stock.high, stock),
        ".detail-low": formatPrice(stock.low, stock),
        ".detail-volume":
          stock.volume != null && Number(stock.volume) > 0
            ? Number(stock.volume).toLocaleString()
            : "--",
      };
      for (const [sel, val] of Object.entries(elMap)) {
        const el = drawerTarget.querySelector(sel);
        if (el && el.textContent !== String(val)) {
          el.textContent = val;
        }
      }
    }
  }

  // Real-time Chart Update if open
  // ガード条件: ローディング中、または読み込み完了直後（0.8秒間）は更新をスキップしてアニメーションの衝突を防ぐ
  let container = wrapper.querySelector(".chart-container");
  if (!container && isDrawerOpen) {
    const drawerChart = document.getElementById("drawerTabChartContent");
    if (drawerChart) {
      container = drawerChart.querySelector(".chart-container");
    }
  }
  const lastRefresh = parseInt(wrapper.dataset.lastRefresh || "0");
  const isCooldown = Date.now() - lastRefresh < 800;
  const isDetailOpen = Boolean(detail?.classList.contains("open"));

  if (
    (isDetailOpen || isDrawerOpen) &&
    container &&
    !container.classList.contains("loading") &&
    !isCooldown
  ) {
    requestAnimationFrame(() => {
      if (isPortfolioTab) {
        const pnlCanvas =
          wrapper.querySelector(".chart-canvas-pnl") ||
          (isDrawerOpen
            ? document.querySelector(
                "#drawerTabChartContent .chart-canvas-pnl, #stock-detail-drawer .chart-canvas-pnl",
              )
            : null);
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
            const canvas =
              wrapper.querySelector(".chart-canvas") ||
              (isDrawerOpen
                ? document.querySelector(
                    "#drawerTabChartContent .chart-canvas, #stock-detail-drawer .chart-canvas",
                  )
                : null);
            const chart = canvas ? chartInstances.get(canvas) : null;
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
          const canvas =
            wrapper.querySelector(".chart-canvas") ||
            (isDrawerOpen
              ? document.querySelector(
                  "#drawerTabChartContent .chart-canvas, #stock-detail-drawer .chart-canvas",
                )
              : null);
          if (canvas) {
            const chart = chartInstances.get(canvas);
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
    if (isStockDetailsCacheFresh(stockKey))
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
      val:
        stock.volume != null && Number(stock.volume) > 0
          ? Number(stock.volume).toLocaleString()
          : "--",
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
    // 初回描画からスクリーンリーダーへアクティブ状態を告知するため、
    // .active と同時に aria-pressed を初期化する(FE-ui3-6)。
    btn.setAttribute("aria-pressed", String(currentType === style.id));
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
    btn.type = "button";
    btn.setAttribute("aria-pressed", String(isOn));
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
    btn.type = "button";
    btn.dataset.period = p;
    btn.setAttribute(
      "aria-pressed",
      String(getChartPref(stockKey, "period", "3mo") === p),
    );
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
    btn.type = "button";
    btn.dataset.interval = item.id;
    btn.setAttribute("aria-pressed", String(currentInterval === item.id));
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
  const isOpenAi =
    typeof isOpenAiCompatibleProvider === "function"
      ? isOpenAiCompatibleProvider()
      : window.APP_CONFIG?.ai_provider === "openai_compatible";
  const aiBtnText = isEligible
    ? "✨ AIテクニカル描画"
    : isOpenAi
      ? "🔒 AIテクニカル描画 (設定要確認)"
      : "🔒 AIテクニカル描画 (Medium/Large限定)";
  const aiBtnCls = isEligible
    ? "ai-tech-lines-btn"
    : "ai-tech-lines-btn locked";

  const aiTechBtn = createEl("button", aiBtnCls, aiBtnText);
  aiTechBtn.type = "button";
  aiTechBtn.title = isEligible
    ? "AIがサポート線・抵抗線・トレンドラインを動的描画"
    : isOpenAi
      ? "AIプロバイダー設定の確認が必要です"
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
  chartCanvas.setAttribute("role", "img");
  chartCanvas.setAttribute(
    "aria-label",
    `${stock.symbol} のテクニカルチャート`,
  );
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
  const {
    className: changeCls,
    text: changeText,
    ariaLabel: changeAriaLabel,
  } = formatStockChangeMetrics(stock);
  const changeEl = createEl("div", changeCls, changeText);
  changeEl.setAttribute("aria-label", changeAriaLabel);
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
  if (ptsTxt) {
    ptsEl.hidden = false;
    ptsEl.removeAttribute("aria-hidden");
  } else {
    ptsEl.hidden = true;
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
  setupBtn(".chat-send-btn", (e) => {
    const input = e?.currentTarget?.parentElement?.querySelector(".chat-input");
    sendChat(wrapper, input);
  });
  setupBtn(".pf-edit-btn", () => openPortfolioModal(stockKey));
  setupBtn(".alert-edit-btn", () => openAlertModal(stockKey));

  detail.querySelector(".chat-input")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      sendChat(wrapper, e.currentTarget);
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
    // Purge every per-stockKey cache (incl. ai_prev_* localStorage) for the
    // removed card — same single-owner helper the SSE _removed path uses.
    if (
      typeof window !== "undefined" &&
      typeof window.purgeStockKeyCaches === "function"
    ) {
      window.purgeStockKeyCaches(wrapper.dataset.stockKey);
    }
    unregisterWrapper(wrapper.dataset.stockKey, wrapper);
    wrapper.remove();
  });

  if (document.querySelector(".tab.active")?.id === "tab-portfolio") {
    renderPortfolio();
  }

  // Reorder in-place without innerHTML="" to preserve Chart.js canvas state.
  // Walk a reference cursor instead of comparing container.children[i]: a
  // newly-created card is not in the container yet, so appending it at the
  // end while every later index already matches left it after all existing
  // cards (e.g. [A,B] plus new C requested as [C,A,B] produced [A,B,C] until
  // the next refresh).
  let referenceNode = container.firstElementChild;
  orderedWrappers.forEach((wrapper) => {
    if (wrapper === referenceNode) {
      referenceNode = referenceNode.nextElementSibling;
      return;
    }
    container.insertBefore(wrapper, referenceNode);
  });
  renderFavorites();
  updateTabCounts();
  // ローディング表示(renderSkeletons)が設定した aria-busy を描画完了時に
  // 解除する(SSE再描画経路で永続 busy になるのを防ぐ)。
  container.setAttribute("aria-busy", "false");
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
      const interval = isPortfolio
        ? "auto"
        : getChartPref(stockKey, "interval", "auto");
      refreshStockChart(wrapper, period, interval);
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
    // スクリーンリーダーにローディング状態を伝える(解除はデータ描画側が担う)
    container.setAttribute("aria-busy", "true");

    // ライブカードが残っている場合に備えた完全な後始末(描画側の削除ループと
    // 同等: チャート破棄・observer解除・レジストリ削除)。これを行わずに
    // textContent="" すると Chart インスタンスと detached ラッパーが漏れる。
    Array.from(container.querySelectorAll(".stock-wrapper")).forEach((w) => {
      if (currentDrawerActiveWrapper === w) {
        closeStockDetailDrawer();
      }
      w.querySelectorAll("canvas").forEach((canvas) => destroyChart(canvas));
      if (cardIntersectionObserver) {
        cardIntersectionObserver.unobserve(w);
      }
      unregisterWrapper(w.dataset.stockKey, w);
    });

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
    // ライブカードを消す前に後始末する(renderSkeletons と同一: チャート
    // 破棄・observer解除・レジストリ削除)。取り残しを防ぐ。
    Array.from(container.querySelectorAll(".stock-wrapper")).forEach((w) => {
      if (currentDrawerActiveWrapper === w) {
        closeStockDetailDrawer();
      }
      w.querySelectorAll("canvas").forEach((canvas) => destroyChart(canvas));
      if (cardIntersectionObserver) {
        cardIntersectionObserver.unobserve(w);
      }
      unregisterWrapper(w.dataset.stockKey, w);
    });
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
    Array.from(container.querySelectorAll(".stock-wrapper")).forEach((w) => {
      // ドロワーが開いたカードを消す場合は先にドロワーを閉じる
      // (detached ラッパーへの描画とチャート取り残しを防ぐ)。
      if (currentDrawerActiveWrapper === w) {
        closeStockDetailDrawer();
      }
      w.querySelectorAll("canvas").forEach((canvas) => destroyChart(canvas));
      if (cardIntersectionObserver) {
        cardIntersectionObserver.unobserve(w);
      }
      unregisterWrapper(w.dataset.stockKey, w);
    });
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
      // 保有解消でカードが消える場合もドロワーを先に閉じる(FE-ui-2)。
      if (currentDrawerActiveWrapper === w) {
        closeStockDetailDrawer();
      }
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

function computeHoldingsHash(holdings) {
  return holdings
    .map((h) => `${h.market}:${h.symbol}:${h.shares}:${h.avg_price}`)
    .join("|");
}

function isUsdHolding(stock) {
  return Boolean(stock && (stock.currency === "USD" || stock.market === "us"));
}

function drawPortfolioSummaryChart(holdings) {
  const canvas = DOM.get("pf-summary-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const chartAnimationControl = resolvePortfolioChartAnimationControl();

  // 最新レートを取得 (state経由)
  const rawUsdJpy = portfolioFixedExchangeRate ?? state.exchangeRate;
  const numUsdJpy = Number(rawUsdJpy);
  const usdJpyRate =
    Number.isFinite(numUsdJpy) && numUsdJpy > 1.0 ? numUsdJpy : null;
  const isMixedCurrency =
    holdings.some(isUsdHolding) && holdings.some((s) => !isUsdHolding(s));

  if (holdings.some(isUsdHolding) && !usdJpyRate) {
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
      rate: isUsdHolding(stock) ? usdJpyRate : 1.0,
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

  // 2. 日付ごとに全銘柄の時価を合算。データ欠損時は初回終値（開始前休場）と直近値（前方補填）で補填。
  const lastPrices = new Map(); // stockKey -> price
  holdings.forEach((stock) => {
    const stockKey = makeStockKey(stock.market, stock.symbol);
    const info = stockHistoryMap.get(stockKey);
    if (!info || info.days.size === 0) return;
    const firstDate = sortedDates.find((d) => info.days.has(d));
    if (firstDate) {
      lastPrices.set(stockKey, info.days.get(firstDate));
    }
  });

  const dataPoints = sortedDates.map((dateStr) => {
    let totalValue = 0;
    let _totalCost = 0;

    holdings.forEach((stock) => {
      const stockKey = makeStockKey(stock.market, stock.symbol);
      const info = stockHistoryMap.get(stockKey);

      let price = info.days.get(dateStr);
      if (price === undefined) {
        price = lastPrices.get(stockKey) ?? 0; // 開始前休場・欠損日は有効な終値で補填
      } else {
        lastPrices.set(stockKey, price);
      }

      totalValue += price * info.shares * info.rate;
    });
    // buildLocalDateKey はローカル日付文字列なので、`new Date("YYYY-MM-DD")`
    // (UTC 0時解釈)で再パースすると UTC 西側で前日にずれる。
    // ローカル各部から Date を組み立てる。
    const [year, month, day] = dateStr.split("-").map(Number);
    return { x: new Date(year, month - 1, day).getTime(), y: totalValue };
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

  const numCurFx = Number(currentFxRate);
  const validCurrentFx =
    Number.isFinite(numCurFx) && numCurFx > 1.0 ? numCurFx : 150.0;
  const numPrevFx = Number(prevFxRate);
  const validPrevFx =
    Number.isFinite(numPrevFx) && numPrevFx > 1.0 ? numPrevFx : validCurrentFx;

  holdings.forEach((stock) => {
    const shares = toFiniteNumber(stock.shares, 0);
    const avgPrice = toFiniteNumber(stock.avg_price, 0);
    const currentPrice = toFiniteNumber(stock.price, 0);
    const changeLocal = toFiniteNumber(stock.change, 0);

    const isUSD = isUsdHolding(stock);
    const curRate = isUSD ? validCurrentFx : 1.0;
    const prvRate = isUSD ? validPrevFx : 1.0;

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
  // USD を含む保有では為替レート欠損時に「1.0」で誤った JPY 合計を出さない。
  // mixed 判定でなく「USD を含むか」で待機表示にする(全USD構成でも同様)。
  const hasUsdHoldings = holdings.some(isUsdHolding);
  if (usdJpyRate == null && hasUsdHoldings) {
    const valEl = DOM.get("pf-total-value");
    if (valEl) valEl.textContent = "為替データ取得中...";
    return;
  }

  const numUsdJpy = Number(usdJpyRate);
  const currentFxRate =
    Number.isFinite(numUsdJpy) && numUsdJpy > 1.0 ? numUsdJpy : 150.0;
  const usdJpyChange = toFiniteNumber(state.indices?.USDJPY?.change, 0);
  const rawPrevFx = currentFxRate - usdJpyChange;
  const prevFxRate = rawPrevFx > 0 ? rawPrevFx : currentFxRate;

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
  const container = canvas.parentElement;

  let emptyElem = container?.querySelector(".pf-sector-empty-state");

  const sectorMap = {};
  let totalValue = 0;
  if (Array.isArray(holdings)) {
    holdings.forEach((stock) => {
      const sector = stock.sector || "Other";
      const shares = toFiniteNumber(stock.shares, 0);
      const price = toFiniteNumber(stock.price, 0);
      const rate = isUsdHolding(stock) ? usdJpyRate : 1.0;
      const value = shares * price * rate;
      if (value > 0) {
        sectorMap[sector] = (sectorMap[sector] || 0) + value;
        totalValue += value;
      }
    });
  }

  if (!holdings || holdings.length === 0 || totalValue <= 0) {
    if (pfSectorChartInstance) {
      pfSectorChartInstance.destroy();
      pfSectorChartInstance = null;
    }
    if (ctx) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
    canvas.style.display = "none";
    if (container) {
      if (!emptyElem) {
        emptyElem = document.createElement("div");
        emptyElem.className = "pf-sector-empty-state";
        emptyElem.setAttribute("role", "status");
        emptyElem.style.cssText =
          "display: flex; align-items: center; justify-content: center; height: 100%; color: var(--text-secondary, #94a3b8); font-size: 0.85rem; text-align: center; padding: 20px;";
        container.appendChild(emptyElem);
      }
      emptyElem.textContent = "保有銘柄が登録されていません";
      emptyElem.style.display = "flex";
    }
    return;
  }

  if (emptyElem) {
    emptyElem.style.display = "none";
  }
  canvas.style.display = "";

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
              // 全保有が0のとき NaN% を出さない
              const pct = total > 0 ? ((val / total) * 100).toFixed(1) : null;
              const pctText = pct !== null ? ` (${pct}%)` : "";
              return ` ${ctx.label}: ${val.toLocaleString(undefined, { maximumFractionDigits: 0 })} JPY${pctText}`;
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
  // Number() は null/"" を 0 に変換するため、空白値は先に "--" を返す
  // (screener.js/data-adapter.js の兄弟実装と同じ契約)。
  if (
    value === null ||
    value === undefined ||
    value === "" ||
    typeof value === "boolean"
  ) {
    return "--";
  }
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
  const row =
    wrapper?.querySelector?.(`.detail-item-${field}`) ||
    document.querySelector(`#stock-detail-drawer .detail-item-${field}`);
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
        // O(1) レジストリLookupを優先し、DOMフォールバックでは CSS.escape で
        // セレクタ注入(引用符を含むシンボルでの SyntaxError)を防ぐ。
        const matchingWrapper =
          (stockKey
            ? wrapperRegistryMap.get(stockKey)?.values().next().value
            : null) ||
          (stockKey
            ? document.querySelector(
                `.stock-wrapper[data-stock-key="${CSS.escape(stockKey)}"]`,
              )
            : null) ||
          document.querySelector(
            `.stock-wrapper[data-symbol="${CSS.escape(item.symbol || "")}"]`,
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

// (L16 と同じ契約) ドロワーがこの銘柄で開いている間は .chart-container が
// #drawerTabChartContent 配下へ移動済みのため、ラッパー直下の問い合わせは
// null を返し、チャートのエラー/情報バナーが不可視のインラインパネルにしか
// 出せなかった。ラッパー → 開いているドロワーの順で設置先を解決する。
function chartErrorHosts(wrapper) {
  const hosts = [];
  const inline =
    wrapper.querySelector(".chart-container") ||
    wrapper.querySelector(".chart-canvas-container");
  if (inline) hosts.push(inline);

  const drawerEl = document.getElementById("stock-detail-drawer");
  const stockKey = wrapper.dataset.stockKey;
  const isDrawerActive =
    (typeof currentDrawerActiveWrapper !== "undefined" &&
      currentDrawerActiveWrapper === wrapper) ||
    Boolean(
      stockKey &&
      drawerEl?.classList.contains("open") &&
      drawerEl.dataset.stockKey === stockKey,
    );
  if (isDrawerActive && drawerEl) {
    const moved =
      drawerEl.querySelector(".chart-container") ||
      drawerEl.querySelector(".chart-canvas-container");
    if (moved && moved !== inline) hosts.push(moved);
  }
  return hosts;
}

function clearChartError(wrapper) {
  // ドロワー開閉でコンテナが移動するため、両側に残ったバナーを掃除する。
  chartErrorHosts(wrapper).forEach((host) => {
    const err = host.querySelector(".chart-error");
    if (err) err.remove();
  });
}

function showChartError(wrapper, msg, type = "error") {
  const [container] = chartErrorHosts(wrapper);
  if (!container) return;

  destroyChart(
    container.querySelector(".chart-canvas") ||
      wrapper.querySelector(".chart-canvas"),
  );
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

  const stockKey = wrapper.dataset.stockKey;
  if (stockKey) {
    const signature = getSparklineSignature(data);
    if (signature) sparklineSignatureMap.set(stockKey, signature);
  }

  // Clean up any legacy Chart.js instance on this canvas
  destroyChart(canvas);

  // OHLC ペイロード({o,h,l,c})と価格列({price})の双方を受け取り、
  // 描画可能点が揃うまで可視化しない(空のスパークラインを見せるのを防ぐ)。
  const prices = data
    .map((d) => (typeof d === "number" ? d : (d?.price ?? d?.c)))
    .filter((p) => typeof p === "number" && !isNaN(p));
  if (prices.length < 2) return;
  setSparklineVisibility(wrapper, true);

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
// 送信ごとに単調増加する生成トークン。古いリクエストの finally が
// 新しいセッションの入力状態を上書きするのを防ぐ(FE-ui3-1)。
let aiDrawerSendGeneration = 0;
let aiDrawerAbortController = null;

function openAiDrawer(symbol, name, market) {
  aiDrawerSendGeneration += 1;
  if (aiDrawerAbortController) {
    aiDrawerAbortController.abort();
    aiDrawerAbortController = null;
  }

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
  const sendBtn = document.getElementById("sendAiDrawerBtn");

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
    // 既に開いているドロワーの再オープンではスクロールロックを重複取得しない
    // (参照カウントの過剰加算で単一クローズに解除しきれないのを防ぐ)。
    const wasHidden = overlay.classList.contains("hidden");
    overlay.removeAttribute("inert");
    overlay.classList.remove("hidden");
    overlay.setAttribute("aria-hidden", "false");
    if (typeof lockBodyScroll === "function" && wasHidden) {
      lockBodyScroll();
    }
  }
  if (sendBtn) {
    sendBtn.disabled = false;
    sendBtn.textContent = "送信";
  }
  if (inputEl) {
    inputEl.disabled = false;
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
  const sendBtn = document.getElementById("sendAiDrawerBtn");
  const messagesEl = document.getElementById("ai-drawer-chat-messages");
  if (!inputEl || !messagesEl) return;

  const text = inputEl.value.trim();
  if (!text) return;
  if (inputEl.disabled) return;

  if (typeof HAS_MISTRAL_API_KEY !== "undefined" && !HAS_MISTRAL_API_KEY) {
    if (typeof showToast === "function") {
      showToast(
        "❌ APIキーが未設定です。設定画面で登録してください。",
        "#ff7d7d",
      );
    }
    return;
  }
  const generation = ++aiDrawerSendGeneration;
  if (aiDrawerAbortController) {
    aiDrawerAbortController.abort();
  }
  const requestController = new AbortController();
  aiDrawerAbortController = requestController;

  inputEl.disabled = true;
  if (sendBtn) sendBtn.disabled = true;
  const originalBtnText = sendBtn
    ? sendBtn.textContent && sendBtn.textContent !== "送信中..."
      ? sendBtn.textContent
      : "送信"
    : "";
  if (sendBtn) sendBtn.textContent = "送信中...";

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

  const isCurrentGeneration = () =>
    generation === aiDrawerSendGeneration && !requestController.signal.aborted;

  try {
    // M-5: token generation is centralized in createRequestToken (single
    // system).  Never fabricate a token from a duplicate fallback path here;
    // if the shared helper is unavailable we stop rather than send a weaker one.
    const genToken = () => {
      if (typeof createRequestToken !== "function") {
        throw new Error(
          "createRequestToken unavailable; refusing to fabricate a request token",
        );
      }
      return createRequestToken();
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
      if (!isCurrentGeneration()) return;
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

    // C-2: ストリーミングを試み、非対応/フォールバック対象失敗時はポーリングへ移行。
    let reply = null;
    let streamApiError = null;
    if (typeof streamChatReply === "function") {
      try {
        reply = await streamChatReply(
          payload,
          (partial) => {
            if (!isCurrentGeneration()) return;
            renderReply(partial);
            messagesEl.scrollTop = messagesEl.scrollHeight;
          },
          { signal: requestController.signal },
        );
      } catch (streamErr) {
        if (!streamErr || !streamErr.isStreamFallback) {
          // サーバーからの明示的なHTTP/APIエラーや中断：ポーリングによる二重POSTを行わない。
          streamApiError = streamErr;
        } else {
          reply = null;
        }
      }
    }
    if (streamApiError) throw streamApiError;
    if (!isCurrentGeneration()) return;

    if (reply === null) {
      if (typeof chatPollingReply === "function") {
        reply = await chatPollingReply(payload, {
          signal: requestController.signal,
        });
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
                  const res = await fetchWithTimeout(url, opts);
                  const json = await res.json().catch(() => ({}));
                  return { response: res, data: json };
                };
        for (let attempt = 0; attempt <= pollMax; attempt++) {
          if (!isCurrentGeneration()) return;
          const { response: res, data: fetched } = await fetchFn("/api/chat", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
            signal: requestController.signal,
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
          await new Promise((resolve, reject) => {
            if (requestController.signal.aborted) {
              const abortErr = new Error("Aborted");
              abortErr.name = "AbortError";
              reject(abortErr);
              return;
            }
            let timer = null;
            const onAbort = () => {
              if (timer !== null) {
                clearTimeout(timer);
                timer = null;
              }
              const abortErr = new Error("Aborted");
              abortErr.name = "AbortError";
              reject(abortErr);
            };
            requestController.signal.addEventListener("abort", onAbort, {
              once: true,
            });
            timer = setTimeout(() => {
              timer = null;
              requestController.signal.removeEventListener("abort", onAbort);
              resolve();
            }, pollInterval);
          });
        }
        if (!resOk) {
          throw new Error("AI応答の生成がタイムアウトしました");
        }
        reply = data.reply || data.summary || "応答を取得できませんでした";
      }
    }
    if (!isCurrentGeneration()) return;
    renderReply(reply);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } catch (err) {
    if (
      generation !== aiDrawerSendGeneration ||
      err?.name === "AbortError" ||
      requestController.signal.aborted
    ) {
      return;
    }
    loadingMsg.textContent = `エラー: ${err.message || "接続エラーが発生しました。"}`;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } finally {
    if (aiDrawerAbortController === requestController) {
      aiDrawerAbortController = null;
    }
    // 現行の送信だけが入力状態を復元する(古い完了が新しいセッションの
    // 「送信中...」表示を壊すのを防ぐ)。
    if (generation === aiDrawerSendGeneration) {
      inputEl.disabled = false;
      if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.textContent = originalBtnText;
      }
      // ドロワーが閉じている間はフォーカスを奪わない(閉じた状態で
      // フォーカスを移すと、ユーザーの新しい操作対象を横取りする)。
      const overlay = document.getElementById("ai-drawer-overlay");
      if (!overlay || !overlay.classList.contains("hidden")) {
        inputEl.focus();
      }
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }
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

  // 両分岐が pfCount を代入するため初期化不要(M36 dead-store 修正)。
  let pfCount;
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
  ).filter((el) => !el.hasAttribute("inert") && isUiElementVisible(el));
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
      if (typeof window.closeTopmostOverlayOnEscape === "function") {
        window.closeTopmostOverlayOnEscape(e);
      } else {
        e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        closeAiDrawer();
      }
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
      if (typeof window.closeTopmostOverlayOnEscape === "function") {
        window.closeTopmostOverlayOnEscape(e);
      } else {
        e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        closeStockDetailDrawer();
      }
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
    chartTabBtn?.setAttribute("tabindex", "0");
    aiTabBtn?.classList.remove("active");
    aiTabBtn?.setAttribute("aria-selected", "false");
    aiTabBtn?.setAttribute("tabindex", "-1");
    chartContent?.classList.remove("hidden");
    chartContent?.removeAttribute("hidden");
    aiContent?.classList.add("hidden");
    aiContent?.setAttribute("hidden", "");
    chartTabBtn?.focus();
  }

  function selectAiTab() {
    aiTabBtn?.classList.add("active");
    aiTabBtn?.setAttribute("aria-selected", "true");
    aiTabBtn?.setAttribute("tabindex", "0");
    chartTabBtn?.classList.remove("active");
    chartTabBtn?.setAttribute("aria-selected", "false");
    chartTabBtn?.setAttribute("tabindex", "-1");
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
    if (["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(e.key)) {
      e.preventDefault();
      if (chartTabBtn?.classList.contains("active")) {
        selectAiTab();
      } else {
        selectChartTab();
      }
    } else if (e.key === "Home") {
      e.preventDefault();
      selectChartTab();
    } else if (e.key === "End") {
      e.preventDefault();
      selectAiTab();
    }
  });
}

let currentDrawerActiveWrapper = null;
let stockDetailDrawerTrigger = null;
// (FE-06, 2026-11-08 review) the deferred chart/detail refresh below used a
// fire-and-forget timer. Closing the drawer within 50 ms still ran the
// refresh against a detached wrapper, and reopening for another symbol let
// the stale timer overwrite the new drawer's chart. Keep the id so the close
// path can cancel it.
let stockDetailDrawerRefreshTimer = null;

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
  const changeVal = Number.isFinite(stock?.change_percent)
    ? Number(stock.change_percent)
    : null;
  let badgeClass = "drawer-price-badge";
  let pctStr = "--%";

  if (changeVal !== null) {
    if (changeVal > 0) {
      badgeClass = "drawer-price-badge pos";
      pctStr = `+${changeVal.toFixed(2)}%`;
    } else if (changeVal < 0) {
      badgeClass = "drawer-price-badge neg";
      pctStr = `${changeVal.toFixed(2)}%`;
    } else {
      badgeClass = "drawer-price-badge";
      pctStr = "0.00%";
    }
  }

  if (symbolEl) symbolEl.textContent = sym;
  if (nameEl) nameEl.textContent = name;
  if (priceBadge) {
    priceBadge.textContent = `${priceStr} (${pctStr})`;
    priceBadge.className = badgeClass;
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

  // 前のドロワーを先に閉じる。open 設定(drawerEl の open クラス/ dataset /
  // トリガー記録)後に close すると、close 側が new 側の状態を消してしまっ
  // た(ドロワーが開いたまま中身だけ壊れる)ため、順序は固定する。
  if (currentDrawerActiveWrapper && currentDrawerActiveWrapper !== wrapper) {
    closeStockDetailDrawer();
  }

  const stockKey =
    wrapper?.dataset?.stockKey ||
    makeStockKey(stock.market || "us", stock.symbol);
  const drawerEl = document.getElementById("stock-detail-drawer");
  if (drawerEl) {
    drawerEl.classList.add("open");
    drawerEl.dataset.stockKey = stockKey;
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
      if (
        wrapper?.__aiTechnicalLines &&
        typeof renderAiTechnicalLinesSummary === "function"
      ) {
        renderAiTechnicalLinesSummary(wrapper, wrapper.__aiTechnicalLines);
      }
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

  // 既に開いている場合(別銘柄への切替含む)はロックを重複取得しない。
  const overlayWasHidden = overlay.classList.contains("hidden");
  overlay.removeAttribute("inert");
  overlay.classList.remove("hidden");
  overlay.setAttribute("aria-hidden", "false");
  if (typeof lockBodyScroll === "function" && overlayWasHidden) {
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
    const interval = isPortfolio
      ? "auto"
      : getChartPref(stockKey, "interval", "auto");

    if (stockDetailDrawerRefreshTimer !== null) {
      clearTimeout(stockDetailDrawerRefreshTimer);
    }
    stockDetailDrawerRefreshTimer = setTimeout(() => {
      stockDetailDrawerRefreshTimer = null;
      refreshStockChart(wrapper, period, interval);
      ensureStockDetails(wrapper);
    }, 50);
  }
}

function closeStockDetailDrawer() {
  if (stockDetailDrawerRefreshTimer !== null) {
    clearTimeout(stockDetailDrawerRefreshTimer);
    stockDetailDrawerRefreshTimer = null;
  }
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

  const drawerEl = document.getElementById("stock-detail-drawer");
  if (drawerEl) {
    drawerEl.classList.remove("open");
    delete drawerEl.dataset.stockKey;
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
      // (L17, 2026-11-07 review) 従来はタブ内の全子要素を無条件で不可視の
      // インライン .detail-inner へ戻していた。この戻し先は描画時に
      // wrapper 側の検索が先にヒットするため、ドロワー専用の一時要素
      // (.ai-tech-lines-summary 等)が一度でもインラインへ戻ると、以後の
      // 再描画が永遠に不可視パネル側へ行われる。openStockDetailDrawer が
      // 移動させた「所有」要素のみを復帰し、一時要素はドロワー内に
      // 残して次回オープン時の replaceChildren() で破棄する。
      const ownedChartSelectors = [
        ".detail-info",
        ".detail-actions",
        ".chart-controls",
        ".chart-container:not(.pnl-chart-container)",
        ".pnl-chart-container",
      ];
      const ownedAiSelectors = [
        ".analyze-btn",
        ".ai-section",
        ".chat-toggle-btn",
        ".chat-section",
      ];
      const appendScoped = (host, selectors) => {
        selectors.forEach((sel) => {
          const el = host.querySelector(`:scope > ${sel}`);
          if (el) detailInner.appendChild(el);
        });
      };
      if (chartContent) appendScoped(chartContent, ownedChartSelectors);
      if (aiContent) appendScoped(aiContent, ownedAiSelectors);
      // TASK-13: buildDetailPanelでの標準順序を完全復元
      // [expandButton, pfBlock, info, actions, chartControls, chartContainer, pnlContainer, analyzeBtn, aiSection, chatToggleBtn, chatSection]
      const orderedSelectors = [
        ".expand-toggle-btn",
        ".pf-detail-block",
        ".detail-info",
        ".detail-actions",
        ".chart-controls",
        ".chart-container:not(.pnl-chart-container)",
        ".pnl-chart-container",
        ".analyze-btn",
        ".ai-section",
        ".chat-toggle-btn",
        ".chat-section",
      ];
      orderedSelectors.forEach((sel) => {
        const el = detailInner.querySelector(`:scope > ${sel}`);
        if (el) {
          detailInner.appendChild(el);
        }
      });
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
    const providerLabel =
      typeof getAiProviderLabel === "function"
        ? getAiProviderLabel()
        : window.APP_CONFIG?.ai_provider === "openai_compatible"
          ? "OpenAI互換 AI"
          : "Mistral AI";
    overlay.appendChild(
      createEl(
        "div",
        "ai-tech-loading-subtitle",
        `${providerLabel} がサポート線・抵抗線・トレンドラインを動的解析中...`,
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
    const isOpenAi =
      typeof isOpenAiCompatibleProvider === "function"
        ? isOpenAiCompatibleProvider()
        : window.APP_CONFIG?.ai_provider === "openai_compatible";
    const warningMsg = isOpenAi
      ? "🔒 AIテクニカル線描画機能をご利用いただけません。設定画面（⚙）よりAPI設定をご確認ください。"
      : "🔒 AIテクニカル線描画機能は Mistral Medium または Large モデルでのみご利用いただけます。設定画面（⚙）よりモデルを変更してください。";
    showToast(warningMsg, "warning", 7000);
    return;
  }

  const stockKey = targetWrapper.dataset.stockKey;
  const stock = targetWrapper.__stockData || getStockByKey(stockKey);
  if (!stock) return;

  targetWrapper.__aiTechnicalLinesController?.abort();
  const linesController = new AbortController();
  targetWrapper.__aiTechnicalLinesController = linesController;
  const isCurrentLinesRequest = () =>
    targetWrapper.__aiTechnicalLinesController === linesController;

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
    // インターバル指定中(1h等)は対応する履歴を参照する(自動キーで
    // 参照してヒットせず、不一致の履歴を送ってしまうのを防ぐ)。
    const interval = getChartPref(stockKey, "interval", "auto");
    const prefetch = getFreshPrefetchedHistory(stockKey, period, interval);
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
      signal: linesController.signal,
    });

    if (!isCurrentLinesRequest()) return;
    const data = await res.json().catch(() => ({}));
    if (!isCurrentLinesRequest()) return;
    if (!res.ok || !data?.ok) {
      if (data?.model_restricted || data?.details?.model_restricted) {
        const isOpenAi =
          typeof isOpenAiCompatibleProvider === "function"
            ? isOpenAiCompatibleProvider()
            : window.APP_CONFIG?.ai_provider === "openai_compatible";
        const defaultRestrictedMsg = isOpenAi
          ? "AIテクニカル線描画機能をご利用いただけません。設定画面（⚙）よりAPI設定をご確認ください。"
          : "AIテクニカル線描画機能は Mistral Medium または Large モデルでのみご利用いただけます。設定画面（⚙）よりモデルを変更してください。";
        const errorMsg =
          data?.details?.reason || data?.error || defaultRestrictedMsg;
        showToast(`🔒 ${errorMsg}`, "warning", 8000);
      } else {
        showToast(
          data?.details?.reason ||
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
    refreshStockChart(targetWrapper, period, interval);

    // If Fullscreen modal is open, redraw it too
    const fsModal = document.getElementById("chart-fullscreen-modal");
    if (fsModal && !fsModal.classList.contains("hidden")) {
      const fsCanvas = document.getElementById("fs-chart-canvas");
      if (fsCanvas) {
        const freshHistory =
          getFreshPrefetchedHistory(stockKey, period, interval) || prefetch;
        if (freshHistory) {
          drawChart(
            targetWrapper,
            freshHistory.formattedData,
            freshHistory.ohlcData,
            {
              targetCanvas: fsCanvas,
              aiTechnicalLines: data,
              period,
              interval,
            },
          );
        }
      }
    }
  } catch (err) {
    if (!isCurrentLinesRequest()) return;
    if (err?.name === "AbortError") return;
    logger.error("AI Technical Lines error:", err);
    const reason = err?.details?.reason || err?.data?.details?.reason || "";
    showToast(
      reason ? `通信エラー（${reason}）` : "通信エラーが発生しました。",
      "error",
    );
  } finally {
    // 後発リクエストが進行中の場合は、先発の後始末でボタン/オーバーレイを
    // 復元しない（先発の finally が後発の実行中UIを壊し、多重送信を許す）。
    if (isCurrentLinesRequest()) {
      hideAiTechnicalLoading(chartContainer);
      hideAiTechnicalLoading(fsCanvasWrapper);
      allBtns.forEach((b) => {
        b.disabled = false;
        b.classList.remove("loading");
        b.textContent = "✨ AIテクニカル描画";
      });
    }
  }
}

function renderAiTechnicalLinesSummary(wrapper, data) {
  if (!wrapper) return;
  const stockKey = wrapper.dataset?.stockKey;
  const drawerEl = document.getElementById("stock-detail-drawer");
  const isDrawerActive =
    currentDrawerActiveWrapper === wrapper ||
    Boolean(
      drawerEl?.classList.contains("open") &&
      drawerEl.dataset.stockKey === stockKey,
    );

  // (L17, 2026-11-07 review) ドロワーがアクティブな間は .chart-container が
  // ドロワー側へ移動しており、要約はドロワー内に描画される。このとき
  // wrapper.querySelector を先に試すと、ドロワー非アクティブ時に作られた
  // 不可視パネル側の古い要約に永遠に描き込み続けてしまうため、
  // アクティブ時はドロワー側を優先する。
  let summaryBox = null;
  if (isDrawerActive) {
    const drawerChartContent = document.getElementById("drawerTabChartContent");
    summaryBox =
      drawerChartContent?.querySelector(".ai-tech-lines-summary") ||
      drawerEl?.querySelector(".ai-tech-lines-summary");
  }
  if (!summaryBox) {
    summaryBox = wrapper.querySelector(".ai-tech-lines-summary");
  }
  // ドロワーアクティブ時は要約がドロワー内になければラッパー側の古い
  // 要約をドロワーへ移して再利用する(不可視パネルに置き去りにしない)。
  if (
    summaryBox &&
    isDrawerActive &&
    !summaryBox.closest("#stock-detail-drawer")
  ) {
    const drawerChartContent = document.getElementById("drawerTabChartContent");
    drawerChartContent?.appendChild(summaryBox);
  }

  if (!summaryBox) {
    summaryBox = createEl("div", "ai-tech-lines-summary hidden");
    let chartContainer = wrapper.querySelector(".chart-container");
    if (!chartContainer && isDrawerActive) {
      const drawerChartContent = document.getElementById(
        "drawerTabChartContent",
      );
      chartContainer =
        drawerChartContent?.querySelector(".chart-container") ||
        drawerEl?.querySelector(".chart-container");
    }
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
  // 再オープン時にスクロールロックを重複取得しない
  // (単一クローズで解除しきれずページがスクロール不能になるのを防ぐ)。
  const modalWasHidden = modal.classList.contains("hidden");
  modal.dataset.stockKey = stockKey;
  modal.removeAttribute("inert");
  modal.setAttribute("aria-hidden", "false");
  modal.classList.remove("hidden");
  modal.classList.add("show");
  modal.style.display = "flex";
  modal.style.zIndex = "20000";
  if (modalWasHidden && typeof lockBodyScroll === "function") {
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
    const isOpenAi =
      typeof isOpenAiCompatibleProvider === "function"
        ? isOpenAiCompatibleProvider()
        : window.APP_CONFIG?.ai_provider === "openai_compatible";
    const aiBtnText = isEligible
      ? "✨ AIテクニカル描画"
      : isOpenAi
        ? "🔒 AIテクニカル描画 (設定要確認)"
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
      btn.setAttribute("aria-pressed", String(currentType === type));
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
      btn.setAttribute("aria-pressed", String(isOn));
      if (key === "volume") {
        btn.dataset.volume = "on";
      } else {
        btn.dataset.ind = key;
      }
      indGroup.appendChild(btn);
    });
    toolbar.appendChild(indGroup);

    const periodGroup = createEl("div", "control-group period-controls");
    // カード側と同一の正規リストを使う("max" が欠けたままになり、max選択中
    // にフルスクリーンへ開くとアクティブボタンが無くなるのを防ぐ)。
    CONSTANTS.PERIODS.forEach((period) => {
      const btn = createEl(
        "button",
        `control-btn ${currentPeriod === period ? "active" : ""}`,
        period.toUpperCase(),
      );
      btn.type = "button";
      btn.dataset.period = period;
      btn.setAttribute("aria-pressed", String(currentPeriod === period));
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
      btn.setAttribute("aria-pressed", String(currentInterval === item.id));
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
            toolbar.querySelectorAll("[data-type]").forEach((b) => {
              const on = b === btn;
              b.classList.toggle("active", on);
              b.setAttribute("aria-pressed", String(on));
            });
          } else if (btn.dataset.period) {
            setChartPref(stockKey, "period", btn.dataset.period);
            toolbar.querySelectorAll("[data-period]").forEach((b) => {
              const on = b === btn;
              b.classList.toggle("active", on);
              b.setAttribute("aria-pressed", String(on));
            });
            updateIntervalControlsVisibility(
              toolbar,
              stockKey,
              btn.dataset.period,
            );
          } else if (btn.dataset.interval) {
            setChartPref(stockKey, "interval", btn.dataset.interval);
            toolbar.querySelectorAll("[data-interval]").forEach((b) => {
              const on = b === btn;
              b.classList.toggle("active", on);
              b.setAttribute("aria-pressed", String(on));
            });
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
            btn.setAttribute("aria-pressed", String(next === "on"));
          } else if (btn.dataset.volume !== undefined) {
            const curr = getChartPref(stockKey, "volume", "on");
            const next = curr === "on" ? "off" : "on";
            setChartPref(stockKey, "volume", next);
            btn.classList.toggle("active", next === "on");
            btn.setAttribute("aria-pressed", String(next === "on"));
          }
          const p = getChartPref(stockKey, "period", "3mo");
          const inv = getChartPref(stockKey, "interval", "auto");
          refreshStockChart(targetWrapper, p, inv);
          setTimeout(() => {
            if (
              modal.classList.contains("hidden") ||
              modal.dataset.stockKey !== stockKey
            ) {
              return;
            }
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
          if (
            modal.classList.contains("hidden") ||
            modal.dataset.stockKey !== stockKey
          ) {
            return;
          }
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
      if (typeof window.closeTopmostOverlayOnEscape === "function") {
        window.closeTopmostOverlayOnEscape(e);
      } else {
        e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        closeFsChartModal();
      }
      return;
    }
    if (e.key !== "Tab") return;
    const focusable = Array.from(
      modal.querySelectorAll(
        'button:not([disabled]):not([style*="display: none"]):not(.hidden), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => isUiElementVisible(el));
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
    modal.style.zIndex = "";
    modal.setAttribute("aria-hidden", "true");
    modal.setAttribute("inert", "");
    delete modal.dataset.stockKey;
    const fsCanvasWrapper = document.getElementById("fs-chart-canvas-wrapper");
    fsCanvasWrapper
      ?.querySelectorAll(".chart-error")
      .forEach((el) => el.remove());
    const fsCanvas = document.getElementById("fs-chart-canvas");
    if (fsCanvas) destroyChart(fsCanvas);
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
