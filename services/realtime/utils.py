# services/realtime/utils.py
"""Helper utilities, constants, and parsing functions for realtime engine."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import requests

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None  # type: ignore[assignment]
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)


def _rt_sleep(seconds: float) -> None:
    import sys
    rt_mod = sys.modules.get("services.realtime_engine")
    if rt_mod is not None and hasattr(rt_mod, "time") and hasattr(rt_mod.time, "sleep"):
        rt_mod.time.sleep(seconds)
    else:
        time.sleep(seconds)


def _rt_time() -> float:
    import sys
    rt_mod = sys.modules.get("services.realtime_engine")
    if rt_mod is not None and hasattr(rt_mod, "time") and hasattr(rt_mod.time, "time"):
        return float(rt_mod.time.time())
    return time.time()


def _interruptible_sleep(
    should_continue: Callable[[], bool], seconds: float, step: float = 0.5
) -> None:
    """Sleep in short slices while ``should_continue()`` stays truthy."""
    deadline = time.monotonic() + seconds
    while should_continue():
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        _rt_sleep(min(step, remaining))


_SCRAPER_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="135", "Google Chrome";v="135"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _create_cffi_session() -> Any:
    """Create a curl_cffi session with Chrome TLS/JA3 impersonation and Chromium Client Hints."""
    if HAS_CURL_CFFI and cffi_requests is not None:
        for imp in ("chrome120", "chrome124", "chrome131", "chrome133a", "safari18_0", "edge101"):
            try:
                cffi_sess: Any = cffi_requests.Session(impersonate=imp)
                cffi_sess.headers.update(_SCRAPER_HEADERS)
                return cffi_sess
            except Exception as exc:
                logger.debug("Failed creating curl_cffi Session with %s: %s", imp, exc)
                continue
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


class WebSocketOpcode8Filter(logging.Filter):
    """Filter out opcode=8 close frames from websocket library logger."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("opcode=8" in msg or "0x03e8" in msg or "goodbye" in msg.lower())


logging.getLogger("websocket").addFilter(WebSocketOpcode8Filter())

JST = ZoneInfo("Asia/Tokyo")
TickerPayload = dict[str, Any]

JP_MORNING_START = dt_time(9, 0)
JP_MORNING_END = dt_time(11, 30)
JP_AFTERNOON_START = dt_time(12, 30)
JP_AFTERNOON_END = dt_time(15, 30)

PTS_SESSION_START_DAY = dt_time(8, 20)
PTS_SESSION_END_DAY = dt_time(16, 0)
PTS_SESSION_START_NIGHT = dt_time(16, 30)
PTS_SESSION_END_NIGHT = dt_time(23, 59)
PTS_POLL_INTERVAL_ACTIVE = 10.0
PTS_POLL_INTERVAL_IDLE = 15.0
PTS_CACHE_STALE_SECONDS = 300.0


def _dedupe_pts_symbols(*symbol_collections: Iterable[str]) -> list[str]:
    """Merge symbol collections for PTS polling, keeping the first-seen form."""
    normalized: dict[str, str] = {}
    for collection in symbol_collections:
        for sym in collection:
            if not sym:
                continue
            base = sym[:-2] if sym.endswith((".T", ".t")) else sym
            normalized.setdefault(base, sym)
    return list(normalized.values())


def is_pts_session(now: datetime | None = None) -> bool:
    """Check whether the JP PTS (daytime or night after-hours) session is active."""
    import sys
    rt_mod = sys.modules.get("services.realtime_engine")
    if rt_mod is not None and "is_pts_session" in rt_mod.__dict__:
        target = rt_mod.__dict__["is_pts_session"]
        if target is not is_pts_session:
            return bool(target(now) if now is not None else target())

    if now is None:
        now = datetime.now(JST)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    try:
        from utils.market_utils import is_jp_market_holiday

        if is_jp_market_holiday(now.date()):
            return False
    except Exception:  # nosec B110
        pass
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


SCRAPER_BLOCK_STATUS_CODES = (401, 402, 403, 429, 439)
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
    import sys
    rt_mod = sys.modules.get("services.realtime_engine")
    if rt_mod is not None and "_is_scraper_blocked" in rt_mod.__dict__:
        target = rt_mod.__dict__["_is_scraper_blocked"]
        if target is not _is_scraper_blocked:
            return bool(target())

    market = _scraper_market_state()
    if market is None or not hasattr(market, "is_scraper_blocked"):
        return False
    return bool(market.is_scraper_blocked())


def _mark_scraper_blocked_from_status(
    status_code: int | None, propagate_to_yfinance: bool = False
) -> None:
    """Record a global scraper block when an upstream returns a block code."""
    if status_code not in SCRAPER_BLOCK_STATUS_CODES:
        return
    market = _scraper_market_state()
    if market is None or not hasattr(market, "mark_scraper_blocked"):
        return
    try:
        market.mark_scraper_blocked(propagate_to_yfinance=propagate_to_yfinance)
    except Exception as exc:
        logger.debug("Failed to mark scraper blocked: %s", exc)


def _is_yf_rate_limited() -> bool:
    """True while yfinance is inside a rate-limit cooldown."""
    import sys
    rt_mod = sys.modules.get("services.realtime_engine")
    if rt_mod is not None and "_is_yf_rate_limited" in rt_mod.__dict__:
        target = rt_mod.__dict__["_is_yf_rate_limited"]
        if target is not _is_yf_rate_limited:
            return bool(target())

    try:
        from app_state import app_state

        market = getattr(app_state, "market", None)
        if market is None or not hasattr(market, "is_yf_rate_limited"):
            return False
        return bool(market.is_yf_rate_limited())
    except Exception as exc:
        logger.debug("Failed to read yfinance rate-limit state: %s", exc)
        return False


def _normalize_tv_symbol(symbol: str) -> str:
    """Normalize a watchlist ticker into the exchange-prefixed TradingView form."""
    if not symbol:
        return symbol
    if ":" in symbol:
        return symbol
    tv_symbol = symbol.replace("-", ".")
    from utils.tradingview_mapper import get_tradingview_symbol

    return get_tradingview_symbol(tv_symbol)


def _get_yfinance_previous_close(symbol: str) -> float | None:
    """Resolve yfinance previous close price for a symbol (JP, US, or index)."""
    if not symbol:
        return None
    try:
        from utils.stock_payload import get_stock_previous_close

        prev = get_stock_previous_close(symbol)
        if prev is not None and prev > 0:
            return prev
        if ":" in symbol:
            bare = symbol.split(":")[-1]
            prev = get_stock_previous_close(bare)
            if prev is not None and prev > 0:
                return prev
        if "." in symbol and not symbol.endswith(".T"):
            dash_sym = symbol.replace(".", "-")
            prev = get_stock_previous_close(dash_sym)
            if prev is not None and prev > 0:
                return prev
    except Exception as exc:
        logger.debug("Failed getting stock previous close for %s: %s", symbol, exc)
    return None


def _tv_purge_key_variants(symbol: str) -> list[str]:
    """Candidate ``market_store`` keys referring to the same ticker as *symbol*."""
    normalized = _normalize_tv_symbol(symbol)
    bare = normalized.split(":")[-1] if ":" in normalized else normalized
    variants = {
        symbol,
        bare,
        symbol.replace("-", "."),
        bare.replace("-", "."),
        symbol.replace(".", "-"),
        bare.replace(".", "-"),
    }
    if symbol.endswith((".T", ".t")):
        variants.add(symbol[:-2])
    elif symbol.isdigit():
        variants.add(f"{symbol}.T")
    if bare.endswith((".T", ".t")):
        variants.add(bare[:-2])
    elif bare.isdigit():
        variants.add(f"{bare}.T")
    if ":" in normalized:
        variants.add(normalized)
        variants.add(normalized.replace(".", "-"))
    return [v for v in variants if v]


_ESCAPED_QUOTE_FIELDS = ("price", "priceChange", "priceChangeRate", "priceChangePercent")
_ESCAPED_QUOTE_RES = {
    field: re.compile(r'\\"' + field + r'\\":{\\"value\\":\\"([^\\\\"]+)\\"')
    for field in _ESCAPED_QUOTE_FIELDS
}
_PTS_PRICE_DATA_MARKERS = (
    re.compile(r'ptsPriceData\\":\s*\{'),
    re.compile(r'"ptsPriceData"\s*:\s*\{'),
)
_PTS_TRADING_FLAG_RE = re.compile(r'ptsTradingFlag\\":(true|false)')
_PTS_TRADING_FLAG_UNESCAPED_RE = re.compile(r'"ptsTradingFlag"\s*:\s*(true|false)')
_NEXT_DATA_SCRIPT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


def _extract_pts_price_data(html: str) -> str | None:
    """Extract the balanced JSON object region of ``ptsPriceData`` from a Yahoo JP page."""
    for marker in _PTS_PRICE_DATA_MARKERS:
        for m in marker.finditer(html):
            start = m.end() - 1
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
    """Extract ``ptsPriceData`` field values from a raw segment."""
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
    fields = dict(re.findall(r'"([a-zA-Z]+)":"([^"]*)"', unescaped))
    for m in re.finditer(r'"([a-zA-Z]+)"\s*:\s*\{\s*"value"\s*:\s*"([^"]*)"', unescaped):
        fields.setdefault(m.group(1), m.group(2))
    return fields


def _extract_next_data_quotes(html: str) -> dict[str, str] | None:
    """Parse quote fields from the page's ``__NEXT_DATA__`` JSON blob."""
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
    """Check if Tokyo Stock Exchange (TSE) is open (weekday + session hours)."""
    if now is None:
        now = datetime.now(JST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (JP_MORNING_START <= t < JP_MORNING_END) or (
        JP_AFTERNOON_START <= t < JP_AFTERNOON_END
    )
