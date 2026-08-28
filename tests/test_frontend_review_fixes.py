"""Regression guards for frontend fixes that have no JavaScript test runner."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_ai_portfolio_copy_refreshes_market_and_portfolio_state():
    source = _read("static/js/ai_portfolio.js")
    copy_start = source.index("async function copyAiPortfolioToMy")
    copy_end = source.index("function renderAiPortfolio", copy_start)
    copy_handler = source[copy_start:copy_end]

    assert "await fetchInitialStocks()" in copy_handler
    assert "await loadPortfolioSnapshot()" in copy_handler
    assert "fetchStockData" not in copy_handler


def test_ai_portfolio_actions_surface_http_and_application_failures():
    source = _read("static/js/ai_portfolio.js")
    styles = _read("static/css/index.css")

    assert source.count("if (!resp.ok)") >= 4
    assert source.count("showAiPortfolioFailure(") >= 5
    assert 'box.setAttribute("role", "alert")' in source
    assert 'retryButton.textContent = "再試行"' in source
    assert 'container.querySelector(".ai-loading-box")?.remove()' in source
    assert ".ai-error-box button:focus-visible" in styles


def test_escape_uses_drawer_close_paths_that_restore_state():
    utils_source = _read("static/js/utils.js")
    ui_source = _read("static/js/ui.js")
    main_source = _read("static/js/index_main.js")

    assert "closeStockDetailDrawer();" in utils_source
    assert "closeAiDrawer();" in utils_source
    assert "closeFsChartModal();" in main_source
    assert "function closeFsChartModal()" in ui_source
    assert "detailInner.appendChild(child)" in ui_source
    assert "stockDetailDrawerTrigger?.focus?.();" in ui_source
    assert "aiDrawerTrigger?.focus?.();" in ui_source


def test_sse_ticket_request_is_cancelled_and_stale_results_are_never_opened():
    api_source = _read("static/js/api.js")
    client_source = _read("static/js/api_client.ts")

    assert "connectionGeneration" in api_source
    assert "ticketAbortController.abort()" in api_source
    assert "signal: ticketAbortController.signal" in api_source
    assert "if (!streamUrl || !isCurrentConnection()) return;" in api_source
    assert "urlProvider?: () => string | null | Promise<string | null>;" in client_source
    assert "if (this._lastSSEParams !== params) return;" in client_source


def test_sse_browser_transport_keeps_ticket_out_of_eventsource_url():
    source = _read("static/js/api.js")
    build_start = source.index("const buildStreamUrl")
    build_end = source.index("const openSseWithTicket", build_start)
    build_handler = source[build_start:build_end]

    assert "sse_ticket=" not in build_handler
    assert 'document.cookie.includes("sse_ticket=")' not in source
    assert "ticketData?.ok" in build_handler


def test_ai_portfolio_copy_surfaces_stale_fx_warning():
    source = _read("static/js/ai_portfolio.js")
    styles = _read("static/css/index.css")
    template = _read("templates/index.html")
    copy_start = source.index("async function copyAiPortfolioToMy")
    copy_end = source.index("function renderAiPortfolio", copy_start)
    copy_handler = source[copy_start:copy_end]

    assert "data.stale_warning" in copy_handler
    assert "⚠️ ${data.stale_warning}" in copy_handler
    ai_summary_rule = styles[styles.index(".ai-pf-summary-container") :]
    ai_summary_rule = ai_summary_rule[: ai_summary_rule.index("}")]
    assert "display: block" in ai_summary_rule
    assert '<div id="toast-container" role="status" aria-live="polite"></div>' in template


def test_latest_async_ui_request_wins_for_screener_portfolio_and_history():
    screener_source = _read("static/js/screener.js")
    portfolio_source = _read("static/js/ai_portfolio.js")
    temporal_source = _read("static/js/experimental/temporal-controller.js")

    assert "screenerAbortController.abort()" in screener_source
    assert "requestGeneration !== screenerRequestGeneration" in screener_source
    assert "beginAiPortfolioRequest" in portfolio_source
    assert "isCurrentAiPortfolioRequest(requestGeneration)" in portfolio_source
    assert "pendingController.abort()" in temporal_source
    assert "isCurrentHistoryContext(cleanSymbol, period)" in temporal_source
    assert "if (!this.isCurrentHistoryContext(symbol, period)) return;" in temporal_source


def test_latest_async_ui_request_owns_deferred_dom_and_polling_work():
    screener_source = _read("static/js/screener.js")
    heatmap_source = _read("static/js/heatmap.js")
    observatory_source = _read("static/js/experimental/orbit-entry.js")

    assert "renderTableRows(data.stocks || [], requestGeneration);" in screener_source
    assert "function renderTableRows(stocks, requestGeneration)" in screener_source
    raf_handler = screener_source[
        screener_source.index("requestAnimationFrame(() => {") :
    ]
    assert "if (requestGeneration !== screenerRequestGeneration) return;" in raf_handler

    assert "loadGeneration: 0" in heatmap_source
    assert "const isCurrentRequest = () =>" in heatmap_source
    assert "{ signal: controller.signal }" in heatmap_source
    assert "if (!isPolling && isCurrentRequest())" in heatmap_source
    assert "loadHeatmap(true, requestGeneration);" in heatmap_source

    assert "let loadRetryTimeout = null;" in observatory_source
    assert "function cancelObservatoryLoadRetry()" in observatory_source
    assert "loadAbortController === controller" in observatory_source
    assert "loadObservatoryData(retryCount + 1, requestGeneration);" in observatory_source
    assert observatory_source.count("signal: controller.signal") >= 2
    assert "cancelObservatoryLoadRetry();" in observatory_source


def test_heatmap_stops_hidden_3d_render_loop_and_restarts_only_for_3d():
    source = _read("static/js/heatmap.js")
    switch_start = source.index("function switchViewMode")
    switch_end = source.index("function resetCamera", switch_start)
    switch_handler = source[switch_start:switch_end]

    assert "start3DAnimation();" in switch_handler
    assert "stop3DAnimation();" in switch_handler
    assert "function start3DAnimation()" in source
    assert "function stop3DAnimation()" in source
    assert 'state.viewMode !== "3d"' in source
    assert "cancelAnimationFrame(state.three.animationFrameId);" in source
    assert "state.three.animationFrameId = null;" in source


def test_heatmap_keeps_2d_when_three_js_is_unavailable():
    source = _read("static/js/heatmap.js")
    switch_start = source.index("function switchViewMode")
    switch_end = source.index("function resetCamera", switch_start)
    switch_handler = source[switch_start:switch_end]
    assert 'typeof THREE === "undefined"' in switch_handler
    assert "return;" in switch_handler
    assert "if (!init3DScene()) return;" in switch_handler
    assert 'console.error("3D heatmap initialization failed:"' in switch_handler


def test_tradingview_messages_require_trusted_origin_and_widget_source():
    source = _read("static/js/tradingview_manager.js")
    handler_start = source.index("const messageHandler = (event) =>")
    handler_end = source.index("if (activeMessageHandler)", handler_start)
    handler = source[handler_start:handler_end]
    assert "trustedOrigins" in handler
    assert "event.origin" in handler
    assert "event.source" in handler


def test_tradingview_chart_inner_uses_flex_sizing_without_conflicting_height():
    """TradingView chart body must be sized by flex, not explicit heights.

    The fullscreen chart container is a column flex box: the control bar
    reserves its own height and the widget ``-inner`` div flexes to fill the
    remainder. A fixed ``height`` on the inner (CSS ``!important`` or inline)
    would either override the control-bar reservation or overflow on short
    viewports and be clipped by the modal body's ``overflow: hidden``.
    """
    css = _read("static/css/index.css")
    js = _read("static/js/tradingview_manager.js")

    container_rule = css[css.index(".tradingview-chart-container {") :]
    container_rule = container_rule[: container_rule.index("}")]
    assert "display: flex" in container_rule
    assert "flex-direction: column" in container_rule

    inner_rule = css[
        css.index('.tradingview-chart-container > div[id$="-inner"]') :
    ]
    inner_rule = inner_rule[: inner_rule.index("}")]
    assert "flex: 1" in inner_rule
    assert "min-height: 0" in inner_rule
    assert "height: 100% !important" not in inner_rule

    inner_creation_start = js.index('const chartInnerContainer = document.createElement')
    inner_creation_end = js.index("container.appendChild(chartInnerContainer)", inner_creation_start)
    inner_creation = js[inner_creation_start:inner_creation_end]
    assert "style.flex = \"1\"" in inner_creation
    assert "calc(100% - 31px)" not in inner_creation
    assert "minHeight" not in inner_creation


def test_3d_heatmap_supports_keyboard_stock_navigation():
    source = _read("static/js/heatmap.js")
    assert "renderer.domElement.tabIndex = 0" in source
    assert 'renderer.domElement.setAttribute("role", "application")' in source
    assert 'event.key === "ArrowRight"' in source
    assert 'event.key === "ArrowLeft"' in source
    assert 'event.key === "Enter"' in source
    assert 'renderer.domElement.addEventListener("keydown", onKeyDown)' in source


def test_initial_stock_fetch_is_cancelled_on_newer_fetch_or_sse_update():
    main_source = _read("static/js/index_main.js")
    api_source = _read("static/js/api.js")

    assert "let fetchInitialStocksAbortController = null;" in main_source
    assert "{ signal: abortController.signal }" in main_source
    assert "if (!isCurrentInitialStocksFetch(myGeneration, abortController))" in main_source
    assert "return INITIAL_STOCKS_FETCH_RESULT.FAILED;" in main_source
    assert "window.invalidatePendingInitialStocksFetch =" in main_source
    assert "invalidatePendingInitialStocksFetch;" in main_source
    assert "window.invalidatePendingInitialStocksFetch();" in api_source
    assert "window.invalidatePendingInitialStocksFetch?.();" in api_source


def test_sync_and_startup_distinguish_initial_stock_fetch_results():
    source = _read("static/js/index_main.js")

    sync_start = source.index("async function handleSyncClick")
    sync_end = source.index("/** Initialize bulk analyze", sync_start)
    sync_handler = source[sync_start:sync_end]
    assert "result === INITIAL_STOCKS_FETCH_RESULT.UPDATED" in sync_handler
    assert "result === INITIAL_STOCKS_FETCH_RESULT.FAILED" in sync_handler
    assert "最新データの到着を待っています" in sync_handler

    init_start = source.index("async function initializeApp")
    init_end = source.index("async function loadPortfolioSnapshot", init_start)
    init_handler = source[init_start:init_end]
    assert "result === INITIAL_STOCKS_FETCH_RESULT.FAILED" in init_handler
    assert "connectSSE();" in init_handler


def test_drawer_resolves_latest_card_data_and_uses_market_currency():
    ui_source = _read("static/js/ui.js")
    chart_source = _read("static/js/chart.js")

    click_start = ui_source.index('compact.addEventListener("click"')
    click_end = ui_source.index("compact.querySelector", click_start)
    click_handler = ui_source[click_start:click_end]
    assert "getLatestStockForDrawer(stock, wrapper)" in click_handler
    assert "wrapper?.__stockData" in ui_source
    assert "getStockByKey(stockKey)" in ui_source
    assert "stock = getLatestStockForDrawer(stock, wrapper);" in ui_source
    assert "const priceStr = formatDrawerPrice(stock);" in ui_source
    assert "updateOpenStockDetailDrawerHeader(wrapper, stock);" in ui_source
    assert "function updateOpenStockDetailDrawerHeader(wrapper, stock)" in ui_source
    assert "`$${priceStr}" not in ui_source
    assert 'if (m === "jp") return "¥";' in chart_source


def test_saved_ai_portfolio_ui_lists_selects_and_deletes_persisted_custom_themes():
    source = _read("static/js/ai_portfolio.js")
    template = _read("templates/index.html")
    styles = _read("static/css/index.css")

    assert 'csrfFetch("/api/ai-portfolio", {' in source or 'fetch("/api/ai-portfolio", {' in source
    assert 'csrfFetch("/api/ai-portfolio/custom", {' in source or 'fetch("/api/ai-portfolio/custom", {' in source
    assert 'method: "DELETE"' in source
    assert "function selectSavedAiPortfolio(portfolioId)" in source
    assert "function deleteSavedAiPortfolio(portfolioId)" in source
    assert "savedAiPortfoliosError" in source
    assert 'id="ai-saved-portfolios"' in template
    assert 'id="ai-saved-portfolios-status"' in template
    assert ".ai-saved-portfolio-list" in styles
    assert ".ai-saved-portfolio-error" in styles


def test_settings_and_observatory_polling_handle_fetching_contracts():
    settings_source = _read("static/js/settings.js")
    ai_dive_source = _read("static/js/experimental/ai-dive-controller.js")
    constellation_source = _read("static/js/experimental/constellation-controller.js")
    orbit_template = _read("templates/experimental_orbit.html")

    assert "data?.fetching" in settings_source
    assert "STOCKS_LOAD_MAX_RETRIES" in settings_source
    assert "getMarketNewsItems(data, stock)" in ai_dive_source
    assert "await this.waitForNewsRetry" in ai_dive_source
    assert "symbols: [stock.symbol]" not in ai_dive_source
    assert "選択市場のニュース速報" in orbit_template
    assert "if (!data?.fetching) break;" in constellation_source
    assert "await this.waitForAiPoll" in constellation_source


def test_observatory_modals_trap_focus_restore_trigger_and_block_global_shortcuts():
    accessibility_source = _read("static/js/experimental/accessibility-controller.js")
    ai_dive_source = _read("static/js/experimental/ai-dive-controller.js")
    entry_source = _read("static/js/experimental/orbit-entry.js")

    assert "const openModal = this.getOpenModal();" in accessibility_source
    assert "this.trapModalFocus(e, openModal);" in accessibility_source
    assert "captureModalReturnFocus" in accessibility_source
    assert "restoreModalFocus" in accessibility_source
    assert "aiDiveOverlay: elements.aiDiveOverlay" in entry_source
    assert "this._returnFocusTarget = document.activeElement;" in ai_dive_source
    assert 'window.addEventListener("keydown", this._keyHandler)' not in ai_dive_source


def test_bulk_analyze_favorites_preserves_market_and_uses_stock_key_in_drawer():
    api_source = _read("static/js/api.js")
    ui_source = _read("static/js/ui.js")

    # In api.js, success and failed records must preserve market, name, and stockKey
    assert "market: result.stock.market" in api_source
    assert "name: result.stock.name" in api_source
    assert "stockKey: stockKey" in api_source

    # In ui.js, detail button must resolve stockKey and not call getStockByKey with bare symbol
    assert "makeStockKey(item.market, item.symbol)" in ui_source
    assert "data-stock-key" in ui_source
    assert "getStockByKey(item.symbol)" not in ui_source
    assert "getStockByKey(stockKey)" in ui_source
