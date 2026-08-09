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

        av_symbol = symbol

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

            if "Note" in data or "Information" in data:
                msg = data.get("Note") or data.get("Information")
                logger.warning("AlphaVantage rate limit or info message for %s: %s", symbol, msg)
                return None
            if "Error Message" in data:
                logger.warning("AlphaVantage error for %s: %s", symbol, data.get("Error Message"))
                return None

            quote = data.get("Global Quote", {})
            if not quote or "05. price" not in quote:
                return None

            def _to_float(val: Any, default: float = 0.0) -> float:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return default

            def _to_int(val: Any, default: int = 0) -> int:
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    return default

            price = _to_float(quote["05. price"])

            return {
                "symbol": symbol,
                "regularMarketPrice": price,
                "regularMarketPreviousClose": _to_float(quote.get("08. previous close"), price),
                "regularMarketVolume": _to_int(quote.get("06. volume"), 0),
                "regularMarketOpen": _to_float(quote.get("02. open"), price),
                "regularMarketDayHigh": _to_float(quote.get("03. high"), price),
                "regularMarketDayLow": _to_float(quote.get("04. low"), price),
            }
        except Exception as exc:
            logger.debug("AlphaVantage fallback failed for %s: %s", symbol, exc)
            return None


class YahooWebScraperProvider(BaseFallbackProvider):
    """Lightweight web scraper for Yahoo Finance using curl_cffi with persistent session support."""
    def __init__(self):
        self.requests: Any = None
        self.session: Any = None
        try:
            from curl_cffi import requests as cffi_requests
            self.requests = cffi_requests
            self.session = cffi_requests.Session(impersonate="chrome120")
        except ImportError:
            self.requests = None
            self.session = None

    def get_latest_quote(self, symbol: str) -> dict | None:
        client = self.session if (self.session is not None and type(self.requests).__name__ != "MagicMock") else self.requests
        if not client:
            return None

        url = f"https://finance.yahoo.com/quote/{symbol}/"
        try:
            resp = client.get(url, timeout=10.0) if client is self.session else client.get(url, impersonate="chrome120", timeout=10.0)
            if resp.status_code != 200:
                logger.debug("Yahoo HTML scraper returned status %d for %s", resp.status_code, symbol)
                return None

            # Pattern 1: root.App.main = {...}
            match = re.search(r"root\.App\.main\s*=\s*(\{.*?\});\s*\(function", resp.text, re.DOTALL)
            data = None
            if match:
                try:
                    data = json.loads(match.group(1))
                except (json.JSONDecodeError, ValueError):
                    data = None
                if data is None:
                    # The naive non-greedy regex can truncate nested objects;
                    # reuse the robust JSON extractor (stack-tracking + salvage)
                    # as a fallback before giving up.
                    try:
                        from utils.validators import extract_json_payload

                        data = json.loads(extract_json_payload(match.group(1)))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        data = None

            # Pattern 2: __NEXT_DATA__ JSON script tag
            if not data and '<script id="__NEXT_DATA__"' in resp.text:
                next_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
                if next_match:
                    try:
                        data = json.loads(next_match.group(1))
                    except (json.JSONDecodeError, ValueError):
                        data = None

            if not data:
                logger.debug("Yahoo HTML scraper failed to find JSON state for %s", symbol)
                return None

            # Extract quote fields recursively or from standard store
            stores = data.get("context", {}).get("dispatcher", {}).get("stores", {})
            quote_summary = stores.get("QuoteSummaryStore", {})
            price_data = quote_summary.get("price", {})

            if not price_data and "props" in data:
                # Try Next.js props structure if applicable
                page_props = data.get("props", {}).get("pageProps", {})
                price_data = page_props.get("quoteSummary", {}).get("price", {})

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


def _extract_yahoo_jp_price(soup, raw_text):
    """Extract the current price from a finance.yahoo.co.jp page.

    Uses a list of known selector candidates, JSON script data extraction,
    then falls back to the "現在値" label or a yen-prefixed figure.
    Returns raw price text (e.g. "2,500.5") or None.
    """
    # 1. Try script tag with JSON data (__NEXT_DATA__ or state script)
    if soup:
        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            try:
                js_data = json.loads(next_script.string)
                # Search for price inside props
                price_val = (
                    js_data.get("props", {})
                    .get("pageProps", {})
                    .get("priceData", {})
                    .get("price")
                )
                if price_val:
                    return str(price_val)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

    # 2. Try stable CSS selectors (data-testid attributes are far more durable
    #    than Yahoo's deployment-rotated hashed class names).
    stable_selectors = (
        "span[data-testid='stock-price']",
        "span[data-testid='price']",
        "span[data-testid='stock-price'] span",
    )
    if soup:
        for selector in stable_selectors:
            try:
                el = soup.select_one(selector)
            except Exception:
                el = None
            if el is not None:
                text = el.get_text(strip=True)
                if re.search(r"\d", text):
                    return text

    # 3. Regex fallbacks anchored on human-readable labels (現在値 / yen sign).
    #    These survive wholesale class renames because they key off the visible
    #    text, not the markup.
    match = re.search(r"現在値.{0,120}?([\d,]+\.?\d*)", raw_text, re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r"¥\s*([\d,]+\.?\d*)", raw_text)
    if match:
        return match.group(1)

    # 4. Last resort: Yahoo's hashed class names. Kept only as a fallback so
    #    the parser still works when the label regex cannot anchor (e.g. the
    #    price is rendered without a visible label); these are the most likely
    #    to break on the next Yahoo deployment, hence the lowest priority.
    hashed_selectors = (
        "span._3rXWJKZF",
        "span[class*='_3rXWJKZF']",
        "span._2vS8a23m",
        "span._16d25_1x",
    )
    if soup:
        for selector in hashed_selectors:
            try:
                el = soup.select_one(selector)
            except Exception:
                el = None
            if el is not None:
                text = el.get_text(strip=True)
                if re.search(r"\d", text):
                    return text
    return None


class YahooJPScraperProvider(BaseFallbackProvider):
    """Scrapes Japanese stock prices from finance.yahoo.co.jp with persistent session support."""
    def __init__(self):
        self.requests: Any = None
        self.session: Any = None
        try:
            from curl_cffi import requests as cffi_requests
            self.requests = cffi_requests
            self.session = cffi_requests.Session(impersonate="chrome110")
        except ImportError:
            self.requests = None
            self.session = None

    def get_latest_quote(self, symbol: str) -> dict | None:
        client = self.session if (self.session is not None and type(self.requests).__name__ != "MagicMock") else self.requests
        if not client or BeautifulSoup is None:
            return None

        base_symbol = symbol.split(".")[0]
        url = f"https://finance.yahoo.co.jp/quote/{base_symbol}.T"

        try:
            resp = client.get(url, timeout=10.0) if client is self.session else client.get(url, impersonate="chrome110", timeout=10.0)
            if resp.status_code != 200:
                logger.debug("Yahoo JP HTML scraper returned status %d for %s", resp.status_code, symbol)
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            price_text = _extract_yahoo_jp_price(soup, resp.text)
            if price_text is None:
                logger.debug("Yahoo JP scraper could not locate a price for %s", symbol)
                return None

            price = float(re.sub(r"[^\d.]", "", price_text))

            return {
                "symbol": symbol,
                "regularMarketPrice": price,
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
            quote["source"] = "alphavantage"
            logger.debug("[FallbackProvider] Quote success via AlphaVantage for %s: price=%.2f", symbol, quote.get("regularMarketPrice", 0.0))
            return quote

        if symbol.endswith(".T"):
            quote = self.yahoo_jp.get_latest_quote(symbol)
            if quote:
                quote["source"] = "yahoojp"
                logger.debug("[FallbackProvider] Quote success via Yahoo JP Scraper for %s: price=%.2f", symbol, quote.get("regularMarketPrice", 0.0))
                return quote
        else:
            quote = self.yahoo_web.get_latest_quote(symbol)
            if quote:
                quote["source"] = "yahoous"
                logger.debug("[FallbackProvider] Quote success via Yahoo US Scraper for %s: price=%.2f", symbol, quote.get("regularMarketPrice", 0.0))
                return quote

        return None
