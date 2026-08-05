# services/realtime_engine.py
"""Realtime Market Data Engine for Mistral NeX Stocks.

Supports:
1. TradingView WebSocket Client (US Stocks, Indices, ETFs)
2. Yahoo! Finance JP Scraper (JP Stocks & Indices with Smart Polling)
3. SBI Securities Scraper (JP Stock Quotes & PTS fallback)
4. Unified Market Engine (Producer-Consumer Queue & Delta Update Dispatcher)
"""

from __future__ import annotations

import json
import logging
import random
import re
import string
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
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


def is_jp_market_open(now: datetime | None = None) -> bool:
    """Check if Tokyo Stock Exchange (TSE) is currently open."""
    if now is None:
        now = datetime.now(JST)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    t = now.time()
    morning_open = datetime.strptime("09:00", "%H:%M").time()  # noqa: DTZ007
    morning_close = datetime.strptime("11:30", "%H:%M").time()  # noqa: DTZ007
    afternoon_open = datetime.strptime("12:30", "%H:%M").time()  # noqa: DTZ007
    afternoon_close = datetime.strptime("15:30", "%H:%M").time()  # noqa: DTZ007

    return (morning_open <= t <= morning_close) or (afternoon_open <= t <= afternoon_close)



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
        self.session_id = "qs_" + "".join(random.choices(string.ascii_lowercase, k=12))
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
                    if symbol and values and self.on_update_callback:
                        price = values.get("lp")
                        change = values.get("ch")
                        change_percent = values.get("chp")
                        volume = values.get("volume")
                        if price is not None:
                            payload: TickerPayload = {
                                "symbol": symbol,
                                "price": float(price),
                                "change": float(change) if change is not None else 0.0,
                                "change_percent": float(change_percent) if change_percent is not None else 0.0,
                                "volume": int(volume) if volume is not None else 0,
                                "source": "tradingview",
                                "updated_at": time.time(),
                            }
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
            except Exception:
                pass


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


    def __init__(self, symbols: list[str] | None = None, on_update_callback: Callable[[TickerPayload], None] | None = None) -> None:
        self.symbols: set[str] = set(symbols or [])
        self.on_update_callback = on_update_callback
        self.running = False
        self.thread: threading.Thread | None = None
        self.session = requests.Session()
        self.lock = threading.Lock()

    def fetch_jp_symbol(self, symbol: str) -> TickerPayload | None:
        """Scrape Yahoo JP quote for a single symbol (e.g. 7203.T or 9984)."""
        clean_code = symbol.replace(".T", "").replace(".t", "")
        url = f"{self.BASE_URL}{clean_code}.T"
        headers = {
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            resp = self.session.get(url, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                html = resp.text
                price_match = re.search(r'"price":\s*"?([^",\s}]+)"?', html) or re.search(r'class="[^"]*price[^"]*"[^>]*>([^<\s]+)', html)
                change_match = re.search(r'"priceChange":\s*"?([^",\s}]+)"?', html) or re.search(r'class="[^"]*priceChange[^"]*"[^>]*>([^<\s]+)', html)
                change_pct_match = re.search(r'"priceChangePercent":\s*"?([^",\s}]+)"?', html) or re.search(r'class="[^"]*priceChangePercent[^"]*"[^>]*>([^<\s]+)', html)

                if price_match:
                    price_str = price_match.group(1).replace(",", "").replace('"', '').strip()
                    price = float(price_str)
                    change = float(change_match.group(1).replace(",", "").replace('"', '').replace("+", "").strip()) if change_match else 0.0
                    change_pct = float(change_pct_match.group(1).replace(",", "").replace('"', '').replace("%", "").replace("+", "").strip()) if change_pct_match else 0.0

                    return {
                        "symbol": f"{clean_code}.T",
                        "price": price,
                        "change": change,
                        "change_percent": change_pct,
                        "volume": 0,
                        "source": "yahoojp",
                        "updated_at": time.time(),
                    }
        except Exception as e:
            logger.debug("Failed Yahoo JP scrape for %s: %s", symbol, e)
        return None

    def _worker_loop(self) -> None:
        while self.running:
            is_open = is_jp_market_open()
            interval = 2.5 if is_open else 30.0  # Smart Polling

            with self.lock:
                target_symbols = list(self.symbols)

            for sym in target_symbols:
                if not self.running:
                    break
                payload = self.fetch_jp_symbol(sym)
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
# 3. SBI Securities Scraper
# ============================================================================

class SBISecuritiesScraper:
    """SBI Securities stock scraper for order book / PTS quote supplementation."""

    def __init__(self, on_update_callback: Callable[[TickerPayload], None] | None = None) -> None:
        self.on_update_callback = on_update_callback
        self.running = False
        self.thread: threading.Thread | None = None

    def fetch_sbi_pts_quote(self, symbol: str) -> TickerPayload | None:
        # Placeholder / Stub for SBI PTS authenticated scraping
        return None

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


# ============================================================================
# 4. Realtime Market Engine & SSE Delta Dispatcher
# ============================================================================

class RealtimeMarketEngine:
    """Core Market Engine maintaining unified market state & dispatching SSE deltas."""

    def __init__(self) -> None:
        self.market_store: dict[str, TickerPayload] = {}
        self.previous_store: dict[str, TickerPayload] = {}
        self.store_lock = threading.Lock()

        # Instantiate Producers
        self.tv_client = TradingViewWSClient(on_update_callback=self._handle_producer_update)
        self.yahoojp_scraper = YahooJPRealtimeScraper(on_update_callback=self._handle_producer_update)
        self.sbi_scraper = SBISecuritiesScraper(on_update_callback=self._handle_producer_update)

        self.running = False

    def _handle_producer_update(self, payload: TickerPayload) -> None:
        symbol = payload["symbol"]
        with self.store_lock:
            self.market_store[symbol] = payload

    def register_symbols(self, tv_symbols: list[str], jp_symbols: list[str]) -> None:
        """Register US / Index / ETF symbols for TV and JP symbols for Yahoo JP."""
        for sym in tv_symbols:
            self.tv_client.add_symbol(sym)
        with self.yahoojp_scraper.lock:
            self.yahoojp_scraper.symbols.update(jp_symbols)

    def get_market_snapshot(self) -> dict[str, TickerPayload]:
        """Return a copy of the current unified market snapshot."""
        with self.store_lock:
            return dict(self.market_store)

    def get_market_deltas(self) -> dict[str, TickerPayload]:
        """Calculate and return changed symbols since last check."""
        deltas: dict[str, TickerPayload] = {}
        with self.store_lock:
            for sym, current in self.market_store.items():
                prev = self.previous_store.get(sym)
                if not prev or prev["price"] != current["price"] or prev["change"] != current["change"]:
                    deltas[sym] = current
                    self.previous_store[sym] = dict(current)
        return deltas

    def start(self) -> None:
        if not self.running:
            self.running = True
            logger.info("Starting RealtimeMarketEngine producers...")
            self.tv_client.start()
            self.yahoojp_scraper.start()
            self.sbi_scraper.start()

    def stop(self) -> None:
        self.running = False
        self.tv_client.stop()
        self.yahoojp_scraper.stop()
        self.sbi_scraper.stop()


# Global Singleton Instance
realtime_market_engine = RealtimeMarketEngine()
