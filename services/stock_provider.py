# services/stock_provider.py
"""Stock Data Provider Abstraction Layer for Mistral NeX Stocks.

Provides uniform interface for retrieving stock ticker data, historical series,
batch downloads, and fast attributes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime
from functools import wraps
from typing import Any, Callable, List, Optional, TypeVar
import logging
import random
import concurrent.futures
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf
from session_manager import yf_session_manager
from utils.http_utils import parse_retry_after
from utils.normalization import normalize_history_frame

from requests.exceptions import ConnectionError as RequestsConnectionError
from constants import RequestsTimeout, CurlRequestsTimeout

logger = logging.getLogger(__name__)


def _is_yfinance_rate_limit_error(exc: Exception) -> bool:
    """Detect yfinance 401/402/429/439-style blocking failures from exception objects.

    Yahoo Finance / yfinance can surface blocking responses through several HTTP
    status codes and error envelopes:

      * 429 - Too Many Requests (classic rate limit)
      * 401 - Unauthorized / Invalid Crumb (session needs rotation)
      * 402 - Payment Required (data now behind the Yahoo paywall)
      * 439 - Yahoo's "your request was denied / temporarily unavailable" block

    In yfinance 1.5.1 a 429 is raised as ``YFRateLimitError`` (a subclass of
    ``YFException``). That exception carries no ``response``/``status_code``
    attribute — only the message "Too Many Requests. Rate limited. Try after a
    while." So we must check the exception *type* first, before relying on the
    response/status_code heuristics below.

    All of these are treated as retriable-with-backoff conditions so the caller
    rotates the session (UA + crumb) and applies graduated backoff instead of
    hammering the endpoint.
    """
    # Type-based check first: YFRateLimitError has no status_code/response attrs.
    try:
        from yfinance.exceptions import YFRateLimitError

        if isinstance(exc, YFRateLimitError):
            return True
    except (ImportError, AttributeError):
        pass

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    exc_name = type(exc).__name__.lower()
    exc_text = str(exc).lower()

    if status_code in (401, 402, 429, 439):
        return True

    text_markers = (
        "ratelimit",
        "too many requests",
        "rate limit",
        "payment required",
        "unauthorized",
        "invalid crumb",
        "access forbidden",
        "your request was denied",
        "temporarily unavailable",
        "thank you for your patience",
    )
    if any(marker in exc_text for marker in text_markers):
        return True

    # Some yfinance endpoints return the numeric block code inside a JSON body
    # (e.g. {"finance": {"error": {"code": "439", ...}}}). Detect those too.
    try:
        body_callable = getattr(response, "json", None)
        if callable(body_callable):
            try:
                payload = body_callable()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                code = (
                    payload.get("code")
                    or (payload.get("finance") or {}).get("error", {}).get("code")
                )
                if code in (401, 402, 429, 439, "401", "402", "429", "439"):
                    return True
    except (AttributeError, ValueError, TypeError):
        pass

    return "ratelimit" in exc_name


def _is_yfinance_invalid_symbol_error(exc: Exception) -> bool:
    """Detect yfinance errors that mean the *symbol itself is invalid*.

    Yahoo Finance / yfinance raise specific exceptions when a ticker does not
    exist (rather than when the service is temporarily unavailable). These are
    the only failures that should count toward automatic removal of a
    user-added symbol. Transient conditions (rate-limit, timeout, network,
    server error) must NOT be treated as invalid so a temporary outage cannot
    silently delete user stocks.
    """
    # Type-based check first: yfinance exposes dedicated "missing" errors.
    try:
        from yfinance.exceptions import (
            YFTickerMissingError,
            YFPricesMissingError,
        )

        if isinstance(exc, (YFTickerMissingError, YFPricesMissingError)):
            return True
    except (ImportError, AttributeError):
        pass

    exc_text = str(exc).lower()
    text_markers = (
        "no data found",
        "ticker does not exist",
        "symbol may be delisted",
        "delisted",
        "unknown symbol",
        "invalid symbol",
        "not found",
        "could not find",
    )
    if any(marker in exc_text for marker in text_markers):
        return True
    return False


def _handle_yf_rate_limit(exc: Exception, m_state: Any, context: str = "") -> float:
    """yfinance の 401/402/429/439 エラーを検知してバックオフを記録し、その秒数を返す。

    各フェッチ経路で繰り返されていた ``if _is_yfinance_rate_limit_error(...)`` +
    ``m_state.mark_yf_429(...)`` のブロックを一箇所に集約するためのヘルパ。
    呼び出し側は戻り値（バックオフ秒）を参照して return/break 等の制御を行う。
    """
    backoff = m_state.mark_yf_429(retry_after=parse_retry_after(exc))
    logger.warning(
        "yfinance rate limit detected%s; backing off %.0fs",
        f" ({context})" if context else "",
        backoff,
    )
    return backoff



# Type variable for the retry decorator
F = TypeVar("F", bound=Callable[..., Any])

_cached_app_state: Any = None
_cached_backoff_base: Any = None

def _get_app_state_cached():
    global _cached_app_state
    if _cached_app_state is None:
        try:
            from app_state import app_state
            _cached_app_state = app_state
        except (ImportError, AttributeError):
            pass
    return _cached_app_state

def _get_backoff_base_cached():
    global _cached_backoff_base
    if _cached_backoff_base is None:
        try:
            from constants import YFINANCE_RETRY_BACKOFF_BASE
            _cached_backoff_base = YFINANCE_RETRY_BACKOFF_BASE
        except (ImportError, AttributeError):
            pass
    return _cached_backoff_base


def with_yfinance_retry(
    func: Optional[F] = None,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    backoff_factor: float = 2.0,
) -> Callable[..., Any] | F:
    """Decorator: exponential-backoff retry for yfinance operations.

    Handles rate limiting (429), timeouts, and transient connection errors.
    Uses exponential backoff: delay = base_delay * backoff_factor^attempt
    plus jitter of ±25%.

    When the app-level yfinance rate limiter is active, retries use even longer
    delays (multiplied by 3x) to avoid hammering Yahoo servers.

    The default backoff_factor is overridden by MNS_YFINANCE_RETRY_BACKOFF_BASE
    when set, allowing runtime configuration without code changes.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        backoff_factor: Multiplier for exponential backoff
    """

    def _rate_limit_multiplier(self_obj: Any = None) -> float:
        """Return 3x multiplier if app-level rate limiter is active."""
        if self_obj and hasattr(self_obj, "_get_market_state"):
            try:
                m_state = self_obj._get_market_state()
                if m_state and m_state.is_yf_rate_limited():
                    return 3.0
            except (AttributeError, RuntimeError):
                pass
            return 1.0

        state_ref = _get_app_state_cached()
        if state_ref is None:
            return 1.0
        try:
            if state_ref.is_yf_rate_limited():
                return 3.0
        except (ImportError, AttributeError):
            pass
        return 1.0

    def decorator(f: F) -> F:
        @wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # M-6: Evaluate the backoff_factor override at call time (not at
            # decoration/import time) so that environment variable changes in
            # tests and runtime configuration are reflected correctly.
            effective_backoff = backoff_factor
            env_backoff = _get_backoff_base_cached()
            if env_backoff is not None and env_backoff != effective_backoff:
                effective_backoff = env_backoff

            import sys
            is_testing = "pytest" in sys.modules or "unittest" in sys.modules

            last_exception: Optional[Exception] = None
            self_obj = args[0] if args else None
            for attempt in range(max_retries + 1):
                try:
                    return f(*args, **kwargs)
                except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        if is_testing:
                            time.sleep(0.0001)
                            continue
                        rl_mult = _rate_limit_multiplier(self_obj)
                        delay = base_delay * (effective_backoff ** attempt) * rl_mult
                        jitter = delay * random.uniform(-0.25, 0.25)
                        total_delay = delay + jitter
                        _target = getattr(args[0], "symbol", None) if args else None
                        logger.debug(
                            "yfinance retry %d/%d for %s after timeout, waiting %.2fs (rl_mult=%.1f)",
                            attempt + 1, max_retries, _target or str(args),
                            total_delay, rl_mult,
                        )
                        time.sleep(total_delay)
                except (ConnectionError, OSError, RequestsConnectionError) as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        if is_testing:
                            time.sleep(0.0001)
                            continue
                        rl_mult = _rate_limit_multiplier(self_obj)
                        delay = base_delay * (effective_backoff ** attempt) * rl_mult
                        jitter = delay * random.uniform(-0.25, 0.25)
                        time.sleep(delay + jitter)
                except Exception as exc:
                    # Non-retriable errors: re-raise immediately
                    # Check for yfinance rate limit errors
                    if _is_yfinance_rate_limit_error(exc):
                        last_exception = exc
                        if attempt < max_retries:
                            if is_testing:
                                time.sleep(0.0001)
                                continue
                            rl_mult = _rate_limit_multiplier(self_obj)
                            # Full jitter (AWS-style): spread retries uniformly in
                            # [0, backoff] to avoid synchronized re-attacks after a 429/401.
                            backoff_delay = max(base_delay * (effective_backoff ** attempt) * rl_mult, 1.0)
                            sleep_time = max(0.5, random.uniform(0.0, backoff_delay))
                            logger.warning(
                                "yfinance rate limited (%s), retry %d/%d after %.1fs (rl_mult=%.1f)",
                                type(exc).__name__, attempt + 1, max_retries, sleep_time, rl_mult,
                            )
                            time.sleep(sleep_time)
                            continue
                    raise
            # All retries exhausted
            if last_exception:
                raise last_exception
        return wrapper  # type: ignore[return-value]

    if func:
        return decorator(func)
    return decorator


class BaseStockProvider(ABC):
    """Abstract Base Class for Stock Providers."""

    @abstractmethod
    def get_ticker(self, symbol: str) -> Optional[Any]:
        """Wrap ticker object instantiation with defensive validation."""

    @abstractmethod
    def get_history(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch historical data for a specific stock ticker."""

    @abstractmethod
    def download_batch(self, symbols: List[str], period: str = "3mo") -> pd.DataFrame:
        """Download historical series in batch for multiple tickers."""

    @abstractmethod
    def get_fast_info(self, symbol: str) -> dict:
        """Retrieve lightweight attributes for metadata caching."""

    @abstractmethod
    def get_info(self, symbol: str) -> dict:
        """Fetch full ticker info including fundamental data."""

    @abstractmethod
    def fetch_quotes_batch(self, symbols: List[str]) -> dict[str, dict]:
        """Fetch current quotes in batch for multiple tickers."""

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search for stocks/instruments by query string."""


class YFinanceProvider(BaseStockProvider):
    """Yahoo Finance API provider implementation."""

    def __init__(self, market_state: Optional[Any] = None):
        self._market_state = market_state

    def _get_market_state(self) -> Any:
        if self._market_state is not None:
            return self._market_state
        from app_state import app_state
        return app_state.market

    def get_ticker(self, symbol: str) -> Optional[Any]:
        m_state = self._get_market_state()
        if m_state.is_yf_rate_limited():
            return None
        try:
            sess = yf_session_manager.get_session()
            return yf.Ticker(symbol, session=sess)
        except (ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            logger.debug("yf.Ticker creation failed for %s: %s", symbol, exc)
            return None

    @with_yfinance_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
    def get_history(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        from constants import YFINANCE_TIMEOUT_SINGLE
        from app_helpers import normalize_history_frame
        m_state = self._get_market_state()

        if m_state.is_circuit_open("yfinance_history", symbol=symbol):
            logger.info("stock-history circuit open symbol=%s", symbol)
            return pd.DataFrame()
        if m_state.is_yf_rate_limited():
            logger.info("yfinance is rate-limited; skipping history fetch symbol=%s", symbol)
            return pd.DataFrame()

        # Speedup: serve a short-lived in-memory copy to avoid duplicate fetches
        # of the same (symbol, period, interval) within one sync cycle.
        cache_key = f"history_short_{symbol}_{period}_{interval}"
        try:
            with m_state.yfinance_short_cache_lock:
                cached_hist = m_state.yfinance_short_cache.get(cache_key)
            if cached_hist is not None:
                logger.debug(
                    "yfinance history cache hit symbol=%s period=%s interval=%s",
                    symbol, period, interval,
                )
                return cached_hist
        except (AttributeError, KeyError, RuntimeError):
            cached_hist = None

        t = self.get_ticker(symbol)
        if not t:
            return pd.DataFrame()

        try:
            result = t.history(
                period=period,
                interval=interval,
                auto_adjust=True,
                actions=False,
                timeout=YFINANCE_TIMEOUT_SINGLE,
            )
            m_state.report_circuit_result(
                "yfinance_history", success=True, symbol=symbol
            )
            try:
                yf_session_manager.reset_consecutive_401_count()
            except (AttributeError, RuntimeError):
                pass
            normalized = normalize_history_frame(result)
            if not normalized.empty:
                try:
                    with m_state.yfinance_short_cache_lock:
                        m_state.yfinance_short_cache[cache_key] = normalized
                except (AttributeError, RuntimeError, TypeError) as cache_exc:
                    logger.debug(
                        "Failed to cache yfinance history for %s: %s", symbol, cache_exc
                    )
            return normalized
        except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as timeout_exc:
            from constants import HISTORY_CIRCUIT_BREAKER_THRESHOLD, HISTORY_CIRCUIT_BREAKER_OPEN_SEC
            m_state.report_circuit_result(
                "yfinance_history",
                success=False,
                symbol=symbol,
                threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
                open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
            )
            logger.debug("stock-history timeout symbol=%s err=%s", symbol, timeout_exc)
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            logger.debug("stock-history error symbol=%s err=%s", symbol, exc, exc_info=True)
            if _is_yfinance_rate_limit_error(exc):
                _handle_yf_rate_limit(exc, m_state, context=f"history symbol={symbol}")
                raise
            return pd.DataFrame()

    @with_yfinance_retry(max_retries=2, base_delay=2.0, backoff_factor=2.0)
    def fetch_quotes_batch(self, symbols: List[str]) -> dict[str, dict]:
        """Yahoo Finance から複数銘柄の最新情報を一括で取得する (v7/finance/quote)"""
        if not symbols:
            return {}

        m_state = self._get_market_state()
        if m_state.is_yf_rate_limited():
            logger.info("yfinance is rate-limited; skipping batch quotes fetch.")
            return {}

        # 重複削除
        unique_symbols = list(dict.fromkeys(symbols))
        url = "https://query1.finance.yahoo.com/v7/finance/quote"
        params = {"symbols": ",".join(unique_symbols)}

        try:
            import yfinance.data as yfd
            # get_raw_json は yfinance 内部のクッキーと crumb を自動的に乗せてリクエストします
            data = yfd.YfData().get_raw_json(url, params=params, timeout=10)
            if not data:
                return {}

            quote_response = data.get("quoteResponse", {})
            results = quote_response.get("result", [])

            quotes_map = {}
            for item in results:
                sym = item.get("symbol")
                if sym:
                    quotes_map[sym] = item

            return quotes_map
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OSError) as exc:
            logger.warning("Batch quotes fetch failed: %s", exc, exc_info=True)
            if _is_yfinance_rate_limit_error(exc):
                _handle_yf_rate_limit(exc, m_state, context="batch quotes")
                raise
            exc_name = type(exc).__name__
            if "Timeout" in exc_name or "Connection" in exc_name:
                raise
            return {}

    def _fetch_single_history(self, symbol: str, period: str, m_state: Any) -> pd.DataFrame:
        """単一銘柄の履歴を yfinance から取得し、キャッシュを更新する"""
        try:
            df = self.get_history(symbol, period=period)
            if df is not None and not df.empty:
                cache_key = f"hist_df_{symbol}_{period}"
                try:
                    data = {
                        "type": "dataframe",
                        "json_data": df.to_json(orient="split", date_format="iso")
                    }
                    from app_state import app_state
                    app_state.stock_disk_cache.set(cache_key, data)
                except (IOError, OSError, TypeError, ValueError) as cache_exc:
                    logger.debug("Failed to save df to disk cache for %s: %s", symbol, cache_exc)
                return df
        except (ValueError, TypeError, KeyError, RuntimeError, AttributeError) as exc:
            logger.warning("Failed to fetch history for %s: %s", symbol, exc)
        return pd.DataFrame()

    def _merge_quote_into_history(self, df: pd.DataFrame, quote: dict, symbol: str) -> pd.DataFrame:
        """最新の一括 quote 情報を履歴 DataFrame にマージする"""
        if df is None or df.empty:
            return pd.DataFrame()

        price = quote.get("regularMarketPrice")
        if price is None:
            return df

        df = df.copy()

        volume = quote.get("regularMarketVolume", 0)
        high = quote.get("regularMarketDayHigh", price)
        low = quote.get("regularMarketDayLow", price)
        open_p = quote.get("regularMarketOpen", price)

        market_time_sec = quote.get("regularMarketTime")

        tz_str = "Asia/Tokyo" if symbol.endswith(".T") else "America/New_York"
        try:
            local_tz = ZoneInfo(tz_str)
            dt = datetime.fromtimestamp(market_time_sec, local_tz) if market_time_sec else datetime.now(local_tz)
        except (ValueError, KeyError, OSError):
            dt = datetime.fromtimestamp(market_time_sec) if market_time_sec else datetime.now()

        date_str = dt.strftime("%Y-%m-%d")

        df_tz = df.index.tz
        if df_tz:
            new_idx = pd.to_datetime(date_str).tz_localize(df_tz)
        else:
            new_idx = pd.to_datetime(date_str)

        last_idx = df.index[-1]
        last_date_str = last_idx.strftime("%Y-%m-%d") if hasattr(last_idx, "strftime") else str(last_idx)

        row_data = {
            "Open": float(open_p) if open_p is not None else float(price),
            "High": float(high) if high is not None else float(price),
            "Low": float(low) if low is not None else float(price),
            "Close": float(price),
            "Volume": int(volume) if volume is not None else 0,
        }

        for col in df.columns:
            if col not in row_data:
                row_data[col] = float(price)

        new_row = pd.Series(row_data, name=new_idx)

        if date_str == last_date_str:
            df.loc[last_idx] = new_row
        else:
            df = pd.concat([df, pd.DataFrame([new_row])])
            df = df[~df.index.duplicated(keep="last")]
            df = df.sort_index()

        return df

    def _pre_warm_caches_from_quotes(self, quotes: dict[str, dict], m_state: Any) -> None:
        """一括 quote 情報をもとに、各種キャッシュ (メモリ/グローバル) にメタデータを事前注入する"""
        from utils.caching import _set_cached_value

        for symbol, quote in quotes.items():
            if not quote:
                continue

            prev_close = quote.get("regularMarketPreviousClose")
            currency = quote.get("currency")
            if currency is None:
                currency = self._infer_currency_from_symbol(symbol)

            # 1. fastinfo_{symbol} キャッシュの注入
            fast_cache_key = f"fastinfo_{symbol}"
            fast_data = {
                "shortName": quote.get("shortName"),
                "regularMarketPreviousClose": prev_close,
                "previousClose": prev_close or quote.get("previousClose"),
                "currency": currency,
                "marketCap": quote.get("marketCap"),
                "exchange": quote.get("exchange"),
                "quoteType": quote.get("quoteType"),
                "symbol": symbol,
            }
            fast_data = {k: v for k, v in fast_data.items() if v is not None}

            try:
                with m_state.yfinance_short_cache_lock:
                    m_state.yfinance_short_cache[fast_cache_key] = fast_data
            except (AttributeError, RuntimeError, TypeError):
                pass

            # 2. info_short_{symbol} キャッシュ (get_stock_info_cached 用) の注入
            info_cache_key = f"info_short_{symbol}"
            info_data = {
                "trailingPE": quote.get("trailingPE"),
                "forwardPE": quote.get("forwardPE"),
                "priceToBook": quote.get("priceToBook"),
                "pegRatio": quote.get("pegRatio"),
                "dividendYield": quote.get("dividendYield"),
                "earningsPerShare": quote.get("epsTrailingTwelveMonths") or quote.get("earningsPerShare"),
                "epsForward": quote.get("epsForward"),
                "bookValue": quote.get("bookValue"),
                "marketCap": quote.get("marketCap"),
                "beta": quote.get("beta"),
                "fiftyTwoWeekHigh": quote.get("fiftyTwoWeekHigh"),
                "fiftyTwoWeekLow": quote.get("fiftyTwoWeekLow"),
                "targetMeanPrice": quote.get("targetPriceMean") or quote.get("targetMeanPrice"),
                "recommendationKey": quote.get("averageAnalystRating") or quote.get("recommendationKey"),
                "shortName": quote.get("shortName"),
                "longName": quote.get("longName"),
                "exchange": quote.get("exchange"),
                "quoteType": quote.get("quoteType"),
                "currency": currency,
                "sharesOutstanding": quote.get("sharesOutstanding"),
                "fiftyDayAverage": quote.get("fiftyDayAverage"),
                "twoHundredDayAverage": quote.get("twoHundredDayAverage"),
                "regularMarketPreviousClose": prev_close,
                "previousClose": prev_close or quote.get("previousClose"),
                "symbol": symbol,
            }
            info_data = {k: v for k, v in info_data.items() if v is not None}

            try:
                with m_state.yfinance_short_cache_lock:
                    m_state.yfinance_short_cache[info_cache_key] = info_data
                _set_cached_value(f"info_{symbol}", info_data, 86400)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass

            # 3. cal_{symbol} キャッシュ (決算日用) の注入
            earnings_timestamp = quote.get("earningsTimestamp")
            if earnings_timestamp:
                try:
                    dt = datetime.fromtimestamp(earnings_timestamp)
                    cal_data = {"Earnings Date": [dt.strftime("%Y-%m-%d %H:%M:%S")]}
                    _set_cached_value(f"cal_{symbol}", cal_data, 3600)
                except (ValueError, TypeError, OSError):
                    pass

    @with_yfinance_retry(max_retries=2, base_delay=3.0, backoff_factor=3.0)
    def download_batch(self, symbols: List[str], period: str = "3mo") -> pd.DataFrame:
        m_state = self._get_market_state()
        if m_state.is_yf_rate_limited():
            logger.info("yfinance is rate-limited; skipping batch download for %d symbols", len(symbols))
            return pd.DataFrame()

        # 1. 一括 quote 情報を取得する
        quotes = self.fetch_quotes_batch(symbols)
        if not quotes:
            logger.warning("Batch quotes fetch returned empty.")
            return pd.DataFrame()

        # 事前キャッシュ注入
        self._pre_warm_caches_from_quotes(quotes, m_state)

        # 2. 各銘柄について、履歴データをキャッシュから引き出す、もしくは並行して取得する
        from app_state import app_state
        merged_dfs = []
        cache_miss_symbols = []
        hist_by_symbol = {}

        for symbol in symbols:
            # a. インメモリキャッシュをチェック
            cache_key = f"history_short_{symbol}_{period}_1d"
            try:
                with m_state.yfinance_short_cache_lock:
                    cached_hist = m_state.yfinance_short_cache.get(cache_key)
            except (AttributeError, RuntimeError):
                cached_hist = None

            if cached_hist is not None and isinstance(cached_hist, pd.DataFrame) and not cached_hist.empty:
                hist_by_symbol[symbol] = cached_hist.copy()
                continue

            # b. ディスクキャッシュをチェック
            disk_key = f"hist_df_{symbol}_{period}"
            cached_disk = None
            try:
                data = app_state.stock_disk_cache.get(disk_key)
                if data and isinstance(data, dict) and data.get("type") == "dataframe":
                    json_str = data.get("json_data")
                    if json_str:
                        df = pd.read_json(json_str, orient="split")
                        if not df.empty:
                            df.index = pd.to_datetime(df.index)
                            cached_disk = df
            except (IOError, OSError, ValueError, KeyError, TypeError) as disk_exc:
                logger.debug("Disk cache retrieval failed for %s: %s", symbol, disk_exc)

            if cached_disk is not None:
                hist_by_symbol[symbol] = cached_disk
                # メモリキャッシュにも載せておく
                try:
                    with m_state.yfinance_short_cache_lock:
                        m_state.yfinance_short_cache[cache_key] = cached_disk.copy()
                except (AttributeError, RuntimeError, TypeError):
                    pass
                continue

            # c. キャッシュにない場合は並行フェッチの対象にする
            cache_miss_symbols.append(symbol)

        # キャッシュミスの銘柄を一括ダウンロード
        if cache_miss_symbols:
            logger.info("Cache miss for %d symbols; fetching in batch via yf.download", len(cache_miss_symbols))
            try:
                sess = yf_session_manager.get_session()
                batch_downloaded = yf.download(
                    tickers=cache_miss_symbols,
                    period=period,
                    interval="1d",
                    group_by="column",
                    session=sess,
                    threads=False,
                    auto_adjust=True,
                    progress=False,
                    timeout=20,
                )
                if not batch_downloaded.empty:
                    for sym in cache_miss_symbols:
                        try:
                            if isinstance(batch_downloaded.columns, pd.MultiIndex):
                                sym_df = batch_downloaded.xs(sym, axis=1, level=1)
                            else:
                                sym_df = batch_downloaded.copy()
                            sym_df = normalize_history_frame(sym_df)
                            if not sym_df.empty:
                                hist_by_symbol[sym] = sym_df
                                # Cache it
                                cache_key = f"history_short_{sym}_{period}_1d"
                                disk_key = f"hist_df_{sym}_{period}"
                                with m_state.yfinance_short_cache_lock:
                                    m_state.yfinance_short_cache[cache_key] = sym_df.copy()
                                try:
                                    data = {
                                        "type": "dataframe",
                                        "json_data": sym_df.to_json(orient="split", date_format="iso")
                                    }
                                    app_state.stock_disk_cache.set(disk_key, data)
                                except Exception:
                                    logger.debug("Failed to cache history for %s", sym)
                        except Exception as e:
                            logger.debug("Failed to extract %s from yf.download: %s", sym, e)
            except Exception as exc:
                logger.warning("Batch yf.download failed: %s. Falling back to parallel individual fetches.", exc)
                if _is_yfinance_rate_limit_error(exc):
                    _handle_yf_rate_limit(exc, m_state, context="batch yf.download")

            # Fallback for any symbols that still missed
            remaining_miss = [sym for sym in cache_miss_symbols if sym not in hist_by_symbol]
            if remaining_miss:
                logger.info("Falling back to parallel fetches for %d remaining missed symbols", len(remaining_miss))
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    futures = {
                        pool.submit(self._fetch_single_history, sym, period, m_state): sym
                        for sym in remaining_miss
                    }
                    for future in concurrent.futures.as_completed(futures):
                        sym = futures[future]
                        try:
                            df = future.result()
                            if df is not None and not df.empty:
                                hist_by_symbol[sym] = df
                        except (ValueError, TypeError, RuntimeError, AttributeError) as e:
                            logger.warning("Failed to fetch single history in batch for %s: %s", sym, e)

        # 3. 各銘柄の履歴データに対し、最新の quote をマージする
        for symbol in symbols:
            df = hist_by_symbol.get(symbol)
            if df is None or df.empty:
                continue

            quote = quotes.get(symbol)
            if quote:
                try:
                    df = self._merge_quote_into_history(df, quote, symbol)
                    # マージ後の DataFrame を再キャッシュ (メモリとディスク)
                    cache_key = f"history_short_{symbol}_{period}_1d"
                    disk_key = f"hist_df_{symbol}_{period}"
                    try:
                        with m_state.yfinance_short_cache_lock:
                            m_state.yfinance_short_cache[cache_key] = df.copy()
                        data = {
                            "type": "dataframe",
                            "json_data": df.to_json(orient="split", date_format="iso")
                        }
                        app_state.stock_disk_cache.set(disk_key, data)
                    except (AttributeError, RuntimeError, TypeError, IOError, OSError):
                        pass
                except (ValueError, TypeError, KeyError, AttributeError, RuntimeError) as merge_exc:
                    logger.debug("Failed to merge quote for %s: %s", symbol, merge_exc)

            df_col = df.copy()
            df_col.columns = pd.MultiIndex.from_product([df_col.columns, [symbol]])
            merged_dfs.append(df_col)

        if not merged_dfs:
            return pd.DataFrame()
        if len(merged_dfs) == 1:
            return merged_dfs[0]

        try:
            return pd.concat(merged_dfs, axis=1)
        except (ValueError, TypeError, KeyError) as exc:
            logger.error("Failed to concatenate merged dataframes in download_batch: %s", exc, exc_info=True)
            return merged_dfs[0] if merged_dfs else pd.DataFrame()

    def _infer_currency_from_symbol(self, symbol: str) -> Optional[str]:
        """Infer currency from symbol suffix — no yfinance call needed.

        Japanese stocks listed on TSE use the .T suffix and trade in JPY.
        Index tickers (^ prefix) are typically in USD.
        All others default to None (caller handles fallback).
        """
        if symbol.endswith(".T"):
            return "JPY"
        if symbol.startswith("^"):
            return "USD"
        if "." not in symbol:
            return "USD"
        return None

    @with_yfinance_retry(max_retries=2, base_delay=1.0, backoff_factor=2.0)
    def get_fast_info(self, symbol: str) -> dict:
        m_state = self._get_market_state()
        if m_state.is_yf_rate_limited():
            return {}
        # Speedup: reuse cached fast_info (previous close, currency, etc.) which
        # rarely changes within a sync cycle.
        cache_key = f"fastinfo_{symbol}"
        try:
            with m_state.yfinance_short_cache_lock:
                cached_fast = m_state.yfinance_short_cache.get(cache_key)
            if isinstance(cached_fast, dict):
                return dict(cached_fast)
        except (AttributeError, RuntimeError):
            cached_fast = None

        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            fast = t.fast_info

            def _fast_get(field_names):
                for field_name in field_names:
                    if isinstance(fast, dict) and field_name in fast:
                        value = fast.get(field_name)
                    else:
                        value = getattr(fast, field_name, None)
                    if value is not None:
                        return value
                return None

            prev_close = _fast_get(
                ["previous_close", "regular_market_previous_close", "previousClose"]
            )
            currency = _fast_get(["currency", "financial_currency"])
            if currency is None:
                currency = self._infer_currency_from_symbol(symbol)
            if currency is None:
                try:
                    currency = (t.info or {}).get("currency")
                except (AttributeError, ValueError, RuntimeError):
                    currency = None

            mapped_info = {
                "shortName": None,
                "regularMarketPreviousClose": prev_close,
                "previousClose": prev_close,
                "currency": currency,
                "marketCap": _fast_get(["market_cap", "marketCap"]),
                "exchange": _fast_get(["exchange"]),
                "quoteType": _fast_get(["quote_type", "quoteType"]),
                "symbol": symbol,
            }
            cleaned = {k: v for k, v in mapped_info.items() if v is not None}
            if cleaned:
                try:
                    with m_state.yfinance_short_cache_lock:
                        m_state.yfinance_short_cache[cache_key] = dict(cleaned)
                except Exception as cache_exc:
                    logger.debug(
                        "Failed to cache fast_info for %s: %s", symbol, cache_exc
                    )
                return cleaned
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OSError) as exc:
            logger.debug("yfinance ticker.fast_info failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                _handle_yf_rate_limit(exc, m_state, context=f"fast_info symbol={symbol}")
                raise
            exc_name = type(exc).__name__
            if "Timeout" in exc_name or "Connection" in exc_name:
                raise
        return {}

    @with_yfinance_retry(max_retries=2, base_delay=2.0, backoff_factor=2.0)
    def get_info(self, symbol: str) -> dict:
        """Fetch full ticker info including fundamental data (P/E, dividend, etc.)."""
        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            info = t.info
            if not info or not isinstance(info, dict):
                return {}

            # Fields to extract from ticker.info for fundamental data
            fundamental_keys = [
                "trailingPE", "forwardPE", "priceToBook", "pegRatio",
                "dividendYield", "trailingAnnualDividendYield",
                "earningsPerShare", "epsForward", "revenuePerShare",
                "bookValue", "priceToSalesTrailing12Months",
                "enterpriseToEbitda", "enterpriseToRevenue",
                "profitMargins", "grossMargins", "operatingMargins",
                "returnOnEquity", "returnOnAssets",
                "totalRevenue", "revenueGrowth", "earningsGrowth",
                "totalCash", "totalDebt", "debtToEquity",
                "currentRatio", "quickRatio",
                "freeCashflow", "operatingCashflow",
                "targetMeanPrice", "targetHighPrice", "targetLowPrice",
                "recommendationMean", "recommendationKey",
                "numberOfAnalystOpinions",
                "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
                "fiftyDayAverage", "twoHundredDayAverage",
                "shortName", "longName", "sector", "industry",
                "currency", "marketCap",
                "sharesOutstanding", "floatShares",
                "heldPercentInsiders", "heldPercentInstitutions",
                "shortRatio", "shortPercentOfFloat",
            ]

            result = {}
            for key in fundamental_keys:
                val = info.get(key)
                if val is not None:
                    result[key] = val

            return result
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OSError) as exc:
            logger.debug("yfinance ticker.info failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _handle_yf_rate_limit(exc, m_state, context=f"info symbol={symbol}")
                raise
            exc_name = type(exc).__name__
            if "Timeout" in exc_name or "Connection" in exc_name:
                raise
        return {}

    def _df_to_records(self, df: Optional[pd.DataFrame], limit: int = 0) -> list[dict]:
        """Convert a DataFrame to a list of dicts, handling DatetimeIndex and NaT."""
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return []
        try:
            df = df.copy()
            if isinstance(df.index, pd.DatetimeIndex):
                df.index = df.index.strftime("%Y-%m-%d %H:%M:%S")  # type: ignore[attr-defined]
            df = df.reset_index()
            # Replace NaN/NaT with None for JSON serialization using vectorised Pandas operations
            df = df.astype(object).where(pd.notnull(df), None)
            records = df.to_dict("records")
            if limit > 0:
                records = records[:limit]
            return records
        except Exception as exc:
            logger.debug("DataFrame to records conversion failed: %s", exc)
            return []

    def get_earnings_dates(self, symbol: str, limit: int = 8) -> list[dict]:
        """Fetch upcoming and past earnings dates with EPS estimate/actual/surprise."""
        t = self.get_ticker(symbol)
        if not t:
            return []
        try:
            df = t.get_earnings_dates(limit=limit)
            return self._df_to_records(df, limit)
        except Exception as exc:
            logger.debug("yfinance earnings_dates failed for %s: %s", symbol, exc)
            return []

    def get_recommendations(self, symbol: str) -> list[dict]:
        """Fetch analyst recommendation summary (buy/hold/sell counts)."""
        t = self.get_ticker(symbol)
        if not t:
            return []
        try:
            df = t.get_recommendations()
            return self._df_to_records(df)
        except Exception as exc:
            logger.debug("yfinance recommendations failed for %s: %s", symbol, exc)
            return []

    def get_institutional_holders(self, symbol: str) -> list[dict]:
        """Fetch top institutional holders."""
        t = self.get_ticker(symbol)
        if not t:
            return []
        try:
            df = t.get_institutional_holders()
            return self._df_to_records(df)
        except Exception as exc:
            logger.debug("yfinance institutional_holders failed for %s: %s", symbol, exc)
            return []

    def get_major_holders(self, symbol: str) -> dict:
        """Fetch ownership breakdown (insiders, institutions, mutual funds)."""
        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            df = t.get_major_holders()
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                return {}
            return {str(k): v for k, v in df.to_dict().items() if v is not None}
        except Exception as exc:
            logger.debug("yfinance major_holders failed for %s: %s", symbol, exc)
            return {}

    def get_analyst_targets(self, symbol: str) -> dict:
        """Fetch analyst price targets (mean, median, high, low)."""
        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            targets = t.get_analyst_price_targets()
            if isinstance(targets, dict):
                return {k: v for k, v in targets.items() if v is not None}
            return {}
        except Exception as exc:
            logger.debug("yfinance analyst_targets failed for %s: %s", symbol, exc)
            return {}

    def get_calendar(self, symbol: str) -> dict:
        """Fetch earnings/dividend calendar (next earnings date, dividend dates)."""
        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            cal = t.get_calendar()
            if isinstance(cal, dict):
                # Convert date objects to strings for JSON
                result = {}
                for k, v in cal.items():
                    if hasattr(v, "isoformat"):
                        result[k] = v.isoformat()
                    elif isinstance(v, list):
                        result[k] = [
                            item.isoformat() if hasattr(item, "isoformat") else item
                            for item in v
                        ]
                    else:
                        result[k] = v
                return result
            return {}
        except Exception as exc:
            logger.debug("yfinance calendar failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _handle_yf_rate_limit(exc, m_state, context=f"calendar symbol={symbol}")
                return {}
            return {}

    def get_news(self, symbol: str, limit: int = 10) -> list[dict]:
        """Fetch recent news for a stock ticker."""
        t = self.get_ticker(symbol)
        if not t:
            return []
        try:
            raw_news = t.get_news()
            if not isinstance(raw_news, list):
                return []
            results = []
            for item in raw_news[:limit]:
                content = item.get("content", {}) if isinstance(item, dict) else {}
                provider = content.get("provider", {}) if isinstance(content, dict) else {}
                thumbnail = content.get("thumbnail", {}) if isinstance(content, dict) else {}
                results.append({
                    "title": content.get("title", ""),
                    "summary": (content.get("summary") or "")[:200],
                    "pubDate": content.get("pubDate") or content.get("displayTime", ""),
                    "provider": provider.get("displayName", "") if isinstance(provider, dict) else "",
                    "providerUrl": provider.get("url", "") if isinstance(provider, dict) else "",
                    "thumbnailUrl": thumbnail.get("originalUrl", "") if isinstance(thumbnail, dict) else "",
                    "link": (content.get("canonicalUrl") or {}).get("url", "")
                            if isinstance(content.get("canonicalUrl"), dict)
                            else content.get("clickThroughUrl", {}).get("url", "")
                            if isinstance(content.get("clickThroughUrl"), dict) else "",
                })
            return results
        except Exception as exc:
            logger.debug("yfinance news failed for %s: %s", symbol, exc)
            return []

    def get_option_chain(self, symbol: str) -> dict:
        """Fetch options chain (puts and calls for nearest expiry)."""
        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            options_dates = t.options
            if not options_dates:
                return {}
            nearest = options_dates[0]
            chain = t.option_chain(nearest)
            result = {"expiry": nearest}
            if hasattr(chain, "calls") and isinstance(chain.calls, pd.DataFrame) and not chain.calls.empty:
                result["calls"] = self._df_to_records(chain.calls)
            if hasattr(chain, "puts") and isinstance(chain.puts, pd.DataFrame) and not chain.puts.empty:
                result["puts"] = self._df_to_records(chain.puts)
            result["available_dates"] = list(options_dates[:12])
            return result
        except Exception as exc:
            logger.debug("yfinance option_chain failed for %s: %s", symbol, exc)
            return {}

    def get_revenue_estimate(self, symbol: str) -> list[dict]:
        """Fetch analyst revenue estimates (quarterly/yearly)."""
        t = self.get_ticker(symbol)
        if not t:
            return []
        try:
            df = t.get_revenue_estimate()
            return self._df_to_records(df)
        except Exception as exc:
            logger.debug("yfinance revenue_estimate failed for %s: %s", symbol, exc)
            return []

    def get_earnings_estimate(self, symbol: str) -> list[dict]:
        """Fetch analyst earnings estimates (quarterly/yearly)."""
        t = self.get_ticker(symbol)
        if not t:
            return []
        try:
            df = t.get_earnings_estimate()
            return self._df_to_records(df)
        except Exception as exc:
            logger.debug("yfinance earnings_estimate failed for %s: %s", symbol, exc)
            return []

    def get_valuation_measures(self, symbol: str) -> list[dict]:
        """Fetch historical valuation measures (P/E, PEG, P/S, etc.)."""
        t = self.get_ticker(symbol)
        if not t:
            return []
        try:
            df = t.get_valuation_measures()
            return self._df_to_records(df)
        except Exception as exc:
            logger.debug("yfinance valuation_measures failed for %s: %s", symbol, exc)
            return []

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search for stocks/instruments via yfinance Search."""
        if not query or len(query.strip()) < 2:
            return []
        m_state = self._get_market_state()
        if m_state.is_yf_rate_limited():
            return []
        try:
            sess = yf_session_manager.get_session()
            s = yf.Search(query, session=sess)
            quotes = getattr(s, "quotes", []) or []
            results = []
            for item in quotes[:max_results]:
                sym = item.get("symbol")
                if not sym:
                    continue
                results.append(
                    {
                        "symbol": sym,
                        # L-8: Return empty string rather than a hardcoded Japanese UI string.
                        # Display fallback ("名称不明" etc.) should be handled by the frontend.
                        "name": item.get("shortname") or item.get("longname") or "",
                        "exchange": item.get("exchange") or item.get("exchDisp") or "",
                    }
                )
            return results
        except Exception as exc:
            logger.error("yfinance Search failed (%s): %s", query, exc)
            if _is_yfinance_rate_limit_error(exc):
                _handle_yf_rate_limit(exc, m_state, context=f"search query={query}")
                return []
            return []
