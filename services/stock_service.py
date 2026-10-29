import copy
import logging
import math
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
from utils.normalization import (
    history_circuit_key,
    normalize_history_candle,
    normalize_history_frame,
    sanitize_history_payload,
)

logger = logging.getLogger(__name__)


def _finite_float(value, default: float | None = None) -> float | None:
    """Convert an external numeric value without allowing NaN/Inf downstream.

    The default is ``None`` on purpose: a missing/non-finite number must never
    silently become ``0.0``.  Callers that have a documented fallback (for
    example the synthetic close price) pass it explicitly.
    """
    if value is None or isinstance(value, bool) or type(value).__name__ in ("bool_", "bool"):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return numeric if math.isfinite(numeric) else default


def _history_payload_short_cache_key(symbol: str, period: str, interval: str = "auto") -> str:
    """Backward-compatible alias for :func:`utils.caching.history_short_payload_cache_key`."""
    return history_short_payload_cache_key(symbol, period, interval)


def _history_short_cache_key(symbol: str, period: str, interval: str) -> str:
    """Backward-compatible alias for :func:`utils.caching.history_short_cache_key`."""
    return history_short_cache_key(symbol, period, interval)


def _history_unavailable_payload(
    symbol: str, market: str, period: str, fetch_interval: str
) -> dict:
    """Return the canonical "no usable bars" payload.

    Used both when the upstream frame is empty and when every bar is rejected
    by :func:`normalize_history_candle`; the two cases are indistinguishable to
    the client, and a filtered-to-empty series must not be cached as success.
    """
    circuit_key = history_circuit_key(symbol, market)
    is_transient = app_state.market.is_yf_rate_limited() or app_state.market.is_circuit_open(
        "yfinance_history", symbol=circuit_key
    )
    return {
        "error": "データが見つかりませんでした。銘柄が上場廃止されているか、選択した期間のデータが存在しない可能性があります。",
        "symbol": symbol,
        "interval_used": fetch_interval,
        "period_requested": period,
        "transient": is_transient,
    }


@with_yfinance_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
def _history_with_timeout(period_value, interval_value, symbol, market=None):
    now = time.time()
    # Clean up old circuit states occasionally
    cleanup_history_circuit_state(now_ts=now)

    # Derive the key through the shared helper so the route, async task,
    # service, and provider all count failures in the same breaker bucket.
    circuit_key = history_circuit_key(symbol, market)

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
        exc_name = type(exc).__name__
        if "Timeout" in exc_name or "Connection" in exc_name:
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
            # Revalidate on read: entries written by an older build may contain
            # the former 0.0-OHLC candles.  On a hit, ``sanitize_history_payload``
            # already returns an isolated deep copy (no shared history list).
            sanitized_cached = sanitize_history_payload(cached_short)
            if sanitized_cached is not None:
                # The response field reports the caller's request, not the
                # adjusted period used internally; an entry written by an
                # older build may still carry the adjusted value.
                sanitized_cached["period_requested"] = period
                return sanitized_cached
            logger.warning(
                "Discarding unusable cached history payload key=%s symbol=%s",
                payload_cache_key,
                symbol,
            )
            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache.pop(payload_cache_key, None)

        t = safe_get_ticker(symbol)
        if not t:
            is_negative = hasattr(
                app_state.market, "is_negative_cached_symbol"
            ) and app_state.market.is_negative_cached_symbol(symbol)
            is_transient = not is_negative or app_state.market.is_yf_rate_limited()
            return {
                "error": "銘柄情報が取得できませんでした。",
                "symbol": symbol,
                "transient": is_transient,
            }

        fetch_interval = requested_interval
        orig_period = period
        orig_interval = requested_interval
        # The period passed to yfinance may be adjusted for interval
        # compatibility. Keep the caller's value in ``period`` so
        # ``period_requested``, the payload cache key, and error payloads all
        # describe the request; only the fetch uses ``fetch_period``.
        fetch_period = period
        # yfinance period vs interval compatibility adjustments
        if fetch_interval in ["1m", "2m"] and period not in ["1d", "5d"]:
            fetch_period = "5d"
        elif fetch_interval in ["5m", "15m", "30m"] and period in ["1y", "2y", "5y", "max"]:
            fetch_period = "1mo"
        elif fetch_interval in ["60m", "1h"] and period in ["5y", "max"]:
            fetch_period = "2y"

        # MA25 計算のために日足では十分な期間を拡張して取得する
        extended_period_map = {
            "1mo": "6mo",
            "3mo": "6mo",
            "6mo": "1y",
            "1y": "2y",
            "2y": "5y",
            "5y": "10y",
        }
        extended_period = fetch_period
        if fetch_interval == "1d" and fetch_period in extended_period_map:
            extended_period = extended_period_map[fetch_period]

        hist = _history_with_timeout(extended_period, fetch_interval, symbol, market)

        # フォールバック 1: 1d/5m または指定足が失敗した場合のフォールバック
        if hist.empty and fetch_period == "1d" and fetch_interval != "1d":
            logger.info(
                "Fallback 1 for %s: %s/%s failed, trying 1d/1d",
                symbol,
                fetch_period,
                fetch_interval,
            )
            hist = _history_with_timeout("1d", "1d", symbol, market)
            fetch_interval = "1d"

        # フォールバック 2: 空またはデータが少なすぎる場合 → 5d/1d を試す
        if (hist.empty or len(hist) < 1) and fetch_period in ["1d", "5d"]:
            logger.info("%s: trying 5d/1d", symbol)
            hist = _history_with_timeout("5d", "1d", symbol, market)
            fetch_interval = "1d"

        # フォールバック 3: スクレイピング / API 代替手段
        if hist.empty and fetch_period == "1d":
            logger.info("%s: all yfinance history fetches failed, trying fallback provider", symbol)
            fallback_quote = app_state.fallback_provider.get_latest_quote(symbol)
            if fallback_quote:
                close_px = _finite_float(fallback_quote.get("regularMarketPrice"), default=None)
                if close_px is not None:
                    # Scrapers return OHLC=None when unknown (they never fake a
                    # previous close). A partial bar used to become a bogus
                    # "0-yen open" candle; the candle writer now drops
                    # non-finite legs, but substituting the close keeps this
                    # known-good quote visible. All-or-nothing keeps the candle
                    # well-formed (H >= max(O, C), L <= min(O, C)).
                    bar = [
                        _finite_float(fallback_quote.get("regularMarketOpen"), default=None),
                        _finite_float(fallback_quote.get("regularMarketDayHigh"), default=None),
                        _finite_float(fallback_quote.get("regularMarketDayLow"), default=None),
                    ]
                    if any(px is None for px in bar):
                        bar = [close_px, close_px, close_px]
                    now_dt = datetime.now(UTC)
                    hist = pd.DataFrame(
                        [
                            {
                                "Open": bar[0],
                                "High": bar[1],
                                "Low": bar[2],
                                "Close": close_px,
                                "Volume": fallback_quote.get("regularMarketVolume"),
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
            return _history_unavailable_payload(symbol, market, period, fetch_interval)

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
            if extended_period != fetch_period and fetch_period in period_offset_map:
                cutoff = hist.index[-1] - period_offset_map[fetch_period]
                hist = hist[hist.index >= cutoff]

        timestamps = [int(dt.timestamp() * 1000) for dt in hist.index]
        # A missing OHLC column is passed as None and becomes a flat bar at the
        # close.  A present-but-non-finite value stays NaN/Inf and is rejected
        # by the candle writer, so corrupt market data is dropped rather than
        # converted into a fabricated 0.0 candle.
        opens = hist["Open"].tolist() if "Open" in hist.columns else [None] * len(hist)
        highs = hist["High"].tolist() if "High" in hist.columns else [None] * len(hist)
        lows = hist["Low"].tolist() if "Low" in hist.columns else [None] * len(hist)
        closes = hist["Close"].tolist() if "Close" in hist.columns else [None] * len(hist)
        volumes = hist["Volume"].tolist() if "Volume" in hist.columns else [None] * len(hist)

        ma5s = hist["MA5"].tolist() if "MA5" in hist.columns else [None] * len(hist)
        ma25s = hist["MA25"].tolist() if "MA25" in hist.columns else [None] * len(hist)

        data_list = []
        for ts, o, h, low_val, c, v, ma5, ma25 in zip(
            timestamps, opens, highs, lows, closes, volumes, ma5s, ma25s, strict=True
        ):
            candle = normalize_history_candle(ts, o, h, low_val, c, v, ma5, ma25)
            if candle is None:
                logger.warning(
                    "Dropping unusable history candle symbol=%s x=%s o=%r h=%r l=%r c=%r",
                    symbol,
                    ts,
                    o,
                    h,
                    low_val,
                    c,
                )
                continue
            data_list.append(candle)

        if not data_list:
            logger.warning(
                "All history candles were unusable symbol=%s period=%s interval=%s",
                symbol,
                fetch_period,
                fetch_interval,
            )
            return _history_unavailable_payload(symbol, market, period, fetch_interval)

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
        is_transient = isinstance(exc, TimeoutError) and "server overloaded" in str(exc)
        return {
            "error": get_error_message(ErrorCode.FETCH_FAILED, lang="ja"),
            "error_code": int(ErrorCode.FETCH_FAILED),
            "symbol": symbol,
            "transient": is_transient,
        }


def fetch_history_async_task(
    symbol, market, period, cache_key, duration, interval="auto", probe=False
):
    probe_success = False
    circuit_key = history_circuit_key(symbol, market)
    try:
        res = fetch_history_sync_impl(symbol, market, period, interval=interval)
        if isinstance(res, dict) and "error" not in res:
            # Final choke point before both the short-lived and persistent
            # caches: a producer added later cannot persist an invalid candle.
            sanitized = sanitize_history_payload(res)
            if sanitized is None:
                logger.warning(
                    "History produced no usable candles; refusing to cache symbol=%s period=%s",
                    symbol,
                    period,
                )
                res = _history_unavailable_payload(symbol, market, period, interval)
            else:
                res = sanitized
        probe_success = isinstance(res, dict) and "error" not in res
        if isinstance(res, dict) and "error" not in res:
            _set_cached_value(cache_key, res, duration)
        # Persist successful history to disk cache for cold-start recovery
        if isinstance(res, dict) and "error" not in res:
            try:
                app_state.stock_disk_cache.set(cache_key, res)
            except Exception as exc:
                logger.debug("Failed to persist history to disk cache: %s", exc)
        elif isinstance(res, dict) and not res.get("transient"):
            # Negative cache: error responses use the short NEGATIVE_CACHE_TTL
            # (not the success duration) so a transient failure does not stick
            # for 3600s on closed-market TTL (R2).
            # Transient failures (e.g. semaphore timeout / server overloaded)
            # must not be cached; doing so would poison the endpoint for
            # NEGATIVE_CACHE_TTL and prevent polling from recovering.
            _set_cached_value(cache_key, res, min(duration, NEGATIVE_CACHE_TTL))
    except Exception as e:
        logger.error("Async background history fetch failed for %s: %s", symbol, e)
    finally:
        if probe:
            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state.get(circuit_key)
                unresolved = (
                    state is not None
                    and state.get("status") == "HALF_OPEN"
                    and state.get("probing")
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
