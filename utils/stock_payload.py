"""
stock_payload.py - Stock payload building, portfolio metrics, chart helpers, and response utilities.

Extracted from app_helpers.py to reduce module complexity.
"""

import copy
import logging
import math
import time
from typing import Any

import pandas as pd
from flask import Response, jsonify

from app_state import app_state
from error_codes import ErrorCode, get_error_message
from sectors import (
    PREDEFINED_INDUSTRIES,
    PREDEFINED_MARKET_CAPS,
    PREDEFINED_NAMES,
    PREDEFINED_SECTORS,
)
from utils.caching import _has_cached_key, _set_cached_value, get_cached, peek_cached
from utils.market_utils import is_market_open
from utils.normalization import (
    _fmt,
    _fmt_vol,
    normalize_history_frame,
)
from utils.text_utils import _sanitize_error_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default stock lists
# ---------------------------------------------------------------------------

DEFAULT_US = {
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "META": "Meta",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "AMD": "AMD",
}

DEFAULT_JP = {
    "7203.T": "トヨタ自動車",
    "6758.T": "ソニーグループ",
    "9984.T": "ソフトバンクグループ",
    "8306.T": "三菱UFJ FG",
    "6861.T": "キーエンス",
    "6098.T": "リクルートHD",
    "9432.T": "NTT",
    "8035.T": "東京エレクトロン",
}

DEFAULT_IDX = {
    "^N225": "日経平均",
    "^DJI": "NYダウ",
    "^IXIC": "NASDAQ",
    "^GSPC": "S&P500",
    "USDJPY=X": "USDJPY",
    "EURJPY=X": "EURJPY",
    "^VIX": "VIX",
}


def get_default_symbols():
    """Return default symbols grouped by market."""
    return {
        "us": list(DEFAULT_US.keys()),
        "jp": list(DEFAULT_JP.keys()),
        "idx": list(DEFAULT_IDX.keys()),
    }


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def clear_yfinance_short_cache_prefix(prefix: str) -> None:
    """Remove symbol-scoped short-cache entries for yfinance helpers."""
    if not prefix:
        return
    with app_state.yfinance_short_cache_lock:
        keys_to_delete = [
            key
            for key in list(app_state.yfinance_short_cache.keys())
            if isinstance(key, str) and key.startswith(prefix)
        ]
        for key in keys_to_delete:
            app_state.yfinance_short_cache.pop(key, None)


def clear_yfinance_short_cache_key(key: str) -> None:
    """Remove a specific key from yfinance_short_cache."""
    if not key:
        return
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.pop(key, None)


# ---------------------------------------------------------------------------
# Stock container helpers
# ---------------------------------------------------------------------------


def _get_stock_container(market: str | None):
    """Return the mutable user-stock container for a normalized market."""
    if market == "us":
        return app_state.market.user_us
    if market == "jp":
        return app_state.market.user_jp
    if market == "idx":
        return app_state.market.user_idx
    return None


def _default_stock_names(market: str) -> dict[str, str]:
    """Return default stock name mappings for a market."""
    if market == "us":
        return DEFAULT_US
    if market == "jp":
        return DEFAULT_JP
    if market == "idx":
        return DEFAULT_IDX
    return {}


def _stock_is_default_or_user(symbol: str, market: str) -> bool:
    """Check if symbol exists in user or default stock lists for the given market."""
    container = _get_stock_container(market)
    return container is not None and (symbol in container or symbol in _default_stock_names(market))


# ---------------------------------------------------------------------------
# Info helpers
# ---------------------------------------------------------------------------


def get_stock_info_cached(symbol: str, *, cache_only: bool = False) -> dict:
    """Retrieve stock info including fundamentals with yfinance rate-limit protection and caching.

    When ``cache_only`` is True only the in-memory/disk caches are consulted and
    no network request is made (returns ``{}`` on cache miss). Request-thread
    fallback paths use this to avoid N+1 amplification when a batch download
    fails or yfinance is rate-limited.

    Strategy (2026-07 refactor):
      * ``fast_info`` (price, currency, market cap) is cheap and always refreshed
        when not rate-limited — it carries the live price we display.
      * ``t.info`` (quoteSummary) is the single most-flagged yfinance endpoint
        and is kept OUT of the per-sync hot path. It is only enriched when (a)
        not currently rate-limited AND (b) the stored result lacks fundamentals
        (i.e. we don't already have a merged fast+full result from the last 24h).
        Fundamentals therefore go ~24h stale instead of being re-fetched every
        cycle — cold-start bursts of quoteSummary calls were a primary 429/439
        driver.
    """
    short_cache_key = f"info_short_{symbol}"
    with app_state.yfinance_short_cache_lock:
        cached_short = app_state.yfinance_short_cache.get(short_cache_key)
    if isinstance(cached_short, dict):
        return dict(cached_short)

    disk_key = f"info_disk_{symbol}"
    # Try reading from disk cache first
    cached_disk = None
    try:
        cached_disk = app_state.stock_disk_cache.get(disk_key, ttl=86400)
    except (OSError, TypeError) as exc:
        logger.debug("Disk cache get failed for %s: %s", symbol, exc)

    if isinstance(cached_disk, dict) and cached_disk:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache[short_cache_key] = dict(cached_disk)
        try:
            _set_cached_value(f"info_{symbol}", dict(cached_disk), 86400)
        except (OSError, TypeError, ValueError):
            pass
        return dict(cached_disk)

    if (
        hasattr(app_state.market, "is_negative_cached_symbol")
        and app_state.market.is_negative_cached_symbol(symbol) is True
    ):
        return {}

    # cache_only mode never fetches: after the in-memory/disk cache misses there
    # is nothing left to serve, so bail out before the negative-cache / network
    # paths (which would otherwise perform real yfinance I/O).
    if cache_only:
        return {}

    # 2026-07 Refactor: Only check negative cache (failure avoidance) if no valid
    # cache exists in memory or on disk. Use the configurable NEGATIVE_CACHE_TTL
    # instead of the hardcoded 600s to avoid keeping the UI blocked in a loop
    # for too long after transient failures.
    from constants import NEGATIVE_CACHE_TTL

    neg_key = f"info_{symbol}__failed"
    if _has_cached_key(neg_key, NEGATIVE_CACHE_TTL):
        return {}

    # Fundamentals keys whose presence marks a "full" (fast+quoteSummary) result.
    _FUNDAMENTAL_KEYS = (
        "trailingPE",
        "dividendYield",
        "sector",
        "industry",
        "targetMeanPrice",
        "marketCap",
        "fiftyTwoWeekHigh",
    )

    def _fetch() -> dict:
        try:
            from utils.market_utils import acquire_yfinance_slot

            # Fallback if rate limited or slot acquisition fails
            rate_limited = app_state.market.is_yf_rate_limited()
            if rate_limited or not acquire_yfinance_slot():
                try:
                    fallback_disk = app_state.stock_disk_cache.get(disk_key, ignore_ttl=True)
                    if isinstance(fallback_disk, dict) and fallback_disk:
                        logger.info(
                            "yfinance rate-limited/slot acquisition failed; returning expired disk cache for %s",
                            symbol,
                        )
                        with app_state.yfinance_short_cache_lock:
                            app_state.yfinance_short_cache[short_cache_key] = dict(fallback_disk)
                        return dict(fallback_disk)
                except (OSError, TypeError):
                    pass
                return {}

            fast: dict[str, Any] = {}
            fast = app_state.stock_provider.get_fast_info(symbol)
            if app_state.market.is_yf_rate_limited() or not fast:
                fallback_quote = app_state.fallback_provider.get_latest_quote(symbol)
                if fallback_quote:
                    # 合成fast info
                    fast = {
                        "regularMarketPrice": fallback_quote.get("regularMarketPrice"),
                        "regularMarketPreviousClose": fallback_quote.get(
                            "regularMarketPreviousClose"
                        ),
                        "regularMarketOpen": fallback_quote.get("regularMarketOpen"),
                        "regularMarketDayHigh": fallback_quote.get("regularMarketDayHigh"),
                        "regularMarketDayLow": fallback_quote.get("regularMarketDayLow"),
                        "regularMarketVolume": fallback_quote.get("regularMarketVolume"),
                    }
                else:
                    merged = dict(fast)
                    if merged:
                        return merged
                    from constants import NEGATIVE_CACHE_TTL

                    _set_cached_value(neg_key, True, NEGATIVE_CACHE_TTL)
                    return {}

            full: dict[str, Any] = {}
            # Only hit quoteSummary when not blocked AND we don't already have a
            # merged fundamentals result cached. If fundamentals are stale, the
            # 24h cache still serves them until refreshed lazily/on-demand.
            prior = peek_cached(f"info_{symbol}", duration=86400)
            prior_is_full = isinstance(prior, dict) and any(k in prior for k in _FUNDAMENTAL_KEYS)
            if (
                not symbol.startswith("^")
                and not app_state.market.is_yf_rate_limited()
                and not prior_is_full
            ):
                try:
                    full = app_state.stock_provider.get_info(symbol) or {}
                except Exception as exc:
                    logger.debug("yfinance ticker.info failed for %s: %s", symbol, exc)

            merged = {**fast, **full}
            if not merged:
                from constants import NEGATIVE_CACHE_TTL

                _set_cached_value(neg_key, True, NEGATIVE_CACHE_TTL)
                return {}

            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache[short_cache_key] = dict(merged)

            # Save to disk cache
            try:
                app_state.stock_disk_cache.set(disk_key, dict(merged))
            except (OSError, TypeError) as disk_exc:
                logger.debug("Disk cache set failed for %s: %s", symbol, disk_exc)

            return dict(merged)
        except Exception as exc:
            logger.debug("yfinance info fetch failed for %s: %s", symbol, exc)
            from constants import NEGATIVE_CACHE_TTL

            _set_cached_value(neg_key, True, NEGATIVE_CACHE_TTL)
            # Try to return expired disk cache on exception
            try:
                fallback_disk = app_state.stock_disk_cache.get(disk_key, ignore_ttl=True)
                if isinstance(fallback_disk, dict) and fallback_disk:
                    return dict(fallback_disk)
            except (OSError, TypeError):
                pass
            return {}

    cached = get_cached(f"info_{symbol}", _fetch, duration=86400, valid_func=bool)
    return dict(cached) if isinstance(cached, dict) else {}


def fetch_stock_info_async(symbol: str) -> None:
    """Populate the stock-info short cache off the request thread.

    yfinance ``t.info`` / ``fast_info`` can block for seconds on a cache miss.
    Calling this from ``data_executor`` lets the request handler return
    ``fetching:True`` immediately (mirroring the history endpoint, H-2) instead
    of stalling a Flask worker. The result lands in ``info_short_{symbol}``,
    which ``get_stock_info_cached`` reads first, so the next poll returns it.
    """
    try:
        res = get_stock_info_cached(symbol)
        if not res:
            # Prevent client-side infinite polling when fetch fails (e.g. rate limit/outage).
            # Write a failed placeholder to short cache so subsequent polls return it and stop.
            short_cache_key = f"info_short_{symbol}"
            with app_state.yfinance_short_cache_lock:
                if short_cache_key not in app_state.yfinance_short_cache:
                    app_state.yfinance_short_cache[short_cache_key] = {
                        "failed": True,
                        "error": True,
                    }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Async stock info fetch failed for %s: %s", symbol, exc)


# ---------------------------------------------------------------------------
# Stock payload building
# ---------------------------------------------------------------------------


def choose_display_name(symbol, fallback_name, info):
    """Choose display name with priority: shortName > longName > displayName > fallback > symbol."""
    if isinstance(fallback_name, dict):
        fallback_name = fallback_name.get("name", "")
    info = info or {}
    return (
        info.get("shortName")
        or info.get("longName")
        or info.get("displayName")
        or fallback_name
        or PREDEFINED_NAMES.get(symbol)
        or symbol
    )


def _extract_portfolio_fields(name_or_dict):
    """Extract portfolio-related fields from name_or_dict (dict or str)."""
    shares = 0.0
    avg_price = 0.0
    avg_fx_rate = None
    name = name_or_dict.get("name", "") if isinstance(name_or_dict, dict) else name_or_dict

    if isinstance(name_or_dict, dict):
        try:
            val = float(name_or_dict.get("shares", 0.0))
            shares = val if math.isfinite(val) and val >= 0 else 0.0
        except (TypeError, ValueError, OverflowError):
            shares = 0.0
        try:
            val = float(name_or_dict.get("avg_price", 0.0))
            avg_price = val if math.isfinite(val) and val >= 0 else 0.0
        except (TypeError, ValueError, OverflowError):
            avg_price = 0.0
        fx_val = name_or_dict.get("avg_fx_rate")
        if fx_val is not None:
            try:
                val = float(fx_val)
                avg_fx_rate = val if math.isfinite(val) and val > 0 else None
            except (TypeError, ValueError, OverflowError):
                avg_fx_rate = None
    return name, shares, avg_price, avg_fx_rate


def _compute_price_metrics(hist, symbol, info=None):
    """Extract price, change, and percentage from history DataFrame and info dict."""
    try:
        price = float(hist["Close"].iloc[-1])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        logger.warning("Stock %s: close price cannot be converted to a finite number", symbol)
        return None, None, None, None
    prev = None
    if isinstance(info, dict):
        raw_prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
        if raw_prev is not None:
            try:
                p_val = float(raw_prev)
                if math.isfinite(p_val) and p_val > 0:
                    prev = p_val
            except (ValueError, TypeError, OverflowError):
                pass
    if prev is None:
        if len(hist) == 1:
            prev = price
        else:
            try:
                prev = float(hist["Close"].iloc[-2])
            except (KeyError, IndexError, TypeError, ValueError, OverflowError):
                logger.warning("Stock %s: previous close cannot be converted", symbol)
                return None, None, None, None

    if pd.isna(price) or pd.isna(prev) or price <= 0 or prev <= 0:
        logger.warning(
            "Stock %s: invalid non-positive close price (price=%s, prev=%s)",
            symbol,
            price,
            prev,
        )
        return None, None, None, None

    change = price - prev
    pct = (change / prev) * 100 if prev else 0
    # Return the RAW previous close (4th element) so callers can surface the
    # true previous close instead of deriving it from rounded price/change
    # (which injects up to 0.005 of rounding error into displayed/realtime
    # change values).
    return _fmt(price), _fmt(change), _fmt(pct), prev


def _finite_or_none(value, *, allow_negative=True, decimals=None):
    """Return a finite number or None, rejecting NaN/Inf from data sources.

    yfinance returns NaN (and occasionally Inf) for missing fundamentals such as
    ``dividendYield`` / ``marketCap``. Those values pass ``is not None`` / truthy
    checks and would otherwise be serialized by the SSE JSON stream, which uses
    ``json.dumps(..., allow_nan=False)`` and raises ``ValueError`` on them.

    Args:
        value: Raw value (number, np float, string, None).
        allow_negative: Keep negative numbers (meaningful for cash-flow fields).
        decimals: Optional rounding precision (e.g. 4 for dividend yield).
    """
    if value is None or isinstance(value, bool) or type(value).__name__ in ("bool_", "bool"):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(num):
        return None
    if not allow_negative and num <= 0:
        return None
    return round(num, decimals) if decimals is not None else num


def _build_chart_ohlc_data(df, chart_data_limit=100, ohlc_data_limit=365):
    """Build chart_data and ohlc_data arrays from a DataFrame with MA columns."""

    def _safe_ohlc(val, fallback=0.0):
        try:
            f = float(val)
            return f if math.isfinite(f) else fallback
        except (TypeError, ValueError, OverflowError):
            return fallback

    recent_df = df.reset_index()
    _DATE_COLUMN_CANDIDATES = ("Date", "date", "timestamp", "Time", "time", "Datetime")
    date_col = "Date"
    for col in recent_df.columns:
        col_str = str(col)
        if col_str in _DATE_COLUMN_CANDIDATES:
            date_col = col_str
            break
    else:
        for col in recent_df.columns:
            if hasattr(recent_df[col], "dtype") and "datetime" in str(recent_df[col].dtype).lower():
                date_col = col
                break
        else:
            date_col = recent_df.columns[0]

    chart = []
    ohlc_data = []
    chart_records = recent_df.to_dict("records")
    target_records = chart_records[-ohlc_data_limit:]
    num_records = len(target_records)

    for i, rd in enumerate(target_records):
        dt = rd.get(date_col)
        ts_ms = dt.timestamp() * 1000 if hasattr(dt, "timestamp") else str(dt)
        c_val = _safe_ohlc(rd.get("Close"))

        try:
            raw_volume = rd.get("Volume", 0)
            volume_float = (
                float(raw_volume)
                if raw_volume is not None and pd.notna(raw_volume)
                else 0.0
            )
            vol = int(volume_float) if math.isfinite(volume_float) else 0
        except (ValueError, TypeError, OverflowError):
            vol = 0

        ohlc_data.append(
            {
                "x": ts_ms,
                "o": _safe_ohlc(rd.get("Open")),
                "h": _safe_ohlc(rd.get("High")),
                "l": _safe_ohlc(rd.get("Low")),
                "c": c_val,
                "v": vol,
            }
        )

        if num_records - i <= chart_data_limit:
            label = dt.strftime("%m/%d") if hasattr(dt, "strftime") else str(dt)
            ma5_val = _safe_ohlc(rd.get("MA5"), fallback=None)
            ma25_val = _safe_ohlc(rd.get("MA25"), fallback=None)
            chart.append(
                {
                    "x": ts_ms,
                    "date": label,
                    "price": c_val,
                    "ma5": ma5_val,
                    "ma25": ma25_val,
                }
            )
    return chart, ohlc_data


def get_current_usdjpy_rate(
    default_rate: float = 150.0,
    max_age_sec: float = 24 * 3600,
) -> tuple[float, bool]:
    """Resolve the latest USDJPY FX rate with fallback chain (memory -> realtime -> disk -> default).

    Returns:
        (rate, is_estimated): Where is_estimated is True if resolved from default fallback or stale.
    """
    # 1. Check in-memory indices cache
    try:
        usdjpy_info = app_state.market.current_indices_cache.get("USDJPY", {})
        if (
            usdjpy_info
            and usdjpy_info.get("price") not in (None, "--", "")
            and not isinstance(usdjpy_info.get("price"), bool)
        ):
            fx = float(usdjpy_info["price"])
            if math.isfinite(fx) and fx > 0:
                return fx, False
    except (ValueError, TypeError, OverflowError):
        pass

    # 2. Check realtime market engine snapshot
    try:
        from services.realtime_engine import realtime_market_engine

        snapshot = realtime_market_engine.get_market_snapshot()
        rt_fx = snapshot.get("USDJPY") or snapshot.get("USDJPY=X")
        if (
            rt_fx
            and isinstance(rt_fx, dict)
            and rt_fx.get("price") is not None
            and not isinstance(rt_fx.get("price"), bool)
        ):
            fx_p = float(rt_fx["price"])
            if math.isfinite(fx_p) and fx_p > 0:
                return fx_p, False
    except (OSError, RuntimeError, ValueError, TypeError, OverflowError, KeyError) as exc:
        # R3: narrow + log
        logger.debug("Realtime FX snapshot lookup failed: %s", exc)

    # 3. Check app_state last known rate with freshness check
    now = time.time()
    try:
        raw_rate = getattr(app_state.market, "last_usdjpy_rate", 0.0)
        raw_ts = getattr(app_state.market, "last_usdjpy_rate_ts", 0.0)
        if isinstance(raw_rate, bool) or isinstance(raw_ts, bool):
            raise TypeError("bool not allowed")
        last_rate = float(raw_rate or 0.0)
        last_ts = float(raw_ts or 0.0)
        if (
            math.isfinite(last_rate)
            and last_rate > 0
            and math.isfinite(last_ts)
            and 0.0 < last_ts <= now + 300.0
            and (now - last_ts) <= max_age_sec
        ):
            return last_rate, False
    except (ValueError, TypeError, OverflowError):
        pass

    # 4. Check disk cache for recent history / info
    try:
        for disk_key in ("info_disk_USDJPY=X", "info_disk_USDJPY"):
            cached_disk = app_state.stock_disk_cache.get(disk_key, ttl=max_age_sec)
            if isinstance(cached_disk, dict):
                p_val = (
                    cached_disk.get("regularMarketPrice")
                    or cached_disk.get("previousClose")
                    or cached_disk.get("price")
                )
                if p_val is not None and not isinstance(p_val, bool):
                    p_float = float(p_val)
                    if math.isfinite(p_float) and p_float > 0:
                        return p_float, False
    except (OSError, RuntimeError, ValueError, TypeError, OverflowError, KeyError) as exc:
        # R3: narrow + log
        logger.debug("Disk cache FX lookup failed: %s", exc)

    # 5. Expired / fallback
    fallback_rate = default_rate
    try:
        last_rate = float(getattr(app_state.market, "last_usdjpy_rate", 0.0) or 0.0)
        if math.isfinite(last_rate) and last_rate > 0:
            fallback_rate = last_rate
    except (ValueError, TypeError, OverflowError):
        pass

    return fallback_rate, True


def _build_portfolio_metrics(shares, avg_price, avg_fx_rate, currency, current_price):
    """Calculate portfolio value and P&L in JPY."""
    portfolio_val_raw = shares * current_price
    portfolio_pl_raw = (current_price - avg_price) * shares

    if currency == "USD":
        current_fx, _ = get_current_usdjpy_rate(default_rate=150.0)
        value_jpy = portfolio_val_raw * current_fx
        cost_jpy = (shares * avg_price) * (avg_fx_rate if avg_fx_rate is not None else current_fx)
        pl_jpy = value_jpy - cost_jpy
    else:
        value_jpy = portfolio_val_raw
        pl_jpy = portfolio_pl_raw
    return _fmt(value_jpy), _fmt(pl_jpy)


def build_stock_payload(
    symbol: str,
    name_or_dict: Any,
    market: str,
    hist: pd.DataFrame,
    snapshot_ts_ms: int | None = None,
    lightweight: bool = False,
) -> dict[str, Any] | None:
    """Build a complete stock payload dictionary from historical data."""
    hist = normalize_history_frame(hist, inplace=True)
    if len(hist) < 1:
        logger.warning("Stock %s: insufficient historical data (len=%d)", symbol, len(hist))
        return None

    name, shares, avg_price, avg_fx_rate = _extract_portfolio_fields(name_or_dict)

    try:
        if lightweight:
            info = {}
            short_cache_key = f"info_short_{symbol}"
            with app_state.yfinance_short_cache_lock:
                cached_short = app_state.yfinance_short_cache.get(short_cache_key)
            if isinstance(cached_short, dict):
                info = dict(cached_short)
            else:
                try:
                    cached_disk = app_state.stock_disk_cache.get(f"info_disk_{symbol}", ttl=86400)
                    if isinstance(cached_disk, dict) and cached_disk:
                        info = dict(cached_disk)
                except (OSError, TypeError):
                    pass
        else:
            info = get_stock_info_cached(symbol) or {}

        price_fmt, change_fmt, pct_fmt, prev_close_raw = _compute_price_metrics(hist, symbol, info)
        if price_fmt is None:
            return None

        df = hist.copy()
        df["MA5"] = df["Close"].rolling(window=5, min_periods=1).mean()
        df["MA25"] = df["Close"].rolling(window=25, min_periods=1).mean()
        # Lightweight payloads (heatmap / screener rows) do not consume the
        # chart/OHLC series: building them converts the whole DataFrame into
        # Python dicts on every background sync cycle (365 rows x symbol count).
        # Skipping keeps the sync loop CPU-cheap while preserving the full
        # payload for the dashboard.
        if lightweight:
            chart, ohlc_data = [], []
        else:
            chart, ohlc_data = _build_chart_ohlc_data(df)

        market_state = "REGULAR" if is_market_open(market) else "CLOSED"
        if market == "us":
            currency = "USD"
        elif market == "jp":
            currency = "JPY"
        else:  # market == "idx"
            currency = info.get("currency") or "USD"

        # Fetch next earnings date (skip index tickers to avoid 404s)
        next_earnings = None
        if not symbol.startswith("^") and market != "idx":
            try:
                cal_cache_key = f"cal_{symbol}"
                from utils.caching import _get_cached_value

                cal = _get_cached_value(cal_cache_key, 3600)
                if isinstance(cal, dict):
                    e_dates = cal.get("Earnings Date")
                    if isinstance(e_dates, list) and e_dates:
                        next_earnings = e_dates[0]
                    elif isinstance(e_dates, str):
                        next_earnings = e_dates
            except Exception as exc:
                logger.debug("Failed to fetch calendar for %s: %s", symbol, exc)

        snapshot_value = int(snapshot_ts_ms if snapshot_ts_ms is not None else time.time() * 1000)

        current_price = float(price_fmt if price_fmt else 0)
        pf_value, pf_pl = _build_portfolio_metrics(
            shares, avg_price, avg_fx_rate, currency, current_price
        )

        from utils.tradingview_mapper import get_tradingview_symbol_meta, register_ticker_exchange

        exchange_raw = info.get("exchange") if isinstance(info, dict) else None
        tv_sym, _, resolved_prefix = get_tradingview_symbol_meta(symbol, exchange=exchange_raw)
        exchange_val = resolved_prefix or exchange_raw
        if exchange_val:
            register_ticker_exchange(symbol, exchange_val)

        return {
            "symbol": symbol,
            "name": choose_display_name(symbol, name, info),
            "market": market,
            "exchange": exchange_val,
            "tv_symbol": tv_sym,
            "snapshot_ts_ms": snapshot_value,
            "price": price_fmt,
            "change": change_fmt,
            "change_percent": pct_fmt,
            # Surface the true previous close (raw float, not 2-decimal rounded):
            # realtime producers derive change = price - previous_close, so
            # rounding here would inject up to 0.005 of error into live deltas.
            # ``prev_close_raw`` comes straight from ``_compute_price_metrics``
            # (the exchange-reported previousClose when available, else the prior
            # close), so it is the accurate basis for live change recomputation.
            "previous_close": prev_close_raw,
            "chart_data": chart,
            "ohlc_data": ohlc_data,
            "high": _fmt(hist["High"].iloc[-1]) if "High" in hist.columns else None,
            "low": _fmt(hist["Low"].iloc[-1]) if "Low" in hist.columns else None,
            "open": _fmt(hist["Open"].iloc[-1]) if "Open" in hist.columns else None,
            "volume": (_fmt_vol(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None),
            "currency": currency,
            "market_state": market_state,
            "shares": shares,
            "avg_price": avg_price,
            "avg_fx_rate": avg_fx_rate,
            "portfolio_value": pf_value,
            "portfolio_pl": pf_pl,
            "sector": info.get("sector") or PREDEFINED_SECTORS.get(symbol, "Other"),
            "industry": info.get("industry") or PREDEFINED_INDUSTRIES.get(symbol, "Other"),
            "pe_ratio": _fmt(info.get("trailingPE")),
            "forward_pe": _fmt(info.get("forwardPE")),
            "price_to_book": _fmt(info.get("priceToBook")),
            "dividend_yield": _finite_or_none(info.get("dividendYield"), decimals=4),
            "eps": _fmt(info.get("earningsPerShare")),
            "market_cap": _finite_or_none(info.get("marketCap"))
            or PREDEFINED_MARKET_CAPS.get(symbol),
            "beta": _fmt(info.get("beta")),
            "fifty_two_week_high": _fmt(info.get("fiftyTwoWeekHigh")),
            "fifty_two_week_low": _fmt(info.get("fiftyTwoWeekLow")),
            "target_mean_price": _fmt(info.get("targetMeanPrice")),
            "recommendation": info.get("recommendationKey"),
            "next_earnings": next_earnings,
            "shares_outstanding": _finite_or_none(info.get("sharesOutstanding")),
            "float_shares": _finite_or_none(info.get("floatShares")),
            "held_percent_insiders": _fmt(info.get("heldPercentInsiders")),
            "held_percent_institutions": _fmt(info.get("heldPercentInstitutions")),
            "short_ratio": _fmt(info.get("shortRatio")),
            "short_percent_of_float": _fmt(info.get("shortPercentOfFloat")),
            "fifty_day_average": _fmt(info.get("fiftyDayAverage")),
            "two_hundred_day_average": _fmt(info.get("twoHundredDayAverage")),
            "price_to_sales": _fmt(info.get("priceToSalesTrailing12Months")),
            "enterprise_to_ebitda": _fmt(info.get("enterpriseToEbitda")),
            "profit_margins": _fmt(info.get("profitMargins")),
            "return_on_equity": _fmt(info.get("returnOnEquity")),
            "debt_to_equity": _fmt(info.get("debtToEquity")),
            "free_cashflow": _finite_or_none(info.get("freeCashflow"), allow_negative=True),
            "operating_cashflow": _finite_or_none(
                info.get("operatingCashflow"), allow_negative=True
            ),
        }
    except (
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
        pd.errors.EmptyDataError,
    ) as exc:
        logger.error("Stock payload build failed (%s): %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Cache snapshot helpers
# ---------------------------------------------------------------------------


# Portfolio fields that identify personal holdings. Stripped from public
# market-data responses so they only travel through the separate CSRF-protected
# snapshot path (which is not a native-process authentication boundary; see
# SECURITY.md).
_PORTFOLIO_RESPONSE_FIELDS = (
    "shares",
    "avg_price",
    "avg_fx_rate",
    "portfolio_value",
    "portfolio_pl",
)


def _strip_portfolio_fields(row: Any) -> Any:
    """Return a shallow copy of a stock row without portfolio-sensitive keys."""
    if not isinstance(row, dict):
        return row
    sanitized = dict(row)
    for key in _PORTFOLIO_RESPONSE_FIELDS:
        sanitized.pop(key, None)
    return sanitized


def _attach_portfolio_fields(row: Any, holding: Any) -> Any:
    """Merge encrypted in-memory holdings into a market-data row."""
    if not isinstance(row, dict):
        return row
    merged = _strip_portfolio_fields(row)
    _, shares, avg_price, avg_fx_rate = _extract_portfolio_fields(holding)
    merged["shares"] = shares
    merged["avg_price"] = avg_price
    if avg_fx_rate is not None:
        merged["avg_fx_rate"] = avg_fx_rate

    try:
        current_price = float(merged.get("price") or 0.0)
        if not math.isfinite(current_price):
            current_price = 0.0
    except (TypeError, ValueError, OverflowError):
        current_price = 0.0
    currency = str(merged.get("currency") or "JPY")
    portfolio_value, portfolio_pl = _build_portfolio_metrics(
        shares, avg_price, avg_fx_rate, currency, current_price
    )
    merged["portfolio_value"] = portfolio_value
    merged["portfolio_pl"] = portfolio_pl
    return merged


def _resolve_stocks_for_response(*, include_portfolio: bool = False, real_data_only: bool = False):
    """Resolve stock cache for API response (current > target > empty, or target > current if real_data_only=True).

    Args:
        include_portfolio: When False (default), strip shares/avg_price and related
            personal holding fields from every row. Set True only for the
            CSRF-protected handler that intentionally returns portfolio data.
        real_data_only: When True, prefer raw scraped target_stocks_cache over
            interpolated current_stocks_cache (used for Mode 2 TV-SSE).
    """
    empty: dict[str, list[Any]] = {"us": [], "jp": [], "idx": []}
    holdings: dict[str, dict[str, Any]] = {"us": {}, "jp": {}, "idx": {}}
    if include_portfolio:
        # Snapshot holdings before taking the SSE cache lock. Other write paths
        # acquire user_stocks_lock before sse_data_lock, so avoiding nested locks
        # here preserves a single lock order and prevents deadlocks.
        with app_state.market.user_stocks_lock:
            holdings = {
                "us": copy.deepcopy(app_state.market.user_us),
                "jp": copy.deepcopy(app_state.market.user_jp),
                "idx": copy.deepcopy(app_state.market.user_idx),
            }
    with app_state.cache.sse_data_lock:
        current = (
            app_state.market.current_stocks_cache
            if isinstance(app_state.market.current_stocks_cache, dict)
            else empty
        )
        target = (
            app_state.market.target_stocks_cache
            if isinstance(app_state.market.target_stocks_cache, dict)
            else empty
        )
        resolved = {}
        for market in ("us", "jp", "idx"):
            c_val = current.get(market)
            current_rows = list(c_val) if isinstance(c_val, list) else []
            t_val = target.get(market)
            target_rows = list(t_val) if isinstance(t_val, list) else []
            if real_data_only:
                rows = list(target_rows if target_rows else current_rows)
            else:
                rows = list(current_rows if current_rows else target_rows)
            if include_portfolio:
                market_holdings = holdings.get(market, {})
                portfolio_rows = []
                for row in rows:
                    holding = None
                    if isinstance(row, dict):
                        symbol = row.get("symbol")
                        if isinstance(symbol, str):
                            holding = market_holdings.get(symbol)
                    portfolio_rows.append(_attach_portfolio_fields(row, holding))
                resolved[market] = portfolio_rows
            else:
                resolved[market] = [_strip_portfolio_fields(row) for row in rows]

        # Attach realtime market quotes and PTS quote data if cached in realtime_market_engine
        try:
            from services.realtime_engine import realtime_market_engine

            market_snapshot = realtime_market_engine.get_market_snapshot()
            if market_snapshot:
                for m_key in ("us", "jp", "idx"):
                    if not resolved.get(m_key):
                        continue
                    m_rows = []
                    for row in resolved[m_key]:
                        r_copy = dict(row)
                        sym = r_copy.get("symbol", "")
                        clean_sym = sym.replace(".T", "").replace(".t", "")
                        rt_info = (
                            market_snapshot.get(sym)
                            or market_snapshot.get(f"{clean_sym}.T")
                            or market_snapshot.get(clean_sym)
                            or market_snapshot.get(sym.replace("-", "."))
                        )
                        if (
                            rt_info
                            and rt_info.get("price") is not None
                            and isinstance(rt_info["price"], (int, float))
                            and not isinstance(rt_info["price"], bool)
                            and rt_info["price"] > 0
                        ):
                            r_copy["price"] = rt_info["price"]
                            if (
                                rt_info.get("change") is not None
                                and isinstance(rt_info["change"], (int, float))
                                and not isinstance(rt_info["change"], bool)
                            ):
                                r_copy["change"] = rt_info["change"]
                            if (
                                rt_info.get("change_percent") is not None
                                and isinstance(rt_info["change_percent"], (int, float))
                                and not isinstance(rt_info["change_percent"], bool)
                            ):
                                r_copy["change_percent"] = rt_info["change_percent"]
                            if (
                                rt_info.get("volume") is not None
                                and isinstance(rt_info["volume"], (int, float))
                                and not isinstance(rt_info["volume"], bool)
                                and rt_info["volume"] > 0
                            ):
                                r_copy["volume"] = rt_info["volume"]
                            if rt_info.get("source"):
                                r_copy["source"] = rt_info["source"]
                            if include_portfolio and r_copy.get("shares"):
                                try:
                                    sh = float(r_copy.get("shares") or 0.0)
                                    ap = float(r_copy.get("avg_price") or 0.0)
                                    afx = r_copy.get("avg_fx_rate")
                                    cur = str(r_copy.get("currency") or "JPY")
                                    p_val, p_pl = _build_portfolio_metrics(
                                        sh, ap, afx, cur, float(rt_info["price"])
                                    )
                                    r_copy["portfolio_value"] = p_val
                                    r_copy["portfolio_pl"] = p_pl
                                except (TypeError, ValueError, OverflowError):
                                    pass
                        m_rows.append(r_copy)
                    resolved[m_key] = m_rows

            pts_snapshot = realtime_market_engine.get_pts_snapshot()
            if resolved.get("jp"):
                jp_rows = []
                for row in resolved["jp"]:
                    r_copy = dict(row)
                    sym = r_copy.get("symbol", "")
                    clean_sym = sym.replace(".T", "").replace(".t", "")
                    pts_info = (
                        pts_snapshot.get(sym)
                        or pts_snapshot.get(f"{clean_sym}.T")
                        or pts_snapshot.get(clean_sym)
                    )
                    if pts_info and pts_info.get("price"):
                        r_copy["pts_price"] = pts_info.get("price")
                        r_copy["pts_change"] = pts_info.get("change")
                        r_copy["pts_trading"] = pts_info.get("pts_trading", False)
                        r_copy["pts_time"] = pts_info.get("pts_time", "")
                    else:
                        if sym:
                            realtime_market_engine.register_symbol(sym, "jp")
                    jp_rows.append(r_copy)
                resolved["jp"] = jp_rows
        except Exception as exc:
            logger.debug("Failed to resolve PTS snapshot for response: %s", exc)
    return resolved


def _resolve_indices_for_response():
    """Resolve indices cache for API response (current > target > empty)."""
    with app_state.cache.sse_data_lock:
        current = (
            app_state.market.current_indices_cache
            if isinstance(app_state.market.current_indices_cache, dict)
            else {}
        )
        target = (
            app_state.market.target_indices_cache
            if isinstance(app_state.market.target_indices_cache, dict)
            else {}
        )
        if current:
            return dict(current)
        return dict(target)


def _has_ready_indices_snapshot() -> bool:
    """Check if indices cache has data ready."""
    with app_state.cache.sse_data_lock:
        current = (
            app_state.market.current_indices_cache
            if isinstance(app_state.market.current_indices_cache, dict)
            else {}
        )
        target = (
            app_state.market.target_indices_cache
            if isinstance(app_state.market.target_indices_cache, dict)
            else {}
        )
        return bool(current) or bool(target)


def _has_ready_stocks_snapshot() -> bool:
    """Check if stocks cache has data ready."""
    empty: dict[str, list] = {"us": [], "jp": [], "idx": []}
    with app_state.cache.sse_data_lock:
        current = (
            app_state.market.current_stocks_cache
            if isinstance(app_state.market.current_stocks_cache, dict)
            else empty
        )
        target = (
            app_state.market.target_stocks_cache
            if isinstance(app_state.market.target_stocks_cache, dict)
            else empty
        )
        for market in ("us", "jp", "idx"):
            c_val = current.get(market)
            current_rows = c_val if isinstance(c_val, list) else []
            t_val = target.get(market)
            target_rows = t_val if isinstance(t_val, list) else []
            if current_rows or target_rows:
                return True
    return False


def _wait_for_initial_market_snapshot(
    snapshot_type: str, timeout_sec: float = 6.0, poll_interval: float = 0.25
) -> bool:
    """Wait briefly for the first market snapshot so the initial page load does not look empty."""
    from app_bg import schedule_sync_all_stocks_now

    check_ready = (
        _has_ready_indices_snapshot if snapshot_type == "indices" else _has_ready_stocks_snapshot
    )
    if check_ready():
        return True

    schedule_sync_all_stocks_now()

    # If first sync has already been attempted (either failed or succeeded),
    # do not block the request thread at all. This prevents starvation (H-8).
    if getattr(app_state.market, "first_sync_attempted", False):
        return check_ready()

    deadline = time.time() + max(0.0, timeout_sec)
    while time.time() < deadline:
        if check_ready():
            return True
        # If the background sync finished while we were waiting, stop waiting
        if getattr(app_state.market, "first_sync_attempted", False):
            break
        time.sleep(poll_interval)
    return check_ready()


# ---------------------------------------------------------------------------
# Error response
# ---------------------------------------------------------------------------


def error_response(
    error_code: ErrorCode, status_code: int = 400, details: dict | None = None
) -> tuple[Response, int]:
    """Return a unified JSON error response."""
    message = get_error_message(error_code, lang="ja")
    sanitized_details = {}
    if details:
        for k, v in details.items():
            sanitized_details[k] = _sanitize_error_message(v) if isinstance(v, str) else v
    return (
        jsonify(
            {
                "ok": False,
                "error": message,
                "error_flag": True,
                "code": str(error_code) if error_code is not None else None,
                "error_code": int(error_code),
                "message": message,
                "details": sanitized_details,
            }
        ),
        status_code,
    )


def get_stock_previous_close(symbol: str) -> float | None:
    """Return the yfinance-derived previous close price for a symbol, if available."""
    if not symbol:
        return None
    from app_state import app_state

    # 1. Previous-close cache: maintained by the sync path (_process_fetched_stocks)
    #    and realtime producer updates. Read without sse_data_lock so TradingView
    #    WS deltas never contend with SSE stream serialization.
    try:
        cached = app_state.market.get_previous_close_cached(symbol)
        if cached is not None:
            return cached
        if symbol.endswith(".T"):
            cached = app_state.market.get_previous_close_cached(symbol[:-2])
        else:
            cached = app_state.market.get_previous_close_cached(f"{symbol}.T")
        if cached is not None:
            return cached
    except Exception as exc:
        logger.debug("Failed looking up previous_close cache for %s: %s", symbol, exc)

    # 2. Fall back to target_stocks_cache / current_stocks_cache (cache miss only).
    try:
        with app_state.cache.sse_data_lock:
            for store in (
                app_state.market.target_stocks_cache,
                app_state.market.current_stocks_cache,
            ):
                if not isinstance(store, dict):
                    continue
                for market in ("us", "jp", "idx"):
                    rows = store.get(market, [])
                    if isinstance(rows, list):
                        for row in rows:
                            if isinstance(row, dict) and row.get("symbol") in (
                                symbol,
                                f"{symbol}.T",
                                symbol.replace(".T", ""),
                            ):
                                prev = row.get("previous_close")
                                if prev is not None:
                                    try:
                                        pval = float(prev)
                                        if math.isfinite(pval) and pval > 0:
                                            return pval
                                    except (TypeError, ValueError, OverflowError):
                                        pass
                                p = row.get("price")
                                c = row.get("change")
                                if p is not None and c is not None:
                                    try:
                                        pval = float(p) - float(c)
                                        if math.isfinite(pval) and pval > 0:
                                            return pval
                                    except (TypeError, ValueError, OverflowError):
                                        pass
    except Exception as exc:
        logger.debug("Failed looking up previous_close from stocks cache for %s: %s", symbol, exc)

    # 3. Try yfinance short cache info
    try:
        short_cache_key = f"info_short_{symbol}"
        with app_state.yfinance_short_cache_lock:
            cached_info = app_state.yfinance_short_cache.get(short_cache_key)
        if isinstance(cached_info, dict):
            raw_prev = cached_info.get("previousClose") or cached_info.get(
                "regularMarketPreviousClose"
            )
            if raw_prev is not None:
                try:
                    pval = float(raw_prev)
                    if math.isfinite(pval) and pval > 0:
                        return pval
                except (TypeError, ValueError, OverflowError):
                    pass
    except Exception as exc:
        logger.debug("Failed looking up previous_close from short cache for %s: %s", symbol, exc)

    return None
