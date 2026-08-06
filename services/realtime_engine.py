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
from collections.abc import Callable
from datetime import datetime
from datetime import time as dt_time
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# Attempt to import websocket-client for TradingView WS
try:
    import websocket
    HAS_WEBSOCKET_CLIENT = True
except ImportError:
    websocket = None  # type: ignore[assignment]
    HAS_WEBSOCKET_CLIENT = False
    logger.warning("websocket-client module not installed. TradingView WS fallback enabled.")

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
# PTS quotes are polled faster during the session and slowly otherwise.
PTS_SESSION_START = dt_time(16, 30)
PTS_SESSION_END = dt_time(23, 59)
PTS_POLL_INTERVAL_ACTIVE = 20.0
PTS_POLL_INTERVAL_IDLE = 120.0


def is_pts_session(now: datetime | None = None) -> bool:
    """Check whether the JP PTS (after-hours) session is active (weekday + hours)."""
    if now is None:
        now = datetime.now(JST)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    return PTS_SESSION_START <= now.time() <= PTS_SESSION_END


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


# Yahoo JP embeds quote data as escaped JSON inside JS strings. The quotes are
# escaped (\") while the object braces are not, e.g. \"price\":{\"value\":\"2,983.5\"}.
# The legacy page format (plain JSON, e.g. "price":"3500.0") is kept as a fallback.
_ESCAPED_QUOTE_FIELDS = ("price", "priceChange", "priceChangeRate", "priceChangePercent")
_ESCAPED_QUOTE_RES = {
    field: re.compile(r'\\"' + field + r'\\":{\\"value\\":\\"([^\\\\"]+)\\"')
    for field in _ESCAPED_QUOTE_FIELDS
}
_PTS_PRICE_DATA_RE = re.compile(r'ptsPriceData\\":{(.{0,1500}?)}}')
_PTS_TRADING_FLAG_RE = re.compile(r'ptsTradingFlag\\":(true|false)')


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

    @staticmethod
    def format_tv_message(func: str, args: list[Any]) -> str:
        """Wrap payload in ~m~len~m~ TradingView framing."""
        payload = json.dumps({"m": func, "p": args}, separators=(",", ":"))
        return f"~m~{len(payload)}~m~{payload}"

    @staticmethod
    def parse_tv_messages(raw: str) -> list[dict[str, Any]]:
        """Parse concatenated ~m~len~m~json messages from raw WS stream."""
        results = []
        pattern = re.compile(r"~m~(\d+)~m~")
        pos = 0
        while pos < len(raw):
            match = pattern.search(raw, pos)
            if not match:
                break
            length = int(match.group(1))
            start = match.end()
            end = start + length
            if end <= len(raw):
                msg_body = raw[start:end]
                try:
                    results.append(json.loads(msg_body))
                except Exception:
                    logger.debug("Failed to parse TV json body: %s", msg_body)
                pos = end
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
                        logger.error("Failed to add symbol %s to TV WS: %s", symbol, e)

    def remove_symbol(self, symbol: str) -> None:
        with self.lock:
            if symbol in self.symbols:
                self.symbols.remove(symbol)
                if self.ws and self.running:
                    try:
                        msg = self.format_tv_message("quote_remove_symbols", [self.session_id, symbol])
                        self.ws.send(msg)
                    except Exception as e:
                        logger.error("Failed to remove symbol %s from TV WS: %s", symbol, e)

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
                    # Guard against malformed/non-numeric payloads so a single
                    # bad quote cannot crash the WS worker loop.
                    try:
                        price = float(values.get("lp"))
                        if not math.isfinite(price):
                            raise ValueError("non-finite price")
                        change = float(values["ch"]) if values.get("ch") is not None else 0.0
                        change_percent = (
                            float(values["chp"]) if values.get("chp") is not None else 0.0
                        )
                        volume = int(values["volume"]) if values.get("volume") is not None else 0
                    except (TypeError, ValueError):
                        logger.debug(
                            "Skipping malformed TradingView quote for %s: lp=%r",
                            symbol,
                            values.get("lp"),
                        )
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
                    logger.debug(
                        "[TradingView WS] Realtime quote update for %s: price=%.2f, change=%.2f (source=tradingview)",
                        symbol,
                        payload["price"],
                        payload["change"],
                    )
                    self.on_update_callback(payload)

    def _run_ws(self) -> None:
        backoff = 1.0
        while self.running:
            if not HAS_WEBSOCKET_CLIENT:
                logger.info("websocket-client not available. TV WS worker sleeping...")
                time.sleep(10.0)
                continue

            try:
                self.ws = websocket.WebSocketApp(
                    self.WS_URL,
                    header={"Origin": self.ORIGIN},
                    on_message=self._on_message,
                    on_error=lambda ws, err: logger.error("TV WS Error: %s", err),
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
                logger.error("TradingView WS Exception: %s", e)

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
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    # After this many consecutive failed scrapes for one symbol, assume the
    # page structure changed (or the symbol is invalid) and emit a loud error
    # instead of failing silently forever.
    STRUCTURE_CHANGE_THRESHOLD = 5
    # Smart-polling interval (seconds) while the JP market is open / closed.
    POLL_INTERVAL_OPEN = 2.5
    POLL_INTERVAL_CLOSED = 30.0

    def __init__(
        self,
        symbols: list[str] | None = None,
        on_update_callback: Callable[[TickerPayload], None] | None = None,
        fallback_provider: Any | None = None,
    ) -> None:
        self.symbols: set[str] = set(symbols or [])
        self.on_update_callback = on_update_callback
        # Optional fallback provider (e.g. ``SBISecuritiesScraper``) consulted
        # when Yahoo JP cannot be reached or returns no data for a symbol.
        self.fallback_provider = fallback_provider
        self.running = False
        self.thread: threading.Thread | None = None
        self.session = requests.Session()
        # ``requests.Session`` is not thread-safe: the regular worker and the
        # PTS worker share this scraper, so HTTP calls are serialized here.
        self._http_lock = threading.Lock()
        self.lock = threading.Lock()
        # Per-symbol consecutive-failure tracking for page-structure detection,
        # tracked separately for regular and PTS scrapes (key = (symbol, kind)).
        self._consecutive_failures: dict[tuple[str, str], int] = {}
        self._structure_change_reported: set[tuple[str, str]] = set()

    def _record_fetch_failure(self, symbol: str, kind: str = "regular") -> None:
        """Track consecutive failures and warn once on a likely structure change."""
        key = (symbol, kind)
        with self.lock:
            count = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = count
            if count >= self.STRUCTURE_CHANGE_THRESHOLD and key not in self._structure_change_reported:
                self._structure_change_reported.add(key)
                logger.error(
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

    def _poll_interval(self) -> float:
        """Return the smart-polling interval for the current JP market state.

        Uses ``utils.market_utils.is_market_open`` (Yahoo live market state with
        a 5-minute cache) so exchange holidays / closures are respected in
        addition to weekends and session hours.
        """
        from utils.market_utils import is_market_open

        return self.POLL_INTERVAL_OPEN if is_market_open("jp") else self.POLL_INTERVAL_CLOSED

    def fetch_jp_symbol(self, symbol: str) -> TickerPayload | None:
        """Scrape Yahoo JP quote for a single symbol (e.g. 7203.T or 9984)."""
        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"{self.BASE_URL}{clean_code}.T"
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            with self._http_lock:
                resp = self.session.get(url, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                html = resp.text
                price_str = self._extract_quote_field(html, "price")
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
            logger.warning("[Yahoo JP Scraper] Failed scrape for %s: %s", symbol, e)
        self._record_fetch_failure(symbol)
        return None

    def fetch_pts_symbol(self, symbol: str) -> TickerPayload | None:
        """Fetch the PTS (after-hours) quote for a JP symbol from Yahoo JP.

        The PTS tab page (``?md=pts``) embeds a ``ptsPriceData`` JSON object with
        the PTS price / change / volume and a ``ptsTradingFlag`` indicating
        whether PTS trading is currently active.
        """
        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"{self.BASE_URL}{clean_code}.T?md=pts"
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            with self._http_lock:
                resp = self.session.get(url, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                html = resp.text
                data_match = _PTS_PRICE_DATA_RE.search(html)
                if data_match:
                    segment = data_match.group(1).replace('\\"', '"')
                    fields = dict(re.findall(r'"([a-zA-Z]+)":"([^"]*)"', segment))
                    price = _parse_quote_number(fields.get("price"))
                    if price > 0:
                        flag_match = _PTS_TRADING_FLAG_RE.search(html)
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
            logger.warning("[Yahoo JP Scraper] Failed PTS scrape for %s: %s", symbol, e)
        self._record_fetch_failure(symbol, kind="pts")
        return None

    def _fetch_regular_with_fallback(self, symbol: str) -> TickerPayload | None:
        """Fetch a regular quote: Yahoo JP first, then the configured fallback."""
        payload = self.fetch_jp_symbol(symbol)
        if not payload and self.fallback_provider is not None:
            try:
                payload = self.fallback_provider.fetch_quote(symbol)
                if payload:
                    logger.debug("[Yahoo JP Scraper] Fallback provider quote for %s", symbol)
            except Exception as exc:
                logger.debug("SBI fallback failed for %s: %s", symbol, exc)
        return payload

    def _worker_loop(self) -> None:
        while self.running:
            interval = self._poll_interval()

            with self.lock:
                target_symbols = list(self.symbols)

            for sym in target_symbols:
                if not self.running:
                    break
                payload = self._fetch_regular_with_fallback(sym)
                if payload and self.on_update_callback:
                    self.on_update_callback(payload)
                time.sleep(0.3)  # Polite intra-request delay

            time.sleep(interval)

    def start(self) -> None:
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._worker_loop, daemon=True, name="YahooJPScraperWorker")
            self.thread.start()

    def stop(self) -> None:
        self.running = False


# ============================================================================
# 3. SBI Securities Scraper (fallback for Yahoo JP)
# ============================================================================

class SBISecuritiesScraper:
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
    STRUCTURE_CHANGE_THRESHOLD = 3
    # After repeated failures, skip further fallback attempts for this long so a
    # persistently failing SBI (e.g. bot-protection) cannot stall the workers.
    FALLBACK_COOLDOWN_SECONDS = 60.0

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            }
        )
        # ``requests.Session`` is not thread-safe: the Yahoo regular worker and
        # the PTS worker may both consult this fallback, so HTTP is serialized.
        self._http_lock = threading.Lock()
        self.lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self._structure_change_reported: set[str] = set()
        self._last_failure_time: dict[str, float] = {}

    def _is_in_cooldown(self, symbol: str, kind: str = "regular") -> bool:
        """True while this symbol/kind is in the fallback cooldown window."""
        with self.lock:
            ts = self._last_failure_time.get(f"{symbol}:{kind}")
            return ts is not None and (time.time() - ts) < self.FALLBACK_COOLDOWN_SECONDS

    def _record_fetch_failure(self, symbol: str, kind: str = "regular") -> None:
        key = f"{symbol}:{kind}"
        with self.lock:
            self._last_failure_time[key] = time.time()
            count = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = count
            if count >= self.STRUCTURE_CHANGE_THRESHOLD and key not in self._structure_change_reported:
                self._structure_change_reported.add(key)
                logger.error(
                    "[SBI Scraper] %d consecutive %s failures for %s: the page structure "
                    "may have changed or the symbol may be invalid. Fallback paused "
                    "for this symbol until a successful fetch.",
                    count,
                    kind,
                    symbol,
                )

    def _record_fetch_success(self, symbol: str, kind: str = "regular") -> None:
        with self.lock:
            self._consecutive_failures.pop(f"{symbol}:{kind}", None)
            self._structure_change_reported.discard(f"{symbol}:{kind}")
            self._last_failure_time.pop(f"{symbol}:{kind}", None)

    def _fetch_page(self, symbol: str) -> str | None:
        """Return the decoded stock detail HTML for *symbol*, or None."""
        code = symbol.replace(".T", "").replace(".t", "")
        params = {**self.DETAIL_PARAMS, "sIssue": code, "getFlg": "on"}
        try:
            with self._http_lock:
                resp = self.session.get(self.BASE_URL, params=params, timeout=6.0)
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
            logger.warning("[SBI Scraper] Failed fetch for %s: %s", symbol, exc)
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
# 4. Realtime Market Engine & SSE Delta Dispatcher
# ============================================================================

class RealtimeMarketEngine:
    """Core Market Engine maintaining unified market state & dispatching SSE deltas."""

    def __init__(self) -> None:
        self.market_store: dict[str, TickerPayload] = {}
        self.previous_store: dict[str, TickerPayload] = {}
        self.pts_store: dict[str, TickerPayload] = {}
        self.previous_pts_store: dict[str, TickerPayload] = {}
        self.store_lock = threading.Lock()
        # Per-listener delta cursors: each SSE client keeps its own last-seen
        # snapshot so a price change is delivered to EVERY connected client
        # rather than only whichever one happens to poll first. The number of
        # registered clients is bounded by MAX_SSE_LISTENERS in the caller.
        self._client_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_pts_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_counter = 0

        # Instantiate Producers. SBI is a fallback provider: it is consulted by
        # the Yahoo JP scraper (regular quotes) and the PTS worker (PTS quotes)
        # whenever Yahoo JP cannot be reached or returns no data.
        self.sbi_scraper = SBISecuritiesScraper()
        self.yahoojp_scraper = YahooJPRealtimeScraper(
            on_update_callback=self._handle_producer_update,
            fallback_provider=self.sbi_scraper,
        )
        self.tv_client = TradingViewWSClient(on_update_callback=self._handle_producer_update)

        self.running = False
        self.pts_thread: threading.Thread | None = None

    def _handle_producer_update(self, payload: TickerPayload) -> None:
        symbol = payload["symbol"]
        with self.store_lock:
            self.market_store[symbol] = payload

    def _handle_pts_update(self, payload: TickerPayload) -> None:
        symbol = payload["symbol"]
        with self.store_lock:
            self.pts_store[symbol] = payload

    def register_client(self) -> str:
        """Register an SSE delta consumer and return its cursor id.

        Pass the returned id to ``get_market_deltas`` / ``get_pts_deltas`` so
        each client only consumes its own deltas. Callers must invoke
        ``unregister_client`` when the stream closes so the cursor is released.
        """
        with self.store_lock:
            self._client_counter += 1
            client_id = f"client_{self._client_counter}"
            self._client_states[client_id] = {}
            self._client_pts_states[client_id] = {}
            return client_id

    def unregister_client(self, client_id: str) -> None:
        """Drop a client's delta cursors (stream closed / disconnected)."""
        with self.store_lock:
            self._client_states.pop(client_id, None)
            self._client_pts_states.pop(client_id, None)

    def register_symbols(self, tv_symbols: list[str], jp_symbols: list[str]) -> None:
        """Register US / Index / ETF symbols for TV and JP symbols for Yahoo JP."""
        for sym in tv_symbols:
            self.tv_client.add_symbol(sym)
        with self.yahoojp_scraper.lock:
            self.yahoojp_scraper.symbols.update(jp_symbols)

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
                    for client_state in self._client_states.values():
                        client_state.pop(key, None)
            self.pts_store.pop(symbol, None)
            self.previous_pts_store.pop(symbol, None)
            for client_state in self._client_pts_states.values():
                client_state.pop(symbol, None)

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
        """
        deltas: dict[str, TickerPayload] = {}
        with self.store_lock:
            prev_store = (
                self.previous_store if client_id is None else self._client_states.get(client_id)
            )
            if prev_store is None:
                return {}
            for sym, current in self.market_store.items():
                prev = prev_store.get(sym)
                if not prev or prev["price"] != current["price"] or prev["change"] != current["change"]:
                    deltas[sym] = current
                    prev_store[sym] = dict(current)
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
        with self.store_lock:
            prev_store = (
                self.previous_pts_store
                if client_id is None
                else self._client_pts_states.get(client_id)
            )
            if prev_store is None:
                return {}
            for sym, current in self.pts_store.items():
                prev = prev_store.get(sym)
                if not prev or prev["price"] != current["price"]:
                    deltas[sym] = current
                    prev_store[sym] = dict(current)
        if deltas:
            logger.debug(
                "[Realtime Engine] PTS deltas generated for %d symbol(s): %s",
                len(deltas),
                list(deltas.keys()),
            )
        return deltas

    def _pts_poll_interval(self) -> float:
        """Poll faster during the PTS session, slower otherwise."""
        return PTS_POLL_INTERVAL_ACTIVE if is_pts_session() else PTS_POLL_INTERVAL_IDLE

    def _fetch_pts_with_fallback(self, symbol: str) -> TickerPayload | None:
        """Fetch a PTS quote: Yahoo JP first, then SBI as fallback."""
        payload = self.yahoojp_scraper.fetch_pts_symbol(symbol)
        if not payload:
            try:
                payload = self.sbi_scraper.fetch_pts_quote(symbol)
                if payload:
                    logger.debug("[Realtime Engine] SBI PTS fallback quote for %s", symbol)
            except Exception as exc:
                logger.debug("SBI PTS fallback failed for %s: %s", symbol, exc)
        return payload

    def _pts_worker_loop(self) -> None:
        while self.running:
            interval = self._pts_poll_interval()
            with self.yahoojp_scraper.lock:
                target_symbols = list(self.yahoojp_scraper.symbols)
            for sym in target_symbols:
                if not self.running:
                    break
                payload = self._fetch_pts_with_fallback(sym)
                if payload:
                    self._handle_pts_update(payload)
                time.sleep(0.4)  # Polite intra-request delay
            time.sleep(interval)

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


# Global Singleton Instance
realtime_market_engine = RealtimeMarketEngine()
