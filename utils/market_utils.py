"""
market_utils.py - Market open/close detection and yfinance slot management.

Extracted from app_helpers.py to reduce module complexity.
"""

import logging
import random
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



def is_market_open(market_type, bypass_cache=False):
    """Determine whether the market is currently open.

    Priority:
    1. Yahoo Finance live metadata (REGULAR/CLOSED)
    2. Time-based heuristic (JST for JP, ET for US)
    3. Cached state
    """
    now_utc = datetime.now(timezone.utc)

    live_state = None
    if bypass_cache:
        live_state = _fetch_live_market_state(market_type)
    else:
        live_state = get_cached(
            f"market_state_{market_type}",
            lambda: _fetch_live_market_state(market_type),
            duration=5,
            valid_func=lambda value: value in ("REGULAR", "CLOSED"),
        )

    if live_state in ("REGULAR", "CLOSED"):
        app_state.update_market_status(market_type, live_state)
        return live_state == "REGULAR"

    # Fallback: time-based heuristic
    if market_type == "jp":
        try:
            jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
        except (ImportError, ValueError, KeyError):
            jst = (now_utc + timedelta(hours=9)).replace(tzinfo=None)
        if jst.weekday() >= 5:
            return False
        return _is_market_session_open(
            jst.time(), dt_time(9, 0), dt_time(11, 30),
            dt_time(12, 30), dt_time(15, 0),
        )

    if market_type in ("us", "idx"):
        try:
            ny = now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            # Last-resort fallback: use fixed offset -5 (EST, no DST)
            ny = (now_utc + timedelta(hours=-5)).replace(tzinfo=None)
        if ny.weekday() >= 5:
            return False
        return _is_market_session_open(ny.time(), dt_time(9, 30), dt_time(16, 0))

    return True



def acquire_yfinance_slot() -> bool:
    """Acquire a yfinance request slot with jitter and adaptive interval.

    Returns:
        True if slot acquired, False if rate-limited.
    """
    wait_time = 0.0
    with app_state.market.yfinance_lock:
        if app_state.is_yf_rate_limited():
            return False

        # Adaptive interval: decay back to baseline when not rate-limited
        min_interval = app_state.market.yfinance_min_interval_sec
        adaptive = app_state.market.yfinance_adaptive_interval_sec
        if adaptive > min_interval:
            app_state.market.yfinance_adaptive_interval_sec = max(
                min_interval,
                adaptive - 1.0,
            )

        effective_interval = max(
            min_interval,
            app_state.market.yfinance_adaptive_interval_sec,
        )
        # Add jitter: +/- 10% to appear more human-like
        jitter_factor = getattr(app_state.market, 'yfinance_jitter_factor', 0.1)
        jittered_interval = effective_interval * (1.0 + random.uniform(-jitter_factor, jitter_factor))
        jittered_interval = max(jittered_interval, min_interval * 0.5)

        now = time.time()
        elapsed = now - app_state.market.yfinance_last_request_ts
        if elapsed < jittered_interval:
            wait_time = jittered_interval - elapsed
        app_state.market.yfinance_last_request_ts = now + wait_time

    if wait_time > 0.0:
        time.sleep(wait_time)
    return True



def safe_get_ticker(symbol):
    """Wrap yf.Ticker instantiation with defensive error handling via stock_provider."""
    return app_state.stock_provider.get_ticker(symbol)
