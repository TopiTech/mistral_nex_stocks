"""
stock_payload.py - Stock payload building, portfolio metrics, chart helpers, and response utilities.

Extracted from app_helpers.py to reduce module complexity.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd
from flask import jsonify

from app_state import app_state
from error_codes import ErrorCode, get_error_message
from sectors import PREDEFINED_INDUSTRIES, PREDEFINED_SECTORS, PREDEFINED_NAMES, PREDEFINED_MARKET_CAPS
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


# ---------------------------------------------------------------------------
# Stock container helpers
# ---------------------------------------------------------------------------


def _get_stock_container(market: Optional[str]):
    """Return the mutable user-stock container for a normalized market."""
    if market == "us":
        return app_state.market.user_us
    if market == "jp":
        return app_state.market.user_jp
    if market == "idx":
        return app_state.market.user_idx
    return None


def _default_stock_names(market: str) -> Dict[str, str]:
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
    return bool(
        container is not None and (symbol in container or symbol in _default_stock_names(market))
    )


# ---------------------------------------------------------------------------
# Info helpers
# ---------------------------------------------------------------------------


def get_stock_info_cached(symbol: str) -> dict:
    """Retrieve stock info including fundamentals with yfinance rate-limit protection and caching.

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
    neg_key = f"info_{symbol}__failed"
    if _has_cached_key(neg_key, 600):
        return {}

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
    except Exception as exc:
        logger.debug("Disk cache get failed for %s: %s", symbol, exc)

    if isinstance(cached_disk, dict) and cached_disk:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache[short_cache_key] = dict(cached_disk)
        try:
            _set_cached_value(f"info_{symbol}", dict(cached_disk), 86400)
        except Exception:
            pass
        return dict(cached_disk)

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
                except Exception:
                    pass
                return {}

            fast: Dict[str, Any] = {}
            fast = app_state.stock_provider.get_fast_info(symbol)
            if app_state.market.is_yf_rate_limited() or not fast:
                merged = dict(fast)
                if merged:
                    return merged
                _set_cached_value(neg_key, True, 600)
                return {}

            full: Dict[str, Any] = {}
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
                _set_cached_value(neg_key, True, 600)
                return {}

            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache[short_cache_key] = dict(merged)

            # Save to disk cache
            try:
                app_state.stock_disk_cache.set(disk_key, dict(merged))
            except Exception as disk_exc:
                logger.debug("Disk cache set failed for %s: %s", symbol, disk_exc)

            return dict(merged)
        except Exception as exc:
            logger.debug("yfinance info fetch failed for %s: %s", symbol, exc)
            _set_cached_value(neg_key, True, 600)
            # Try to return expired disk cache on exception
            try:
                fallback_disk = app_state.stock_disk_cache.get(disk_key, ignore_ttl=True)
                if isinstance(fallback_disk, dict) and fallback_disk:
                    return dict(fallback_disk)
            except Exception:
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
        get_stock_info_cached(symbol)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Async stock info fetch failed for %s: %s", symbol, exc)


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
            shares = float(name_or_dict.get("shares", 0.0))
        except (TypeError, ValueError):
            shares = 0.0
        try:
            avg_price = float(name_or_dict.get("avg_price", 0.0))
        except (TypeError, ValueError):
            avg_price = 0.0
        fx_val = name_or_dict.get("avg_fx_rate")
        if fx_val is not None:
            try:
                avg_fx_rate = float(fx_val)
            except (TypeError, ValueError):
                avg_fx_rate = None
    return name, shares, avg_price, avg_fx_rate


def _compute_price_metrics(hist, symbol):
    """Extract price, change, and percentage from history DataFrame."""
    price = float(hist["Close"].iloc[-1])
    if len(hist) == 1:
        prev = price
    else:
        prev = float(hist["Close"].iloc[-2])

    if pd.isna(price) or pd.isna(prev) or price <= 0 or prev <= 0:
        logger.warning(
            "Stock %s: invalid non-positive close price (price=%s, prev=%s)",
            symbol,
            price,
            prev,
        )
        return None, None, None

    change = price - prev
    pct = (change / prev) * 100 if prev else 0
    return _fmt(price), _fmt(change), _fmt(pct)


def _build_chart_ohlc_data(df, chart_data_limit=100, ohlc_data_limit=365):
    """Build chart_data and ohlc_data arrays from a DataFrame with MA columns."""

    def _safe_ohlc(val, fallback=0.0):
        try:
            f = float(val)
            return f if pd.notna(f) else fallback
        except (TypeError, ValueError):
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
            vol = (
                int(float(rd.get("Volume", 0)))
                if rd.get("Volume") is not None and pd.notna(rd.get("Volume"))
                else 0
            )
        except (ValueError, TypeError):
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


def _build_portfolio_metrics(shares, avg_price, avg_fx_rate, currency, current_price):
    """Calculate portfolio value and P&L in JPY."""
    portfolio_val_raw = shares * current_price
    portfolio_pl_raw = (current_price - avg_price) * shares

    if currency == "USD":
        usdjpy_info = app_state.market.current_indices_cache.get("USDJPY", {})
        current_fx = 150.0
        try:
            if usdjpy_info and usdjpy_info.get("price") not in (None, "--", ""):
                current_fx = float(usdjpy_info["price"])
        except (ValueError, TypeError):
            pass
        value_jpy = portfolio_val_raw * current_fx
        cost_jpy = (shares * avg_price) * (avg_fx_rate if avg_fx_rate is not None else current_fx)
        pl_jpy = value_jpy - cost_jpy
    else:
        value_jpy = portfolio_val_raw
        pl_jpy = portfolio_pl_raw
    return _fmt(value_jpy), _fmt(pl_jpy)


def build_stock_payload(symbol, name_or_dict, market, hist, snapshot_ts_ms=None, lightweight=False):
    """Build a complete stock payload dictionary from historical data."""
    hist = normalize_history_frame(hist, inplace=True)
    if len(hist) < 1:
        logger.warning("Stock %s: insufficient historical data (len=%d)", symbol, len(hist))
        return None

    name, shares, avg_price, avg_fx_rate = _extract_portfolio_fields(name_or_dict)

    try:
        price_fmt, change_fmt, pct_fmt = _compute_price_metrics(hist, symbol)
        if price_fmt is None:
            return None

        df = hist.copy()
        df["MA5"] = df["Close"].rolling(window=5, min_periods=1).mean()
        df["MA25"] = df["Close"].rolling(window=25, min_periods=1).mean()
        chart, ohlc_data = _build_chart_ohlc_data(df)

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
                except Exception:
                    pass
        else:
            info = get_stock_info_cached(symbol) or {}

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

        return {
            "symbol": symbol,
            "name": choose_display_name(symbol, name, info),
            "market": market,
            "snapshot_ts_ms": snapshot_value,
            "price": price_fmt,
            "change": change_fmt,
            "change_percent": pct_fmt,
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
            "dividend_yield": (
                round(float(info["dividendYield"]), 4)
                if info.get("dividendYield") is not None
                else None
            ),
            "eps": _fmt(info.get("earningsPerShare")),
            "market_cap": info.get("marketCap") or PREDEFINED_MARKET_CAPS.get(symbol),
            "beta": _fmt(info.get("beta")),
            "fifty_two_week_high": _fmt(info.get("fiftyTwoWeekHigh")),
            "fifty_two_week_low": _fmt(info.get("fiftyTwoWeekLow")),
            "target_mean_price": _fmt(info.get("targetMeanPrice")),
            "recommendation": info.get("recommendationKey"),
            "next_earnings": next_earnings,
            "shares_outstanding": info.get("sharesOutstanding"),
            "float_shares": info.get("floatShares"),
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
            "free_cashflow": info.get("freeCashflow"),
            "operating_cashflow": info.get("operatingCashflow"),
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


# Portfolio fields that identify personal holdings. Stripped from unauthenticated
# public market-data responses so a local process cannot scrape asset allocation
# from /api/stocks or the SSE stream without an authenticated write path (H-3).
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


def _resolve_stocks_for_response(*, include_portfolio: bool = False):
    """Resolve stock cache for API response (current > target > empty).

    Args:
        include_portfolio: When False (default), strip shares/avg_price and related
            personal holding fields from every row. Set True only for trusted
            authenticated handlers that intentionally need portfolio data.
    """
    empty: dict[str, list[Any]] = {"us": [], "jp": [], "idx": []}
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
        current_rows = c_val if isinstance(c_val, list) else []
        t_val = target.get(market)
        target_rows = t_val if isinstance(t_val, list) else []
        rows = list(current_rows if current_rows else target_rows)
        if include_portfolio:
            resolved[market] = rows
        else:
            resolved[market] = [_strip_portfolio_fields(row) for row in rows]
    return resolved


def _resolve_indices_for_response():
    """Resolve indices cache for API response (current > target > empty)."""
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
    empty: Dict[str, List] = {"us": [], "jp": [], "idx": []}
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
    deadline = time.time() + max(0.0, timeout_sec)
    while time.time() < deadline:
        if check_ready():
            return True
        time.sleep(poll_interval)
    return False


# ---------------------------------------------------------------------------
# Error response
# ---------------------------------------------------------------------------


def error_response(error_code: ErrorCode, status_code: int = 400, details: Optional[dict] = None):
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
                "error_code": int(error_code),
                "message": message,
                "details": sanitized_details,
            }
        ),
        status_code,
    )
