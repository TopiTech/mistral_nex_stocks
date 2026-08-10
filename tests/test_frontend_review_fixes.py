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

    assert "closeStockDetailDrawer();" in utils_source
    assert "closeAiDrawer();" in utils_source
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
    assert "this._lastSSEParams !== params || !resolvedUrl" in client_source


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
    assert "window.addEventListener(\"keydown\", this._keyHandler)" not in ai_dive_source
