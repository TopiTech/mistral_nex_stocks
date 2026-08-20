import copy
import logging
import time
from datetime import UTC, datetime

import pandas as pd

from app_state import app_state
from constants import (
    HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
    HISTORY_CIRCUIT_BREAKER_THRESHOLD,
    HISTORY_SEMAPHORE_TIMEOUT,
    NEGATIVE_CACHE_TTL,
    YFINANCE_TIMEOUT_SINGLE,
    CurlRequestsTimeout,
    RequestsTimeout,
)
from error_codes import ErrorCode, get_error_message
from route_helpers import cleanup_history_circuit_state
from services.stock_provider import is_yfinance_rate_limit_error, with_yfinance_retry
from utils.caching import (
    _set_cached_value,
    history_short_cache_key,
    history_short_payload_cache_key,
)
from utils.market_utils import safe_get_ticker
from utils.normalization import normalize_history_frame

logger = logging.getLogger(__name__)


def _history_payload_short_cache_key(symbol: str, period: str, interval: str = "auto") -> str:
    """Backward-compatible alias for :func:`utils.caching.history_short_payload_cache_key`."""
    return history_short_payload_cache_key(symbol, period, interval)


def _history_short_cache_key(symbol: str, period: str, interval: str) -> str:
    """Backward-compatible alias for :func:`utils.caching.history_short_cache_key`."""
    return history_short_cache_key(symbol, period, interval)


@with_yfinance_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
def _history_with_timeout(period_value, interval_value, symbol, market=None):
    now = time.time()
    # Clean up old circuit states occasionally
    cleanup_history_circuit_state(now_ts=now)

    # Key the circuit breaker by ``market:symbol`` so the same symbol string
    # under different markets does not share fail-fast state. Callers outside
    # a market context pass ``market=None`` to preserve the legacy symbol-only
    # keying.
    circuit_key = f"{market}:{symbol}" if market else symbol

    short_cache_key = _history_short_cache_key(symbol, period_value, interval_value)
    with app_state.yfinance_short_cache_lock:
        cached_short = app_state.yfinance_short_cache.get(short_cache_key)
    if isinstance(cached_short, pd.DataFrame):
        return cached_short.copy()

    if app_state.market.is_yf_rate_limited():
        logger.info("yfinance is currently rate-limited; skipping history fetch symbol=%s", symbol)
        return pd.DataFrame()

    if app_state.market.is_circuit_open("yfinance_history", symbol=circuit_key):
        logger.info("stock-history circuit open symbol=%s", circuit_key)
        return pd.DataFrame()

    # Acquire semaphore with timeout to protect Web threads from blocking
    acquired = app_state.market.yfinance_history_semaphore.acquire(
        blocking=True, timeout=HISTORY_SEMAPHORE_TIMEOUT
    )
    if not acquired:
        logger.warning(
            "Timed out waiting for history semaphore symbol=%s period=%s",
            symbol,
            period_value,
        )
        raise TimeoutError("Timed out waiting for history semaphore (server overloaded)")

    try:
        t = safe_get_ticker(symbol)
        if not t:
            return pd.DataFrame()
        hist = t.history(
            period=period_value,
            interval=interval_value,
            auto_adjust=True,
            actions=False,
            timeout=YFINANCE_TIMEOUT_SINGLE,
        )
        app_state.market.report_circuit_result("yfinance_history", success=True, symbol=circuit_key)
        normalized = normalize_history_frame(hist)
        if not normalized.empty:
            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache[short_cache_key] = normalized.copy()
        return normalized
    except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as timeout_exc:
        app_state.market.report_circuit_result(
            "yfinance_history",
            success=False,
            symbol=circuit_key,
            threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
            open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
        )
        logger.debug("stock-history timeout symbol=%s err=%s", symbol, timeout_exc)
        raise
    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        RuntimeError,
        OSError,
    ) as exc:
        logger.debug("stock-history error symbol=%s err=%s", symbol, exc, exc_info=True)
        # @with_yfinance_retry owns rate-limit detection/recording; re-raising
        # here avoids double-counting the same 429 (mark_yf_429 was previously
        # called both here and inside the retry decorator).
        if is_yfinance_rate_limit_error(exc):
            logger.warning(
                "yfinance rate-limit (429/Too Many Requests) detected in _history_with_timeout symbol=%s: %s",
                symbol,
                exc,
            )
            raise
        return pd.DataFrame()
    finally:
        if acquired:
            app_state.market.yfinance_history_semaphore.release()


def fetch_history_sync_impl(symbol, market, period, interval="auto"):
    try:
        if not interval or interval == "auto":
            requested_interval = "5m" if period == "1d" else ("15m" if period == "5d" else "1d")
        else:
            requested_interval = interval.lower()

        payload_cache_key = _history_payload_short_cache_key(symbol, period, requested_interval)
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

        fetch_interval = requested_interval
        orig_period = period
        orig_interval = requested_interval
        # yfinance period vs interval compatibility adjustments
        if fetch_interval in ["1m", "2m"] and period not in ["1d", "5d"]:
            period = "5d"
        elif fetch_interval in ["5m", "15m", "30m"] and period in ["1y", "2y", "5y", "max"]:
            period = "1mo"
        elif fetch_interval in ["60m", "1h"] and period in ["5y", "max"]:
            period = "2y"

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
        if fetch_interval == "1d" and period in extended_period_map:
            extended_period = extended_period_map[period]

        hist = _history_with_timeout(extended_period, fetch_interval, symbol, market)

        # フォールバック 1: 1d/5m または指定足が失敗した場合のフォールバック
        if hist.empty and period == "1d" and fetch_interval != "1d":
            logger.info(
                "Fallback 1 for %s: %s/%s failed, trying 1d/1d", symbol, period, fetch_interval
            )
            hist = _history_with_timeout("1d", "1d", symbol, market)
            fetch_interval = "1d"

        # フォールバック 2: 空またはデータが少なすぎる場合 → 5d/1d を試す
        if (hist.empty or len(hist) < 1) and period in ["1d", "5d"]:
            logger.info("%s: trying 5d/1d", symbol)
            hist = _history_with_timeout("5d", "1d", symbol, market)
            fetch_interval = "1d"

        # フォールバック 3: スクレイピング / API 代替手段
        if hist.empty and period == "1d":
            logger.info("%s: all yfinance history fetches failed, trying fallback provider", symbol)
            fallback_quote = app_state.fallback_provider.get_latest_quote(symbol)
            if fallback_quote:
                now_dt = datetime.now(UTC)
                hist = pd.DataFrame(
                    [
                        {
                            "Open": fallback_quote["regularMarketOpen"],
                            "High": fallback_quote["regularMarketDayHigh"],
                            "Low": fallback_quote["regularMarketDayLow"],
                            "Close": fallback_quote["regularMarketPrice"],
                            "Volume": fallback_quote["regularMarketVolume"],
                        }
                    ],
                    # tz-aware UTC index: the synthetic candle must anchor to
                    # UTC midnight, otherwise ``Timestamp.timestamp()`` below
                    # interprets the naive date in the server's local timezone
                    # and the chart x-axis drifts by the TZ offset.
                    index=[pd.Timestamp(now_dt.date(), tz=UTC)],
                )
                fetch_interval = "1d"

        if hist.empty:
            return {
                "error": "データが見つかりませんでした。銘柄が上場廃止されているか、選択した期間のデータが存在しない可能性があります。",
                "symbol": symbol,
                "interval_used": fetch_interval,
                "period_requested": period,
            }

        # MA計算 (日足の場合のみ)
        # 拡張取得した全データで MA を計算するため NaN になる先頭行が減る
        if fetch_interval == "1d":
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
        for ts, o, h, low_val, c, v, ma5, ma25 in zip(
            timestamps, opens, highs, lows, closes, volumes, ma5s, ma25s
        ):
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
            "interval_used": fetch_interval,
            "period_requested": period,
        }

        # Cache the successful payload so subsequent requests with the same
        # (symbol, period, interval) skip the entire yfinance fetch path entirely.
        # Key is based on the ORIGINAL request parameters, not adjusted ones.
        corrected_cache_key = _history_payload_short_cache_key(symbol, orig_period, orig_interval)
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache[corrected_cache_key] = copy.deepcopy(result)

        return result
    except Exception as exc:
        logger.error("Stock history fetch failed (%s, %s, %s): %s", symbol, period, interval, exc)
        return {
            "error": get_error_message(ErrorCode.FETCH_FAILED, lang="ja"),
            "error_code": int(ErrorCode.FETCH_FAILED),
            "symbol": symbol,
        }


def fetch_history_async_task(
    symbol, market, period, cache_key, duration, interval="auto", probe=False
):
    probe_success = False
    circuit_key = f"{market}:{symbol}" if market else symbol
    try:
        res = fetch_history_sync_impl(symbol, market, period, interval=interval)
        probe_success = isinstance(res, dict) and "error" not in res
        if isinstance(res, dict) and "error" not in res:
            _set_cached_value(cache_key, res, duration)
        # Persist successful history to disk cache for cold-start recovery
        if isinstance(res, dict) and "error" not in res:
            try:
                app_state.stock_disk_cache.set(cache_key, res)
            except Exception as exc:
                logger.debug("Failed to persist history to disk cache: %s", exc)
        elif isinstance(res, dict):
            # Negative cache: error responses use the short NEGATIVE_CACHE_TTL
            # (not the success duration) so a transient failure does not stick
            # for 3600s on closed-market TTL (R2).
            _set_cached_value(cache_key, res, min(duration, NEGATIVE_CACHE_TTL))
    except Exception as e:
        logger.error("Async background history fetch failed for %s: %s", symbol, e)
    finally:
        if probe:
            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state.get(circuit_key)
                unresolved = state is not None and state.get("status") == "HALF_OPEN" and state.get(
                    "probing"
                )
            if unresolved:
                if probe_success:
                    app_state.market.report_circuit_result(
                        "yfinance_history", success=True, symbol=circuit_key
                    )
                else:
                    app_state.market.report_circuit_result(
                        "yfinance_history",
                        success=False,
                        symbol=circuit_key,
                        threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
                        open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
                    )
        with app_state.history_fetch_lock:
            app_state.history_fetch_inflight.discard(cache_key)
