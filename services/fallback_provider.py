# services/fallback_provider.py
"""Fallback Stock Data Providers for Mistral NeX Stocks.

Provides HTML scraping (Yahoo Finance) and official API (Alpha Vantage) alternatives
to use when yfinance fails (e.g. rate limit, 404, or format changes).
"""

import json
import logging
import math
import re
import threading
import time
import weakref
from typing import Any, ClassVar

from credential_manager import get_alphavantage_api_key
from utils.http_utils import parse_retry_after
from utils.normalization import _finite_number, is_bool_like

BeautifulSoup: Any
try:
    from bs4 import BeautifulSoup as _BS  # type: ignore[import-untyped]

    BeautifulSoup = _BS
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)


def _mark_yahoo_block(
    status_code: int | None,
    text_snippet: str = "",
    *,
    is_yahoo_host: bool,
    response: Any = None,
) -> None:
    """Propagate a 401/402/403/429/439 to the global block state.

    Yahoo-hosted fetches (finance.yahoo.com / .co.jp) share IP-level
    throttling with yfinance, so they escalate to both ``scraper_block``
    and ``yfinance`` pacing (``propagate_to_yfinance=True``). Third-party
    origins (AlphaVantage, Minkabu) only touch scraper_block.
    ``text_snippet`` is inspected for rate-limit hint strings as a
    defense-in-depth when status_code is missing or 200.

    When the failing HTTP response is available it is passed via ``response``
    so a server-supplied ``Retry-After`` header is honored as the backoff
    floor by ``mark_scraper_blocked`` / ``mark_yf_429``.
    """
    hint = (text_snippet or "").lower()
    is_block_status = status_code in (401, 402, 403, 429, 439)
    is_block_hint = any(
        kw in hint
        for kw in (
            "too many requests",
            "rate limit",
            "payment required",
            "unauthorized",
            "invalid crumb",
        )
    )
    if not is_block_status and not is_block_hint:
        return
    try:
        from app_state import app_state as _app_state

        retry_after = parse_retry_after(response) if response is not None else None
        _app_state.market.mark_scraper_blocked(
            retry_after=retry_after, propagate_to_yfinance=is_yahoo_host
        )
        if is_yahoo_host:
            _app_state.market.mark_yf_429(retry_after=retry_after)
    except Exception:  # nosec B110
        pass


def _is_scraper_blocked() -> bool:
    """Return True if global scraper rate limiting/blocking is in effect."""
    try:
        from app_state import app_state as _app_state

        market = getattr(_app_state, "market", None)
        if market is not None and hasattr(market, "is_scraper_blocked"):
            return bool(market.is_scraper_blocked())
    except Exception:
        pass
    return False


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

        from utils.normalization import is_valid_symbol

        if not is_valid_symbol(symbol):
            logger.debug("AlphaVantage rejected invalid symbol: %r", symbol)
            return None

        av_symbol = symbol

        import requests

        params = {"function": "GLOBAL_QUOTE", "symbol": av_symbol, "apikey": api_key}
        try:
            resp = requests.get(self._base_url, params=params, timeout=10.0)
            if resp.status_code != 200:
                _mark_yahoo_block(
                    resp.status_code,
                    resp.text[:500] if hasattr(resp, "text") else "",
                    is_yahoo_host=False,
                    response=resp,
                )
            resp.raise_for_status()
            data = resp.json()

            if "Note" in data or "Information" in data:
                msg = data.get("Note") or data.get("Information")
                logger.warning("AlphaVantage rate limit or info message for %s: %s", symbol, msg)
                _mark_yahoo_block(None, str(msg), is_yahoo_host=False)
                return None
            if "Error Message" in data:
                logger.warning("AlphaVantage error for %s: %s", symbol, data.get("Error Message"))
                return None

            quote = data.get("Global Quote", {})
            if not quote or "05. price" not in quote:
                return None

            def _to_float(val: Any, default: float = 0.0) -> float:
                num = _finite_number(val)
                return num if num is not None else default

            def _to_int(val: Any, default: int = 0) -> int:
                try:
                    if is_bool_like(val):
                        return default
                    num = float(val)
                except (ValueError, TypeError, OverflowError):
                    # OverflowError: float(10**400)-style JSON integers escape
                    # the numeric-coercion boundary and previously aborted the
                    # whole quote via the outer handler (M21, 2026-11-07).
                    return default
                return int(num) if math.isfinite(num) else default

            price = _to_float(quote["05. price"])

            # M22 (2026-11-07 review): never fake unknown OHLC as the current
            # price — a fabricated previous close forces change=0.00% and
            # misrepresents stale fallback data ("flat market"). JP-tier
            # providers already return None for exactly this reason; the
            # downstream payload and services tolerate None (no change
            # rendered until real data arrives).
            prev_close = _finite_number(quote.get("08. previous close"))
            open_num = _finite_number(quote.get("02. open"))
            high_num = _finite_number(quote.get("03. high"))
            low_num = _finite_number(quote.get("04. low"))

            return {
                "symbol": symbol,
                "regularMarketPrice": price,
                "regularMarketPreviousClose": prev_close,
                "regularMarketVolume": _to_int(quote.get("06. volume"), 0),
                "regularMarketOpen": open_num,
                "regularMarketDayHigh": high_num,
                "regularMarketDayLow": low_num,
            }
        except Exception as exc:
            # requests exception text embeds the full request URL, which
            # contains apikey=<secret>; redact it before logging (the repo-wide
            # convention is to never log credential material).
            safe_exc = re.sub(
                r"(apikey=)[^&\s'\"]+", r"\1[REDACTED]", str(exc), flags=re.IGNORECASE
            )
            logger.debug("AlphaVantage fallback failed for %s: %s", symbol, safe_exc)
            return None


class YahooWebScraperProvider(BaseFallbackProvider):
    """Lightweight web scraper for Yahoo Finance using curl_cffi with persistent thread-local session support."""

    def __init__(self):
        self.requests: Any = None
        self.session: Any = None
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._all_sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        try:
            from curl_cffi import requests as cffi_requests

            self.requests = cffi_requests
        except ImportError:
            self.requests = None

    def close(self) -> None:
        """Close all allocated HTTP sessions."""
        with self._sessions_lock:
            sessions = list(self._all_sessions)
            self._all_sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass
        self._local.session = None

    @staticmethod
    def _parse_live_price_marker(resp_text: str, symbol: str) -> dict | None:
        """Parse a live price from direct HTML markers (data-field / data-testid).

        Returns a quote dict with ``regularMarketPrice`` only; the previous
        close is unknown from these markers, so it is set to None rather than
        faked as the current price (which would force change=0 and corrupt
        realtime change calculations downstream).
        """
        m_price = re.search(
            r'data-field=["\']regularMarketPrice["\'][^>]*value=["\']([^"\']+)["\']',
            resp_text,
        )
        if not m_price:
            m_price = re.search(r'data-testid=["\']qsp-price["\'][^>]*>([^<]+)<', resp_text)
        if not m_price:
            m_price = re.search(
                r'class=["\']livePrice[^"\']*["\'][^>]*><span>([^<]+)</span>',
                resp_text,
            )
        if not m_price:
            return None
        parsed = _finite_number(m_price.group(1).replace(",", "").strip())
        if parsed is None or parsed <= 0:
            return None
        p_val = parsed
        return {
            "symbol": symbol,
            "regularMarketPrice": p_val,
            "regularMarketPreviousClose": None,
            "regularMarketVolume": 0,
            "regularMarketOpen": p_val,
            "regularMarketDayHigh": p_val,
            "regularMarketDayLow": p_val,
        }

    def _get_client(self) -> tuple[Any, bool]:
        # An explicitly injected session (tests / custom setups) always wins.
        if self.session is not None:
            return self.session, True
        if not self.requests:
            return None, False
        if not hasattr(self._local, "session") or self._local.session is None:
            for imp in (
                "chrome120",
                "chrome124",
                "chrome131",
                "chrome133a",
                "safari18_0",
                "edge101",
            ):
                try:
                    sess = self.requests.Session(impersonate=imp)
                    self._local.session = sess
                    with self._sessions_lock:
                        self._all_sessions.add(sess)
                    break
                except Exception:
                    self._local.session = None
        if self._local.session is not None:
            return self._local.session, True
        return self.requests, False

    def get_latest_quote(self, symbol: str) -> dict | None:
        if _is_scraper_blocked():
            return None
        from utils.normalization import is_valid_symbol

        if not is_valid_symbol(symbol):
            logger.debug("Yahoo HTML scraper rejected invalid symbol: %r", symbol)
            return None
        client, is_session = self._get_client()
        if not client:
            return None

        url = f"https://finance.yahoo.com/quote/{symbol}/"
        try:
            resp = (
                client.get(url, timeout=10.0)
                if is_session
                else client.get(url, impersonate="chrome120", timeout=10.0)
            )
            if resp.status_code != 200:
                _mark_yahoo_block(
                    resp.status_code,
                    getattr(resp, "text", "")[:500],
                    is_yahoo_host=True,
                    response=resp,
                )
                logger.debug(
                    "Yahoo HTML scraper returned status %d for %s", resp.status_code, symbol
                )
                return None

            # Pattern 1: root.App.main = {...}
            match = re.search(
                r"root\.App\.main\s*=\s*(\{.*?\});\s*\(function", resp.text, re.DOTALL
            )
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
                next_match = re.search(
                    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL
                )
                if next_match:
                    try:
                        data = json.loads(next_match.group(1))
                    except (json.JSONDecodeError, ValueError):
                        data = None

            if not data:
                # Fallback: parse direct live price markers from HTML attributes /
                # testids when the JSON state is unavailable (markup drift).
                marker_quote = self._parse_live_price_marker(resp.text, symbol)
                if marker_quote is not None:
                    return marker_quote
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
                # JSON state exists but carries no quote fields (markup drift):
                # fall back to direct live price markers before giving up.
                marker_quote = self._parse_live_price_marker(resp.text, symbol)
                if marker_quote is not None:
                    return marker_quote
                return None

            def _extract_fmt(field):
                val = price_data.get(field, {})
                if isinstance(val, dict):
                    return val.get("raw")
                return val

            price = _extract_fmt("regularMarketPrice")
            if price is None or _finite_number(price) is None:
                return None

            def _quote_float(field: str, default: float | None) -> float | None:
                raw = _extract_fmt(field)
                num = _finite_number(raw) if raw is not None else None
                return num if num is not None else default

            def _quote_int(field: str, default: int = 0) -> int:
                raw = _extract_fmt(field)
                if raw is None or is_bool_like(raw):
                    return default
                try:
                    num = float(raw)
                except (ValueError, TypeError, OverflowError):
                    # M21: extreme JSON integers must degrade, not abort.
                    return default
                if not math.isfinite(num) or num < 0:
                    return default
                return int(num)

            price_finite = _finite_number(price)
            if price_finite is None:
                return None
            price_val = price_finite
            # M22: see the AlphaVantage branch — unknown OHLC is None, not a
            # copy of the current price.
            return {
                "symbol": symbol,
                "regularMarketPrice": price_val,
                "regularMarketPreviousClose": _quote_float("regularMarketPreviousClose", None),
                "regularMarketVolume": _quote_int("regularMarketVolume", 0),
                "regularMarketOpen": _quote_float("regularMarketOpen", None),
                "regularMarketDayHigh": _quote_float("regularMarketDayHigh", None),
                "regularMarketDayLow": _quote_float("regularMarketDayLow", None),
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
                    js_data.get("props", {}).get("pageProps", {}).get("priceData", {}).get("price")
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
    """Scrapes Japanese stock prices from finance.yahoo.co.jp with persistent thread-local session support."""

    def __init__(self):
        self.requests: Any = None
        self.session: Any = None
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._all_sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        try:
            from curl_cffi import requests as cffi_requests

            self.requests = cffi_requests
        except ImportError:
            self.requests = None

    def close(self) -> None:
        """Close all allocated HTTP sessions."""
        with self._sessions_lock:
            sessions = list(self._all_sessions)
            self._all_sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass
        self._local.session = None

    def _get_client(self) -> tuple[Any, bool]:
        # An explicitly injected session (tests / custom setups) always wins.
        if self.session is not None:
            return self.session, True
        if not self.requests:
            return None, False
        if not hasattr(self._local, "session") or self._local.session is None:
            for imp in (
                "chrome120",
                "chrome124",
                "chrome131",
                "chrome133a",
                "safari18_0",
                "edge101",
            ):
                try:
                    sess = self.requests.Session(impersonate=imp)
                    self._local.session = sess
                    with self._sessions_lock:
                        self._all_sessions.add(sess)
                    break
                except Exception:
                    self._local.session = None
        if self._local.session is not None:
            return self._local.session, True
        return self.requests, False

    def get_latest_quote(self, symbol: str) -> dict | None:
        if _is_scraper_blocked():
            return None
        from utils.normalization import is_valid_symbol

        if not is_valid_symbol(symbol):
            logger.debug("Yahoo JP scraper rejected invalid symbol: %r", symbol)
            return None
        client, is_session = self._get_client()
        if not client or BeautifulSoup is None:
            return None

        base_symbol = symbol.split(".")[0]
        url = f"https://finance.yahoo.co.jp/quote/{base_symbol}.T"

        try:
            resp = (
                client.get(url, timeout=10.0)
                if is_session
                else client.get(url, impersonate="chrome120", timeout=10.0)
            )
            if resp.status_code != 200:
                _mark_yahoo_block(
                    resp.status_code,
                    getattr(resp, "text", "")[:500],
                    is_yahoo_host=True,
                    response=resp,
                )
                logger.debug(
                    "Yahoo JP HTML scraper returned status %d for %s", resp.status_code, symbol
                )
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            price_text = _extract_yahoo_jp_price(soup, resp.text)
            if price_text is None:
                logger.debug("Yahoo JP scraper could not locate a price for %s", symbol)
                return None

            price = float(re.sub(r"[^\d.]", "", price_text))

            # Previous close is unknown from this page; report None instead of
            # faking it as the current price (faking would force change=0 and
            # misrepresent stale fallback data as a flat market), matching the
            # _parse_live_price_marker policy in YahooWebScraperProvider.
            return {
                "symbol": symbol,
                "regularMarketPrice": price,
                "regularMarketPreviousClose": None,
                "regularMarketVolume": 0,
                "regularMarketOpen": None,
                "regularMarketDayHigh": None,
                "regularMarketDayLow": None,
            }
        except Exception as exc:
            logger.debug("Yahoo JP HTML scraper failed for %s: %s", symbol, exc)
            return None


class Nikkei225JPProvider(BaseFallbackProvider):
    """Scrapes Japanese stock prices, ADR quotes, and indices from nikkei225jp.com with persistent session support."""

    ADR_ALL_URL = "https://nikkei225jp.com/_data/_nfsDATA/adr/_adr_all.js"
    ADR_URL = "https://nikkei225jp.com/adr/adr.php"
    INDEX_MID_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_TOP_mid.js"
    INDEX_BTM_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_TOP_btm.js"
    INDEX_NDY_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_NDY_min.js"

    INDEX_MAP: ClassVar[dict[str, int]] = {
        "^N225": 111,
        "N225": 111,
        "^DJI": 211,
        "DJI": 211,
        "^IXIC": 212,
        "NASDAQ": 212,
        "^GSPC": 213,
        "SP500": 213,
        "^NDX": 214,
        "NASDAQ100": 214,
        "USDJPY=X": 511,
        "USDJPY": 511,
        "JPY=X": 511,
        "EURJPY=X": 514,
        "EURJPY": 514,
        "EURUSD=X": 523,
        "EURUSD": 523,
        "^VIX": 621,
        "VIX": 621,
        "BTC-USD": 1001,
        "BTC": 1001,
    }

    def __init__(self):
        self.requests: Any = None
        self.session: Any = None
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._all_sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        self._adr_cache: dict[str, list[str]] = {}
        self._adr_cache_time: float = 0.0
        self._index_cache: dict[int, list[str]] = {}
        self._index_cache_time: float = 0.0
        self._cache_lock = threading.Lock()
        try:
            from curl_cffi import requests as cffi_requests

            self.requests = cffi_requests
        except ImportError:
            self.requests = None

    def close(self) -> None:
        """Close all allocated HTTP sessions."""
        with self._sessions_lock:
            sessions = list(self._all_sessions)
            self._all_sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass
        self._local.session = None

    def _get_client(self) -> tuple[Any, bool]:
        if self.session is not None:
            return self.session, True
        if not self.requests:
            return None, False
        if not hasattr(self._local, "session") or self._local.session is None:
            for imp in (
                "chrome120",
                "chrome124",
                "chrome131",
                "chrome133a",
                "safari18_0",
                "edge101",
            ):
                try:
                    sess = self.requests.Session(impersonate=imp)
                    self._local.session = sess
                    with self._sessions_lock:
                        self._all_sessions.add(sess)
                    break
                except Exception:
                    self._local.session = None
        if self._local.session is not None:
            return self._local.session, True
        return self.requests, False

    def _refresh_adr_cache(
        self, client: Any, is_session: bool, max_age: float = 10.0
    ) -> dict[str, list[str]]:
        now = time.time()
        with self._cache_lock:
            if self._adr_cache and (now - self._adr_cache_time) < max_age:
                return self._adr_cache

        try:
            resp = (
                client.get(self.ADR_ALL_URL, timeout=6.0)
                if is_session
                else client.get(self.ADR_ALL_URL, impersonate="chrome120", timeout=6.0)
            )
            if resp.status_code == 200:
                text = (
                    resp.text
                    if hasattr(resp, "text")
                    else resp.content.decode("utf-8", errors="replace")
                )
                cache: dict[str, list[str]] = {}
                for line in text.splitlines():
                    if line.startswith("A0["):
                        m = re.search(r'A0\[\w+\]="([^"]+)"', line)
                        if m:
                            parts = m.group(1).split("_")
                            if len(parts) >= 21:
                                cache[parts[0]] = parts
                if cache:
                    with self._cache_lock:
                        self._adr_cache = cache
                        self._adr_cache_time = now
        except Exception as exc:
            logger.debug("Nikkei225JPProvider failed fetching _adr_all.js: %s", exc)

        with self._cache_lock:
            return self._adr_cache

    def _refresh_index_cache(
        self, client: Any, is_session: bool, max_age: float = 10.0
    ) -> dict[int, list[str]]:
        now = time.time()
        with self._cache_lock:
            if self._index_cache and (now - self._index_cache_time) < max_age:
                return self._index_cache

        cache: dict[int, list[str]] = {}
        for url in (self.INDEX_MID_URL, self.INDEX_BTM_URL):
            try:
                resp = (
                    client.get(url, timeout=6.0)
                    if is_session
                    else client.get(url, impersonate="chrome120", timeout=6.0)
                )
                if resp.status_code == 200:
                    text = (
                        resp.text
                        if hasattr(resp, "text")
                        else resp.content.decode("utf-8", errors="replace")
                    )
                    for line in text.splitlines():
                        m = re.search(r'A\[(\d+)\]="([^"]+)"', line)
                        if m:
                            code = int(m.group(1))
                            parts = m.group(2).split("_")
                            if len(parts) >= 3:
                                cache[code] = parts
            except Exception as exc:
                logger.debug("Nikkei225JPProvider failed fetching index %s: %s", url, exc)

        try:
            resp = (
                client.get(self.INDEX_NDY_URL, timeout=6.0)
                if is_session
                else client.get(self.INDEX_NDY_URL, impersonate="chrome120", timeout=6.0)
            )
            if resp.status_code == 200:
                text = (
                    resp.text
                    if hasattr(resp, "text")
                    else resp.content.decode("utf-8", errors="replace")
                )
                for m in re.finditer(r"var NDY(\d+)V=([\d.]+),NDY\1Z=([+-]?[\d.]+);", text):
                    code = int(m.group(1))
                    if code not in cache:
                        cache[code] = [m.group(2), m.group(3), "0", "", ""]
        except Exception as exc:
            logger.debug("Nikkei225JPProvider failed fetching NDY min: %s", exc)

        if cache:
            with self._cache_lock:
                self._index_cache = cache
                self._index_cache_time = now

        with self._cache_lock:
            return self._index_cache

    def get_latest_quote(self, symbol: str) -> dict | None:
        if _is_scraper_blocked():
            return None
        from utils.normalization import is_valid_symbol

        if not is_valid_symbol(symbol):
            logger.debug("Nikkei225JP rejected invalid symbol: %r", symbol)
            return None
        client, is_session = self._get_client()
        if not client:
            return None

        def _to_float(val: Any, default: float = 0.0) -> float:
            try:
                s = str(val).replace(",", "").replace("+", "").strip()
                num = float(s)
                return num if math.isfinite(num) else default
            except (ValueError, TypeError):
                return default

        # 1. Index / Forex mapping
        if symbol.startswith("^") or "=" in symbol or symbol in self.INDEX_MAP:
            code = self.INDEX_MAP.get(symbol)
            if not code:
                return None
            idx_cache = self._refresh_index_cache(client, is_session)
            parts = idx_cache.get(code)
            if not parts or len(parts) < 2:
                return None
            price = _to_float(parts[0])
            if price <= 0:
                return None
            change = _to_float(parts[1]) if len(parts) > 1 else 0.0
            # OHLC are unknown from this feed; report None instead of faking
            # them as the current price (same policy as YahooJPScraperProvider
            # above — faked O=H=L would misrepresent the fallback quote as a
            # real intraday range). Downstream history synthesis substitutes
            # the close for a well-formed flat candle.
            return {
                "symbol": symbol,
                "regularMarketPrice": price,
                "regularMarketPreviousClose": price - change,
                "regularMarketVolume": 0,
                "regularMarketOpen": None,
                "regularMarketDayHigh": None,
                "regularMarketDayLow": None,
                "source": "nikkei225jp",
            }

        # 2. JP Stock ADR mapping
        clean_code = symbol.split(".")[0].strip()
        adr_cache = self._refresh_adr_cache(client, is_session)
        parts = adr_cache.get(clean_code)

        if not parts:
            try:
                url = f"{self.ADR_URL}?a={clean_code}"
                resp = (
                    client.get(url, timeout=6.0)
                    if is_session
                    else client.get(url, impersonate="chrome120", timeout=6.0)
                )
                if resp.status_code == 200 and f'var Sno="{clean_code}"' in resp.text:
                    m_parts = re.search(r'var\s+A0\s*=\s*"([^"]+)"', resp.text)
                    if m_parts:
                        parts = m_parts.group(1).split("_")
                    else:
                        parts = self._refresh_adr_cache(client, is_session, max_age=0.0).get(
                            clean_code
                        )
            except Exception as exc:
                logger.debug("Nikkei225JPProvider direct fetch failed for %s: %s", symbol, exc)

        if not parts or len(parts) < 11:
            return None

        price = _to_float(parts[8])
        if price <= 0:
            return None

        change = _to_float(parts[9])
        # OHLC unknown — None per the provider policy (see the index branch).
        return {
            "symbol": symbol,
            "regularMarketPrice": price,
            "regularMarketPreviousClose": price - change,
            "regularMarketVolume": 0,
            "regularMarketOpen": None,
            "regularMarketDayHigh": None,
            "regularMarketDayLow": None,
            "source": "nikkei225jp_adr",
        }


class MinkabuProvider(BaseFallbackProvider):
    """Fallback provider for JP stocks using minkabu.jp (lowest tier)."""

    def __init__(self):
        self.requests: Any = None
        self.session: Any = None
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._all_sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        try:
            from curl_cffi import requests as cffi_requests

            self.requests = cffi_requests
        except ImportError:
            self.requests = None

    def close(self) -> None:
        """Close all allocated HTTP sessions."""
        with self._sessions_lock:
            sessions = list(self._all_sessions)
            self._all_sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass
        self._local.session = None

    def _get_client(self) -> tuple[Any, bool]:
        if self.session is not None:
            return self.session, True
        if not self.requests:
            return None, False
        if not hasattr(self._local, "session") or self._local.session is None:
            for imp in (
                "chrome120",
                "chrome124",
                "chrome131",
                "chrome133a",
                "safari18_0",
                "edge101",
            ):
                try:
                    sess = self.requests.Session(impersonate=imp)
                    self._local.session = sess
                    with self._sessions_lock:
                        self._all_sessions.add(sess)
                    break
                except Exception:
                    self._local.session = None
        if self._local.session is not None:
            return self._local.session, True
        return self.requests, False

    def get_latest_quote(self, symbol: str) -> dict | None:
        if _is_scraper_blocked():
            return None
        from utils.normalization import is_valid_symbol

        if not is_valid_symbol(symbol):
            logger.debug("Minkabu rejected invalid symbol: %r", symbol)
            return None
        client, is_session = self._get_client()
        if not client:
            return None
        code = symbol.split(".")[0].strip()
        url = f"https://minkabu.jp/stock/{code}"
        try:
            resp = (
                client.get(url, timeout=6.0)
                if is_session
                else client.get(url, impersonate="chrome120", timeout=6.0)
            )
            if resp.status_code == 200:
                # SVC-10: shared label-anchored extractor replaces the bare
                # "...円" fallback, which matched the first yen figure anywhere
                # on the page (e.g. 手数料290円) and returned it as the price.
                from services.realtime.utils import extract_minkabu_price

                price = extract_minkabu_price(resp.text)
                if price is not None:
                    # Previous close / OHLC are unknown from this page;
                    # report None rather than the current price so the
                    # fallback quote cannot fake a 0% change (see
                    # YahooJPScraperProvider for the same rationale).
                    return {
                        "symbol": symbol,
                        "regularMarketPrice": price,
                        "regularMarketPreviousClose": None,
                        "regularMarketVolume": 0,
                        "regularMarketOpen": None,
                        "regularMarketDayHigh": None,
                        "regularMarketDayLow": None,
                        "source": "minkabu",
                    }
            elif resp.status_code in (401, 402, 403, 429, 439):
                _mark_yahoo_block(
                    resp.status_code,
                    getattr(resp, "text", "")[:500],
                    is_yahoo_host=False,
                    response=resp,
                )
        except Exception as exc:
            logger.debug("MinkabuProvider fallback failed for %s: %s", symbol, exc)
        return None


# SVC-13: aggregate wall-clock budget for the sequential hop chain in
# CompositeFallbackProvider.get_latest_quote (see the method docstring).
_COMPOSITE_QUOTE_BUDGET_SEC = 15.0


class CompositeFallbackProvider:
    """Manages the fallback strategy."""

    def __init__(self):
        self.alpha_vantage = AlphaVantageProvider()
        self.yahoo_web = YahooWebScraperProvider()
        self.yahoo_jp = YahooJPScraperProvider()
        self.nikkei225jp = Nikkei225JPProvider()
        self.minkabu = MinkabuProvider()

    def close(self) -> None:
        """Close all sub-provider HTTP sessions."""
        for provider in (self.yahoo_web, self.yahoo_jp, self.nikkei225jp, self.minkabu):
            if hasattr(provider, "close"):
                provider.close()

    def get_latest_quote(self, symbol: str) -> dict | None:
        """Returns the latest quote using the best available fallback."""
        # SVC-13: per-hop timeouts (10s / 6s) multiply across the sequential
        # chain (worst case ~44s for a JP symbol on the caller's thread).
        # Enforce an aggregate wall-clock budget: the first hop always runs,
        # later hops are skipped once the budget is exhausted. Per-hop
        # timeouts stay unchanged so an early hop still gets its full window.
        deadline = time.monotonic() + _COMPOSITE_QUOTE_BUDGET_SEC

        def _budget_remains() -> bool:
            return time.monotonic() < deadline

        quote = self.alpha_vantage.get_latest_quote(symbol)
        if quote:
            quote.setdefault("source", "alphavantage")
            logger.debug(
                "[FallbackProvider] Quote success via AlphaVantage for %s: price=%.2f",
                symbol,
                quote.get("regularMarketPrice", 0.0),
            )
            return quote

        # For index / forex symbols, try Nikkei225JP then Yahoo Web
        if (
            symbol.startswith("^")
            or "=" in symbol
            or symbol
            in (
                "N225",
                "DJI",
                "NASDAQ",
                "SP500",
                "USDJPY",
                "EURJPY",
                "EURUSD",
                "VIX",
                "BTC-USD",
                "BTC",
            )
        ):
            if not _budget_remains():
                return None
            quote = self.nikkei225jp.get_latest_quote(symbol)
            if quote:
                quote.setdefault("source", "nikkei225jp")
                logger.debug(
                    "[FallbackProvider] Quote success via Nikkei225JP for %s: price=%.2f",
                    symbol,
                    quote.get("regularMarketPrice", 0.0),
                )
                return quote
            if not _budget_remains():
                return None
            quote = self.yahoo_web.get_latest_quote(symbol)
            if quote:
                quote.setdefault("source", "yahoous")
                logger.debug(
                    "[FallbackProvider] Quote success via Yahoo US Scraper for %s: price=%.2f",
                    symbol,
                    quote.get("regularMarketPrice", 0.0),
                )
                return quote
            return None

        from utils.normalization import is_jp_stock_code

        if symbol.endswith(".T") or is_jp_stock_code(symbol):
            # 1. Yahoo JP Scraper
            if not _budget_remains():
                return None
            quote = self.yahoo_jp.get_latest_quote(symbol)
            if quote:
                quote.setdefault("source", "yahoojp")
                logger.debug(
                    "[FallbackProvider] Quote success via Yahoo JP Scraper for %s: price=%.2f",
                    symbol,
                    quote.get("regularMarketPrice", 0.0),
                )
                return quote
            # 2. Nikkei225JP ADR Scraper
            if not _budget_remains():
                return None
            quote = self.nikkei225jp.get_latest_quote(symbol)
            if quote:
                quote.setdefault("source", "nikkei225jp_adr")
                logger.debug(
                    "[FallbackProvider] Quote success via Nikkei225JP ADR for %s: price=%.2f",
                    symbol,
                    quote.get("regularMarketPrice", 0.0),
                )
                return quote
            # 3. Minkabu (lowest tier fallback)
            if not _budget_remains():
                return None
            quote = self.minkabu.get_latest_quote(symbol)
            if quote:
                quote.setdefault("source", "minkabu")
                logger.debug(
                    "[FallbackProvider] Quote success via Minkabu for %s: price=%.2f",
                    symbol,
                    quote.get("regularMarketPrice", 0.0),
                )
                return quote
        else:
            if not _budget_remains():
                return None
            quote = self.yahoo_web.get_latest_quote(symbol)
            if quote:
                quote.setdefault("source", "yahoous")
                logger.debug(
                    "[FallbackProvider] Quote success via Yahoo US Scraper for %s: price=%.2f",
                    symbol,
                    quote.get("regularMarketPrice", 0.0),
                )
                return quote

        return None
