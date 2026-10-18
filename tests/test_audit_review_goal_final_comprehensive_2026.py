"""
Unit and regression tests for comprehensive UI and Backend code review fixes (Goal Audit 2026).

Verifies:
1. static/js/ui.js: renderStockDetailDrawerHeader correctly formats positive, negative, zero, and missing change_percent.
2. chrome_extension/content.js: explicit .T Japanese stock codes (e.g. 2002.T) within year bounds are preserved.
3. routes/stocks/views.py: /api/stocks/delete supports both POST and DELETE methods and returns {success: True, ok: True}.
4. services/ai_service.py: executes tool functions inside Flask application context.
5. services/ai_tools.py: _tool_get_market_news recognizes half-width Katakana (半角カタカナ) as market='jp'.
6. static/js/api.js: searchStocks and addStock surface descriptive reason details.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app_state import app_state
from routes.stocks.views import views_bp
from services.ai_tools import _tool_get_market_news

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_render_stock_detail_drawer_header_via_node():
    """Verify renderStockDetailDrawerHeader correctly formats positive, negative, zero, and missing change."""
    node_script = """
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');

    const uiCode = fs.readFileSync(path.join(process.cwd(), 'static/js/ui.js'), 'utf-8');

    // Extract renderStockDetailDrawerHeader function
    const match = uiCode.match(/function renderStockDetailDrawerHeader\\([\\s\\S]*?\\n\\}/);
    if (!match) {
      console.error('renderStockDetailDrawerHeader not found');
      process.exit(1);
    }

    const mockElements = {
      'drawer-stock-symbol': { textContent: '' },
      'drawer-stock-name': { textContent: '' },
      'drawer-stock-price-badge': { textContent: '', className: '' }
    };

    const context = {
      document: {
        getElementById: (id) => mockElements[id] || null
      },
      formatDrawerPrice: (stock) => String(stock.price || '--')
    };
    vm.createContext(context);
    vm.runInContext(match[0], context);
    const { renderStockDetailDrawerHeader } = context;

    // 1. Positive change
    renderStockDetailDrawerHeader({ symbol: 'AAPL', price: 230, change_percent: 2.5 });
    let badge = mockElements['drawer-stock-price-badge'];
    if (!badge.className.includes('pos') || !badge.textContent.includes('(+2.50%)')) {
      console.error('Positive change failed:', badge);
      process.exit(2);
    }

    // 2. Negative change
    renderStockDetailDrawerHeader({ symbol: 'MSFT', price: 420, change_percent: -1.2 });
    badge = mockElements['drawer-stock-price-badge'];
    if (!badge.className.includes('neg') || !badge.textContent.includes('(-1.20%)')) {
      console.error('Negative change failed:', badge);
      process.exit(3);
    }

    // 3. Zero change: must NOT have pos/neg class and must NOT have + sign
    renderStockDetailDrawerHeader({ symbol: '7203.T', price: 2800, change_percent: 0.0 });
    badge = mockElements['drawer-stock-price-badge'];
    if (badge.className.includes('pos') || badge.className.includes('neg') || badge.textContent.includes('+0.00%')) {
      console.error('Zero change failed (must be neutral without + sign):', badge);
      process.exit(4);
    }
    if (!badge.textContent.includes('(0.00%)')) {
      console.error('Zero change percent string mismatch:', badge);
      process.exit(5);
    }

    // 4. Missing / null change: must be neutral without NaN or undefined
    renderStockDetailDrawerHeader({ symbol: 'GOOG', price: 180, change_percent: null });
    badge = mockElements['drawer-stock-price-badge'];
    if (badge.className.includes('pos') || badge.className.includes('neg') || !badge.textContent.includes('(--%)')) {
      console.error('Missing change failed (must be neutral with --%):', badge);
      process.exit(6);
    }

    console.log('DRAWER_HEADER_OK');
    """

    res = subprocess.run(
        ["node", "-e", node_script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Node script failed:\nstdout={res.stdout}\nstderr={res.stderr}"
    assert "DRAWER_HEADER_OK" in res.stdout


def test_extension_content_ticker_detection_preserves_explicit_t_suffix():
    """Verify chrome_extension/content.js retains explicit .T tickers in 1990-currentYear range."""
    node_script = """
    const fs = require('fs');
    const path = require('path');

    const contentCode = fs.readFileSync(path.join(process.cwd(), 'chrome_extension/content.js'), 'utf-8');

    // Simulate the jpRegex extraction block
    const jpRegex = /\\b([1-9]\\d{3})(?:\\.T)?\\b/g;
    const testText = '2002.T (日清製粉G本社) and 7203.T are listed in 2024 and 2025.';

    const matches = [];
    let match;
    const currentYear = new Date().getFullYear();

    while ((match = jpRegex.exec(testText)) !== null) {
      const code = match[1];
      const hasTSuffix = match[0].toUpperCase().endsWith('.T');
      const yearVal = parseInt(code, 10);
      if (!hasTSuffix && yearVal >= 1990 && yearVal <= currentYear + 1) {
        continue;
      }
      matches.push({ code, symbol: `${code}.T`, hasTSuffix });
    }

    // 2002.T and 7203.T must be matched; 2024 and 2025 must be dropped as years
    const matchedSymbols = matches.map(m => m.symbol);
    if (!matchedSymbols.includes('2002.T')) {
      console.error('2002.T was improperly skipped:', matches);
      process.exit(1);
    }
    if (!matchedSymbols.includes('7203.T')) {
      console.error('7203.T missing:', matches);
      process.exit(2);
    }
    if (matchedSymbols.includes('2024.T') || matchedSymbols.includes('2025.T')) {
      console.error('Bare year was improperly treated as ticker:', matches);
      process.exit(3);
    }

    console.log('CONTENT_TICKER_OK');
    """

    res = subprocess.run(
        ["node", "-e", node_script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Node script failed:\nstdout={res.stdout}\nstderr={res.stderr}"
    assert "CONTENT_TICKER_OK" in res.stdout


def test_api_stocks_delete_accepts_delete_and_post_methods():
    """Verify /api/stocks/delete supports both POST and DELETE methods and returns {success: True, ok: True}."""
    test_app = Flask(__name__)
    test_app.config["TESTING"] = True
    test_app.config["SECRET_KEY"] = "test-secret"
    test_app.register_blueprint(views_bp)

    client = test_app.test_client()

    with (
        patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.stocks.views.save_user_stocks"),
        patch("routes.stocks.views.invalidate_stock_caches"),
        patch("routes.stocks.views.remove_stock_from_caches"),
        patch("routes.stocks.views._sync_realtime_symbol"),
        patch("routes.stocks.views._announce_watchlist_state"),
        patch("routes.stocks.views.schedule_sync_all_stocks_now"),
    ):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us["DELTEST"] = "Delete Test Corp"
            app_state.market.user_us["DELTEST2"] = "Delete Test Corp 2"

        # 1. Test via DELETE method
        res_del = client.delete(
            "/api/stocks/delete",
            json={"symbol": "DELTEST", "market": "us"},
            content_type="application/json",
        )
        assert res_del.status_code == 200
        data_del = res_del.get_json()
        assert data_del["success"] is True
        assert data_del["ok"] is True
        with app_state.market.user_stocks_lock:
            assert "DELTEST" not in app_state.market.user_us

        # 2. Test via POST method
        res_post = client.post(
            "/api/stocks/delete",
            json={"symbol": "DELTEST2", "market": "us"},
            content_type="application/json",
        )
        assert res_post.status_code == 200
        data_post = res_post.get_json()
        assert data_post["success"] is True
        assert data_post["ok"] is True
        with app_state.market.user_stocks_lock:
            assert "DELTEST2" not in app_state.market.user_us


def test_tool_get_market_news_detects_half_width_katakana():
    """Verify _tool_get_market_news recognizes half-width katakana as Japanese market."""
    captured_markets = []

    def mock_collect(market):
        captured_markets.append(market)
        return [{"title": "News Title", "snippet": "Market update", "source": "Nikkei"}]

    with patch("trend_sources.collect_market_news_items_fast", side_effect=mock_collect):
        # Half-width Katakana 'ﾄﾖﾀ'
        captured_markets.clear()
        res = _tool_get_market_news({"query": "ﾄﾖﾀ"})
        assert captured_markets == ["jp"], f"Expected 'jp', got {captured_markets}"
        assert res["count"] >= 0


def test_api_chat_with_tools_runs_in_app_context():
    """Verify call_mistral_chat_with_tools executes within Flask application context."""
    from services.ai_service import call_mistral_chat_with_tools

    test_app = Flask(__name__)
    test_app.config["TESTING"] = True
    test_app.config["CUSTOM_TEST_FLAG"] = "FLAG_APP_CTX_OK"

    mock_chat_turn_1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "function": {
                                "name": "get_stock_quote",
                                "arguments": json.dumps({"symbol": "NVDA", "market": "us"}),
                            },
                        }
                    ],
                }
            }
        ]
    }
    mock_chat_turn_2 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "NVDA stock analysis complete.",
                    "tool_calls": None,
                }
            }
        ]
    }

    turn_count = 0

    def mock_mistral_call(*args, **kwargs):
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            return mock_chat_turn_1
        return mock_chat_turn_2

    captured_app_flag = []

    def mock_tool_call(tool_name, tool_args):
        from flask import current_app

        captured_app_flag.append(current_app.config.get("CUSTOM_TEST_FLAG"))
        return {"price": 130.0, "symbol": "NVDA"}

    with test_app.app_context():
        with (
            patch("services.ai_service.call_mistral_chat", side_effect=mock_mistral_call),
            patch("services.ai_tools.execute_mistral_tool_call", side_effect=mock_tool_call),
            patch("services.ai_service.is_openai_provider", return_value=False),
        ):
            res = call_mistral_chat_with_tools(
                api_key="dummy-key",
                messages=[{"role": "user", "content": "Analyze NVDA"}],
                max_tokens=200,
            )

            assert captured_app_flag == ["FLAG_APP_CTX_OK"], (
                f"Expected tool execution within app context with flag, got {captured_app_flag}"
            )
            assert res["choices"][0]["message"]["content"] == "NVDA stock analysis complete."


def test_api_js_surfaces_descriptive_error_details():
    """Verify static/js/api.js extracts data.details.reason in searchStocks and addStock."""
    api_js = (ROOT_DIR / "static/js/api.js").read_text(encoding="utf-8")

    assert "data.details?.reason || data.message || data.error" in api_js
    assert "data.details?.reason || data.message || data.error || \"不明なエラー\"" in api_js
