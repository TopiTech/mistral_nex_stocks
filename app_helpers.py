import hashlib
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional
import pandas as pd

logger = logging.getLogger(__name__)
from flask import jsonify, request
from werkzeug.exceptions import BadRequest

from app_state import app_state
from constants import (
    MAX_JSON_SIZE,
)
from error_codes import ErrorCode, get_error_message
from sectors import PREDEFINED_SECTORS, PREDEFINED_INDUSTRIES

from utils.storage import load_user_stocks, save_user_stocks, USER_STOCKS_FILE  # noqa: F401

# Import refactored functions for backward compatibility (Facade Pattern)
# We use # noqa: F401 to prevent linters from removing these re-exports.
from utils.networking import (  # noqa: F401
    _normalize_extension_origin,
    _load_allowed_extension_origins,
    get_allowed_cors_origins,
    require_trusted_state_changing_request,
    _is_allowed_shutdown_origin,
    _is_loopback_ip,
    _is_local_request,
)
from utils.normalization import (  # noqa: F401
    VALID_MARKETS,
    SYMBOL_PATTERN,
    normalize_market,
    normalize_symbol,
    normalize_text,
    normalize_symbol_for_market,
    is_valid_symbol,
    normalize_optional_number,
    _fmt,
    _fmt_vol,
    normalize_history_frame,
)
from utils.caching import (  # noqa: F401
    sanitize_cache_key,
    get_cached,
    clear_cache_prefix,
    _ensure_cache_bucket,
    _has_cached_key,
    _set_cached_value,
    _get_cached_value,
    get_cached_context_with_negative_cache,
)

# Constants
VALID_HISTORY_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}
MAX_STOCK_NAME_LENGTH = 200

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
}


def get_default_symbols():
    """市場別のデフォルト銘柄一覧を返す"""
    return {
        "us": list(DEFAULT_US.keys()),
        "jp": list(DEFAULT_JP.keys()),
        "idx": list(DEFAULT_IDX.keys()),
    }


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
    if market == "us":
        return DEFAULT_US
    if market == "jp":
        return DEFAULT_JP
    if market == "idx":
        return DEFAULT_IDX
    return {}


def _stock_is_default_or_user(symbol: str, market: str) -> bool:
    container = _get_stock_container(market)
    return bool(
        container is not None
        and (symbol in container or symbol in _default_stock_names(market))
    )


def _short_text(value, limit=160):
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else (text[:limit] + "...")


def _token_fingerprint(token):
    """トークンの安全なフィンガープリント生成（SHA256ハッシュ）"""
    t = (token or "").strip()
    if not t:
        return "none"
    digest = hashlib.sha256(t.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"sha256={digest}"


def _token_mask(token):
    """トークンのマスク表示（最初と最後の2文字のみ保持）"""
    t = (token or "").strip()
    if not t:
        return "none"
    if len(t) <= 4:
        return "*" * len(t)
    return f"{t[:2]}...{t[-2:]}"


def _is_valid_api_key(value, min_length=8):
    """Validate API key format for minimum length and no whitespace."""
    if not value or not isinstance(value, str):
        return False
    token = value.strip()
    if len(token) < min_length:
        return False
    if re.search(r"\s", token):
        return False
    return True


def _parse_json_request():
    """Parse a JSON request body and return an object or None for missing/malformed JSON."""
    content_length = request.content_length
    if content_length and content_length > MAX_JSON_SIZE:
        return None

    try:
        payload = request.get_json(force=False, silent=False)
    except (ValueError, TypeError, AttributeError):
        return None
    except BadRequest:
        return None

    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _sanitize_error_message(error_msg):
    """エラーメッセージから機密情報を削除"""
    if not error_msg:
        return ""
    sensitive_patterns = [
        r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"authorization['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"bearer\s+[a-z0-9\._\-]{10,}",
        r"https?://[a-z0-9]+:[a-z0-9]+@",
        r"secret['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
    ]
    sanitized = str(error_msg)
    for pattern in sensitive_patterns:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


def parse_non_negative_float(value, field_name, max_value=None):
    """Safely parse a number and ensure it is non-negative and finite."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field_name} must be <= {max_value}")
    return parsed


def _resolve_stocks_for_response():
    """Use current cache by default and fill empty markets from target cache.

    Returns a snapshot using list copy (shallow) instead of deepcopy for performance.
    SSE consumers receive serialized JSON anyway, so deep copying is unnecessary.
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
        resolved[market] = list(current_rows if current_rows else target_rows)
    return resolved


def _resolve_indices_for_response():
    """Prefer current cache, but fall back to target cache for fast first paint.

    Returns a snapshot using dict() shallow copy instead of deepcopy.
    """
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
        _has_ready_indices_snapshot
        if snapshot_type == "indices"
        else _has_ready_stocks_snapshot
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


def error_response(error_code: ErrorCode, status_code: int = 400, details: Optional[dict] = None):
    """統一されたエラーレスポンスを返す

    すべてのエラーレスポンスに ``ok`` フィールドを含めることで
    フロントエンドでのエラーハンドリングを統一する。
    """
    message = get_error_message(error_code, lang="ja")
    sanitized_details = {}
    if details:
        for k, v in details.items():
            sanitized_details[k] = (
                _sanitize_error_message(v) if isinstance(v, str) else v
            )
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


def _is_market_session_open(
    t, morning_start, morning_end, afternoon_start=None, afternoon_end=None
):
    """セッションの開始・終了時刻に基づいて市場が開いているか判定する。"""
    if morning_start <= t <= morning_end:
        return True
    if afternoon_start and afternoon_end:
        if afternoon_start <= t <= afternoon_end:
            return True
    return False


def _market_status_symbol(market_type):
    if market_type == "jp":
        return "^N225"
    if market_type in ("us", "idx"):
        return "^GSPC"
    return None


def _market_state_from_metadata(metadata):
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
    """市場が現在開いているかを判定。Yahoo Financeのステータスを優先し、フォールバックとして時間ベースの判定を行う。"""
    from datetime import datetime, timedelta, timezone
    from datetime import time as dt_time
    from zoneinfo import ZoneInfo

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

    now_utc = datetime.now(timezone.utc)
    if market_type == "jp":
        try:
            jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
        except (ImportError, ValueError, KeyError):
            jst = (now_utc + timedelta(hours=9)).replace(tzinfo=None)
        if jst.weekday() >= 5:
            return False
        return _is_market_session_open(
            jst.time(), dt_time(9, 0), dt_time(11, 30), dt_time(12, 30), dt_time(15, 0)
        )

    if market_type in ("us", "idx"):
        try:
            ny = now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            year = now_utc.year
            mar_8 = datetime(year, 3, 8, tzinfo=timezone.utc)
            dst_start = mar_8 + timedelta(days=(6 - mar_8.weekday()) % 7)
            nov_1 = datetime(year, 11, 1, tzinfo=timezone.utc)
            dst_end = nov_1 + timedelta(days=(6 - nov_1.weekday()) % 7)
            offset = -4 if dst_start <= now_utc < dst_end else -5
            ny = (now_utc + timedelta(hours=offset)).replace(tzinfo=None)
        if ny.weekday() >= 5:
            return False
        return _is_market_session_open(ny.time(), dt_time(9, 30), dt_time(16, 0))

    return True


def acquire_yfinance_slot() -> bool:
    """yfinance のリクエスト用スロットを取得する。"""
    wait_time = 0.0
    with app_state.market.yfinance_lock:
        if app_state.market.is_yfinance_rate_limited and (
            time.time() < app_state.market.yfinance_rate_limit_until
        ):
            return False

        if app_state.market.is_yfinance_rate_limited:
            app_state.market.is_yfinance_rate_limited = False

        now = time.time()
        elapsed = now - app_state.market.yfinance_last_request_ts
        if elapsed < app_state.market.yfinance_min_interval_sec:
            wait_time = app_state.market.yfinance_min_interval_sec - elapsed
        app_state.market.yfinance_last_request_ts = now + wait_time

    if wait_time > 0.0:
        time.sleep(wait_time)
    return True


def safe_get_ticker(symbol):
    """
    Wrap yf.Ticker instantiation with defensive error handling via stock_provider.
    """
    return app_state.stock_provider.get_ticker(symbol)


def get_stock_info_cached(symbol: str) -> dict:
    """Retrieve stock info including fundamentals with yfinance rate-limit protection and caching.

    Merges fast_info (price, market cap) with ticker.info (P/E, dividend, etc.).
    """
    neg_key = f"info_{symbol}__failed"
    if _has_cached_key(neg_key, 600):
        return {}

    def _fetch() -> dict:
        try:
            if not acquire_yfinance_slot():
                return {}

            # fast_info is lightweight: previous_close, currency, market_cap, exchange
            fast = app_state.stock_provider.get_fast_info(symbol)
            # ticker.info has fundamental data: P/E, P/B, dividend, margins, etc.
            full = {}
            try:
                full = app_state.stock_provider.get_info(symbol) or {}
            except Exception as exc:
                logger.debug("yfinance ticker.info failed for %s: %s", symbol, exc)

            # Merge: fast_info for basic fields, full info for fundamentals
            # full info keys take precedence for overlapping fields
            merged = {**fast, **full}

            if not merged:
                _set_cached_value(neg_key, True, 600)
                return {}
            return dict(merged)
        except Exception as exc:
            logger.debug("yfinance info fetch failed for %s: %s", symbol, exc)
            _set_cached_value(neg_key, True, 600)
            return {}

    cached = get_cached(f"info_{symbol}", _fetch, duration=86400, valid_func=bool)
    return dict(cached) if isinstance(cached, dict) else {}


def choose_display_name(symbol, fallback_name, info):
    """表示名を優先順位に従って選択する"""
    if isinstance(fallback_name, dict):
        fallback_name = fallback_name.get("name", "")
    info = info or {}
    return (
        info.get("shortName")
        or info.get("longName")
        or info.get("displayName")
        or fallback_name
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
            symbol, price, prev
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

        ohlc_data.append({
            "x": ts_ms, "o": _safe_ohlc(rd.get("Open")),
            "h": _safe_ohlc(rd.get("High")), "l": _safe_ohlc(rd.get("Low")),
            "c": c_val, "v": vol,
        })

        if num_records - i <= chart_data_limit:
            label = dt.strftime("%m/%d") if hasattr(dt, "strftime") else str(dt)
            ma5_val = _safe_ohlc(rd.get("MA5"), fallback=None)
            ma25_val = _safe_ohlc(rd.get("MA25"), fallback=None)
            chart.append({"x": ts_ms, "date": label, "price": c_val,
                          "ma5": ma5_val, "ma25": ma25_val})
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


def build_stock_payload(symbol, name_or_dict, market, hist, snapshot_ts_ms=None):
    """銘柄のペイロード辞書を構築する"""
    hist = normalize_history_frame(hist, inplace=True)
    if len(hist) < 1:
        logger.warning(
            "Stock %s: insufficient historical data (len=%d)", symbol, len(hist)
        )
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

        info = get_stock_info_cached(symbol) or {}
        market_state = "REGULAR" if is_market_open(market) else "CLOSED"
        currency = info.get("currency") or ("JPY" if market == "jp" else "USD")

        # Fetch next earnings date from calendar (cached separately)
        next_earnings = None
        try:
            cal_cache_key = f"cal_{symbol}"
            cal = get_cached(
                cal_cache_key,
                lambda: app_state.stock_provider.get_calendar(symbol),
                duration=3600,
            )
            if isinstance(cal, dict):
                e_dates = cal.get("Earnings Date")
                if isinstance(e_dates, list) and e_dates:
                    next_earnings = e_dates[0]
                elif isinstance(e_dates, str):
                    next_earnings = e_dates
        except Exception:
            pass

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
            "volume": _fmt_vol(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None,
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
            "dividend_yield": round(float(info["dividendYield"]), 4) if info.get("dividendYield") is not None else None,
            "eps": _fmt(info.get("earningsPerShare")),
            "market_cap": info.get("marketCap"),
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
        KeyError, AttributeError, TypeError, ValueError, pd.errors.EmptyDataError,
    ) as exc:
        logger.error(
            "Stock payload build failed (%s): %s", symbol, exc
        )
        return None
