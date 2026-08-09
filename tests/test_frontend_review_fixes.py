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
