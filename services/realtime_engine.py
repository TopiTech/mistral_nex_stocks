# services/realtime_engine.py
"""Realtime Market Data Engine for Mistral NeX Stocks.

Supports:
1. TradingView WebSocket Client (US Stocks, Indices, ETFs)
2. Yahoo! Finance JP Scraper (JP Stocks & PTS with Smart Polling)
3. SBI Securities Scraper (fallback for Yahoo JP regular & PTS quotes)
4. Unified Market Engine (Producer-Consumer Queue & Delta Update Dispatcher)
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import string
import threading
import time
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from datetime import time as dt_time
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import requests

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None  # type: ignore[assignment]
    HAS_CURL_CFFI = False

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

# Browser-like headers applied to BOTH the curl_cffi impersonating session and
# the plain-``requests`` fallback. The fallback previously shipped with no
# headers at all (requests' default UA), which made it trivially
# fingerprintable and far more likely to be blocked by upstream providers.
_SCRAPER_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="140", "Google Chrome";v="140"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _create_cffi_session() -> Any:
    """Create a curl_cffi session with Chrome 120 TLS/JA3 impersonation and Chromium Client Hints."""
    if HAS_CURL_CFFI and cffi_requests is not None:
        try:
            cffi_sess: Any = cffi_requests.Session(impersonate="chrome120")
            cffi_sess.headers.update(_SCRAPER_HEADERS)
            return cffi_sess
        except Exception as exc:
            logger.debug("Failed creating curl_cffi Session: %s", exc)
    fallback_sess: Any = requests.Session()
    fallback_sess.headers.update(_SCRAPER_HEADERS)
    return fallback_sess

# Attempt to import websocket-client for TradingView WS
try:
    import websocket
    HAS_WEBSOCKET_CLIENT = True
except ImportError:
    websocket = None  # type: ignore[assignment]
    HAS_WEBSOCKET_CLIENT = False
    logger.warning("websocket-client module not installed. TradingView WS fallback enabled.")

# Filter out opcode=8 (goodbye close frames) from the third-party websocket library logger
class WebSocketOpcode8Filter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("opcode=8" in msg or "0x03e8" in msg or "goodbye" in msg.lower())


logging.getLogger("websocket").addFilter(WebSocketOpcode8Filter())

JST = ZoneInfo("Asia/Tokyo")

# Standardized Ticker Schema Types
TickerPayload = dict[str, Any]

# JST trading session bounds (weekday heuristic only). Exchange holidays are
# covered by the live market-state lookup used by the scraper polling interval
# (``utils.market_utils.is_market_open``), not by this lightweight check.
JP_MORNING_START = dt_time(9, 0)
JP_MORNING_END = dt_time(11, 30)
JP_AFTERNOON_START = dt_time(12, 30)
JP_AFTERNOON_END = dt_time(15, 30)

# PTS (Proprietary Trading System / after-hours session) bounds (JST).
# PTS daytime: 08:20 - 16:00, PTS night: 16:30 - 23:59 (JST weekdays).
PTS_SESSION_START_DAY = dt_time(8, 20)
PTS_SESSION_END_DAY = dt_time(16, 0)
PTS_SESSION_START_NIGHT = dt_time(16, 30)
PTS_SESSION_END_NIGHT = dt_time(23, 59)
PTS_POLL_INTERVAL_ACTIVE = 10.0
PTS_POLL_INTERVAL_IDLE = 15.0


def _dedupe_pts_symbols(*symbol_collections: Iterable[str]) -> list[str]:
    """Merge symbol collections for PTS polling, keeping the first-seen form.

    Symbols that differ only by the ``.T`` suffix refer to the same JP stock
    (e.g. the scraper may keep ``7203`` while user stocks use ``7203.T``).
    Collapsing them avoids fetching the same stock twice within one cycle.
    """
    normalized: dict[str, str] = {}
    for collection in symbol_collections:
        for sym in collection:
            if not sym:
                continue
            base = sym[:-2] if sym.endswith((".T", ".t")) else sym
            normalized.setdefault(base, sym)
    return list(normalized.values())
# Cached PTS quotes older than this (seconds) are refreshed even while the PTS
# session is closed, so the last-known price stays fresh without polling the
# upstream providers on every idle pass.
PTS_CACHE_STALE_SECONDS = 300.0


def is_pts_session(now: datetime | None = None) -> bool:
    """Check whether the JP PTS (daytime or night after-hours) session is active (weekday + hours)."""
    if now is None:
        now = datetime.now(JST)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    t = now.time()
    return (PTS_SESSION_START_DAY <= t <= PTS_SESSION_END_DAY) or (
        PTS_SESSION_START_NIGHT <= t <= PTS_SESSION_END_NIGHT
    )


def _parse_quote_number(value: Any, default: float = 0.0) -> float:
    """Parse a scraped number string (may contain commas, +, %, quotes)."""
    if value is None:
        return default
    try:
        cleaned = str(value).replace(",", "").replace("+", "").replace("%", "").strip()
        num = float(cleaned)
        return num if math.isfinite(num) else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Web scraper global block detection
# ---------------------------------------------------------------------------
# All scrapers (Yahoo JP / Kabutan / SBI / Minkabu) share the same IP as
# yfinance. When any of them returns a site-wide block code, EVERY scraper
# pauses for a graduated cooldown (see MarketDataState.mark_scraper_blocked)
# instead of hammering the upstream providers and deepening the block.
SCRAPER_BLOCK_STATUS_CODES = (401, 402, 403, 429, 439)

# The shared MarketDataState reference, resolved lazily once on first use to
# avoid a per-call ``from app_state import app_state`` in the hot scraper paths.
_scraper_market_ref: Any | None = None


def _scraper_market_state() -> Any | None:
    """Return the shared MarketDataState (cached) or None."""
    global _scraper_market_ref
    if _scraper_market_ref is not None:
        return _scraper_market_ref
    try:
        from app_state import app_state

        market = getattr(app_state, "market", None)
        if market is not None:
            _scraper_market_ref = market
            return market
    except Exception as exc:
        logger.debug("Failed to obtain scraper market state: %s", exc)
    return None


def _is_scraper_blocked() -> bool:
    """True while the web-scraper global block cooldown is active."""
    market = _scraper_market_state()
    if market is None or not hasattr(market, "is_scraper_blocked"):
        return False
    return bool(market.is_scraper_blocked())


def _mark_scraper_blocked_from_status(status_code: int | None) -> None:
    """Record a global scraper block when an upstream returns a block code."""
    if status_code not in SCRAPER_BLOCK_STATUS_CODES:
        return
    market = _scraper_market_state()
    if market is None or not hasattr(market, "mark_scraper_blocked"):
        return
    try:
        market.mark_scraper_blocked()
    except Exception as exc:
        logger.debug("Failed to mark scraper blocked: %s", exc)


# Yahoo JP embeds quote data as escaped JSON inside JS strings. The quotes are
# escaped (\") while the object braces are not, e.g. \"price\":{\"value\":\"2,983.5\"}.
# The legacy page format (plain JSON, e.g. "price":"3500.0") is kept as a fallback.
_ESCAPED_QUOTE_FIELDS = ("price", "priceChange", "priceChangeRate", "priceChangePercent")
_ESCAPED_QUOTE_RES = {
    field: re.compile(r'\\"' + field + r'\\":{\\"value\\":\\"([^\\\\"]+)\\"')
    for field in _ESCAPED_QUOTE_FIELDS
}
# Marker regexes locating the start of the ptsPriceData object (escaped-quote
# JS-string form and plain JSON form). The object body is extracted with a
# balanced-brace scan (see ``_extract_pts_price_data``) instead of a non-greedy
# regex so nested objects inside the block (e.g. "price":{"value":"2,973.9"})
# cannot truncate the capture early.
_PTS_PRICE_DATA_MARKERS = (
    re.compile(r'ptsPriceData\\":\s*\{'),
    re.compile(r'"ptsPriceData"\s*:\s*\{'),
)
_PTS_TRADING_FLAG_RE = re.compile(r'ptsTradingFlag\\":(true|false)')
_PTS_TRADING_FLAG_UNESCAPED_RE = re.compile(r'"ptsTradingFlag"\s*:\s*(true|false)')
# Yahoo JP renders its quote page as a Next.js app; the full page state lives in
# a `<script id="__NEXT_DATA__" type="application/json">` blob. When the
# targeted quote regexes above fail (e.g. after a markup/format change), parsing
# this JSON blob is far more resilient than matching more regexes.
_NEXT_DATA_SCRIPT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


def _extract_pts_price_data(html: str) -> str | None:
    """Extract the balanced JSON object region of ``ptsPriceData`` from a Yahoo JP page.

    Returns the raw segment (opening ``{`` through the matching closing ``}``)
    or None when the marker is absent or the block never closes (e.g. truncated
    HTML). Handles both the escaped-quote JS-string and the plain JSON forms;
    multiple occurrences of the marker are scanned until one yields a balanced
    object.
    """
    for marker in _PTS_PRICE_DATA_MARKERS:
        for m in marker.finditer(html):
            start = m.end() - 1  # position of the opening '{'
            depth = 0
            for i in range(start, len(html)):
                ch = html[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return html[start : i + 1]
    return None


def _extract_pts_fields(segment: str) -> dict[str, str]:
    """Extract ``ptsPriceData`` field values from a raw segment.

    Handles both the flat (``"price":"1,234.5"``) and the nested value-object
    (``"price":{"value":"1,234.5"}``) shapes. A strict JSON parse is attempted
    first; if the segment is not valid JSON (page-specific escaping), a regex
    fallback extracts flat pairs and ``{"value": ...}`` objects.
    """
    unescaped = segment.replace('\\"', '"')
    try:
        data = json.loads(unescaped)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        fields: dict[str, str] = {}
        for key, val in data.items():
            if isinstance(val, dict):
                for sub in ("value", "raw", "fmt"):
                    if val.get(sub) is not None:
                        val = val[sub]
                        break
            if isinstance(val, bool):
                fields[str(key)] = "true" if val else "false"
            elif isinstance(val, (str, int, float)):
                fields[str(key)] = str(val)
        if fields:
            return fields
    # Regex fallback: flat pairs plus nested value objects.
    fields = dict(re.findall(r'"([a-zA-Z]+)":"([^"]*)"', unescaped))
    for m in re.finditer(r'"([a-zA-Z]+)"\s*:\s*\{\s*"value"\s*:\s*"([^"]*)"', unescaped):
        fields.setdefault(m.group(1), m.group(2))
    return fields


def _extract_next_data_quotes(html: str) -> dict[str, str] | None:
    """Parse quote fields from the page's ``__NEXT_DATA__`` JSON blob.

    Returns a dict of the same field names ``_extract_quote_field`` produces
    (price / priceChange / priceChangeRate / priceChangePercent) or None when
    the blob is absent, malformed, or lacks a recognizable quote shape.
    ``__NEXT_DATA__`` is HTML-escaped JSON, so ``&quot;`` must be decoded back
    to ``"`` before parsing.
    """
    m = _NEXT_DATA_SCRIPT_RE.search(html)
    if not m:
        return None
    try:
        import html as html_mod

        raw = html_mod.unescape(m.group(1))
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    def _find_quote(node: Any, depth: int = 0) -> dict[str, Any] | None:
        """Recursively locate a dict carrying a ``price`` object with ``value``."""
        if depth > 8 or not isinstance(node, dict):
            return None
        price_obj = node.get("price")
        if isinstance(price_obj, dict) and "value" in price_obj:
            return node
        for value in node.values():
            if isinstance(value, dict):
                found = _find_quote(value, depth + 1)
                if found:
                    return found
        return None

    quote_node = _find_quote(data)
    if not quote_node:
        return None

    def _num(field: str) -> str | None:
        obj = quote_node.get(field)
        if isinstance(obj, dict):
            val = obj.get("value")
            if val is not None:
                return str(val)
            for candidate in ("raw", "fmt"):
                if candidate in obj and obj[candidate] is not None:
                    return str(obj[candidate])
        return None

    result: dict[str, str] = {}
    for field in _ESCAPED_QUOTE_FIELDS:
        val = _num(field)
        if val is not None:
            result[field] = val
    if "price" in result:
        return result
    return None


def is_jp_market_open(now: datetime | None = None) -> bool:
    """Check if Tokyo Stock Exchange (TSE) is open (weekday + session hours).

    Note: this is a lightweight weekday/time heuristic and does NOT account for
    exchange holidays. Callers that need holiday-awareness (e.g. the Yahoo JP
    scraper polling interval) should use ``utils.market_utils.is_market_open``,
    which consults Yahoo's live market state (REGULAR/CLOSED).
    """
    if now is None:
        now = datetime.now(JST)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    t = now.time()
    return (JP_MORNING_START <= t <= JP_MORNING_END) or (
        JP_AFTERNOON_START <= t <= JP_AFTERNOON_END
    )


# ============================================================================
# 1. TradingView WebSocket Client
# ============================================================================

class TradingViewWSClient:
    """TradingView WebSocket client implementing TV message framing (~m~len~m~payload)."""

    WS_URL = "wss://data.tradingview.com/socket.io/websocket"
    ORIGIN = "https://data.tradingview.com"

    def __init__(self, symbols: list[str] | None = None, on_update_callback: Callable[[TickerPayload], None] | None = None) -> None:
        self.symbols: set[str] = set(symbols or [])
        self.on_update_callback = on_update_callback
        self.session_id = "qs_" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
        self.ws: Any = None
        self.running = False
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self._last_quotes: dict[str, TickerPayload] = {}

    @staticmethod
    def format_tv_message(func: str, args: list[Any]) -> str:
        """Wrap payload in ~m~len~m~ TradingView framing."""
        payload = json.dumps({"m": func, "p": args}, separators=(",", ":"))
        return f"~m~{len(payload)}~m~{payload}"

    @staticmethod
    def parse_tv_messages(raw: str) -> list[dict[str, Any]]:
        """Parse concatenated ~m~len~m~json messages from raw WS stream without regex overhead."""
        results = []
        pos = 0
        raw_len = len(raw)
        while pos < raw_len:
            start_m = raw.find("~m~", pos)
            if start_m == -1:
                break
            end_m = raw.find("~m~", start_m + 3)
            if end_m == -1:
                break
            len_str = raw[start_m + 3 : end_m]
            if not len_str.isdigit():
                pos = end_m + 3
                continue
            length = int(len_str)
            start_body = end_m + 3
            end_body = start_body + length
            if end_body <= raw_len:
                msg_body = raw[start_body:end_body]
                try:
                    results.append(json.loads(msg_body))
                except Exception:
                    logger.debug("Failed to parse TV json body: %s", msg_body)
                pos = end_body
            else:
                break
        return results

    def add_symbol(self, symbol: str) -> None:
        with self.lock:
            if symbol not in self.symbols:
                self.symbols.add(symbol)
                if self.ws and self.running:
                    try:
                        msg = self.format_tv_message("quote_add_symbols", [self.session_id, symbol])
                        self.ws.send(msg)
                    except Exception as e:
                        logger.info("Failed to add symbol %s to TV WS: %s", symbol, e)

    def remove_symbol(self, symbol: str) -> None:
        with self.lock:
            if symbol in self.symbols:
                self.symbols.remove(symbol)
                if self.ws and self.running:
                    try:
                        msg = self.format_tv_message("quote_remove_symbols", [self.session_id, symbol])
                        self.ws.send(msg)
                    except Exception as e:
                        logger.info("Failed to remove symbol %s from TV WS: %s", symbol, e)

    def _on_message(self, ws: Any, message: str) -> None:
        # Handle TradingView WS Heartbeats (~m~len~m~~h~1)
        if "~h~" in message:
            ws.send(message)  # Echo back heartbeat
            return

        parsed_list = self.parse_tv_messages(message)
        for msg in parsed_list:
            if not isinstance(msg, dict):
                continue
            m_type = msg.get("m")
            p_args = msg.get("p")
            if m_type == "qsd" and isinstance(p_args, list) and len(p_args) >= 2:
                qsd_data = p_args[1]
                if isinstance(qsd_data, dict):
                    symbol = qsd_data.get("n")
                    values = qsd_data.get("v", {})
                    if not symbol or not values or not self.on_update_callback:
                        continue

                    prev_quote = self._last_quotes.get(symbol, {})
                    price = prev_quote.get("price")
                    change = prev_quote.get("change", 0.0)
                    change_percent = prev_quote.get("change_percent", 0.0)
                    volume = prev_quote.get("volume", 0)

                    if "lp" in values and values["lp"] is not None:
                        try:
                            p_val = float(values["lp"])
                            if math.isfinite(p_val) and p_val > 0:
                                price = p_val
                        except (TypeError, ValueError):
                            pass

                    if "ch" in values and values["ch"] is not None:
                        try:
                            c_val = float(values["ch"])
                            if math.isfinite(c_val):
                                change = c_val
                        except (TypeError, ValueError):
                            pass

                    if "chp" in values and values["chp"] is not None:
                        try:
                            cp_val = float(values["chp"])
                            if math.isfinite(cp_val):
                                change_percent = cp_val
                        except (TypeError, ValueError):
                            pass

                    if "volume" in values and values["volume"] is not None:
                        try:
                            v_val = int(float(values["volume"]))
                            if v_val >= 0:
                                volume = v_val
                        except (TypeError, ValueError):
                            pass

                    if price is None or not math.isfinite(price) or price <= 0:
                        continue

                    payload: TickerPayload = {
                        "symbol": symbol,
                        "price": price,
                        "change": change,
                        "change_percent": change_percent,
                        "volume": volume,
                        "source": "tradingview",
                        "updated_at": time.time(),
                    }
                    self._last_quotes[symbol] = payload
                    logger.debug(
                        "[TradingView WS] Realtime quote update for %s: price=%.2f, change=%.2f (source=tradingview)",
                        symbol,
                        payload["price"],
                        payload["change"],
                    )
                    self.on_update_callback(payload)

                    if ":" in symbol:
                        bare_sym = symbol.split(":")[-1]
                        bare_payload = dict(payload)
                        bare_payload["symbol"] = bare_sym
                        self.on_update_callback(bare_payload)

    def _on_ws_error(self, ws: Any, err: Any) -> None:
        """Handle TradingView WS errors, treating opcode 8 close frames as clean closes."""
        err_str = str(err)
        if "opcode=8" in err_str or "0x03e8" in err_str or "goodbye" in err_str.lower() or "1000" in err_str:
            logger.info("TradingView WS clean close frame received: %s", err)
        else:
            logger.info("TradingView WS notice: %s", err)

    def _run_ws(self) -> None:
        backoff = 1.0
        while self.running:
            from app_state import app_state

            if websocket is None:
                logger.info("websocket-client not available. TV WS worker sleeping...")
                if app_state.execution.shutdown_event.wait(10.0):
                    break
                continue

            from utils.market_utils import is_market_open
            # Skip TradingView WS connection during US market closed hours
            if not is_market_open("us"):
                if app_state.execution.shutdown_event.wait(5.0):
                    break
                continue

            try:
                # Generate a fresh session_id on every connection to prevent session reuse rejection
                self.session_id = "qs_" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))

                self.ws = websocket.WebSocketApp(
                    self.WS_URL,
                    header={"Origin": self.ORIGIN},
                    on_message=self._on_message,
                    on_error=self._on_ws_error,
                    on_close=lambda ws, status, msg: logger.info("TV WS Closed: %s %s", status, msg),
                )

                def _on_open(ws: Any) -> None:
                    # Initialize TV Session
                    ws.send(self.format_tv_message("set_auth_token", ["unauthorized_user_token"]))
                    ws.send(self.format_tv_message("quote_create_session", [self.session_id]))
                    ws.send(
                        self.format_tv_message(
                            "quote_set_fields",
                            [
                                self.session_id,
                                "lp",
                                "ch",
                                "chp",
                                "volume",
                                "ask",
                                "bid",
                                "description",
                            ],
                        )
                    )
                    with self.lock:
                        for sym in self.symbols:
                            ws.send(self.format_tv_message("quote_add_symbols", [self.session_id, sym]))

                self.ws.on_open = _on_open
                backoff = 1.0
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.info("TradingView WS Exception: %s", e)

            if self.running:
                logger.info("Reconnecting TradingView WS in %.1f seconds...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    def start(self) -> None:
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._run_ws, daemon=True, name="TradingViewWSWorker")
            self.thread.start()

    def stop(self) -> None:
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception as exc:
                logger.debug("Failed closing TradingView WS connection: %s", exc)


# ============================================================================
# 2. Yahoo! Finance JP Realtime Scraper
# ============================================================================

class YahooJPRealtimeScraper:
    """High-frequency scraper for Yahoo! Finance Japan with Smart Polling."""

    BASE_URL = "https://finance.yahoo.co.jp/quote/"
    USER_AGENTS = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    )
    # After this many consecutive failed scrapes for one symbol, assume the
    # page structure changed (or the symbol is invalid) and emit an info message
    # instead of failing silently forever.
    STRUCTURE_CHANGE_THRESHOLD = 5
    # Automatically reset consecutive failure tracking after this cooldown (seconds)
    # so temporary network glitches auto-recover rather than remaining permanently paused.
    RECOVERY_COOLDOWN_SECONDS = 600.0
    # Smart-polling interval (seconds) while the JP market is open / closed.
    POLL_INTERVAL_OPEN = 1.0
    POLL_INTERVAL_CLOSED = 15.0
    # When the previous polling cycle produced NO price changes, stretch the
    # next interval by this multiplier (2s while open) so quiet markets do not
    # fetch every full quote page every second. Any price change collapses the
    # interval back to the base value immediately.
    IDLE_POLL_EXTENSION = 2.0

    def __init__(
        self,
        symbols: list[str] | None = None,
        on_update_callback: Callable[[TickerPayload], None] | None = None,
        fallback_provider: Any | None = None,
    ) -> None:
        self.symbols: set[str] = set(symbols or [])
        self.on_update_callback = on_update_callback
        # Optional fallback providers (e.g. ``SBISecuritiesScraper``, ``MinkabuScraper``) consulted
        # when Yahoo JP cannot be reached or returns no data for a symbol.
        self.fallback_provider = fallback_provider
        self.secondary_fallback_provider: Any | None = None
        self.running = False
        self.thread: threading.Thread | None = None
        self._thread_local = threading.local()
        # ``requests.Session`` / ``cffi_requests.Session`` are thread-local so
        # concurrent worker threads scrape in parallel without blocking each other.
        self._http_lock = threading.Lock()
        self.lock = threading.Lock()
        # Per-symbol consecutive-failure tracking for page-structure detection,
        # tracked separately for regular and PTS scrapes (key = (symbol, kind)).
        self._consecutive_failures: dict[tuple[str, str], int] = {}
        self._structure_change_reported: set[tuple[str, str]] = set()
        self._structure_change_reported_time: dict[tuple[str, str], float] = {}
        # Adaptive idle polling: number of price-changing payloads dispatched in
        # the previous worker cycle, plus the last dispatched price per symbol
        # used to detect change (bounded by the subscribed symbol set).
        # Initialised to 1 (not 0) so the very first cycle polls at the base
        # interval instead of immediately stretching to the idle extension.
        self._last_cycle_updates = 1
        self._last_dispatch_price: dict[str, float] = {}
        self._executor: ThreadPoolExecutor | None = None

    def _get_session(self) -> Any:
        """Return a thread-local curl_cffi/requests session for non-blocking parallel scrapes."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = _create_cffi_session()
            self._thread_local.session = session
        return session

    @property
    def session(self) -> Any:
        return self._get_session()

    def close(self) -> None:
        """Close the thread-local HTTP session if present."""
        sess = getattr(self._thread_local, "session", None)
        if sess is not None:
            try:
                sess.close()
            except Exception as exc:
                logger.debug("Failed closing Yahoo JP scraper session: %s", exc)
            self._thread_local.session = None

    def _record_fetch_failure(self, symbol: str, kind: str = "regular") -> None:
        """Track consecutive failures and report once at INFO level on a likely structure change."""
        key = (symbol, kind)
        now = time.time()
        with self.lock:
            # Auto-recovery: if cooldown has elapsed since structure change was reported, reset
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
                    "[Yahoo JP Scraper] %d consecutive %s scrape failures for %s: the page "
                    "structure may have changed or the symbol may be invalid. Realtime "
                    "updates for this symbol are paused until a successful scrape.",
                    count,
                    kind,
                    symbol,
                )

    def _record_fetch_success(self, symbol: str, kind: str = "regular") -> None:
        """Reset consecutive-failure tracking after a successful scrape."""
        key = (symbol, kind)
        with self.lock:
            self._consecutive_failures.pop(key, None)
            self._structure_change_reported.discard(key)
            self._structure_change_reported_time.pop(key, None)

    @staticmethod
    def _extract_quote_field(html: str, field: str) -> str | None:
        """Extract a quote field from the current or legacy Yahoo JP page format."""
        res = _ESCAPED_QUOTE_RES.get(field)
        if res is not None:
            m = res.search(html)
            if m:
                return m.group(1)
        # Legacy plain-JSON format: "price":"3500.0"
        m = re.search(r'"' + field + r'":\s*"?([^",\s}]+)"?', html)
        return m.group(1) if m else None

    def _is_startup_ready(self, force_check: bool = False) -> bool:
        """Return True when initial yfinance fetch and TradingView widget initialization window are complete."""
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
        """Return the smart-polling interval for the current JP market state.

        Uses ``utils.market_utils.is_market_open`` (Yahoo live market state with
        a 5-minute cache) so exchange holidays / closures are respected in
        addition to weekends and session hours.
        """
        from utils.market_utils import is_market_open

        return self.POLL_INTERVAL_OPEN if is_market_open("jp") else self.POLL_INTERVAL_CLOSED

    def _dispatch_price_changed(self, payload: TickerPayload) -> bool:
        """Return True when the payload price differs from the last dispatched
        value for that symbol (and update the tracker).

        Used by adaptive idle polling to decide whether the poll interval should
        stay fast (any change collapses it back to the base value).
        """
        symbol = payload.get("symbol")
        price = payload.get("price")
        if symbol is None or not isinstance(price, (int, float)) or not math.isfinite(price):
            return False
        price_f = float(price)
        prev = self._last_dispatch_price.get(symbol)
        self._last_dispatch_price[symbol] = price_f
        return prev is None or prev != price_f

    def _fetch_kabutan_symbol(self, symbol: str) -> TickerPayload | None:
        """Fetch regular JP stock quote from Kabutan (kabutan.jp)."""
        if _is_scraper_blocked():
            return None
        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"https://kabutan.jp/stock/?code={clean_code}"
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            resp = self._get_session().get(url, headers=headers, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code)
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
        """Fetch PTS after-hours quote from Kabutan (kabutan.jp)."""
        if _is_scraper_blocked():
            return None
        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"https://kabutan.jp/stock/?code={clean_code}"
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            resp = self._get_session().get(url, headers=headers, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code)
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
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            resp = self._get_session().get(url, headers=headers, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code)
            if resp.status_code == 200:
                html = resp.text
                price_str = self._extract_quote_field(html, "price")
                # Fallback: parse the Next.js __NEXT_DATA__ JSON blob when the
                # targeted regexes miss (markup/format drift).
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
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            resp = self._get_session().get(url, headers=headers, timeout=5.0)
            _mark_scraper_blocked_from_status(resp.status_code)
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
        """Fetch a regular quote: Yahoo JP first, then primary & secondary fallbacks."""
        payload = self.fetch_jp_symbol(symbol)
        if not payload and self.fallback_provider is not None:
            try:
                payload = self.fallback_provider.fetch_quote(symbol)
                if payload:
                    logger.debug("[Yahoo JP Scraper] Fallback provider quote for %s", symbol)
            except Exception as exc:
                logger.debug("Primary fallback failed for %s: %s", symbol, exc)
        if not payload and self.secondary_fallback_provider is not None:
            try:
                payload = self.secondary_fallback_provider.fetch_quote(symbol)
                if payload:
                    logger.debug("[Yahoo JP Scraper] Secondary fallback provider quote for %s", symbol)
            except Exception as exc:
                logger.debug("Secondary fallback failed for %s: %s", symbol, exc)
        return payload

    def _worker_loop(self) -> None:
        while self.running:
            try:
                if not self._is_startup_ready():
                    time.sleep(1.0)
                    continue

                from utils.market_utils import is_market_open
                # Skip regular JP scraping completely outside TSE regular trading hours
                if not is_market_open("jp"):
                    time.sleep(5.0)
                    continue

                # Global block: the upstream blocked this IP — pause all scrapers
                # until the graduated cooldown elapses instead of hammering it.
                if _is_scraper_blocked():
                    time.sleep(2.0)
                    continue

                interval = self._poll_interval()
                # Adaptive idle polling: when the previous cycle produced no price
                # changes, stretch the interval (IDLE_POLL_EXTENSION x) so quiet
                # markets do not hammer the upstream providers every second. Any
                # change collapses the interval back to the base value.
                if self._last_cycle_updates == 0:
                    interval *= self.IDLE_POLL_EXTENSION
                cycle_updates = 0

                with self.lock:
                    target_symbols = list(self.symbols)

                if target_symbols:
                    # Drop price-tracking entries for symbols no longer subscribed
                    # so the dict stays bounded by the active symbol set.
                    if len(self._last_dispatch_price) > len(target_symbols) * 2:
                        target_set = set(target_symbols)
                        self._last_dispatch_price = {
                            k: v
                            for k, v in self._last_dispatch_price.items()
                            if k in target_set
                        }

                    # Bounded concurrency for responsive scraping. The worker
                    # count and per-submission stagger are configurable
                    # (MNS_SCRAPER_MAX_WORKERS / MNS_SCRAPER_REQUEST_STAGGER_SEC)
                    # so operators can trade request rate vs. latency while
                    # keeping the upstream request rate flat.
                    from constants import SCRAPER_MAX_WORKERS, SCRAPER_REQUEST_STAGGER_SEC

                    workers = min(SCRAPER_MAX_WORKERS, len(target_symbols))
                    if workers > 1:
                        if self._executor is None:
                            self._executor = ThreadPoolExecutor(
                                max_workers=SCRAPER_MAX_WORKERS,
                                thread_name_prefix="YahooJPScraper",
                            )
                        future_to_sym = {}
                        for sym in target_symbols:
                            if not self.running:
                                break
                            fut = self._executor.submit(self._fetch_regular_with_fallback, sym)
                            future_to_sym[fut] = sym
                            time.sleep(SCRAPER_REQUEST_STAGGER_SEC)
                        for future in as_completed(future_to_sym):
                            if not self.running:
                                break
                            try:
                                payload = future.result()
                                if payload:
                                    if self._dispatch_price_changed(payload):
                                        cycle_updates += 1
                                    if self.on_update_callback:
                                        self.on_update_callback(payload)
                            except Exception as exc:
                                sym = future_to_sym[future]
                                logger.debug("[Yahoo JP Scraper] Async worker error for %s: %s", sym, exc)
                    else:
                        for sym in target_symbols:
                            if not self.running:
                                break
                            payload = self._fetch_regular_with_fallback(sym)
                            if payload:
                                if self._dispatch_price_changed(payload):
                                    cycle_updates += 1
                                if self.on_update_callback:
                                    self.on_update_callback(payload)

                self._last_cycle_updates = cycle_updates
                time.sleep(interval)
            except Exception as exc:
                logger.error("[Yahoo JP Scraper] Worker loop error: %s", exc)
                time.sleep(2.0)

    def start(self) -> None:
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._worker_loop, daemon=True, name="YahooJPScraperWorker")
            self.thread.start()

    def stop(self) -> None:
        self.running = False
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.debug("Error shutting down YahooJPScraper executor: %s", exc)
            self._executor = None


# ============================================================================
# 3. Fallback scrapers (SBI Securities / Minkabu) for Yahoo JP
# ============================================================================

class _BaseFallbackScraper:
    """Shared failure-tracking / cooldown / session plumbing for fallback scrapers.

    SBI and Minkabu both implement the same per-symbol consecutive-failure
    tracking, structure-change detection, fallback cooldown, and thread-local
    HTTP session management. Consolidating that logic here means bug fixes and
    policy changes (thresholds, cooldowns) land in one place instead of being
    copy-pasted across providers.
    """

    STRUCTURE_CHANGE_THRESHOLD = 3
    # After repeated failures, skip further fallback attempts for this long so a
    # persistently failing provider (e.g. bot-protection) cannot stall workers.
    FALLBACK_COOLDOWN_SECONDS = 60.0
    # Auto-recovery for consecutive failure pauses
    RECOVERY_COOLDOWN_SECONDS = 600.0
    # Short label used in log messages (overridden by subclasses).
    _SCRAPER_LABEL = "Fallback"

    def __init__(self) -> None:
        self._thread_local = threading.local()
        self._http_lock = threading.Lock()
        self.lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self._structure_change_reported: set[str] = set()
        self._structure_change_reported_time: dict[str, float] = {}
        self._last_failure_time: dict[str, float] = {}

    def _get_session(self) -> Any:
        """Return a thread-local curl_cffi/requests session."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = _create_cffi_session()
            self._thread_local.session = session
        return session

    @property
    def session(self) -> Any:
        return self._get_session()

    def close(self) -> None:
        """Close the thread-local HTTP session if present."""
        sess = getattr(self._thread_local, "session", None)
        if sess is not None:
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
            # Auto-recovery: if cooldown has elapsed since structure change was reported, reset
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
    """SBI Securities (sbisec.co.jp) quote scraper — fallback for Yahoo JP.

    SBI's etGate stock detail page is served as Windows-31J and requires a
    session cookie. This scraper establishes a session, fetches the stock
    detail page for a symbol and parses the regular quote (現在値 / 前日比)
    plus the PTS (after-hours) quote section. It is used as a fallback when
    Yahoo JP cannot be reached or returns no data for a symbol. SBI may reject
    scripted access; failures are tracked with the same structure-change
    detection pattern so the engine degrades gracefully instead of hammering
    the endpoint.
    """

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
            _mark_scraper_blocked_from_status(resp.status_code)
            if resp.status_code != 200:
                logger.debug("[SBI Scraper] Non-200 response (%s) for %s", resp.status_code, symbol)
                return None
            # The etGate stock detail page is Windows-31J (Shift_JIS) encoded.
            html = resp.content.decode("cp932", errors="replace")
            # The stock detail page always contains the 現在値 label; other pages
            # (bot-protection / invalid-session responses) do not.
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
        # The PTS section label is followed by the PTS price; fall back to any
        # number inside the section if the label-specific parse fails.
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


# ============================================================================
# 3b. Minkabu Scraper (secondary fallback for JP stocks & PTS quotes)
# ============================================================================

class MinkabuScraper(_BaseFallbackScraper):
    """Minkabu (minkabu.jp) stock scraper — fallback provider for JP stocks & PTS."""

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
            _mark_scraper_blocked_from_status(resp.status_code)
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


# ============================================================================
# 4. Realtime Market Engine & SSE Delta Dispatcher
# ============================================================================

class RealtimeMarketEngine:
    """Core Market Engine maintaining unified market state & dispatching SSE deltas."""

    def __init__(self) -> None:
        self.market_store: dict[str, TickerPayload] = {}
        self.previous_store: dict[str, TickerPayload] = {}
        self.pts_store: dict[str, TickerPayload] = {}
        self.previous_pts_store: dict[str, TickerPayload] = {}
        self.store_lock = threading.RLock()
        # Per-listener delta cursors: each SSE client keeps its own last-seen
        # snapshot so a price change is delivered to EVERY connected client
        # rather than only whichever one happens to poll first. The number of
        # registered clients is bounded by MAX_SSE_LISTENERS in the caller.
        self._client_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_pts_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_events: dict[str, threading.Event] = {}
        self._client_last_seen: dict[str, float] = {}
        self._client_counter = 0
        self._last_stale_client_purge = time.time()
        # Dirty-symbol tracking: symbols updated since the last delta scan.
        # ``get_market_deltas`` / ``get_pts_deltas`` scan only these instead of
        # the whole ``market_store`` on every SSE poll (0.5s x listeners), which
        # scales as O(changed symbols) rather than O(all symbols) per client.
        self._dirty_symbols: set[str] = set()
        self._dirty_pts_symbols: set[str] = set()
        # Per-client pending sets: symbols updated since each client's last
        # poll. Every producer update fans the symbol out to each registered
        # client's pending set, and a poll drains exactly that client's set -
        # so no shared-dirty-set prune pass (O(clients) per poll) is needed,
        # and a symbol can never linger dirty forever because the default
        # cursor never polled (the production SSE path always uses client ids).
        # The default cursor's pending set is ``_dirty_symbols`` itself.
        #
        # Invariant: every ``market_store`` / ``pts_store`` write MUST go
        # through ``_handle_producer_update`` / ``_handle_pts_update`` so the
        # fan-out above (and thus delivery to already-polled cursors) is
        # guaranteed. A direct store write would silently never reach a
        # connected client's cursor.
        self._client_pending: dict[str, set[str]] = {}
        self._client_pts_pending: dict[str, set[str]] = {}

        # Instantiate Producers. SBI and Minkabu are fallback providers:
        # consulted when Yahoo JP cannot be reached or returns no data.
        self.sbi_scraper = SBISecuritiesScraper()
        self.minkabu_scraper = MinkabuScraper()
        self.yahoojp_scraper = YahooJPRealtimeScraper(
            on_update_callback=self._handle_producer_update,
            fallback_provider=self.sbi_scraper,
        )
        self.yahoojp_scraper.secondary_fallback_provider = self.minkabu_scraper
        self.tv_client = TradingViewWSClient(on_update_callback=self._handle_producer_update)

        # Bounded executor for one-off background fetches (priority PTS fetch,
        # symbol-registration warm-up). Spawning a raw ``threading.Thread`` per
        # symbol scaled unboundedly with watchlist size; this caps the number of
        # concurrent background fetch workers (Python 3.9+ executor workers are
        # daemon threads, so they never block interpreter exit).
        self._bg_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="RealtimeBg"
        )

        self.running = False
        self.pts_thread: threading.Thread | None = None

    def _notify_all_clients(self) -> None:
        """Wake up all active SSE client threads on incoming price updates."""
        with self.store_lock:
            evts = list(self._client_events.values())
        for evt in evts:
            evt.set()

    def _handle_producer_update(self, payload: TickerPayload) -> None:
        symbol = payload["symbol"]
        with self.store_lock:
            self.market_store[symbol] = payload
            self._dirty_symbols.add(symbol)
            for pending in self._client_pending.values():
                pending.add(symbol)
            self._notify_all_clients()

    def _handle_pts_update(self, payload: TickerPayload) -> None:
        symbol = payload["symbol"]
        with self.store_lock:
            self.pts_store[symbol] = payload
            self._dirty_pts_symbols.add(symbol)
            for pending in self._client_pts_pending.values():
                pending.add(symbol)
            self._notify_all_clients()

    def _purge_stale_clients(self, ttl_seconds: float = 120.0) -> None:
        """Purge client cursors that have been inactive for > 2 minutes."""
        now = time.time()
        with self.store_lock:
            stale_ids = [
                cid for cid, last_seen in self._client_last_seen.items()
                if (now - last_seen) > ttl_seconds
            ]
            for cid in stale_ids:
                self._client_states.pop(cid, None)
                self._client_pts_states.pop(cid, None)
                self._client_pending.pop(cid, None)
                self._client_pts_pending.pop(cid, None)
                evt = self._client_events.pop(cid, None)
                if evt:
                    evt.set()
                self._client_last_seen.pop(cid, None)
                logger.debug("[Realtime Engine] Purged inactive client cursor id=%s", cid)

    def register_client(self) -> str:
        """Register an SSE delta consumer and return its cursor id.

        Pass the returned id to ``get_market_deltas`` / ``get_pts_deltas`` so
        each client only consumes its own deltas. Callers must invoke
        ``unregister_client`` when the stream closes so the cursor is released.
        """
        with self.store_lock:
            self._purge_stale_clients()
            self._client_counter += 1
            client_id = f"client_{self._client_counter}"
            self._client_states[client_id] = {}
            self._client_pts_states[client_id] = {}
            self._client_pending[client_id] = set()
            self._client_pts_pending[client_id] = set()
            evt = threading.Event()
            evt.set()  # Initial wake-up to allow consuming initial batch
            self._client_events[client_id] = evt
            self._client_last_seen[client_id] = time.time()
            return client_id

    def unregister_client(self, client_id: str) -> None:
        """Drop a client's delta cursors (stream closed / disconnected)."""
        with self.store_lock:
            self._client_states.pop(client_id, None)
            self._client_pts_states.pop(client_id, None)
            self._client_pending.pop(client_id, None)
            self._client_pts_pending.pop(client_id, None)
            evt = self._client_events.pop(client_id, None)
            if evt:
                evt.set()
            self._client_last_seen.pop(client_id, None)

    @contextmanager
    def client_context(self) -> Generator[str, None, None]:
        """Context manager that registers an SSE client and guarantees unregistration on exit."""
        cid = self.register_client()
        try:
            yield cid
        finally:
            self.unregister_client(cid)

    def wait_for_updates(self, client_id: str, timeout: float = 0.5) -> bool:
        """Wait for delta updates on the specified client's event handle.

        Returns True if an update arrived before timeout, False otherwise.
        """
        with self.store_lock:
            evt = self._client_events.get(client_id)
        if evt is None:
            time.sleep(timeout)
            return False
        signaled = evt.wait(timeout)
        evt.clear()
        return signaled

    def register_symbols(self, tv_symbols: list[str], jp_symbols: list[str]) -> None:
        """Register US / Index / ETF symbols for TV and JP symbols for Yahoo JP."""
        for sym in tv_symbols:
            self.tv_client.add_symbol(sym)
        with self.yahoojp_scraper.lock:
            self.yahoojp_scraper.symbols.update(jp_symbols)
        for sym in jp_symbols:
            if self._pts_cached_payload(sym) is None:
                def _bg_fetch(target_sym: str = sym) -> None:
                    try:
                        pts_payload = self._fetch_pts_with_fallback(target_sym)
                        if pts_payload:
                            self._handle_pts_update(pts_payload)
                    except Exception as e:
                        logger.debug("Background PTS fetch failed for %s: %s", target_sym, e)
                self._bg_executor.submit(_bg_fetch)

    def register_symbol(self, symbol: str, market: str) -> None:
        """Register a single symbol for realtime updates (incremental).

        Mirrors the default registration performed at startup so symbols added
        to the watchlist after boot also receive realtime quotes.
        """
        if market == "us":
            self.tv_client.add_symbol(symbol)
        elif market == "jp":
            with self.yahoojp_scraper.lock:
                self.yahoojp_scraper.symbols.add(symbol)

            def _priority_fetch() -> None:
                try:
                    payload = self.yahoojp_scraper._fetch_regular_with_fallback(symbol)
                    if payload:
                        self._handle_producer_update(payload)
                    # PTS quotes cannot change outside PTS hours, so the extra
                    # fetch is skipped unless the PTS session is active or the
                    # symbol has no cached PTS quote yet.
                    if is_pts_session() or self._pts_cached_payload(symbol) is None:
                        pts_payload = self._fetch_pts_with_fallback(symbol)
                        if pts_payload:
                            self._handle_pts_update(pts_payload)
                except Exception as e:
                    logger.debug("Priority fetch failed for %s: %s", symbol, e)
            self._bg_executor.submit(_priority_fetch)

    def unregister_symbol(self, symbol: str, market: str) -> None:
        """Unregister a symbol and purge its stored quote state (incl. PTS)."""
        if market == "us":
            self.tv_client.remove_symbol(symbol)
        elif market == "jp":
            with self.yahoojp_scraper.lock:
                self.yahoojp_scraper.symbols.discard(symbol)
        with self.store_lock:
            for key in list(self.market_store):
                if key == symbol or key.endswith(f":{symbol}"):
                    self.market_store.pop(key, None)
                    self.previous_store.pop(key, None)
                    self._dirty_symbols.discard(key)
                    for client_state in self._client_states.values():
                        client_state.pop(key, None)
                    for client_pending in self._client_pending.values():
                        client_pending.discard(key)
            self.pts_store.pop(symbol, None)
            self.previous_pts_store.pop(symbol, None)
            self._dirty_pts_symbols.discard(symbol)
            for client_state in self._client_pts_states.values():
                client_state.pop(symbol, None)
            for client_pending in self._client_pts_pending.values():
                client_pending.discard(symbol)

    def get_market_snapshot(self) -> dict[str, TickerPayload]:
        """Return a copy of the current unified market snapshot."""
        with self.store_lock:
            return dict(self.market_store)

    def get_pts_snapshot(self) -> dict[str, TickerPayload]:
        """Return a copy of the current PTS quote snapshot."""
        with self.store_lock:
            return dict(self.pts_store)

    def get_market_deltas(self, client_id: str | None = None) -> dict[str, TickerPayload]:
        """Return symbols changed since the given client's last check.

        When ``client_id`` is None the shared module-level cursor is used
        (backwards-compatible single-consumer mode). SSE consumers should pass
        the id returned by ``register_client`` so every connected client
        receives every update independently.

        Each cursor owns its pending set, filled on every producer update and
        drained by that cursor's own poll: a poll revisits only the symbols
        that changed since that cursor last checked - no full-store scan and no
        cross-cursor prune pass. A fresh cursor (empty previous store) receives
        the full snapshot on its first poll.
        """
        deltas: dict[str, TickerPayload] = {}
        prev_store: dict[str, TickerPayload]
        pending: set[str]
        with self.store_lock:
            if client_id is not None:
                client_prev = self._client_states.get(client_id)
                client_pending = self._client_pending.get(client_id)
                if client_prev is None or client_pending is None:
                    return {}
                # Only touch last_seen for live clients; polling with a stale
                # (already unregistered) id must not resurrect its entry.
                self._client_last_seen[client_id] = time.time()
                prev_store = client_prev
                pending = client_pending
            else:
                prev_store = self.previous_store
                pending = self._dirty_symbols
            if not prev_store:
                # First scan for this cursor: deliver the whole snapshot.
                for sym, current in self.market_store.items():
                    deltas[sym] = current
                    prev_store[sym] = dict(current)
                pending.clear()
            else:
                for sym in list(pending):
                    cur = self.market_store.get(sym)
                    if cur is None:
                        pending.discard(sym)
                        continue
                    prev = prev_store.get(sym)
                    if not prev or prev["price"] != cur["price"] or prev["change"] != cur["change"]:
                        deltas[sym] = cur
                        prev_store[sym] = dict(cur)
                    pending.discard(sym)
        if deltas:
            logger.debug(
                "[Realtime Engine] Market deltas generated for %d symbol(s): %s",
                len(deltas),
                list(deltas.keys()),
            )
        return deltas

    def get_pts_deltas(self, client_id: str | None = None) -> dict[str, TickerPayload]:
        """Return changed PTS quotes since the given client's last check.

        Mirrors ``get_market_deltas``: pass a ``register_client`` id for
        per-connection delivery; None uses the shared default cursor.
        """
        deltas: dict[str, TickerPayload] = {}
        prev_store: dict[str, TickerPayload]
        pending: set[str]
        with self.store_lock:
            if client_id is not None:
                client_prev = self._client_pts_states.get(client_id)
                client_pending = self._client_pts_pending.get(client_id)
                if client_prev is None or client_pending is None:
                    return {}
                # Only touch last_seen for live clients; polling with a stale
                # (already unregistered) id must not resurrect its entry.
                self._client_last_seen[client_id] = time.time()
                prev_store = client_prev
                pending = client_pending
            else:
                prev_store = self.previous_pts_store
                pending = self._dirty_pts_symbols
            if not prev_store:
                # First scan for this cursor: deliver the whole PTS snapshot.
                for sym, current in self.pts_store.items():
                    deltas[sym] = current
                    prev_store[sym] = dict(current)
                pending.clear()
            else:
                for sym in list(pending):
                    cur = self.pts_store.get(sym)
                    if cur is None:
                        pending.discard(sym)
                        continue
                    prev = prev_store.get(sym)
                    if (
                        not prev
                        or prev.get("price") != cur.get("price")
                        or prev.get("volume") != cur.get("volume")
                        or prev.get("change") != cur.get("change")
                        or prev.get("pts_trading") != cur.get("pts_trading")
                    ):
                        deltas[sym] = cur
                        prev_store[sym] = dict(cur)
                    pending.discard(sym)
        if deltas:
            logger.debug(
                "[Realtime Engine] PTS deltas generated for %d symbol(s): %s",
                len(deltas),
                list(deltas.keys()),
            )
        return deltas

    def _pts_cached_payload(self, symbol: str) -> TickerPayload | None:
        """Return the cached PTS quote for *symbol* (any key form), or None."""
        clean_sym = symbol.replace(".T", "").replace(".t", "")
        with self.store_lock:
            return (
                self.pts_store.get(symbol)
                or self.pts_store.get(f"{clean_sym}.T")
                or self.pts_store.get(clean_sym)
            )

    def _fetch_pts_with_fallback(self, symbol: str) -> TickerPayload | None:
        """Fetch a PTS quote: Yahoo JP first, then SBI, then Minkabu as fallback."""
        payload = self.yahoojp_scraper.fetch_pts_symbol(symbol)
        if not payload:
            try:
                payload = self.sbi_scraper.fetch_pts_quote(symbol)
                if payload:
                    logger.debug("[Realtime Engine] SBI PTS fallback quote for %s", symbol)
            except Exception as exc:
                logger.debug("SBI PTS fallback failed for %s: %s", symbol, exc)
        if not payload:
            try:
                payload = self.minkabu_scraper.fetch_pts_quote(symbol)
                if payload:
                    logger.debug("[Realtime Engine] Minkabu PTS fallback quote for %s", symbol)
            except Exception as exc:
                logger.debug("Minkabu PTS fallback failed for %s: %s", symbol, exc)
        return payload

    def _pts_worker_loop(self) -> None:
        while self.running:
            try:
                now_ts = time.time()
                if (now_ts - self._last_stale_client_purge) > 60.0:
                    self._last_stale_client_purge = now_ts
                    self._purge_stale_clients()

                if not self.yahoojp_scraper._is_startup_ready():
                    time.sleep(1.0)
                    continue

                # Global block: pause all scrapers until the cooldown elapses.
                if _is_scraper_blocked():
                    time.sleep(2.0)
                    continue

                active = is_pts_session()
                # The idle interval stays short enough (15s) to detect PTS
                # session starts promptly instead of waiting a full minute.
                interval = PTS_POLL_INTERVAL_ACTIVE if active else PTS_POLL_INTERVAL_IDLE

                with self.yahoojp_scraper.lock:
                    scraper_symbols = list(self.yahoojp_scraper.symbols)

                user_jp_symbols: set[str] = set()
                try:
                    from app_state import app_state
                    if hasattr(app_state, "market") and app_state.market is not None:
                        with app_state.market.user_stocks_lock:
                            user_jp_symbols = set(app_state.market.user_jp.keys())
                except Exception as exc:
                    logger.warning(
                        "[Realtime Engine] Failed to read user_jp symbols for PTS polling: %s",
                        exc,
                    )

                # Collapse ".T"-suffixed variants so the same stock is not
                # fetched twice within one cycle.
                target_symbols = _dedupe_pts_symbols(scraper_symbols, user_jp_symbols)

                now_ts = time.time()
                for sym in target_symbols:
                    if not self.running:
                        break
                    cached_payload = self._pts_cached_payload(sym)
                    is_stale = (
                        cached_payload is not None
                        and (now_ts - cached_payload.get("updated_at", 0.0))
                        > PTS_CACHE_STALE_SECONDS
                    )
                    # Fetch when the PTS session is active (live quotes), when
                    # the symbol is missing from the cache, or when the cached
                    # quote is stale (slow periodic refresh keeps the last-known
                    # price fresh).
                    if active or cached_payload is None or is_stale:
                        payload = self._fetch_pts_with_fallback(sym)
                        if payload:
                            self._handle_pts_update(payload)
                        # Polite intra-request delay (configurable, default 0.1s)
                        from constants import SCRAPER_REQUEST_STAGGER_SEC

                        time.sleep(SCRAPER_REQUEST_STAGGER_SEC)

                time.sleep(interval)
            except Exception as exc:
                logger.error("[Realtime Engine] PTS worker loop error: %s", exc)
                time.sleep(2.0)

    def start(self) -> None:
        if not self.running:
            self.running = True
            logger.info("Starting RealtimeMarketEngine producers...")
            self.tv_client.start()
            self.yahoojp_scraper.start()
            self.pts_thread = threading.Thread(
                target=self._pts_worker_loop, daemon=True, name="JPPTSWorker"
            )
            self.pts_thread.start()

    def stop(self) -> None:
        self.running = False
        self.tv_client.stop()
        self.yahoojp_scraper.stop()
        try:
            # Cancel queued background fetches; in-flight ones are bounded by
            # their fetch timeouts so shutdown never hangs.
            self._bg_executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            logger.debug("Failed shutting down realtime bg executor: %s", exc)


# Global Singleton Instance
realtime_market_engine = RealtimeMarketEngine()
