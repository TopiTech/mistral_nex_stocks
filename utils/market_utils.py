"""
market_utils.py - Market open/close detection and yfinance slot management.

Extracted from app_helpers.py to reduce module complexity.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from app_state import app_state
from utils.caching import get_cached

logger = logging.getLogger(__name__)


def _is_market_session_open(
    t, morning_start, morning_end, afternoon_start=None, afternoon_end=None
):
    """Check if the current time falls within a trading session."""
    if morning_start <= t <= morning_end:
        return True
    if afternoon_start and afternoon_end:
        if afternoon_start <= t <= afternoon_end:
            return True
    return False



def _market_status_symbol(market_type):
    """Return the yfinance symbol used to query market status for a given market type."""
    if market_type == "jp":
        return "^N225"
    if market_type in ("us", "idx"):
        return "^GSPC"
    return None



def _market_state_from_metadata(metadata):
    """Extract market state (REGULAR/CLOSED) from yfinance history metadata."""
    if not isinstance(metadata, dict):
        return None

    raw_state = metadata.get("marketState") or metadata.get("market_state")
    if isinstance(raw_state, str):
        normalized_state = raw_state.strip().upper()
        if normalized_state == "REGULAR":
            return "REGULAR"
        if normalized_state:
            return "CLOSED"

    current_period = metadata.get("currentTradingPeriod")
    if isinstance(current_period, dict):
        regular_period = current_period.get("regular")
        if isinstance(regular_period, dict):
            regular_start_raw = regular_period.get("start")
            regular_end_raw = regular_period.get("end")
            if regular_start_raw is None or regular_end_raw is None:
                return None
            try:
                regular_start = float(regular_start_raw)
                regular_end = float(regular_end_raw)
            except (TypeError, ValueError):
                return None
            now_ts = time.time()
            return "REGULAR" if regular_start <= now_ts < regular_end else "CLOSED"

    return None



def _fetch_live_market_state(market_type):
    """Fetch live market state from yfinance metadata."""
    symbol = _market_status_symbol(market_type)
    if not symbol:
        return None

    try:
        ticker = safe_get_ticker(symbol)
        if not ticker:
            return None

        try:
            metadata = ticker.get_history_metadata()
        except Exception:
            metadata = getattr(ticker, "history_metadata", None)

        return _market_state_from_metadata(metadata)
    except Exception as exc:
        logger.debug(
            "Live market state fetch failed for %s (%s): %s",
            market_type,
            symbol,
            exc,
        )
        return None



def is_market_open(market_type, bypass_cache=False, ignore_weekend=False):
    """Determine whether the market is currently open.

    Priority:
    1. Weekend check (immediate False unless ``ignore_weekend`` is set)
    2. Yahoo Finance live metadata (REGULAR/CLOSED) with 5-minute caching
    3. Time-based heuristic (JST for JP, ET for US)

    Args:
        market_type: "us", "jp", or "idx".
        bypass_cache: Skip the 5-minute live-state cache when True.
        ignore_weekend: When True, skip the weekend early-return so the live
            state / time-based fallback is consulted even on Sat/Sun. Used by
            tests and any caller that wants the "true" market state rather than
            the optimization that treats weekends as always-closed.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Weekend check (optimization to skip live queries when market is 100% closed)
    if not ignore_weekend:
        if market_type == "jp":
            try:
                jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
            except (ImportError, ValueError, KeyError):
                jst = (now_utc + timedelta(hours=9)).replace(tzinfo=None)
            if jst.weekday() >= 5:
                app_state.update_market_status(market_type, "CLOSED")
                return False
        elif market_type in ("us", "idx"):
            try:
                ny = now_utc.astimezone(ZoneInfo("America/New_York"))
            except Exception:
                ny = (now_utc + timedelta(hours=-5)).replace(tzinfo=None)
            if ny.weekday() >= 5:
                app_state.update_market_status(market_type, "CLOSED")
                return False

    # 2. Live query (or cache check) with 5-minute TTL (300 seconds)
    live_state = None
    if bypass_cache:
        live_state = _fetch_live_market_state(market_type)
    else:
        live_state = get_cached(
            f"market_state_{market_type}",
            lambda: _fetch_live_market_state(market_type),
            duration=300,
            valid_func=lambda value: value in ("REGULAR", "CLOSED"),
        )

    if live_state in ("REGULAR", "CLOSED"):
        app_state.update_market_status(market_type, live_state)
        return live_state == "REGULAR"

    # 3. Fallback: time-based weekday session check
    if market_type == "jp":
        try:
            jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
        except (ImportError, ValueError, KeyError):
            jst = (now_utc + timedelta(hours=9)).replace(tzinfo=None)
        return _is_market_session_open(
            jst.time(), dt_time(9, 0), dt_time(11, 30),
            dt_time(12, 30), dt_time(15, 0),
        )

    if market_type in ("us", "idx"):
        try:
            ny = now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            ny = (now_utc + timedelta(hours=-5)).replace(tzinfo=None)
        return _is_market_session_open(ny.time(), dt_time(9, 30), dt_time(16, 0))

    return True



def acquire_yfinance_slot() -> bool:
    """Gate a yfinance request against the app-level rate limiter.

    Returns:
        True if a request may proceed, False if rate-limited.

    Note: inter-request *spacing* (the actual pacing that prevents 429/401)
    is enforced solely by ``YFinanceSessionManager.custom_request``. Having two
    independent pacers previously made effective spacing unpredictable, so this
    function is intentionally a gate only — no sleep, no jitter, no decay. The
    adaptive interval in the session manager is the single source of truth.
    """
    with app_state.market.yfinance_lock:
        if app_state.is_yf_rate_limited():
            return False
    return True



def safe_get_ticker(symbol):
    """Wrap yf.Ticker instantiation with defensive error handling via stock_provider."""
    return app_state.stock_provider.get_ticker(symbol)
