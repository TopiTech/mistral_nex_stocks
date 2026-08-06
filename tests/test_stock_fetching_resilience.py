# tests/test_stock_fetching_resilience.py
"""Tests for stock data fetching resilience and performance optimizations."""

import json
from unittest.mock import patch

from services.fallback_provider import (
    CompositeFallbackProvider,
    YahooJPScraperProvider,
    YahooWebScraperProvider,
    _extract_yahoo_jp_price,
)
from services.realtime_engine import TradingViewWSClient


def test_tradingview_parse_tv_messages_fast():
    """Verify TradingView WS fast message parser parses concatenated ~m~ messages correctly."""
    msg1 = json.dumps({"m": "qsd", "p": ["session", {"n": "NASDAQ:AAPL", "v": {"lp": 180.5, "ch": 1.2, "chp": 0.67, "volume": 10000}}]})
    msg2 = json.dumps({"m": "ping", "p": [12345]})

    raw_stream = f"~m~{len(msg1)}~m~{msg1}~m~{len(msg2)}~m~{msg2}"

    parsed = TradingViewWSClient.parse_tv_messages(raw_stream)
    assert len(parsed) == 2
    assert parsed[0]["m"] == "qsd"
    assert parsed[0]["p"][1]["n"] == "NASDAQ:AAPL"
    assert parsed[1]["m"] == "ping"


def test_tradingview_parse_tv_messages_invalid():
    """Verify parser gracefully handles malformed frames."""
    raw_stream = "~m~invalid~m~{}"
    parsed = TradingViewWSClient.parse_tv_messages(raw_stream)
    assert parsed == []


def test_yahoo_jp_price_extraction_next_data():
    """Verify _extract_yahoo_jp_price parses __NEXT_DATA__ script content."""
    from bs4 import BeautifulSoup

    html = """
    <html>
      <head>
        <script id="__NEXT_DATA__" type="application/json">
          {"props": {"pageProps": {"priceData": {"price": 3250.0}}}}
        </script>
      </head>
      <body></body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    price = _extract_yahoo_jp_price(soup, html)
    assert price == "3250.0"


def test_yahoo_jp_price_extraction_selector_fallback():
    """Verify _extract_yahoo_jp_price falls back to CSS selectors when __NEXT_DATA__ is missing."""
    from bs4 import BeautifulSoup

    html = '<html><body><span data-testid="stock-price">1,450.5</span></body></html>'
    soup = BeautifulSoup(html, "html.parser")
    price = _extract_yahoo_jp_price(soup, html)
    assert price == "1,450.5"


def test_fallback_providers_use_session():
    """Verify Yahoo scrapers support persistent HTTP sessions."""
    y_us = YahooWebScraperProvider()
    assert hasattr(y_us, "session")

    y_jp = YahooJPScraperProvider()
    assert hasattr(y_jp, "session")


def test_composite_fallback_provider():
    """Test CompositeFallbackProvider routing."""
    composite = CompositeFallbackProvider()

    with patch.object(composite.alpha_vantage, "get_latest_quote", return_value=None), \
         patch.object(composite.yahoo_jp, "get_latest_quote", return_value={"regularMarketPrice": 2500.0}) as mock_jp:
        res = composite.get_latest_quote("7203.T")
        assert res is not None
        assert res["source"] == "yahoojp"
        assert res["regularMarketPrice"] == 2500.0
        mock_jp.assert_called_once_with("7203.T")
