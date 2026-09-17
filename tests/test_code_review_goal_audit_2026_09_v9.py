"""Tests for 2026-09 code review improvements (v9):
- ScreenerQueryRequest 0.0 boundary value acceptance (R36)
- Quotes API force query parameter normalization (?force=true, ?force=1, etc.) (R37)
- WAI-ARIA 4-way arrow keys and roving tabindex across dashboard, ai_portfolio, and ui drawer (R38)
- WAI-ARIA 4-way arrow keys and roving tabindex in Market Observatory (R39)
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app import create_app
from schemas.stocks import ScreenerQueryRequest


def test_screener_query_request_zero_and_boundary_validations():
    """Verify ScreenerQueryRequest allows 0.0 for max_price, max_market_cap, and max_pe."""
    # 1. Exact zero boundaries allowed
    req = ScreenerQueryRequest(
        min_price=0.0,
        max_price=0.0,
        min_market_cap=0.0,
        max_market_cap=0.0,
        min_pe=0.0,
        max_pe=0.0,
    )
    assert req.min_price == 0.0
    assert req.max_price == 0.0
    assert req.min_market_cap == 0.0
    assert req.max_market_cap == 0.0
    assert req.min_pe == 0.0
    assert req.max_pe == 0.0

    # 2. Positive boundaries allowed
    req_pos = ScreenerQueryRequest(
        min_price=10.5,
        max_price=500.0,
        min_market_cap=1_000_000.0,
        max_market_cap=10_000_000.0,
        min_pe=5.0,
        max_pe=35.0,
    )
    assert req_pos.min_price == 10.5
    assert req_pos.max_price == 500.0

    # 3. Negative bounds must be rejected
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(max_price=-1.0)
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(max_market_cap=-0.01)
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(max_pe=-5.0)

    # 4. Inverted bounds must be rejected
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(min_price=100.0, max_price=50.0)
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(min_market_cap=1000.0, max_market_cap=500.0)
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(min_pe=25.0, max_pe=10.0)


@pytest.fixture
def app_client():
    """Create test flask client."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.mark.parametrize(
    "query_val, should_sync",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("YES", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        (None, False),
    ],
)
def test_quotes_api_force_sync_handling(app_client, query_val, should_sync):
    """Verify api_indices and api_stocks normalize force parameter properly."""
    with patch(
        "routes.stocks.quotes.require_trusted_or_admin", return_value=(True, "ok")
    ), patch(
        "routes.stocks.quotes.schedule_sync_all_stocks_now"
    ) as mock_sync, patch(
        "routes.stocks.quotes.resolve_indices_for_response", return_value={"^DJI": {}}
    ), patch(
        "routes.stocks.quotes.resolve_stocks_for_response",
        return_value={"us": [], "jp": [], "idx": []},
    ):
        # 1. Test /api/indices
        url_idx = "/api/indices" if query_val is None else f"/api/indices?force={query_val}"
        resp_idx = app_client.get(url_idx)
        assert resp_idx.status_code == 200

        if should_sync:
            mock_sync.assert_called_with(force=True)
        else:
            mock_sync.assert_not_called()

        mock_sync.reset_mock()

        # 2. Test /api/stocks
        url_stocks = "/api/stocks" if query_val is None else f"/api/stocks?force={query_val}"
        resp_stocks = app_client.get(url_stocks)
        assert resp_stocks.status_code == 200

        if should_sync:
            mock_sync.assert_called_with(force=True)
        else:
            mock_sync.assert_not_called()


def test_frontend_wai_aria_navigation_scripts():
    """Verify WAI-ARIA roving tabindex and arrow keys via Node execution and content inspection."""
    node_script = """
    const fs = require('fs');

    // 1. Check index_main.js for 4-way arrow keys in initTabEvents
    const indexCode = fs.readFileSync('static/js/index_main.js', 'utf8');
    const hasArrowUp = indexCode.includes('"ArrowUp"');
    const hasArrowDown = indexCode.includes('"ArrowDown"');
    if (!hasArrowUp || !hasArrowDown) {
      throw new Error('index_main.js missing ArrowUp/ArrowDown in tab navigation');
    }

    // 2. Check ai_portfolio.js for 4-way arrow keys in mode switcher and roving tabindex in preset pills
    const aiCode = fs.readFileSync('static/js/ai_portfolio.js', 'utf8');
    if (!aiCode.includes('"ArrowUp"') || !aiCode.includes('"ArrowDown"')) {
      throw new Error('ai_portfolio.js missing ArrowUp/ArrowDown');
    }
    if (!aiCode.includes('tabindex') || !aiCode.includes('aria-pressed')) {
      throw new Error('ai_portfolio.js missing roving tabindex/aria-pressed');
    }

    // 3. Check ui.js for 4-way arrow keys in drawer-tab-bar
    const uiCode = fs.readFileSync('static/js/ui.js', 'utf8');
    if (!uiCode.includes('"ArrowUp"') || !uiCode.includes('"ArrowDown"')) {
      throw new Error('ui.js missing ArrowUp/ArrowDown in drawer-tab-bar');
    }

    // 4. Check orbit-entry.js for roving tabindex and 4-way arrow keys in market selector
    const orbitCode = fs.readFileSync('static/js/experimental/orbit-entry.js', 'utf8');
    if (!orbitCode.includes('marketButtons') || !orbitCode.includes('tabindex')) {
      throw new Error('orbit-entry.js missing market button roving tabindex');
    }
    if (!orbitCode.includes('"ArrowUp"') || !orbitCode.includes('"ArrowDown"')) {
      throw new Error('orbit-entry.js missing ArrowUp/ArrowDown in market selector');
    }

    // 5. Check temporal-controller.js for roving tabindex and 4-way arrow keys in granularityGroup
    const tcCode = fs.readFileSync('static/js/experimental/temporal-controller.js', 'utf8');
    if (!tcCode.includes('_granularityKeydownHandler')) {
      throw new Error('temporal-controller.js missing _granularityKeydownHandler');
    }
    if (!tcCode.includes('"ArrowUp"') || !tcCode.includes('"ArrowDown"')) {
      throw new Error('temporal-controller.js missing ArrowUp/ArrowDown in granularityGroup');
    }

    console.log('ALL_FRONTEND_WAI_ARIA_CHECKS_PASSED');
    """

    res = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Node check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "ALL_FRONTEND_WAI_ARIA_CHECKS_PASSED" in res.stdout
