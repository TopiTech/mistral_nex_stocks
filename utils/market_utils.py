import calendar
import logging
import time
from datetime import UTC, datetime, timedelta
from datetime import date as dt_date
from datetime import time as dt_time
from datetime import timedelta as dt_timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app_state import app_state
from utils.caching import get_cached

logger = logging.getLogger(__name__)


def _vernal_equinox_day(year: int) -> int:
    """Calculate Vernal Equinox Day (春分の日) day of month for Japan."""
    if year <= 1979:
        return int(20.8357 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    if year <= 2099:
        return int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    return int(21.8510 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _autumnal_equinox_day(year: int) -> int:
    """Calculate Autumnal Equinox Day (秋分の日) day of month for Japan."""
    if year <= 1979:
        return int(23.2588 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    if year <= 2099:
        return int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    return int(24.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _get_nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> int:
    """Get day of month for the N-th weekday (0=Monday, ..., 6=Sunday)."""
    first_weekday = calendar.weekday(year, month, 1)
    offset = (weekday - first_weekday) % 7
    return 1 + offset + (n - 1) * 7


def is_jp_market_holiday(target_date: Any = None) -> bool:
    """Determine whether the specified date is a Japanese stock exchange holiday (JPX).

    Includes:
    - Year-end / New Year holidays (Dec 31, Jan 1, Jan 2, Jan 3)
    - Japanese National Holidays (国民の祝日) & Substitute Holidays (振替休日)
    - Citizen's Holidays (国民の休日)
    """
    if target_date is None:
        try:
            target_date = datetime.now(ZoneInfo("Asia/Tokyo")).date()
        except Exception:
            target_date = (datetime.now(UTC) + timedelta(hours=9)).date()
    elif hasattr(target_date, "astimezone"):
        try:
            target_date = target_date.astimezone(ZoneInfo("Asia/Tokyo")).date()
        except Exception:
            target_date = target_date.date()
    elif hasattr(target_date, "date") and callable(target_date.date):
        target_date = target_date.date()

    month = target_date.month
    day = target_date.day
    year = target_date.year

    # 1. Year-end / New Year holidays (東証市場休業日: 12/31 - 1/3)
    if (month == 12 and day == 31) or (month == 1 and day in (1, 2, 3)):
        return True

    # 2. Compute national holidays for the given year
    holidays: set[tuple[int, int]] = set()

    # Fixed holidays
    holidays.add((1, 1))  # 元日
    holidays.add((2, 11))  # 建国記念の日
    if year >= 2020:
        holidays.add((2, 23))  # 天皇誕生日
    holidays.add((3, _vernal_equinox_day(year)))  # 春分の日
    holidays.add((4, 29))  # 昭和の日
    holidays.add((5, 3))  # 憲法記念日
    holidays.add((5, 4))  # みどりの日
    holidays.add((5, 5))  # こどもの日
    holidays.add((8, 11))  # 山の日
    holidays.add((9, _autumnal_equinox_day(year)))  # 秋分の日
    holidays.add((11, 3))  # 文化の日
    holidays.add((11, 23))  # 勤労感謝の日

    # Happy Monday holidays
    holidays.add((1, _get_nth_weekday_of_month(year, 1, 0, 2)))  # 成人の日: 1月第2月曜日
    holidays.add((7, _get_nth_weekday_of_month(year, 7, 0, 3)))  # 海の日: 7月第3月曜日
    holidays.add((9, _get_nth_weekday_of_month(year, 9, 0, 3)))  # 敬老の日: 9月第3月曜日
    holidays.add((10, _get_nth_weekday_of_month(year, 10, 0, 2)))  # スポーツの日: 10月第2月曜日

    # Exact date check
    if (month, day) in holidays:
        return True

    # Check for Substitute Holiday (振替休日):
    # If a holiday fell on Sunday, the next non-holiday weekday is a substitute holiday.
    target_dt = dt_date(year, month, day)
    check_day = target_dt - dt_timedelta(days=1)
    while (check_day.month, check_day.day) in holidays:
        if check_day.weekday() == 6:  # Sunday
            return True
        check_day -= dt_timedelta(days=1)

    # Check for Citizen's Holiday (国民の休日):
    # A weekday sandwiched between two national holidays (e.g. Silver Week).
    prev_day = target_dt - dt_timedelta(days=1)
    next_day = target_dt + dt_timedelta(days=1)
    return (
        (prev_day.month, prev_day.day) in holidays
        and (next_day.month, next_day.day) in holidays
        and target_dt.weekday() != 6
    )


def _us_observed_holiday(year: int, month: int, day: int) -> dt_date:
    """Apply the NYSE observance rule for fixed-date holidays.

    NYSE observes fixed-date holidays on the actual weekday; when the date
    falls on a Saturday the market closes on the preceding Friday, and when
    it falls on a Sunday it closes on the following Monday.
    """
    holiday = dt_date(year, month, day)
    if holiday.weekday() == 5:  # Saturday
        return holiday - dt_timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday
        return holiday + dt_timedelta(days=1)
    return holiday


def _last_monday_of_month(year: int, month: int) -> dt_date:
    """Return the date of the last Monday in *month* (Memorial Day rule)."""
    last_day = calendar.monthrange(year, month)[1]
    date = dt_date(year, month, last_day)
    while date.weekday() != 0:  # 0 = Monday
        date -= dt_timedelta(days=1)
    return date


def _easter_sunday(year: int) -> dt_date:
    """Compute Easter Sunday via the Anonymous Gregorian algorithm (computus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return dt_date(year, month, day)


def is_us_market_holiday(target_date: Any = None) -> bool:
    """Determine whether the specified date is a US stock exchange holiday (NYSE).

    Implements the standard NYSE holiday calendar with the Saturday -> preceding
    Friday / Sunday -> following Monday observance rule:
      - New Year's Day, MLK Day, Washington's Birthday, Good Friday,
        Memorial Day, Juneteenth (since 2022), Independence Day, Labor Day,
        Thanksgiving, Christmas Day.

    Early-close (half-day) sessions are NOT covered here: a half day is still a
    trading day and is handled by the session-hours check in ``is_market_open``.
    """
    if target_date is None:
        try:
            target_date = datetime.now(ZoneInfo("America/New_York")).date()
        except Exception:
            target_date = (datetime.now(UTC) - timedelta(hours=5)).date()
    elif hasattr(target_date, "astimezone"):
        try:
            target_date = target_date.astimezone(ZoneInfo("America/New_York")).date()
        except Exception:
            target_date = target_date.date()
    elif hasattr(target_date, "date") and callable(target_date.date):
        target_date = target_date.date()

    year = target_date.year

    holidays: set[dt_date] = {
        _us_observed_holiday(year, 1, 1),  # New Year's Day
        dt_date(year, 1, _get_nth_weekday_of_month(year, 1, 0, 3)),  # MLK Day: 3rd Monday of January
        dt_date(year, 2, _get_nth_weekday_of_month(year, 2, 0, 3)),  # Washington's Birthday: 3rd Monday of February
        _easter_sunday(year) - dt_timedelta(days=2),  # Good Friday
        _last_monday_of_month(year, 5),  # Memorial Day: last Monday of May
        _us_observed_holiday(year, 7, 4),  # Independence Day
        dt_date(year, 9, _get_nth_weekday_of_month(year, 9, 0, 1)),  # Labor Day: 1st Monday of September
        dt_date(year, 11, _get_nth_weekday_of_month(year, 11, 3, 4)),  # Thanksgiving: 4th Thursday of November
        _us_observed_holiday(year, 12, 25),  # Christmas Day
    }
    if year >= 2022:
        holidays.add(_us_observed_holiday(year, 6, 19))  # Juneteenth (since 2022)
    # New Year's Day observance can fall on the last trading day of the previous
    # year (Jan 1 on a Saturday -> closed the preceding Friday).
    holidays.add(_us_observed_holiday(year + 1, 1, 1))

    return target_date in holidays


def _is_market_session_open(
    t, morning_start, morning_end, afternoon_start=None, afternoon_end=None
):
    """Check if the current time falls within a trading session."""
    # Session end timestamps are exclusive: at the published close time the
    # exchange is already closed (important for the JPX 15:30 boundary).
    if morning_start <= t < morning_end:
        return True
    return bool(afternoon_start and afternoon_end and afternoon_start <= t < afternoon_end)


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
    now_utc = datetime.now(UTC)

    # 1. Weekend check (optimization to skip live queries when market is 100% closed)
    if not ignore_weekend:
        if market_type == "jp":
            try:
                jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
            except (ImportError, ValueError, KeyError):
                jst = (now_utc + timedelta(hours=9)).replace(tzinfo=None)
            if jst.weekday() >= 5:
                app_state.market.update_market_status(market_type, "CLOSED")
                return False
        elif market_type in ("us", "idx"):
            try:
                ny = now_utc.astimezone(ZoneInfo("America/New_York"))
            except Exception:
                ny = (now_utc + timedelta(hours=-5)).replace(tzinfo=None)
            if ny.weekday() >= 5:
                app_state.market.update_market_status(market_type, "CLOSED")
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
        app_state.market.update_market_status(market_type, live_state)
        return live_state == "REGULAR"

    # 3. Fallback: time-based weekday session check
    if market_type == "jp":
        try:
            jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
        except (ImportError, ValueError, KeyError):
            jst = (now_utc + timedelta(hours=9)).replace(tzinfo=None)
        if is_jp_market_holiday(jst.date()):
            app_state.market.update_market_status(market_type, "CLOSED")
            return False
        return _is_market_session_open(
            jst.time(),
            dt_time(9, 0),
            dt_time(11, 30),
            dt_time(12, 30),
            dt_time(15, 30),
        )

    if market_type in ("us", "idx"):
        try:
            ny = now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            ny = (now_utc + timedelta(hours=-5)).replace(tzinfo=None)
        # Mirrors the JP holiday check: when Yahoo's live market metadata is
        # unavailable, NYSE holidays must still be reported CLOSED instead of
        # being treated as a regular weekday session.
        if is_us_market_holiday(ny.date()):
            app_state.market.update_market_status(market_type, "CLOSED")
            return False
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
        if app_state.market.is_yf_rate_limited():
            return False
    return True


def safe_get_ticker(symbol):
    """Wrap yf.Ticker instantiation with defensive error handling via stock_provider."""
    return app_state.stock_provider.get_ticker(symbol)
