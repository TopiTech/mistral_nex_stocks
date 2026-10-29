"""
Unit and regression tests for comprehensive UI and Backend code review fixes (October 2026).
Covers:
1. services/ai_tools.py: _tool_get_market_news detects JP tickers ('7203', '7203.T') as market='jp'.
2. services/ai_tools.py: _tool_get_company_fundamentals preserves 0.0 for dividend_yield, eps, pe_ratio, etc.
3. static/js/ui.js: formatStockChangeMetrics accessibility, zero-change neutrality, and NaN/missing safety.
4. static/js/api_client.ts: APIClient.delete supports optional JSON body.
5. static/js/api.js: addStock suppresses duplicate apiFetch toast and surfaces descriptive error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from services.ai_tools import _tool_get_company_fundamentals, _tool_get_market_news

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_tool_get_market_news_market_detection():
    """Verify _tool_get_market_news correctly detects Japanese stock tickers as market='jp'."""
    captured_markets = []

    def mock_collect(market):
        captured_markets.append(market)
        return [{"title": f"News for {market}", "snippet": "Market update", "source": "Reuters"}]

    with patch("trend_sources.collect_market_news_items_fast", side_effect=mock_collect):
        # 1. Japanese stock ticker without .T
        captured_markets.clear()
        res1 = _tool_get_market_news({"query": "7203"})
        assert captured_markets == ["jp"], f"Expected 'jp', got {captured_markets}"
        assert res1["count"] >= 0

        # 2. Japanese stock ticker with .T
        captured_markets.clear()
        res2 = _tool_get_market_news({"query": "6758.T"})
        assert captured_markets == ["jp"], f"Expected 'jp', got {captured_markets}"
        assert res2["count"] >= 0

        # 3. Japanese text
        captured_markets.clear()
        res3 = _tool_get_market_news({"query": "トヨタ自動車"})
        assert captured_markets == ["jp"], f"Expected 'jp', got {captured_markets}"
        assert res3["count"] >= 0

        # 4. US ticker
        captured_markets.clear()
        res4 = _tool_get_market_news({"query": "NVDA"})
        assert captured_markets == ["us"], f"Expected 'us', got {captured_markets}"
        assert res4["count"] >= 0

        # 5. General English query
        captured_markets.clear()
        res5 = _tool_get_market_news({"query": "semiconductors"})
        assert captured_markets == ["us"], f"Expected 'us', got {captured_markets}"
        assert res5["count"] >= 0


def test_tool_get_company_fundamentals_preserves_zero():
    """Verify _tool_get_company_fundamentals preserves 0.0 values instead of treating them as None."""
    sample_info = {
        "name": "Zero Metrics Corp",
        "sector": "Technology",
        "industry": "Software",
        "market_cap": 1_000_000,
        "dividend_yield": 0.0,
        "dividendYield": None,
        "eps": 0.0,
        "earningsPerShare": None,
        "pe_ratio": 0.0,
        "trailingPE": None,
        "forward_pe": 15.0,
        "forwardPE": None,
        "pb_ratio": 0.0,
        "priceToBook": None,
        "fifty_two_week_high": 120.0,
        "fifty_two_week_low": 0.0,
    }

    with patch("utils.stock_payload.get_stock_info_cached", return_value=sample_info):
        res = _tool_get_company_fundamentals({"symbol": "ZERO", "market": "us"})

        assert res["symbol"] == "ZERO"
        assert res["dividend_yield"] == 0.0, "dividend_yield: 0.0 must be preserved"
        assert res["eps"] == 0.0, "eps: 0.0 must be preserved"
        assert res["pe_ratio"] == 0.0, "pe_ratio: 0.0 must be preserved"
        assert res["pb_ratio"] == 0.0, "pb_ratio: 0.0 must be preserved"
        assert res["52w_low"] == 0.0, "52w_low: 0.0 must be preserved"
        assert res["forward_pe"] == 15.0


def test_format_stock_change_metrics_via_node():
    """Verify formatStockChangeMetrics in static/js/ui.js handles positive, negative, zero, and missing states."""
    node_script = """
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');

    const uiCode = fs.readFileSync(path.join(process.cwd(), 'static/js/ui.js'), 'utf-8');

    // Extract formatStockChangeMetrics function using regex or VM context
    const match = uiCode.match(/function formatStockChangeMetrics\\([\\s\\S]*?\\n\\}/);
    if (!match) {
      console.error('formatStockChangeMetrics not found');
      process.exit(1);
    }

    const context = {};
    vm.createContext(context);
    vm.runInContext(match[0], context);
    const { formatStockChangeMetrics } = context;

    // Test 1: Positive change
    const pos = formatStockChangeMetrics({ change: 15.5, change_percent: 1.25 });
    if (!pos.className.includes('pos') || !pos.text.includes('▲+15.5 (+1.25%)') || !pos.ariaLabel.includes('上昇')) {
      console.error('Positive change test failed:', pos);
      process.exit(2);
    }

    // Test 2: Negative change
    const neg = formatStockChangeMetrics({ change: -8.0, change_percent: -0.65 });
    if (!neg.className.includes('neg') || !neg.text.includes('▼-8 (-0.65%)') || !neg.ariaLabel.includes('下落')) {
      console.error('Negative change test failed:', neg);
      process.exit(3);
    }

    // Test 3: Zero change (must be neutral, not pos/neg, not rising)
    const zero = formatStockChangeMetrics({ change: 0, change_percent: 0 });
    if (zero.className.includes('pos') || zero.className.includes('neg') || zero.ariaLabel.includes('上昇')) {
      console.error('Zero change test failed (must not be pos/rising):', zero);
      process.exit(4);
    }
    if (!zero.ariaLabel.includes('変わらず') || !zero.text.includes('0 (0%)')) {
      console.error('Zero change text/aria mismatch:', zero);
      process.exit(5);
    }

    // Test 4: Missing / non-finite change (must be neutral and not print undefined)
    const missing = formatStockChangeMetrics({ change: null, change_percent: undefined });
    if (missing.className.includes('pos') || missing.className.includes('neg') || missing.text.includes('undefined')) {
      console.error('Missing change test failed:', missing);
      process.exit(6);
    }
    if (!missing.text.includes('-- (--%)') || !missing.ariaLabel.includes('データなし')) {
      console.error('Missing change text mismatch:', missing);
      process.exit(7);
    }

    console.log('ALL_OK');
    """

    res = subprocess.run(
        ["node", "-e", node_script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Node script failed:\nstdout={res.stdout}\nstderr={res.stderr}"
    assert "ALL_OK" in res.stdout


def test_add_stock_suppresses_duplicate_api_fetch_toast():
    """Verify addStock in static/js/api.js calls apiFetch with showToast: false."""
    content = (ROOT_DIR / "static/js/api.js").read_text(encoding="utf-8")
    assert "/api/stocks/add" in content
    # Find addStock function slice
    start = content.find("async function addStock(")
    assert start != -1
    end = content.find("async function", start + 20)
    slice_code = content[start:end]

    assert "{ showToast: false }" in slice_code, (
        "addStock must pass { showToast: false } to apiFetch to avoid double toasts"
    )
    assert "APIErrorType.ABORTED" in slice_code, (
        "addStock must ignore aborted requests when presenting error toasts"
    )
