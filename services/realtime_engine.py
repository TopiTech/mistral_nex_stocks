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


def _create_cffi_session() -> Any:
    """Create a curl_cffi session with Chrome 120 TLS/JA3 impersonation and Chromium Client Hints."""
    if HAS_CURL_CFFI and cffi_requests is not None:
        try:
            cffi_sess: Any = cffi_requests.Session(impersonate="chrome120")
            cffi_sess.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
            return cffi_sess
        except Exception as exc:
            logger.debug("Failed creating curl_cffi Session: %s", exc)
    fallback_sess: Any = requests.Session()
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


# Yahoo JP embeds quote data as escaped JSON inside JS strings. The quotes are
# escaped (\") while the object braces are not, e.g. \"price\":{\"value\":\"2,983.5\"}.
# The legacy page format (plain JSON, e.g. "price":"3500.0") is kept as a fallback.
_ESCAPED_QUOTE_FIELDS = ("price", "priceChange", "priceChangeRate", "priceChangePercent")
_ESCAPED_QUOTE_RES = {
    field: re.compile(r'\\"' + field + r'\\":{\\"value\\":\\"([^\\\\"]+)\\"')
    for field in _ESCAPED_QUOTE_FIELDS
}
_PTS_PRICE_DATA_RE = re.compile(r'ptsPriceData\\":{(.{0,1500}?)}}')
_PTS_PRICE_DATA_UNESCAPED_RE = re.compile(r'"ptsPriceData"\s*:\s*{(.{0,1500}?)}}')
_PTS_TRADING_FLAG_RE = re.compile(r'ptsTradingFlag\\":(true|false)')
_PTS_TRADING_FLAG_UNESCAPED_RE = re.compile(r'"ptsTradingFlag"\s*:\s*(true|false)')


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
            if not HAS_WEBSOCKET_CLIENT:
                logger.info("websocket-client not available. TV WS worker sleeping...")
                time.sleep(10.0)
                continue

            from utils.market_utils import is_market_open
            # Skip TradingView WS connection during US market closed hours
            if not is_market_open("us"):
                time.sleep(5.0)
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
        self.session = _create_cffi_session()
        # ``requests.Session`` is not thread-safe: the regular worker and the
        # PTS worker share this scraper, so HTTP calls are serialized here.
        self._http_lock = threading.Lock()
        self.lock = threading.Lock()
        # Per-symbol consecutive-failure tracking for page-structure detection,
        # tracked separately for regular and PTS scrapes (key = (symbol, kind)).
        self._consecutive_failures: dict[tuple[str, str], int] = {}
        self._structure_change_reported: set[tuple[str, str]] = set()
        self._structure_change_reported_time: dict[tuple[str, str], float] = {}

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

    def _fetch_kabutan_symbol(self, symbol: str) -> TickerPayload | None:
        """Fetch regular JP stock quote from Kabutan (kabutan.jp)."""
        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"https://kabutan.jp/stock/?code={clean_code}"
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            with self._http_lock:
                resp = self.session.get(url, headers=headers, timeout=5.0)
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
        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"https://kabutan.jp/stock/?code={clean_code}"
        headers = {
            "User-Agent": secrets.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            with self._http_lock:
                resp = self.session.get(url, headers=headers, timeout=5.0)
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
            logger.info("[Yahoo JP Scraper] Failed scrape for %s: %s", symbol, e)
        self._record_fetch_failure(symbol)
        return None

    def fetch_pts_symbol(self, symbol: str) -> TickerPayload | None:
        """Fetch the PTS (after-hours) quote for a JP symbol (Kabutan first, then Yahoo JP)."""
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
            with self._http_lock:
                resp = self.session.get(url, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                html = resp.text
                data_match = _PTS_PRICE_DATA_RE.search(html) or _PTS_PRICE_DATA_UNESCAPED_RE.search(html)
                if data_match:
                    segment = data_match.group(1).replace('\\"', '"')
                    fields = dict(re.findall(r'"([a-zA-Z]+)":"([^"]*)"', segment))
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

                interval = self._poll_interval()

                with self.lock:
                    target_symbols = list(self.symbols)

                for sym in target_symbols:
                    if not self.running:
                        break
                    payload = self._fetch_regular_with_fallback(sym)
                    if payload and self.on_update_callback:
                        self.on_update_callback(payload)
                    time.sleep(0.1)  # Polite intra-request delay

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
    # Auto-recovery for consecutive failure pauses
    RECOVERY_COOLDOWN_SECONDS = 600.0

    def __init__(self) -> None:
        self.session = _create_cffi_session()
        # ``requests.Session`` is not thread-safe: the Yahoo regular worker and
        # the PTS worker may both consult this fallback, so HTTP is serialized.
        self._http_lock = threading.Lock()
        self.lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self._structure_change_reported: set[str] = set()
        self._structure_change_reported_time: dict[str, float] = {}
        self._last_failure_time: dict[str, float] = {}

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
                    "[SBI Scraper] %d consecutive %s failures for %s: the page structure "
                    "may have changed or the symbol may be invalid. Fallback paused "
                    "for this symbol until a successful fetch.",
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

class MinkabuScraper:
    """Minkabu (minkabu.jp) stock scraper — fallback provider for JP stocks & PTS."""

    BASE_URL = "https://minkabu.jp/stock/"
    STRUCTURE_CHANGE_THRESHOLD = 3
    FALLBACK_COOLDOWN_SECONDS = 60.0
    RECOVERY_COOLDOWN_SECONDS = 600.0

    def __init__(self) -> None:
        self.session = _create_cffi_session()
        self._http_lock = threading.Lock()
        self.lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self._structure_change_reported: set[str] = set()
        self._structure_change_reported_time: dict[str, float] = {}
        self._last_failure_time: dict[str, float] = {}

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
                    "[Minkabu Scraper] %d consecutive %s failures for %s: pausing fallback",
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

    def fetch_quote(self, symbol: str) -> TickerPayload | None:
        """Fetch the regular session quote for *symbol* from Minkabu."""
        if self._is_in_cooldown(symbol):
            return None
        code = symbol.replace(".T", "").replace(".t", "")
        url = f"{self.BASE_URL}{code}"
        try:
            with self._http_lock:
                resp = self.session.get(url, timeout=5.0)
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
        self.store_lock = threading.Lock()
        # Per-listener delta cursors: each SSE client keeps its own last-seen
        # snapshot so a price change is delivered to EVERY connected client
        # rather than only whichever one happens to poll first. The number of
        # registered clients is bounded by MAX_SSE_LISTENERS in the caller.
        self._client_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_pts_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_last_seen: dict[str, float] = {}
        self._client_counter = 0

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

    def _purge_stale_clients(self, ttl_seconds: float = 600.0) -> None:
        """Purge client cursors that have been inactive for > 10 minutes."""
        now = time.time()
        stale_ids = [
            cid for cid, last_seen in self._client_last_seen.items()
            if (now - last_seen) > ttl_seconds
        ]
        for cid in stale_ids:
            self._client_states.pop(cid, None)
            self._client_pts_states.pop(cid, None)
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
            self._client_last_seen[client_id] = time.time()
            return client_id

    def unregister_client(self, client_id: str) -> None:
        """Drop a client's delta cursors (stream closed / disconnected)."""
        with self.store_lock:
            self._client_states.pop(client_id, None)
            self._client_pts_states.pop(client_id, None)
            self._client_last_seen.pop(client_id, None)

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

            def _priority_fetch():
                try:
                    payload = self.yahoojp_scraper._fetch_regular_with_fallback(symbol)
                    if payload:
                        self._handle_producer_update(payload)
                    if is_pts_session():
                        pts_payload = self._fetch_pts_with_fallback(symbol)
                        if pts_payload:
                            self._handle_pts_update(pts_payload)
                except Exception as e:
                    logger.debug("Priority fetch failed for %s: %s", symbol, e)
            threading.Thread(target=_priority_fetch, daemon=True, name=f"PriorityFetch_{symbol}").start()

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
            if client_id is not None:
                self._client_last_seen[client_id] = time.time()
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
                if not self.yahoojp_scraper._is_startup_ready():
                    time.sleep(1.0)
                    continue

                if not is_pts_session():
                    time.sleep(5.0)
                    continue

                interval = self._pts_poll_interval()
                with self.yahoojp_scraper.lock:
                    target_symbols = list(self.yahoojp_scraper.symbols)
                for sym in target_symbols:
                    if not self.running:
                        break
                    payload = self._fetch_pts_with_fallback(sym)
                    if payload:
                        self._handle_pts_update(payload)
                    time.sleep(0.1)  # Polite intra-request delay
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


# Global Singleton Instance
realtime_market_engine = RealtimeMarketEngine()
