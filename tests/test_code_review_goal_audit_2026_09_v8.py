"""Tests for 2026-09 code review improvements:
- LangSearch rerank score coercion (None, string, NaN, non-finite scores)
- Search service trending titles count coercion and clamping
- Portfolio metrics FX rate resilience (non-positive / NaN rate fallback)
"""

from __future__ import annotations

import math
import subprocess
from unittest.mock import patch

from services.search.langsearch import langsearch_rerank
from services.search_service import collect_market_trending_titles


def test_langsearch_rerank_handles_none_and_string_scores():
    """Verify langsearch_rerank safely coerces non-standard relevance scores without raising TypeError."""
    docs = [
        {"title": "Doc 0", "snippet": "Text 0"},
        {"title": "Doc 1", "snippet": "Text 1"},
        {"title": "Doc 2", "snippet": "Text 2"},
        {"title": "Doc 3", "snippet": "Text 3"},
    ]

    mock_resp = {
        "results": [
            {"index": 0, "relevance_score": None},
            {"index": 1, "relevance_score": "0.85"},
            {"index": 2, "relevance_score": 0.95},
            {"index": 3, "relevance_score": "invalid_score"},
        ]
    }

    with patch("services.search.langsearch._langsearch_post_json", return_value=mock_resp):
        reranked = langsearch_rerank("test query", docs, "dummy_api_key")

    assert len(reranked) == 4
    # Expected ordering: Doc 2 (0.95), Doc 1 (0.85), Doc 0 (0.0), Doc 3 (0.0)
    assert reranked[0]["title"] == "Doc 2"
    assert math.isclose(reranked[0]["relevance_score"], 0.95)
    assert reranked[1]["title"] == "Doc 1"
    assert math.isclose(reranked[1]["relevance_score"], 0.85)
    assert reranked[2]["relevance_score"] == 0.0
    assert reranked[3]["relevance_score"] == 0.0


def test_collect_market_trending_titles_count_coercion():
    """Verify collect_market_trending_titles handles string, negative, and invalid count inputs."""
    fake_titles = [f"Trend Title {i}" for i in range(15)]

    with patch("services.search_service._get_market_trending_titles", return_value=fake_titles):
        # 1. String count "5"
        res_str = collect_market_trending_titles("us", count="5")
        assert len(res_str) == 5

        # 2. String count "100" (should be capped at 15)
        res_large = collect_market_trending_titles("us", count="100")
        assert len(res_large) == 15

        # 3. Non-numeric string "invalid" (should default to 10)
        res_invalid = collect_market_trending_titles("us", count="invalid")
        assert len(res_invalid) == 10

        # 4. Negative count -5 (should be clamped to 1)
        res_neg = collect_market_trending_titles("us", count=-5)
        assert len(res_neg) == 1

        # 5. Zero count 0 (should be clamped to 1)
        res_zero = collect_market_trending_titles("us", count=0)
        assert len(res_zero) == 1


def test_ui_portfolio_metrics_fx_resilience():
    """Verify calculatePortfolioMetrics and updatePortfolioHeader handle non-positive/anomalous FX rates via Node VM."""
    node_script = """
    const fs = require('fs');
    const vm = require('vm');

    const uiCode = fs.readFileSync('static/js/ui.js', 'utf8');

    // Create a sandbox with minimal browser / app mocks
    const sandbox = {
      window: { addEventListener: () => {}, removeEventListener: () => {} },
      document: {
        getElementById: () => null,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: () => ({ appendChild: () => {}, classList: { add: () => {}, remove: () => {} } }),
        addEventListener: () => {},
        removeEventListener: () => {},
      },
      state: { stocks: { us: [], jp: [], idx: [] }, indices: { USDJPY: { change: 160.0 } } },
      toFiniteNumber: (val, fallback = 0) => {
        const n = Number(val);
        return Number.isFinite(n) ? n : fallback;
      },
      DOM: { get: () => null },
      drawSectorPieChart: () => {},
      console: console,
    };
    vm.createContext(sandbox);

    // Extract calculatePortfolioMetrics definition and run
    vm.runInContext(uiCode, sandbox);

    const holdings = [
      { symbol: 'AAPL', market: 'us', currency: 'USD', shares: 10, avg_price: 150, price: 160, change: 5 }
    ];

    // Case 1: currentFxRate <= 0 should safely default to 1.0
    const m1 = sandbox.calculatePortfolioMetrics(holdings, 0, 0);
    if (!Number.isFinite(m1.totalValue) || m1.totalValue <= 0) {
      throw new Error('totalValue should be positive finite number even with 0 FX rate');
    }

    // Case 2: prevFxRate <= 0 should safely default to validCurrentFx
    const m2 = sandbox.calculatePortfolioMetrics(holdings, 150.0, -10.0);
    if (!Number.isFinite(m2.todayPl)) {
      throw new Error('todayPl should be finite number with negative prevFxRate');
    }

    // Case 3: updatePortfolioHeader handles usdJpyChange > currentFxRate without negative prevFxRate
    let capturedPrevFx = null;
    sandbox.calculatePortfolioMetrics = (h, curFx, prevFx) => {
      capturedPrevFx = prevFx;
      return { totalValue: 0, totalCost: 0, totalPl: 0, todayPl: 0 };
    };
    sandbox.updatePortfolioHeader(holdings, 150.0, true, { animation: false });
    if (capturedPrevFx <= 0) {
      throw new Error(`capturedPrevFx should be strictly positive, got ${capturedPrevFx}`);
    }

    console.log('ALL TESTS PASSED');
    """

    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=True)
    assert "ALL TESTS PASSED" in res.stdout
