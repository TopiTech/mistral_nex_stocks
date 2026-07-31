# services/fallback_provider.py
"""Fallback Stock Data Providers for Mistral NeX Stocks.

Provides HTML scraping (Yahoo Finance) and official API (Alpha Vantage) alternatives
to use when yfinance fails (e.g. rate limit, 404, or format changes).
"""

import json
import logging
import re
from typing import Any

from config_utils import get_alphavantage_api_key

BeautifulSoup: Any
try:
    from bs4 import BeautifulSoup as _BS  # type: ignore[import-untyped]
    BeautifulSoup = _BS
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)

class BaseFallbackProvider:
    """Base class for fallback providers."""
    def get_latest_quote(self, symbol: str) -> dict | None:
        """Fetch the latest price and basic data for a given symbol."""
        raise NotImplementedError


class AlphaVantageProvider(BaseFallbackProvider):
    """Provides fallback data using the Alpha Vantage API.
    
    Requires an API key configured by the user.
    """
    def __init__(self):
        self._base_url = "https://www.alphavantage.co/query"

    def get_latest_quote(self, symbol: str) -> dict | None:
        api_key = get_alphavantage_api_key()
        if not api_key:
            return None

        # Determine Alpha Vantage compatible symbol format
        av_symbol = symbol
        if av_symbol.endswith(".T"):
            av_symbol = av_symbol.replace(".T", ".TRK")

        import requests
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": av_symbol,
            "apikey": api_key
        }
        try:
            resp = requests.get(self._base_url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            quote = data.get("Global Quote", {})
            if not quote or "05. price" not in quote:
                return None
            
            return {
                "symbol": symbol,
                "regularMarketPrice": float(quote["05. price"]),
                "regularMarketPreviousClose": float(quote.get("08. previous close", quote["05. price"])),
                "regularMarketVolume": int(quote.get("06. volume", 0)),
                "regularMarketOpen": float(quote.get("02. open", quote["05. price"])),
                "regularMarketDayHigh": float(quote.get("03. high", quote["05. price"])),
                "regularMarketDayLow": float(quote.get("04. low", quote["05. price"])),
            }
        except Exception as exc:
            logger.debug("AlphaVantage fallback failed for %s: %s", symbol, exc)
            return None


class YahooWebScraperProvider(BaseFallbackProvider):
    """Lightweight web scraper for Yahoo Finance using curl_cffi."""
    def __init__(self):
        self.requests: Any = None
        try:
            from curl_cffi import requests as cffi_requests
            self.requests = cffi_requests
        except ImportError:
            self.requests = None

    def get_latest_quote(self, symbol: str) -> dict | None:
        if not self.requests:
            return None
            
        url = f"https://finance.yahoo.com/quote/{symbol}/"
        try:
            resp = self.requests.get(url, impersonate="chrome110", timeout=10.0)
            if resp.status_code != 200:
                logger.debug("Yahoo HTML scraper returned status %d for %s", resp.status_code, symbol)
                return None
                
            match = re.search(r"root\.App\.main\s*=\s*(\{.*?\});\s*\(function", resp.text, re.DOTALL)
            if not match:
                logger.debug("Yahoo HTML scraper failed to find JSON state for %s", symbol)
                return None
                
            data = json.loads(match.group(1))
            stores = data.get("context", {}).get("dispatcher", {}).get("stores", {})
            quote_summary = stores.get("QuoteSummaryStore", {})
            price_data = quote_summary.get("price", {})
            
            if not price_data:
                return None
                
            def _extract_fmt(field):
                val = price_data.get(field, {})
                if isinstance(val, dict):
                    return val.get("raw")
                return val

            price = _extract_fmt("regularMarketPrice")
            if price is None:
                return None
                
            return {
                "symbol": symbol,
                "regularMarketPrice": float(price),
                "regularMarketPreviousClose": float(_extract_fmt("regularMarketPreviousClose") or price),
                "regularMarketVolume": int(_extract_fmt("regularMarketVolume") or 0),
                "regularMarketOpen": float(_extract_fmt("regularMarketOpen") or price),
                "regularMarketDayHigh": float(_extract_fmt("regularMarketDayHigh") or price),
                "regularMarketDayLow": float(_extract_fmt("regularMarketDayLow") or price),
            }
        except Exception as exc:
            logger.debug("Yahoo HTML scraper failed for %s: %s", symbol, exc)
            return None


class YahooJPScraperProvider(BaseFallbackProvider):
    """Scrapes Japanese stock prices from finance.yahoo.co.jp."""
    def __init__(self):
        self.requests: Any = None
        try:
            from curl_cffi import requests as cffi_requests
            self.requests = cffi_requests
        except ImportError:
            self.requests = None

    def get_latest_quote(self, symbol: str) -> dict | None:
        if not self.requests or BeautifulSoup is None:
            return None
            
        base_symbol = symbol.split(".")[0]
        url = f"https://finance.yahoo.co.jp/quote/{base_symbol}.T"
        
        try:
            resp = self.requests.get(url, impersonate="chrome110", timeout=10.0)
            if resp.status_code != 200:
                logger.debug("Yahoo JP HTML scraper returned status %d for %s", resp.status_code, symbol)
                return None
                
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Find the current price
            price_span = soup.find('span', class_='_3rXWJKZF')
            if not price_span:
                return None
                
            price_text = price_span.text.replace(',', '')
            price = float(price_text)
            
            return {
                "symbol": symbol,
                "regularMarketPrice": price,
                "regularMarketPreviousClose": price,
                "regularMarketVolume": 0,
                "regularMarketOpen": price,
                "regularMarketDayHigh": price,
                "regularMarketDayLow": price,
            }
        except Exception as exc:
            logger.debug("Yahoo JP HTML scraper failed for %s: %s", symbol, exc)
            return None


class CompositeFallbackProvider:
    """Manages the fallback strategy."""
    def __init__(self):
        self.alpha_vantage = AlphaVantageProvider()
        self.yahoo_web = YahooWebScraperProvider()
        self.yahoo_jp = YahooJPScraperProvider()
        
    def get_latest_quote(self, symbol: str) -> dict | None:
        """Returns the latest quote using the best available fallback."""
        quote = self.alpha_vantage.get_latest_quote(symbol)
        if quote:
            logger.info("Fallback successful using AlphaVantage for %s", symbol)
            return quote
            
        if symbol.endswith(".T"):
            quote = self.yahoo_jp.get_latest_quote(symbol)
            if quote:
                logger.info("Fallback successful using Yahoo JP Scraper for %s", symbol)
                return quote
        else:
            quote = self.yahoo_web.get_latest_quote(symbol)
            if quote:
                logger.info("Fallback successful using Yahoo US Scraper for %s", symbol)
                return quote
                
        return None
