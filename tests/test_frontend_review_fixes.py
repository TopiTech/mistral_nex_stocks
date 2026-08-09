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


def test_escape_uses_drawer_close_paths_that_restore_state():
    utils_source = _read("static/js/utils.js")
    ui_source = _read("static/js/ui.js")

    assert "closeStockDetailDrawer();" in utils_source
    assert "closeAiDrawer();" in utils_source
    assert "detailInner.appendChild(child)" in ui_source
    assert "stockDetailDrawerTrigger?.focus?.();" in ui_source
    assert "aiDrawerTrigger?.focus?.();" in ui_source
