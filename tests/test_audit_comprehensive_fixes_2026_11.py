"""Regression and verification tests for November 2026 comprehensive code review fixes.

Covers:
1. services/realtime/scrapers.py: YahooJPRealtimeScraper.fetch_jp_symbol correctly
   extracts priceChange and priceChangeRate / priceChangePercent from __NEXT_DATA__
   when direct quote-field regex extraction fails.
2. services/ai_portfolio_service.py: generate_ai_portfolio_by_theme relies on
   sanitize_ai_portfolio to reject boolean weight_pct entries without coercing them
   into numeric weights.
3. routes/stocks/portfolio.py: api_update_portfolio ignores boolean avg_fx_rate
   values instead of coercing them to 1.0 in placeholder cache setup.
4. utils/stock_payload.py: resolve_stocks_for_response safely handles boolean volume,
   shares, avg_price, and avg_fx_rate entries.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app import app
from app_state import app_state
from routes.stocks.common import resolve_stocks_for_response
from services.ai_portfolio_service import generate_ai_portfolio_by_theme, sanitize_ai_portfolio
from services.realtime.scrapers import YahooJPRealtimeScraper


class _MockResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def test_yahoojp_scraper_extracts_change_from_next_data():
    """Verify YahooJPRealtimeScraper extracts change and change_percent from __NEXT_DATA__."""
    scraper = YahooJPRealtimeScraper()

    next_data_payload = {
        "props": {
            "pageProps": {
                "detail": {
                    "price": {"value": "2450.5"},
                    "priceChange": {"value": "+45.0"},
                    "priceChangeRate": {"value": "+1.87"},
                }
            }
        }
    }
    # HTML without escaped regex quote patterns, forcing fallback to __NEXT_DATA__.
    html = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <script id="__NEXT_DATA__" type="application/json">
          {json.dumps(next_data_payload)}
        </script>
      </head>
      <body><div>Stock Page</div></body>
    </html>
    """

    mock_session = MagicMock()
    mock_session.get.return_value = _MockResponse(html, status_code=200)

    with patch.object(scraper, "_get_session", return_value=mock_session):
        with patch.object(scraper, "_fetch_kabutan_symbol", return_value=None):
            payload = scraper.fetch_jp_symbol("7203.T")

    assert payload is not None
    assert payload["symbol"] == "7203.T"
    assert payload["price"] == 2450.5
    assert payload["change"] == 45.0
    assert payload["change_percent"] == 1.87
    assert payload["source"] == "yahoojp"


def test_yahoojp_scraper_extracts_change_percent_alternative_field():
    """Verify fallback to priceChangePercent if priceChangeRate is absent in __NEXT_DATA__."""
    scraper = YahooJPRealtimeScraper()

    next_data_payload = {
        "props": {
            "pageProps": {
                "quote": {
                    "price": {"value": "1820.0"},
                    "priceChange": {"value": "-25.0"},
                    "priceChangePercent": {"value": "-1.35"},
                }
            }
        }
    }
    html = f"""
    <html>
      <head>
        <script id="__NEXT_DATA__" type="application/json">
          {json.dumps(next_data_payload)}
        </script>
      </head>
      <body></body>
    </html>
    """

    mock_session = MagicMock()
    mock_session.get.return_value = _MockResponse(html, status_code=200)

    with patch.object(scraper, "_get_session", return_value=mock_session):
        with patch.object(scraper, "_fetch_kabutan_symbol", return_value=None):
            payload = scraper.fetch_jp_symbol("6758.T")

    assert payload is not None
    assert payload["symbol"] == "6758.T"
    assert payload["price"] == 1820.0
    assert payload["change"] == -25.0
    assert payload["change_percent"] == -1.35


def test_ai_portfolio_rejects_boolean_weight_pct():
    """Verify sanitize_ai_portfolio drops boolean weights and normalizes remainder to 100%."""
    raw = {
        "id": "test-bool-weight",
        "title": "Boolean Weight Test",
        "description": "Testing boolean weight rejection",
        "items": [
            {"symbol": "NVDA", "market": "us", "weight_pct": True, "target_price": 150.0},
            {"symbol": "MSFT", "market": "us", "weight_pct": 50.0, "target_price": 450.0},
            {"symbol": "AAPL", "market": "us", "weight_pct": 50.0, "target_price": 240.0},
        ],
    }
    cleaned = sanitize_ai_portfolio(raw)
    symbols = [it["symbol"] for it in cleaned["items"]]
    assert "NVDA" not in symbols
    assert "MSFT" in symbols
    assert "AAPL" in symbols
    assert sum(it["weight_pct"] for it in cleaned["items"]) == 100.0
    assert cleaned["items"][0]["weight_pct"] == 50.0
    assert cleaned["items"][1]["weight_pct"] == 50.0


def test_ai_portfolio_generate_with_mocked_llm(tmp_path):
    """Verify generate_ai_portfolio_by_theme cleans and normalizes LLM generated items."""
    llm_output = {
        "title": "LLM Generated Theme",
        "description": "Theme description",
        "risk_level": "mid",
        "expected_return": "10-15%",
        "commentary": "Market commentary",
        "items": [
            {"symbol": "GOOGL", "market": "us", "weight_pct": 40.0, "target_price": 180.0, "rationale": "Strong", "risk_level": "mid"},
            {"symbol": "AMZN", "market": "us", "weight_pct": 60.0, "target_price": 200.0, "rationale": "Cloud", "risk_level": "mid"},
        ],
    }

    test_storage = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage):
        with patch("services.ai_portfolio_service.get_mistral_api_key", return_value="dummy_key"):
            with patch("services.ai_portfolio_service.call_mistral_chat") as mock_chat:
                mock_chat.return_value = {
                    "choices": [{"message": {"content": json.dumps(llm_output)}}]
                }
                res = generate_ai_portfolio_by_theme("ai_tech_theme")

    res_symbols = [it["symbol"] for it in res["items"]]
    assert "GOOGL" in res_symbols
    assert "AMZN" in res_symbols
    assert res.get("generated_by") == "ai"
    assert sum(it["weight_pct"] for it in res["items"]) == 100.0


def test_portfolio_update_boolean_avg_fx_rate_placeholder():
    """Verify api_update_portfolio ignores boolean avg_fx_rate stored on stock when updating placeholder cache."""
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app_state.market.user_stocks_lock:
        orig_us = dict(app_state.market.user_us)
        app_state.market.user_us["NVDA"] = {
            "name": "NVIDIA",
            "shares": 5,
            "avg_price": 100.0,
            "avg_fx_rate": True,
        }
    try:
        with app.test_client() as client:
            with patch("routes.stocks.portfolio.require_trusted_or_admin", return_value=(True, None)):
                with patch("routes.stocks.portfolio.save_user_stocks", return_value=True):
                    with patch("routes.stocks.portfolio.ensure_stock_placeholder_in_caches") as mock_ensure:
                        resp = client.post(
                            "/api/stocks/portfolio",
                            json={
                                "symbol": "NVDA",
                                "market": "us",
                                "shares": 10,
                                "avg_price": 120.0,
                            },
                        )
                        assert resp.status_code == 200
                        mock_ensure.assert_called_once()
                        _, kwargs = mock_ensure.call_args
                        assert kwargs.get("avg_fx_rate") is None
    finally:
        app.config["TESTING"] = orig_testing
        app.config["WTF_CSRF_ENABLED"] = orig_csrf
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = orig_us


def test_resolve_stocks_for_response_boolean_portfolio_safeguards():
    """Verify resolve_stocks_for_response safely handles booleans in portfolio data."""
    with app_state.market.user_stocks_lock:
        orig_us = dict(app_state.market.user_us)
        app_state.market.user_us["AAPL"] = {
            "name": "Apple",
            "shares": True,
            "avg_price": True,
            "avg_fx_rate": True,
            "volume": True,
        }

    with app_state.cache.sse_data_lock:
        orig_target = dict(app_state.market.target_stocks_cache) if isinstance(app_state.market.target_stocks_cache, dict) else {}
        orig_curr = dict(app_state.market.current_stocks_cache) if isinstance(app_state.market.current_stocks_cache, dict) else {}
        app_state.market.current_stocks_cache = {
            "us": [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "price": 200.0,
                    "change": 2.0,
                    "change_percent": 1.0,
                    "market": "us",
                    "volume": True,
                    "shares": True,
                    "avg_price": True,
                    "avg_fx_rate": True,
                }
            ],
            "jp": [],
            "idx": [],
        }

    try:
        with patch("services.realtime_engine.realtime_market_engine") as mock_engine:
            mock_engine.get_market_snapshot.return_value = {
                "AAPL": {"price": 200.0, "change": 2.0, "change_percent": 1.0, "source": "tradingview", "volume": 1000}
            }
            mock_engine.get_pts_snapshot.return_value = {}
            with patch("utils.stock_payload.get_stock_volume", return_value=50000):
                res = resolve_stocks_for_response(include_portfolio=True)

        assert "us" in res
        assert len(res["us"]) == 1
        aapl_row = res["us"][0]
        assert aapl_row["symbol"] == "AAPL"
        assert aapl_row["price"] == 200.0
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = orig_us
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache = orig_target
            app_state.market.current_stocks_cache = orig_curr
