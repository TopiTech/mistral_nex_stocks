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
from unittest.mock import patch

from app import app


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

    # 1. Stepper buttons
    assert 'aria-label="保有数を減らす"' in html
    assert 'aria-label="保有数を増やす"' in html
    assert 'aria-label="取得単価を減らす"' in html
    assert 'aria-label="取得単価を増やす"' in html

    # 2. Modal action buttons have type="button"
    assert '<button type="button" id="savePortfolioBtn"' in html
    assert '<button type="button" id="saveAlertBtn"' in html

    # 3. Chart canvases have role="img" and descriptive aria-labels
    assert 'id="pf-summary-canvas" role="img" aria-label="ポートフォリオ評価額推移チャート"' in html
    assert 'id="pf-sector-canvas" role="img" aria-label="ポートフォリオセクター別資産構成チャート"' in html
    assert 'id="ai-pf-summary-canvas" role="img" aria-label="AIポートフォリオ仮想パフォーマンス推移チャート"' in html
    assert 'id="ai-pf-sector-canvas" role="img" aria-label="AIポートフォリオアセット構成チャート"' in html
    assert 'id="fs-chart-canvas" role="img" aria-label="フルスクリーン詳細チャート"' in html


def test_experimental_orbit_accessibility_attributes() -> None:
    """Verify experimental_orbit.html has proper search input labels and canvas roles."""
    client = app.test_client()
    res = client.get("/experimental/orbit")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # 1. Search input aria-label
    assert 'id="orbit-search-input"' in html
    assert 'aria-label="銘柄コード・会社名・セクターで検索"' in html

    # 2. Canvases have role="img"
    assert 'id="orbit-canvas"' in html
    assert 'role="img"' in html
    assert 'id="constellation-chart-canvas"' in html
    assert 'aria-label="相対パフォーマンス比較チャート"' in html


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
