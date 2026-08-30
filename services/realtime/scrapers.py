# services/realtime/scrapers.py
"""Scrapers for Yahoo! Finance Japan, SBI Securities, Nikkei225JP, and Minkabu."""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, ClassVar

from services.realtime.utils import (
    _ESCAPED_QUOTE_RES,
    _PTS_TRADING_FLAG_RE,
    _PTS_TRADING_FLAG_UNESCAPED_RE,
    TickerPayload,
    _create_cffi_session,
    _extract_next_data_quotes,
    _extract_pts_fields,
    _extract_pts_price_data,
    _interruptible_sleep,
    _is_scraper_blocked,
    _is_yf_rate_limited,
    _mark_scraper_blocked_from_status,
    _parse_quote_number,
    _scraper_market_state,
)
from utils.threading import DaemonThreadPoolExecutor

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


def _get_logger() -> Any:
    import sys
    rt_mod = sys.modules.get("services.realtime_engine")
    if rt_mod is not None and "logger" in rt_mod.__dict__:
        return rt_mod.__dict__["logger"]
    return logger


class _BaseFallbackScraper:
    """Shared failure-tracking / cooldown / session plumbing for fallback scrapers."""

    STRUCTURE_CHANGE_THRESHOLD = 3
    FALLBACK_COOLDOWN_SECONDS = 60.0
    RECOVERY_COOLDOWN_SECONDS = 600.0
    _SCRAPER_LABEL = "Fallback"

    def __init__(self) -> None:
        self._thread_local = threading.local()
        self._http_lock = threading.Lock()
        self._sessions_lock = threading.Lock()
        self._all_sessions: set[Any] = set()
        self.lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self._structure_change_reported: set[str] = set()
        self._structure_change_reported_time: dict[str, float] = {}
        self._last_failure_time: dict[str, float] = {}

    def remove_symbol(self, symbol: str) -> None:
        """Purge tracking state for an unregistered symbol."""
        with self.lock:
            for k in (symbol, f"{symbol}:regular", f"{symbol}:pts"):
                self._consecutive_failures.pop(k, None)
                self._structure_change_reported.discard(k)
                self._structure_change_reported_time.pop(k, None)
                self._last_failure_time.pop(k, None)

    def _get_session(self) -> Any:
        """Return a thread-local curl_cffi/requests session."""
        session = getattr(self._thread_local, "session", None)
        with self._sessions_lock:
            if session is not None and session not in self._all_sessions:
                session = None
                self._thread_local.session = None
        if session is None:
            session = _create_cffi_session()
            self._thread_local.session = session
            with self._sessions_lock:
                self._all_sessions.add(session)
        return session

    @property
    def session(self) -> Any:
        return self._get_session()

    def close(self) -> None:
        """Close all allocated HTTP sessions."""
        with self._sessions_lock:
            sessions = list(self._all_sessions)
            self._all_sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception as exc:
                logger.debug("Failed closing fallback scraper session: %s", exc)
        self._thread_local.session = None

    def _is_in_cooldown(self, symbol: str, kind: str = "regular") -> bool:
        """True while this symbol/kind is in the fallback cooldown window."""
        with self.lock:
            ts = self._last_failure_time.get(f"{symbol}:{kind}")
            return ts is not None and (time.time() - ts) < self.FALLBACK_COOLDOWN_SECONDS

    def _record_fetch_failure(self, symbol: str, kind: str = "regular") -> None:
        key = f"{symbol}:{kind}"
        now = time.time()
        with self.lock:
            self._last_failure_time[key] = now
            last_rep = self._structure_change_reported_time.get(key, 0.0)
            if key in self._structure_change_reported and (now - last_rep) > self.RECOVERY_COOLDOWN_SECONDS:
                self._consecutive_failures.pop(key, None)
                self._structure_change_reported.discard(key)
                self._structure_change_reported_time.pop(key, None)

            count = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = count
            if count >= self.STRUCTURE_CHANGE_THRESHOLD and key not in self._structure_change_reported:
                self._structure_change_reported.add(key)
                self._structure_change_reported_time[key] = now
                logger.info(
                    "[%s Scraper] %d consecutive %s failures for %s: the page structure "
                    "may have changed or the symbol may be invalid. Fallback paused "
                    "for this symbol until a successful fetch.",
                    self._SCRAPER_LABEL,
                    count,
                    kind,
                    symbol,
                )

    def _record_fetch_success(self, symbol: str, kind: str = "regular") -> None:
        key = f"{symbol}:{kind}"
        with self.lock:
            self._consecutive_failures.pop(key, None)
            self._structure_change_reported.discard(key)
            self._structure_change_reported_time.pop(key, None)
            self._last_failure_time.pop(key, None)


class SBISecuritiesScraper(_BaseFallbackScraper):
    """SBI Securities (sbisec.co.jp) quote scraper — fallback for Yahoo JP."""

    BASE_URL = "https://www.sbisec.co.jp/ETGate/"
    DETAIL_PARAMS: ClassVar[dict[str, str]] = {
        "_ControlID": "WPLETmgR001Control",
        "_PageID": "WPLETmgR001Dtll",
        "_DataStoreID": "DSWPLETmgR001Control",
        "_ActionID": "_Control",
    }
    _SCRAPER_LABEL = "SBI"

    def _fetch_page(self, symbol: str) -> str | None:
        """Return the decoded stock detail HTML for *symbol*, or None."""
        if _is_scraper_blocked():
            return None
        code = symbol.replace(".T", "").replace(".t", "")
        params = {**self.DETAIL_PARAMS, "sIssue": code, "getFlg": "on"}
        try:
            resp = self._get_session().get(self.BASE_URL, params=params, timeout=6.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
            if resp.status_code != 200:
                logger.debug("[SBI Scraper] Non-200 response (%s) for %s", resp.status_code, symbol)
                return None
            html = resp.content.decode("cp932", errors="replace")
            if "現在値" not in html:
                logger.debug("[SBI Scraper] Page for %s lacks quote markers (blocked?)", symbol)
                return None
            return html
        except Exception as exc:
            logger.info("[SBI Scraper] Failed fetch for %s: %s", symbol, exc)
            return None

    @staticmethod
    def _number_after_label(html: str, label: str, signed: bool = False) -> float | None:
        """Extract the first number after *label* within a 400-char window."""
        idx = html.find(label)
        if idx < 0:
            return None
        window = html[idx : idx + 400]
        pattern = r"([+-]?[0-9][0-9,]*\.?[0-9]*)" if signed else r"([0-9][0-9,]*\.?[0-9]*)"
        m = re.search(pattern, window)
        if not m:
            return None
        num = _parse_quote_number(m.group(1))
        return num if math.isfinite(num) else None

    def fetch_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch the regular session quote for *symbol* from SBI."""
        if self._is_in_cooldown(symbol):
            return None
        html = self._fetch_page(symbol)
        if html is None:
            self._record_fetch_failure(symbol)
            return None
        price = self._number_after_label(html, "現在値")
        if price is None or price <= 0:
            self._record_fetch_failure(symbol)
            return None
        change = self._number_after_label(html, "前日比", signed=True) or 0.0
        payload: TickerPayload = {
            "symbol": symbol,
            "price": price,
            "change": change,
            "change_percent": 0.0,
            "volume": 0,
            "source": "sbi",
            "updated_at": time.time(),
        }
        logger.debug("[SBI Scraper] Quote for %s: price=%.2f", symbol, price)
        self._record_fetch_success(symbol)
        return payload

    def fetch_pts_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch the PTS (after-hours) quote for *symbol* from SBI."""
        if self._is_in_cooldown(symbol, kind="pts"):
            return None
        html = self._fetch_page(symbol)
        if html is None:
            self._record_fetch_failure(symbol, kind="pts")
            return None
        idx = html.find("PTS")
        if idx < 0:
            self._record_fetch_failure(symbol, kind="pts")
            return None
        window = html[idx : idx + 600]
        price = self._number_after_label(window, "PTS")
        if price is None or price <= 0:
            m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", window)
            if m:
                price = _parse_quote_number(m.group(1))
        if price is None or price <= 0:
            self._record_fetch_failure(symbol, kind="pts")
            return None
        payload: TickerPayload = {
            "symbol": symbol,
            "price": price,
            "change": 0.0,
            "change_percent": 0.0,
            "volume": 0,
            "source": "sbi_pts",
            "pts": True,
            "pts_trading": False,
            "pts_time": "",
            "updated_at": time.time(),
        }
        logger.debug("[SBI Scraper] PTS quote for %s: price=%.2f", symbol, price)
        self._record_fetch_success(symbol, kind="pts")
        return payload


class Nikkei225JPScraper(_BaseFallbackScraper):
    """Nikkei225JP (nikkei225jp.com) scraper — fallback provider for JP stocks, ADRs, PTS & indices."""

    BASE_URL = "https://nikkei225jp.com/"
    ADR_URL = "https://nikkei225jp.com/adr/adr.php"
    ADR_ALL_URL = "https://nikkei225jp.com/_data/_nfsDATA/adr/_adr_all.js"
    INDEX_MID_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_TOP_mid.js"
    INDEX_BTM_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_TOP_btm.js"
    INDEX_NDY_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_NDY_min.js"
    _SCRAPER_LABEL = "Nikkei225JP"

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

    def __init__(self) -> None:
        super().__init__()
        self._adr_cache: dict[str, list[str]] = {}
        self._adr_cache_time: float = 0.0
        self._index_cache: dict[int, list[str]] = {}
        self._index_cache_time: float = 0.0
        self._cache_lock = threading.Lock()

    def _refresh_adr_cache(self, max_age: float = 10.0) -> dict[str, list[str]]:
        now = time.time()
        with self._cache_lock:
            if self._adr_cache and (now - self._adr_cache_time) < max_age:
                return self._adr_cache

        if _is_scraper_blocked():
            with self._cache_lock:
                return self._adr_cache

        try:
            resp = self._get_session().get(self.ADR_ALL_URL, timeout=6.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
            if resp.status_code == 200:
                text = resp.content.decode("utf-8", errors="replace")
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
            logger.debug("[Nikkei225JP Scraper] Failed to fetch _adr_all.js: %s", exc)

        with self._cache_lock:
            return self._adr_cache

    def _refresh_index_cache(self, max_age: float = 10.0) -> dict[int, list[str]]:
        now = time.time()
        with self._cache_lock:
            if self._index_cache and (now - self._index_cache_time) < max_age:
                return self._index_cache

        if _is_scraper_blocked():
            with self._cache_lock:
                return self._index_cache

        cache: dict[int, list[str]] = {}
        session = self._get_session()
        for url in (self.INDEX_MID_URL, self.INDEX_BTM_URL):
            try:
                resp = session.get(url, timeout=6.0)
                _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
                if resp.status_code == 200:
                    text = resp.content.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        m = re.search(r'A\[(\d+)\]="([^"]+)"', line)
                        if m:
                            code = int(m.group(1))
                            parts = m.group(2).split("_")
                            if len(parts) >= 3:
                                cache[code] = parts
            except Exception as exc:
                logger.debug("[Nikkei225JP Scraper] Failed to fetch index url %s: %s", url, exc)

        try:
            resp = session.get(self.INDEX_NDY_URL, timeout=6.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
            if resp.status_code == 200:
                text = resp.content.decode("utf-8", errors="replace")
                for m in re.finditer(r"var NDY(\d+)V=([\d.]+),NDY\1Z=([+-]?[\d.]+);", text):
                    code = int(m.group(1))
                    if code not in cache:
                        cache[code] = [m.group(2), m.group(3), "0", "", ""]
        except Exception as exc:
            logger.debug("[Nikkei225JP Scraper] Failed to fetch NDY min: %s", exc)

        if cache:
            with self._cache_lock:
                self._index_cache = cache
                self._index_cache_time = now

        with self._cache_lock:
            return self._index_cache

    def fetch_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch regular session stock quote or index quote for *symbol* from nikkei225jp."""
        if self._is_in_cooldown(symbol):
            return None
        if _is_scraper_blocked():
            return None

        if symbol.startswith("^") or "=" in symbol or symbol in self.INDEX_MAP:
            return self.fetch_index_quote(symbol)

        clean_code = symbol.replace(".T", "").replace(".t", "").strip()
        adr_cache = self._refresh_adr_cache()
        parts = adr_cache.get(clean_code)

        if not parts:
            try:
                url = f"{self.ADR_URL}?a={clean_code}"
                resp = self._get_session().get(url, timeout=6.0)
                _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
                if resp.status_code == 200 and f'var Sno="{clean_code}"' in resp.text:
                    m_parts = re.search(r'var\s+A0\s*=\s*"([^"]+)"', resp.text)
                    if m_parts:
                        parts = m_parts.group(1).split("_")
                    else:
                        parts = self._refresh_adr_cache(max_age=0.0).get(clean_code)
            except Exception as exc:
                logger.debug("[Nikkei225JP Scraper] Direct adr.php fetch failed for %s: %s", symbol, exc)

        if not parts or len(parts) < 11:
            self._record_fetch_failure(symbol)
            return None

        price = _parse_quote_number(parts[8])
        if not math.isfinite(price) or price <= 0:
            self._record_fetch_failure(symbol)
            return None

        change = _parse_quote_number(parts[9])
        if not math.isfinite(change):
            change = 0.0
        change_pct = _parse_quote_number(parts[10])
        if not math.isfinite(change_pct):
            change_pct = 0.0

        payload: TickerPayload = {
            "symbol": symbol,
            "price": price,
            "change": change,
            "change_percent": change_pct,
            "volume": 0,
            "source": "nikkei225jp_adr",
            "updated_at": time.time(),
        }
        logger.debug("[Nikkei225JP Scraper] Quote for %s: price=%.2f", symbol, price)
        self._record_fetch_success(symbol)
        return payload

    def fetch_pts_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch PTS (after-hours) quote for *symbol* from nikkei225jp."""
        if self._is_in_cooldown(symbol, kind="pts"):
            return None
        if _is_scraper_blocked():
            return None

        clean_code = symbol.replace(".T", "").replace(".t", "").strip()
        adr_cache = self._refresh_adr_cache()
        parts = adr_cache.get(clean_code)
        if not parts or len(parts) < 21:
            self._record_fetch_failure(symbol, kind="pts")
            return None

        pts_price = _parse_quote_number(parts[20])
        if not math.isfinite(pts_price) or pts_price <= 0:
            self._record_fetch_failure(symbol, kind="pts")
            return None

        pts_vol = 0
        if len(parts) > 21:
            parsed_vol = _parse_quote_number(parts[21])
            if math.isfinite(parsed_vol) and parsed_vol > 0:
                pts_vol = int(parsed_vol)

        pts_time = parts[19] if len(parts) > 19 else ""
        tokyo_price = _parse_quote_number(parts[8]) if len(parts) > 8 else 0.0
        pts_change = (pts_price - tokyo_price) if (math.isfinite(tokyo_price) and tokyo_price > 0) else 0.0
        pts_change_pct = (pts_change / tokyo_price * 100.0) if (math.isfinite(tokyo_price) and tokyo_price > 0) else 0.0

        payload: TickerPayload = {
            "symbol": symbol,
            "price": pts_price,
            "change": pts_change,
            "change_percent": pts_change_pct,
            "volume": pts_vol,
            "source": "nikkei225jp_pts",
            "pts": True,
            "pts_trading": False,
            "pts_time": pts_time,
            "updated_at": time.time(),
        }
        logger.debug("[Nikkei225JP Scraper] PTS quote for %s: price=%.2f", symbol, pts_price)
        self._record_fetch_success(symbol, kind="pts")
        return payload

    def fetch_index_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch index or FX quote for *symbol* from nikkei225jp."""
        code = self.INDEX_MAP.get(symbol)
        if not code:
            return None

        idx_cache = self._refresh_index_cache()
        parts = idx_cache.get(code)
        if not parts or len(parts) < 3:
            self._record_fetch_failure(symbol)
            return None

        price = _parse_quote_number(parts[0])
        if not math.isfinite(price) or price <= 0:
            self._record_fetch_failure(symbol)
            return None

        change = _parse_quote_number(parts[1]) if len(parts) > 1 else 0.0
        if not math.isfinite(change):
            change = 0.0
        change_pct = _parse_quote_number(parts[2]) if len(parts) > 2 else 0.0
        if not math.isfinite(change_pct):
            change_pct = 0.0

        payload: TickerPayload = {
            "symbol": symbol,
            "price": price,
            "change": change,
            "change_percent": change_pct,
            "volume": 0,
            "source": "nikkei225jp",
            "updated_at": time.time(),
        }
        logger.debug("[Nikkei225JP Scraper] Index quote for %s: price=%.2f", symbol, price)
        self._record_fetch_success(symbol)
        return payload


class MinkabuScraper(_BaseFallbackScraper):
    """Minkabu (minkabu.jp) stock scraper — lowest-tier fallback provider for JP stocks & PTS."""

    BASE_URL = "https://minkabu.jp/stock/"
    _SCRAPER_LABEL = "Minkabu"

    def fetch_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch the regular session quote for *symbol* from Minkabu."""
        if self._is_in_cooldown(symbol):
            return None
        if _is_scraper_blocked():
            return None
        code = symbol.replace(".T", "").replace(".t", "")
        url = f"{self.BASE_URL}{code}"
        try:
            resp = self._get_session().get(url, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
            if resp.status_code == 200:
                html = resp.text
                m = re.search(r'class=["\']stock_price["\'][^>]*>\s*([0-9,]+\.?[0-9]*)', html)
                if not m:
                    m = re.search(r'([0-9,]+\.?[0-9]*)\s*円', html)
                if m:
                    price = _parse_quote_number(m.group(1))
                    if price > 0:
                        payload: TickerPayload = {
                            "symbol": symbol,
                            "price": price,
                            "change": 0.0,
                            "change_percent": 0.0,
                            "volume": 0,
                            "source": "minkabu",
                            "updated_at": time.time(),
                        }
                        logger.debug("[Minkabu Scraper] Quote for %s: price=%.2f", symbol, price)
                        self._record_fetch_success(symbol)
                        return payload
            self._record_fetch_failure(symbol)
        except Exception as exc:
            logger.debug("[Minkabu Scraper] Failed fetch for %s: %s", symbol, exc)
            self._record_fetch_failure(symbol)
        return None

    def fetch_pts_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch the PTS quote for *symbol* from Minkabu."""
        if self._is_in_cooldown(symbol, kind="pts"):
            return None
        payload = self.fetch_quote(symbol)
        if payload:
            payload["source"] = "minkabu_pts"
            payload["pts"] = True
            payload["pts_trading"] = False
            payload["pts_time"] = ""
            self._record_fetch_success(symbol, kind="pts")
            return payload
        self._record_fetch_failure(symbol, kind="pts")
        return None


class YahooJPRealtimeScraper:
    """High-frequency scraper for Yahoo! Finance Japan with Smart Polling."""

    BASE_URL = "https://finance.yahoo.co.jp/quote/"
    USER_AGENTS = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
    )
    STRUCTURE_CHANGE_THRESHOLD = 5
    PAUSE_COOLDOWN_INITIAL = 15.0
    PAUSE_COOLDOWN_MAX = 120.0
    RECOVERY_COOLDOWN_SECONDS = 120.0
    POLL_INTERVAL_OPEN = 1.0
    POLL_INTERVAL_CLOSED = 15.0
    IDLE_POLL_EXTENSION = 2.0
    STOP_JOIN_TIMEOUT_SEC = 1.0

    def __init__(
        self,
        symbols: list[str] | None = None,
        on_update_callback: Callable[[TickerPayload], None] | None = None,
        fallback_provider: Any | None = None,
    ) -> None:
        self.symbols: set[str] = set(symbols or [])
        self.on_update_callback = on_update_callback
        self.fallback_provider = fallback_provider
        self.secondary_fallback_provider: Any | None = None
        self.tertiary_fallback_provider: Any | None = None
        self.fallback_providers: list[Any] = []
        self.running = False
        self.thread: threading.Thread | None = None
        self._epoch = 0
        self._thread_local = threading.local()
        self._http_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._sessions_lock = threading.Lock()
        self._all_sessions: set[Any] = set()
        self.lock = threading.Lock()
        self._consecutive_failures: dict[tuple[str, str], int] = {}
        self._structure_change_reported: set[tuple[str, str]] = set()
        self._structure_change_reported_time: dict[tuple[str, str], float] = {}
        self._pause_until: dict[tuple[str, str], float] = {}
        self._last_cycle_updates = 1
        self._last_dispatch_price: dict[str, float] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._symbol_tokens: dict[str, object] = {}

    def _all_fallback_providers(self) -> list[Any]:
        """Return list of active fallback providers in priority order."""
        providers: list[Any] = []
        if self.fallback_provider is not None:
            providers.append(self.fallback_provider)
        if self.secondary_fallback_provider is not None:
            providers.append(self.secondary_fallback_provider)
        if self.tertiary_fallback_provider is not None:
            providers.append(self.tertiary_fallback_provider)
        for p in self.fallback_providers:
            if p is not None and p not in providers:
                providers.append(p)
        return providers

    def add_symbol(self, symbol: str) -> object:
        """Register *symbol* and return its current subscription token."""
        token = object()
        with self.lock:
            self.symbols.add(symbol)
            self._symbol_tokens[symbol] = token
        return token

    def remove_symbol(self, symbol: str) -> None:
        """Remove symbol from monitoring set and purge all associated tracking state."""
        with self.lock:
            self.symbols.discard(symbol)
            self._symbol_tokens.pop(symbol, None)
            self._last_dispatch_price.pop(symbol, None)
            for kind in ("regular", "pts"):
                key = (symbol, kind)
                self._consecutive_failures.pop(key, None)
                self._structure_change_reported.discard(key)
                self._structure_change_reported_time.pop(key, None)
                self._pause_until.pop(key, None)
        for fb in self._all_fallback_providers():
            if hasattr(fb, "remove_symbol"):
                fb.remove_symbol(symbol)

    def _is_symbol_current(self, symbol: str, token: object | None = None) -> bool:
        """Return whether a fetch belongs to the current symbol registration."""
        with self.lock:
            if symbol in self.symbols:
                return token is None or self._symbol_tokens.get(symbol) is token
            alias = symbol[:-2] if symbol.endswith((".T", ".t")) else f"{symbol}.T"
            if alias in self.symbols:
                return token is None or self._symbol_tokens.get(alias) is token
            return False

    def _is_worker_current(self, epoch: int) -> bool:
        with self.lock:
            return self.running and self._epoch == epoch

    def _get_session(self) -> Any:
        """Return a thread-local curl_cffi/requests session."""
        session = getattr(self._thread_local, "session", None)
        with self._sessions_lock:
            if session is not None and session not in self._all_sessions:
                session = None
                self._thread_local.session = None
        if session is None:
            session = _create_cffi_session()
            self._thread_local.session = session
            with self._sessions_lock:
                self._all_sessions.add(session)
        return session

    @property
    def session(self) -> Any:
        return self._get_session()

    def close(self) -> None:
        """Close all allocated HTTP sessions and child fallbacks."""
        with self._sessions_lock:
            sessions = list(self._all_sessions)
            self._all_sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception as exc:
                logger.debug("Failed closing Yahoo JP scraper session: %s", exc)
        self._thread_local.session = None
        for fb in self._all_fallback_providers():
            if hasattr(fb, "close"):
                fb.close()

    def _record_fetch_failure(self, symbol: str, kind: str = "regular") -> None:
        key = (symbol, kind)
        now = time.time()
        with self.lock:
            last_rep = self._structure_change_reported_time.get(key, 0.0)
            if key in self._structure_change_reported and (now - last_rep) > self.RECOVERY_COOLDOWN_SECONDS:
                self._consecutive_failures.pop(key, None)
                self._structure_change_reported.discard(key)
                self._structure_change_reported_time.pop(key, None)

            count = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = count
            if count >= self.STRUCTURE_CHANGE_THRESHOLD:
                if key not in self._structure_change_reported:
                    self._structure_change_reported.add(key)
                    self._structure_change_reported_time[key] = now
                    _get_logger().info(
                        "[Yahoo JP Scraper] %d consecutive %s scrape failures for %s: "
                        "pausing temporarily before automatic retry.",
                        count,
                        kind,
                        symbol,
                    )
                pause_mult = 2 ** min(count - self.STRUCTURE_CHANGE_THRESHOLD, 3)
                pause_duration = min(self.PAUSE_COOLDOWN_INITIAL * pause_mult, self.PAUSE_COOLDOWN_MAX)
                self._pause_until[key] = now + pause_duration

    def _record_fetch_success(self, symbol: str, kind: str = "regular") -> None:
        key = (symbol, kind)
        with self.lock:
            self._consecutive_failures.pop(key, None)
            self._structure_change_reported.discard(key)
            self._structure_change_reported_time.pop(key, None)
            self._pause_until.pop(key, None)

    @staticmethod
    def _extract_quote_field(html: str, field: str) -> str | None:
        res = _ESCAPED_QUOTE_RES.get(field)
        if res is not None:
            m = res.search(html)
            if m:
                return m.group(1)
        m = re.search(r'"' + field + r'":\s*"?([^",\s}]+)"?', html)
        return m.group(1) if m else None

    def _is_startup_ready(self, force_check: bool = False) -> bool:
        try:
            import sys
            if not force_check and ("pytest" in sys.modules or "unittest" in sys.modules):
                return True

            from app_state import app_state
            if not hasattr(app_state, "market") or app_state.market is None:
                return True

            if not getattr(app_state.market, "first_sync_attempted", False):
                return False

            first_sync_ts = getattr(app_state.market, "first_sync_completed_at", 0.0)
            return not (first_sync_ts > 0 and (time.time() - first_sync_ts) < 3.0)
        except Exception:
            return True

    def _poll_interval(self) -> float:
        from utils.market_utils import is_market_open

        return self.POLL_INTERVAL_OPEN if is_market_open("jp") else self.POLL_INTERVAL_CLOSED

    def _dispatch_price_changed(self, payload: TickerPayload) -> bool:
        symbol = payload.get("symbol")
        price = payload.get("price")
        if symbol is None or not isinstance(price, (int, float)) or not math.isfinite(price):
            return False
        price_f = float(price)
        prev = self._last_dispatch_price.get(symbol)
        self._last_dispatch_price[symbol] = price_f
        return prev is None or prev != price_f

    def _fetch_kabutan_symbol(self, symbol: str) -> TickerPayload | None:
        if _is_scraper_blocked():
            return None
        clean_code = symbol.replace(".T", "").replace(".t", "")
        if not clean_code.isdigit():
            return None
        url = f"https://kabutan.jp/stock/?code={clean_code}"
        try:
            resp = self._get_session().get(url, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
            if resp.status_code == 200 and BeautifulSoup is not None:
                soup = BeautifulSoup(resp.text, "html.parser")
                price_el = soup.select_one(".si_i1_2 span.kabuka") or soup.select_one("span.kabuka")
                if price_el:
                    price_clean = re.sub(r"[^\d.]", "", price_el.text)
                    price = float(price_clean) if price_clean else 0.0
                    if price > 0:
                        change = 0.0
                        pct = 0.0
                        dl_el = soup.select_one("dl.si_i1_dl1")
                        if dl_el:
                            dds = dl_el.find_all("dd")
                            if len(dds) >= 1:
                                c_clean = re.sub(
                                    r"[^\d.\-+]",
                                    "",
                                    dds[0].text.replace("▲", "-").replace("▼", "-").replace("+", ""),
                                )
                                try:
                                    change = float(c_clean)
                                except ValueError:
                                    pass
                            if len(dds) >= 2:
                                p_clean = re.sub(
                                    r"[^\d.\-+]",
                                    "",
                                    dds[1].text.replace("▲", "-").replace("▼", "-").replace("+", ""),
                                )
                                try:
                                    pct = float(p_clean)
                                except ValueError:
                                    pass
                        res_payload: TickerPayload = {
                            "symbol": f"{clean_code}.T",
                            "price": price,
                            "change": change,
                            "change_percent": pct,
                            "volume": 0,
                            "source": "kabutan",
                            "updated_at": time.time(),
                        }
                        logger.debug(
                            "[Kabutan Scraper] Quote success for %s.T: price=%.2f, change=%.2f, pct=%.2f%%",
                            clean_code,
                            price,
                            change,
                            pct,
                        )
                        self._record_fetch_success(symbol)
                        return res_payload
        except Exception as e:
            logger.debug("[Kabutan Scraper] Fetch error for %s: %s", symbol, e)
        return None

    def _fetch_kabutan_pts_symbol(self, symbol: str) -> TickerPayload | None:
        if _is_scraper_blocked():
            return None
        clean_code = symbol.replace(".T", "").replace(".t", "")
        if not clean_code.isdigit():
            return None
        url = f"https://kabutan.jp/stock/?code={clean_code}"
        try:
            resp = self._get_session().get(url, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
            if resp.status_code == 200 and BeautifulSoup is not None:
                soup = BeautifulSoup(resp.text, "html.parser")
                pts_box = soup.select_one(".si_i1_3")
                if pts_box:
                    pts_val_el = pts_box.select_one(".kabuka2")
                    if pts_val_el:
                        pts_clean = re.sub(r"[^\d.]", "", pts_val_el.text)
                        if pts_clean:
                            price = float(pts_clean)
                            if price > 0:
                                pts_time_el = pts_box.select_one(".kabuka3")
                                pts_time = pts_time_el.text.strip() if pts_time_el else ""
                                res_payload: TickerPayload = {
                                    "symbol": f"{clean_code}.T",
                                    "price": price,
                                    "change": 0.0,
                                    "change_percent": 0.0,
                                    "volume": 0,
                                    "source": "kabutan_pts",
                                    "pts": True,
                                    "pts_trading": True,
                                    "pts_time": pts_time,
                                    "updated_at": time.time(),
                                }
                                self._record_fetch_success(symbol, kind="pts")
                                return res_payload
        except Exception as e:
            logger.debug("[Kabutan PTS Scraper] Fetch error for %s: %s", symbol, e)
        return None

    def fetch_jp_symbol(self, symbol: str) -> TickerPayload | None:
        """Scrape JP quote for a single symbol (Kabutan first, then Yahoo JP)."""
        if _is_scraper_blocked():
            return None
        payload = self._fetch_kabutan_symbol(symbol)
        if payload:
            return payload

        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"{self.BASE_URL}{clean_code}.T"
        try:
            resp = self._get_session().get(url, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)
            if resp.status_code == 200:
                html = resp.text
                price_str = self._extract_quote_field(html, "price")
                if price_str is None:
                    next_data = _extract_next_data_quotes(html)
                    if next_data:
                        price_str = next_data.get("price")
                        if price_str is not None:
                            logger.debug(
                                "[Yahoo JP Scraper] Quote parsed from __NEXT_DATA__ for %s",
                                symbol,
                            )
                if price_str is not None:
                    price = _parse_quote_number(price_str)
                    if price > 0:
                        change = _parse_quote_number(self._extract_quote_field(html, "priceChange"))
                        change_pct_str = self._extract_quote_field(html, "priceChangeRate")
                        if change_pct_str is None:
                            change_pct_str = self._extract_quote_field(html, "priceChangePercent")
                        res_payload = {
                            "symbol": f"{clean_code}.T",
                            "price": price,
                            "change": change,
                            "change_percent": _parse_quote_number(change_pct_str),
                            "volume": 0,
                            "source": "yahoojp",
                            "updated_at": time.time(),
                        }
                        logger.debug(
                            "[Yahoo JP Scraper] Scrape success for %s.T: price=%.2f, change=%.2f (source=yahoojp)",
                            clean_code,
                            price,
                            change,
                        )
                        self._record_fetch_success(symbol)
                        return res_payload
                else:
                    logger.debug("[Yahoo JP Scraper] No price found on page for %s", symbol)
            else:
                logger.debug(
                    "[Yahoo JP Scraper] Non-200 response (%s) for %s", resp.status_code, symbol
                )
        except Exception as e:
            logger.info("[Yahoo JP Scraper] Failed scrape for %s: %s", symbol, e)
        self._record_fetch_failure(symbol)
        return None

    def fetch_pts_symbol(self, symbol: str) -> TickerPayload | None:
        """Fetch the PTS (after-hours) quote for a JP symbol (Kabutan first, then Yahoo JP)."""
        if _is_scraper_blocked():
            return None
        payload = self._fetch_kabutan_pts_symbol(symbol)
        if payload:
            return payload

        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"{self.BASE_URL}{clean_code}.T?md=pts"
        try:
            resp = self._get_session().get(url, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code, propagate_to_yfinance=False)

            if resp.status_code == 200:
                html = resp.text
                segment = _extract_pts_price_data(html)
                if segment:
                    fields = _extract_pts_fields(segment)
                    price = _parse_quote_number(fields.get("price"))
                    if price > 0:
                        flag_match = _PTS_TRADING_FLAG_RE.search(html) or _PTS_TRADING_FLAG_UNESCAPED_RE.search(html)
                        payload = {
                            "symbol": f"{clean_code}.T",
                            "price": price,
                            "change": _parse_quote_number(fields.get("changePrice")),
                            "change_percent": _parse_quote_number(fields.get("changeRate")),
                            "volume": int(_parse_quote_number(fields.get("volume"))),
                            "source": "yahoojp_pts",
                            "pts": True,
                            "pts_trading": bool(flag_match and flag_match.group(1) == "true"),
                            "pts_time": fields.get("priceTime") or "",
                            "updated_at": time.time(),
                        }
                        logger.debug(
                            "[Yahoo JP Scraper] PTS quote for %s.T: price=%.2f (source=yahoojp_pts)",
                            clean_code,
                            price,
                        )
                        self._record_fetch_success(symbol, kind="pts")
                        return payload
                else:
                    logger.debug("[Yahoo JP Scraper] No PTS data on page for %s", symbol)
            else:
                logger.debug(
                    "[Yahoo JP Scraper] PTS non-200 response (%s) for %s", resp.status_code, symbol
                )
        except Exception as e:
            logger.info("[Yahoo JP Scraper] Failed PTS scrape for %s: %s", symbol, e)
        self._record_fetch_failure(symbol, kind="pts")
        return None

    def _fetch_regular_with_fallback(self, symbol: str) -> TickerPayload | None:
        """Fetch a regular quote: Yahoo JP first, then fallback providers in priority order."""
        payload = self.fetch_jp_symbol(symbol)
        if not payload:
            for fb in self._all_fallback_providers():
                try:
                    payload = fb.fetch_quote(symbol)
                    if payload:
                        lbl = getattr(fb, "_SCRAPER_LABEL", type(fb).__name__)
                        logger.debug("[Yahoo JP Scraper] Fallback provider (%s) quote for %s", lbl, symbol)
                        break
                except Exception as exc:
                    lbl = getattr(fb, "_SCRAPER_LABEL", type(fb).__name__)
                    logger.debug("Fallback %s failed for %s: %s", lbl, symbol, exc)
        return payload

    def _active_symbols(self, symbols: Iterable[str], kind: str = "regular") -> list[str]:
        """Filter out symbols currently paused after a structure-change streak."""
        now_ts = time.time()
        with self.lock:
            return [
                sym
                for sym in symbols
                if self._pause_until.get((sym, kind), 0.0) <= now_ts
            ]

    def _worker_loop(self) -> None:
        my_epoch = self._epoch
        while self.running and self._epoch == my_epoch:
            try:
                if not self._is_startup_ready():
                    _interruptible_sleep(lambda: self.running and self._epoch == my_epoch, 1.0)
                    continue

                if _is_scraper_blocked() or _is_yf_rate_limited():
                    market = _scraper_market_state()
                    remains = market.scraper_block_clears_in() if market and hasattr(market, "scraper_block_clears_in") else 2.0
                    sleep_time = max(2.0, min(remains, 5.0)) if remains > 0 else 2.0
                    _interruptible_sleep(
                        lambda: self.running and self._epoch == my_epoch, sleep_time
                    )
                    continue

                from utils.market_utils import is_market_open

                is_jp_open = is_market_open("jp")
                interval = self.POLL_INTERVAL_OPEN if is_jp_open else self.POLL_INTERVAL_CLOSED

                if self._last_cycle_updates == 0:
                    interval *= self.IDLE_POLL_EXTENSION
                cycle_updates = 0

                with self.lock:
                    subscribed_symbols = list(self.symbols)

                now_ts = time.time()
                with self.lock:
                    paused_count = sum(
                        1
                        for sym in subscribed_symbols
                        if self._pause_until.get((sym, "regular"), 0.0) > now_ts
                    )
                if paused_count and paused_count >= len(subscribed_symbols) * 0.5:
                    interval *= 2.0

                target_symbols = self._active_symbols(subscribed_symbols)
                with self.lock:
                    target_tokens = {
                        sym: self._symbol_tokens.get(sym) for sym in target_symbols
                    }

                if target_symbols:
                    if len(self._last_dispatch_price) > len(target_symbols) * 2:
                        target_set = set(target_symbols)
                        self._last_dispatch_price = {
                            k: v
                            for k, v in self._last_dispatch_price.items()
                            if k in target_set
                        }

                    from constants import SCRAPER_MAX_WORKERS, SCRAPER_REQUEST_STAGGER_SEC

                    workers = min(SCRAPER_MAX_WORKERS, len(target_symbols))
                    if workers > 1:
                        with self._lifecycle_lock:
                            if not self.running or not self._is_worker_current(my_epoch):
                                break
                            if self._executor is None:
                                self._executor = DaemonThreadPoolExecutor(
                                    max_workers=SCRAPER_MAX_WORKERS,
                                    thread_name_prefix="YahooJPScraper",
                                )
                            executor = self._executor
                        future_to_sym = {}
                        for sym in target_symbols:
                            if not self.running or not self._is_worker_current(my_epoch):
                                break
                            try:
                                fut = executor.submit(self._fetch_regular_with_fallback, sym)
                                future_to_sym[fut] = sym
                            except (RuntimeError, AttributeError) as exc:
                                logger.debug(
                                    "[Yahoo JP Scraper] Task submission skipped on shutdown: %s",
                                    exc,
                                )
                                break
                            time.sleep(SCRAPER_REQUEST_STAGGER_SEC)
                        for future in as_completed(future_to_sym):
                            if not self._is_worker_current(my_epoch):
                                break
                            sym = future_to_sym[future]
                            try:
                                payload = future.result()
                                if (
                                    payload
                                    and self._is_worker_current(my_epoch)
                                    and self._is_symbol_current(sym, target_tokens.get(sym))
                                ):
                                    if self._dispatch_price_changed(payload):
                                        cycle_updates += 1
                                    if self.on_update_callback:
                                        self.on_update_callback(payload)
                            except Exception as exc:
                                logger.debug(
                                    "[Yahoo JP Scraper] Async worker error for %s: %s", sym, exc
                                )
                    else:
                        for sym in target_symbols:
                            if not self._is_worker_current(my_epoch):
                                break
                            payload = self._fetch_regular_with_fallback(sym)
                            if payload and self._is_worker_current(my_epoch) and self._is_symbol_current(
                                sym, target_tokens.get(sym)
                            ):
                                if self._dispatch_price_changed(payload):
                                    cycle_updates += 1
                                if self.on_update_callback:
                                    self.on_update_callback(payload)

                self._last_cycle_updates = cycle_updates
                _interruptible_sleep(lambda: self.running and self._epoch == my_epoch, interval)
            except Exception as exc:
                logger.error("[Yahoo JP Scraper] Worker loop error: %s", exc)
                _interruptible_sleep(lambda: self.running and self._epoch == my_epoch, 2.0)

    def start(self) -> None:
        with self._lifecycle_lock:
            if not self.running:
                self.running = True
                self._epoch += 1
                self.thread = threading.Thread(
                    target=self._worker_loop, daemon=True, name="YahooJPScraperWorker"
                )
                self.thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            worker = self.thread
            self.running = False
            self._epoch += 1
            executor = self._executor
            self._executor = None
        if executor is not None:
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception as exc:
                logger.debug("Error shutting down YahooJPScraper executor: %s", exc)
        if (
            worker is not None
            and worker is not threading.current_thread()
            and worker.is_alive()
        ):
            worker.join(timeout=self.STOP_JOIN_TIMEOUT_SEC)
        with self._lifecycle_lock:
            if self.thread is worker and (worker is None or not worker.is_alive()):
                self.thread = None
