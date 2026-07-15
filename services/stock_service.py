import logging
import time
import pandas as pd

from constants import RequestsTimeout, CurlRequestsTimeout
from app_state import app_state
from error_codes import ErrorCode, get_error_message
from utils.caching import _set_cached_value
from utils.normalization import normalize_history_frame
from route_helpers import cleanup_history_circuit_state
from services.stock_provider import _is_yfinance_rate_limit_error, with_yfinance_retry
from utils.http_utils import parse_retry_after
from utils.market_utils import safe_get_ticker
from constants import (
    YFINANCE_TIMEOUT_SINGLE,
    HISTORY_SEMAPHORE_TIMEOUT,
    HISTORY_CIRCUIT_BREAKER_THRESHOLD,
    HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
)

logger = logging.getLogger(__name__)


def _history_payload_short_cache_key(symbol: str, period: str) -> str:
    return f"history_short_payload_{symbol}_{period}"


def _history_short_cache_key(symbol: str, period: str, interval: str) -> str:
    return f"history_short_{symbol}_{period}_{interval}"


@with_yfinance_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
def _history_with_timeout(period_value, interval_value, symbol):
    now = time.time()
    # Clean up old circuit states occasionally
    cleanup_history_circuit_state(now_ts=now)

    short_cache_key = _history_short_cache_key(symbol, period_value, interval_value)
    with app_state.yfinance_short_cache_lock:
        cached_short = app_state.yfinance_short_cache.get(short_cache_key)
    if isinstance(cached_short, pd.DataFrame):
        return cached_short.copy()

    if app_state.market.is_yf_rate_limited():
        logger.info("yfinance is currently rate-limited; skipping history fetch symbol=%s", symbol)
        return pd.DataFrame()

    if app_state.market.is_circuit_open("yfinance_history", symbol=symbol):
        logger.info("stock-history circuit open symbol=%s", symbol)
        return pd.DataFrame()

    # Acquire semaphore with timeout to protect Web threads from blocking
    acquired = app_state.market.yfinance_history_semaphore.acquire(blocking=True, timeout=HISTORY_SEMAPHORE_TIMEOUT)
    if not acquired:
        logger.warning("Timeout acquiring history semaphore for symbol=%s", symbol)
        return pd.DataFrame()

    try:
        ticker_obj = safe_get_ticker(symbol)
        if not ticker_obj:
            return pd.DataFrame()
        result = ticker_obj.history(
            period=period_value,
            interval=interval_value,
            auto_adjust=True,
            timeout=YFINANCE_TIMEOUT_SINGLE,
        )
        result = normalize_history_frame(result)
        app_state.market.report_circuit_result(
            "yfinance_history", success=True, symbol=symbol
        )
        if not result.empty:
            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache[short_cache_key] = result.copy()
        return result
    except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as timeout_exc:
        app_state.market.report_circuit_result(
            "yfinance_history",
            success=False,
            symbol=symbol,
            threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
            open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
        )
        logger.debug(
            "stock-history timeout symbol=%s err=%s", symbol, timeout_exc
        )
        raise
    except Exception as exc:
        if _is_yfinance_rate_limit_error(exc):
            backoff = app_state.market.mark_yf_429(retry_after=parse_retry_after(exc))
            logger.warning(
                "yfinance rate limit detected in history fetch for %s; backing off %.0fs",
                symbol,
                backoff,
            )
            raise
        raise
    finally:
        app_state.market.yfinance_history_semaphore.release()


def fetch_history_sync_impl(symbol, market, period):
    try:
        payload_cache_key = _history_payload_short_cache_key(symbol, period)
        with app_state.yfinance_short_cache_lock:
            cached_short = app_state.yfinance_short_cache.get(payload_cache_key)
        if isinstance(cached_short, dict):
            return dict(cached_short)

        t = safe_get_ticker(symbol)
        if not t:
            return {
                "error": "銘柄情報が取得できませんでした。",
                "symbol": symbol,
            }

        # 1d の場合は短いインターバルで取得を試みる
        interval = "5m" if period == "1d" else "1d"
        if period == "5d":
            interval = "15m"

        # MA25 計算のために日足では十分な期間を拡張して取得する
        extended_period_map = {
            "1mo": "6mo",
            "3mo": "6mo",
            "6mo": "1y",
            "1y": "2y",
            "2y": "5y",
            "5y": "10y",
        }
        extended_period = period
        if interval == "1d" and period in extended_period_map:
            extended_period = extended_period_map[period]

        hist = _history_with_timeout(extended_period, interval, symbol)

        # フォールバック 1: 1d/5m が失敗 → 1d/1d を試す
        if hist.empty and period == "1d" and interval == "5m":
            logger.info(
                "Fallback 1 for %s: 1d/5m failed, trying 1d/1d", symbol
            )
            hist = _history_with_timeout("1d", "1d", symbol)
            interval = "1d"

        # フォールバック 2: 空またはデータが少なすぎる場合 → 5d/1d を試す
        if (hist.empty or len(hist) < 1) and period in ["1d", "5d"]:
            logger.info("%s: trying 5d/1d", symbol)
            hist = _history_with_timeout("5d", "1d", symbol)
            interval = "1d"

        if hist.empty:
            return {
                "error": "データが見つかりませんでした。銘柄が上場廃止されているか、選択した期間のデータが存在しない可能性があります。",
                "symbol": symbol,
                "interval_used": interval,
                "period_requested": period,
            }

        # MA計算 (日足の場合のみ)
        # 拡張取得した全データで MA を計算するため NaN になる先頭行が減る
        if interval == "1d":
            if len(hist) >= 5:
                hist["MA5"] = hist["Close"].rolling(window=5).mean()
            if len(hist) >= 25:
                hist["MA25"] = hist["Close"].rolling(window=25).mean()

            # 元のピリオドに対応するカレンダー期間でデータをトリミング
            period_offset_map = {
                "1mo": pd.DateOffset(months=1),
                "3mo": pd.DateOffset(months=3),
                "6mo": pd.DateOffset(months=6),
                "1y": pd.DateOffset(years=1),
                "2y": pd.DateOffset(years=2),
                "5y": pd.DateOffset(years=5),
            }
            if extended_period != period and period in period_offset_map:
                cutoff = hist.index[-1] - period_offset_map[period]
                hist = hist[hist.index >= cutoff]

        timestamps = [int(dt.timestamp() * 1000) for dt in hist.index]
        opens = hist["Open"].tolist() if "Open" in hist.columns else [0.0] * len(hist)
        highs = hist["High"].tolist() if "High" in hist.columns else [0.0] * len(hist)
        lows = hist["Low"].tolist() if "Low" in hist.columns else [0.0] * len(hist)
        closes = hist["Close"].tolist() if "Close" in hist.columns else [0.0] * len(hist)
        volumes = hist["Volume"].tolist() if "Volume" in hist.columns else [0.0] * len(hist)

        ma5s = hist["MA5"].tolist() if "MA5" in hist.columns else [None] * len(hist)
        ma25s = hist["MA25"].tolist() if "MA25" in hist.columns else [None] * len(hist)

        data_list = []
        for ts, o, h, low_val, c, v, ma5, ma25 in zip(timestamps, opens, highs, lows, closes, volumes, ma5s, ma25s):
            try:
                vol = int(float(v)) if (v is not None and pd.notna(v)) else 0
            except (TypeError, ValueError):
                vol = 0
            d = {
                "x": ts,
                "o": float(o) if (o is not None and pd.notna(o)) else 0.0,
                "h": float(h) if (h is not None and pd.notna(h)) else 0.0,
                "l": float(low_val) if (low_val is not None and pd.notna(low_val)) else 0.0,
                "c": float(c) if (c is not None and pd.notna(c)) else 0.0,
                "v": vol,
            }
            if ma5 is not None and pd.notna(ma5):
                d["ma5"] = float(ma5)
            if ma25 is not None and pd.notna(ma25):
                d["ma25"] = float(ma25)
            data_list.append(d)

        # Build the result payload from data_list
        result = {
            "symbol": symbol,
            "history": data_list,
            "interval_used": interval,
        }

        # Cache the successful payload so subsequent requests with the same
        # (symbol, period) skip the entire yfinance fetch path entirely.
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache[payload_cache_key] = dict(result)

        return result
    except Exception as exc:
        logger.error(
            "Stock history fetch failed (%s, %s): %s", symbol, period, exc
        )
        return {
            "error": get_error_message(ErrorCode.FETCH_FAILED, lang="ja"),
            "error_code": int(ErrorCode.FETCH_FAILED),
            "symbol": symbol,
        }


def fetch_history_async_task(symbol, market, period, cache_key, duration):
    try:
        res = fetch_history_sync_impl(symbol, market, period)
        _set_cached_value(cache_key, res, duration)
        # Persist successful history to disk cache for cold-start recovery
        if isinstance(res, dict) and "error" not in res:
            try:
                app_state.stock_disk_cache.set(cache_key, res)
            except Exception as exc:
                logger.debug("Failed to persist history to disk cache: %s", exc)
    except Exception as e:
        logger.error("Async background history fetch failed for %s: %s", symbol, e)
    finally:
        with app_state.history_fetch_lock:
            app_state.history_fetch_inflight.discard(cache_key)
