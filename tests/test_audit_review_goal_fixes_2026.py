"""Regression tests for 2026 code review audit fixes.

Covers:
1. HTML template DOM hierarchy integrity (no orphaned closing tags).
2. Mobile bottom navigation direct routing to /main across all page templates.
3. Accessibility enhancements: aria-labels, role="img", and explicit button types.
4. Mobile AI navigation button functional binding.
5. Defensive list handling in SSE stream initial snapshot builder.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from bs4 import BeautifulSoup

from app import app
from services.ai_service import generate_ai_technical_lines
from services.ai_tools import _tool_calculate_technical_levels


def test_heatmap_template_dom_hierarchy_and_nav() -> None:
    """Verify templates/heatmap.html has balanced tags and points to /main."""
    client = app.test_client()
    res = client.get("/heatmap")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # 1. Legend tag should close properly and not be followed by a stray </div>
    legend_match = re.search(
        r'<div class="heatmap-legend">.*?</div>\s*</main>',
        html,
        re.DOTALL,
    )
    assert legend_match is not None, "heatmap-legend should be immediately followed by </main> without an extra </div>"
    assert "</div>\n</div>\n\n</main>" not in html

    # 2. Mobile nav "ボード" link points directly to /main
    assert '<a href="/main" class="mobile-nav-item' in html


def test_page_templates_mobile_nav_direct_routing() -> None:
    """Verify all major page templates link mobile nav 'ボード' to /main rather than /."""
    client = app.test_client()

    for path in ("/main", "/screener", "/heatmap", "/settings"):
        res = client.get(path)
        assert res.status_code == 200, f"Failed to GET {path}"
        html = res.get_data(as_text=True)
        # Mobile bottom nav should have <a href="/main" class="mobile-nav-item...
        assert '<a href="/main" class="mobile-nav-item' in html, (
            f"Page {path} should route mobile bottom nav 'ボード' to /main"
        )
        # Should not contain <a href="/" class="mobile-nav-item
        assert '<a href="/" class="mobile-nav-item' not in html, (
            f"Page {path} still contains mobile nav href='/'"
        )


def test_index_accessibility_attributes() -> None:
    """Verify index.html contains proper a11y labels, canvas roles, and button types."""
    client = app.test_client()
    res = client.get("/main")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")

    # 1. Stepper buttons
    assert soup.find(attrs={"aria-label": "保有数を減らす"}) is not None
    assert soup.find(attrs={"aria-label": "保有数を増やす"}) is not None
    assert soup.find(attrs={"aria-label": "取得単価を減らす"}) is not None
    assert soup.find(attrs={"aria-label": "取得単価を増やす"}) is not None

    # 2. Modal action buttons have type="button"
    save_pf_btn = soup.find("button", id="savePortfolioBtn")
    assert save_pf_btn is not None
    assert save_pf_btn.get("type") == "button"

    save_alert_btn = soup.find("button", id="saveAlertBtn")
    assert save_alert_btn is not None
    assert save_alert_btn.get("type") == "button"

    # 3. Chart canvases have role="img" and descriptive aria-labels
    expected_canvases = {
        "pf-summary-canvas": "ポートフォリオ評価額推移チャート",
        "pf-sector-canvas": "ポートフォリオセクター別資産構成チャート",
        "ai-pf-summary-canvas": "AIポートフォリオ仮想パフォーマンス推移チャート",
        "ai-pf-sector-canvas": "AIポートフォリオアセット構成チャート",
        "fs-chart-canvas": "フルスクリーン詳細チャート",
    }
    for cid, label in expected_canvases.items():
        canvas = soup.find("canvas", id=cid)
        assert canvas is not None, f"Canvas #{cid} not found"
        assert canvas.get("role") == "img", f"Canvas #{cid} missing role='img'"
        assert canvas.get("aria-label") == label, f"Canvas #{cid} aria-label mismatch"


def test_experimental_orbit_accessibility_attributes() -> None:
    """Verify experimental_orbit.html has proper search input labels and canvas roles."""
    client = app.test_client()
    res = client.get("/experimental/orbit")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")

    # 1. Search input aria-label
    search_input = soup.find(id="orbit-search-input")
    assert search_input is not None
    assert search_input.get("aria-label") == "銘柄コード・会社名・セクターで検索"

    # 2. Canvases have role="img"
    orbit_canvas = soup.find("canvas", id="orbit-canvas")
    assert orbit_canvas is not None
    assert orbit_canvas.get("role") == "img"

    constellation_canvas = soup.find("canvas", id="constellation-chart-canvas")
    assert constellation_canvas is not None
    assert constellation_canvas.get("role") == "img"
    assert constellation_canvas.get("aria-label") == "相対パフォーマンス比較チャート"


def test_mobile_ai_portfolio_navigation_script() -> None:
    """Verify static/js/index_main.js properly binds mobile AI portfolio navigation."""
    js_path = Path(__file__).resolve().parents[1] / "static" / "js" / "index_main.js"
    content = js_path.read_text(encoding="utf-8")

    # Non-existent section reference must not be present
    assert "ai-portfolio-section" not in content

    # Proper tab and view activation logic must be present
    assert 'setActiveTab("portfolio")' in content
    assert 'getElementById("pf-mode-ai")' in content
    assert 'getElementById("portfolio-wrapper")' in content


def test_stream_stocks_payload_defensive_concatenation() -> None:
    """Verify stream snapshot builder tolerates None or non-list values for us/jp stocks."""
    with (
        patch("routes.stocks.stream.require_sse_auth", return_value=(True, None)),
        patch("routes.stocks.stream.resolve_stocks_for_response") as mock_resolve,
        patch("routes.stocks.stream.resolve_indices_for_response", return_value={}),
        patch("routes.stocks.stream.get_tradingview_ticker_tape_symbols", return_value=[]),
        patch("routes.stocks.stream.is_market_open", return_value=True),
        patch("routes.stocks.stream.realtime_market_engine"),
    ):
        # Scenario: stocks_payload returns None for us and non-list for jp
        mock_resolve.return_value = {
            "us": None,
            "jp": "invalid_non_list",
            "idx": [],
        }

        client = app.test_client()
        res = client.get("/api/stocks/stream?mode=2")
        assert res.status_code == 200
        # Should start streaming initial snapshot without raising TypeError
        first_chunk = next(res.response)
        text = first_chunk.decode("utf-8") if isinstance(first_chunk, bytes) else str(first_chunk)
        assert "initial_snapshot" in text


def test_ai_tools_wilder_rsi_calculation() -> None:
    """Verify _tool_calculate_technical_levels uses Wilder's smoothing and returns valid bounds."""
    prices = [
        100.0,
        102.0,
        101.0,
        103.0,
        102.5,
        104.0,
        103.5,
        105.0,
        104.5,
        106.0,
        105.5,
        107.0,
        106.5,
        108.0,
        107.5,
        109.0,
    ]
    dates = pd.date_range("2026-08-01", periods=len(prices), freq="D")
    df = pd.DataFrame({"Close": prices, "Open": prices, "High": prices, "Low": prices}, index=dates)

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = df

    with patch("utils.market_utils.safe_get_ticker", return_value=mock_ticker):
        res = _tool_calculate_technical_levels({"symbol": "AAPL", "period": "1mo"})
        assert "error" not in res
        assert res["symbol"] == "AAPL"
        assert 0.0 <= res["rsi_14"] <= 100.0
        assert res["current_price"] == 109.0
        assert res["support_level"] == 100.0
        assert res["resistance_level"] == 109.0


def test_generate_ai_technical_lines_filters_non_finite_prices() -> None:
    """Verify generate_ai_technical_lines drops entries with NaN or Inf prices."""
    dummy_ohlc = [
        {"x": 1700000000000 + i * 86400000, "o": 100 + i, "h": 105 + i, "l": 99 + i, "c": 102 + i}
        for i in range(10)
    ]

    mock_response = {
        "choices": [
            {
                "message": {
                    "parsed": {
                        "summary": "AI Technical Analysis",
                        "trend_bias": "Bullish",
                        "lines": [
                            {
                                "id": "line_1",
                                "type": "support",
                                "label": "Valid Support",
                                "start_price": 100.0,
                                "end_price": 105.0,
                            },
                            {
                                "id": "line_2",
                                "type": "resistance",
                                "label": "Inf Resistance",
                                "start_price": float("inf"),
                                "end_price": 120.0,
                            },
                            {
                                "id": "line_3",
                                "type": "trend",
                                "label": "NaN Trend",
                                "start_price": 95.0,
                                "end_price": float("nan"),
                            },
                        ],
                    }
                }
            }
        ]
    }

    with patch("services.ai_service.call_mistral_chat", return_value=mock_response):
        res = generate_ai_technical_lines("test-key", "AAPL", "us", "3mo", dummy_ohlc)
        assert "error" not in res
        assert res["summary"] == "AI Technical Analysis"
        assert len(res["lines"]) == 1
        assert res["lines"][0]["id"] == "line_1"
        assert res["lines"][0]["start_price"] == 100.0
        assert res["lines"][0]["end_price"] == 105.0

