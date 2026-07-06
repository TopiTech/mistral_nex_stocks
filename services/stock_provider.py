# services/stock_provider.py
"""Stock Data Provider Abstraction Layer for Mistral NeX Stocks.

Provides uniform interface for retrieving stock ticker data, historical series,
batch downloads, and fast attributes.
"""

import time
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any, Callable, List, Optional, TypeVar
import logging
import random
import pandas as pd
import yfinance as yf

from requests.exceptions import ConnectionError as RequestsConnectionError
from constants import RequestsTimeout, CurlRequestsTimeout

logger = logging.getLogger(__name__)


def _is_yfinance_rate_limit_error(exc: Exception) -> bool:
    """Detect yfinance 401/429-style failures from exception objects."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    exc_name = type(exc).__name__.lower()
    exc_text = str(exc).lower()
    return bool(
        status_code in (401, 429)
        or "ratelimit" in exc_name
        or "too many requests" in exc_text
        or "invalid crumb" in exc_text
        or "unauthorized" in exc_text
        or "rate limit" in exc_text
    )

# Type variable for the retry decorator
F = TypeVar("F", bound=Callable[..., Any])

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
    # Apply environment variable override for backoff_factor at decoration time
    try:
        from constants import YFINANCE_RETRY_BACKOFF_BASE as _env_backoff
        if _env_backoff != backoff_factor:
            backoff_factor = _env_backoff
    except (ImportError, AttributeError):
        pass

    # Module-level reference to avoid repeated imports inside the loop
    _app_state_ref: Any = None

    def _rate_limit_multiplier() -> float:
        """Return 3x multiplier if app-level rate limiter is active."""
        nonlocal _app_state_ref
        if _app_state_ref is None:
            try:
                from app_state import app_state as _app_state_ref  # type: ignore[no-redef]
            except (ImportError, AttributeError):
                return 1.0
        try:
            if _app_state_ref.is_yf_rate_limited():
                return 3.0
        except (ImportError, AttributeError):
            pass
        return 1.0

    def decorator(f: F) -> F:
        @wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    return f(*args, **kwargs)
                except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        rl_mult = _rate_limit_multiplier()
                        delay = base_delay * (backoff_factor ** attempt) * rl_mult
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
                        rl_mult = _rate_limit_multiplier()
                        delay = base_delay * (backoff_factor ** attempt) * rl_mult
                        jitter = delay * random.uniform(-0.25, 0.25)
                        time.sleep(delay + jitter)
                except Exception as exc:
                    # Non-retriable errors: re-raise immediately
                    # Check for yfinance rate limit errors
                    if _is_yfinance_rate_limit_error(exc):
                        last_exception = exc
                        if attempt < max_retries:
                            rl_mult = _rate_limit_multiplier()
                            delay = max(base_delay * (backoff_factor ** attempt) * rl_mult, 1.0)
                            jitter = delay * random.uniform(-0.1, 0.1)
                            logger.warning(
                                "yfinance rate limited (%s), retry %d/%d after %.1fs (rl_mult=%.1f)",
                                type(exc).__name__, attempt + 1, max_retries, delay + jitter, rl_mult,
                            )
                            time.sleep(delay + jitter)
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
    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search for stocks/instruments by query string."""


class YFinanceProvider(BaseStockProvider):
    """Yahoo Finance API provider implementation."""

    def get_ticker(self, symbol: str) -> Optional[Any]:
        from app_state import yf_session_manager
        from app_state import app_state
        if app_state.is_yf_rate_limited():
            return None
        try:
            sess = yf_session_manager.get_session()
            return yf.Ticker(symbol, session=sess)
        except (ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            logger.debug("yf.Ticker creation failed for %s: %s", symbol, exc)
            return None

    @with_yfinance_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
    def get_history(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        from app_state import app_state
        from constants import YFINANCE_TIMEOUT_SINGLE
        from app_helpers import normalize_history_frame

        if app_state.is_circuit_open("yfinance_history", symbol=symbol):
            logger.info("stock-history circuit open symbol=%s", symbol)
            return pd.DataFrame()
        if app_state.is_yf_rate_limited():
            logger.info("yfinance is rate-limited; skipping history fetch symbol=%s", symbol)
            return pd.DataFrame()

        t = self.get_ticker(symbol)
        if not t:
            return pd.DataFrame()

        try:
            result = t.history(
                period=period,
                interval=interval,
                auto_adjust=True,
                timeout=YFINANCE_TIMEOUT_SINGLE,
            )
            app_state.report_circuit_result(
                "yfinance_history", success=True, symbol=symbol
            )
            return normalize_history_frame(result)
        except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as timeout_exc:
            from constants import HISTORY_CIRCUIT_BREAKER_THRESHOLD, HISTORY_CIRCUIT_BREAKER_OPEN_SEC
            app_state.report_circuit_result(
                "yfinance_history",
                success=False,
                symbol=symbol,
                threshold=HISTORY_CIRCUIT_BREAKER_THRESHOLD,
                open_sec=HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
            )
            logger.debug("stock-history timeout symbol=%s err=%s", symbol, timeout_exc)
            raise  # Re-raise for retry decorator to handle
        except Exception as exc:
            logger.debug("stock-history error symbol=%s err=%s", symbol, exc)
            # Check for yfinance rate limit errors
            if _is_yfinance_rate_limit_error(exc):
                backoff = app_state.mark_yf_429()
                logger.warning(
                    "yfinance rate limit detected for history symbol=%s; backing off %.0fs",
                    symbol,
                    backoff,
                )
                return pd.DataFrame()
            return pd.DataFrame()

    @with_yfinance_retry(max_retries=2, base_delay=3.0, backoff_factor=3.0)
    def download_batch(self, symbols: List[str], period: str = "3mo") -> pd.DataFrame:
        from constants import YFINANCE_TIMEOUT_BATCH
        from app_state import yf_session_manager
        from app_state import app_state
        if app_state.is_yf_rate_limited():
            logger.info("yfinance is rate-limited; skipping batch download for %d symbols", len(symbols))
            return pd.DataFrame()
        try:
            sess = yf_session_manager.get_session()
            # threads=True enables parallel HTTP fetch within yfinance's managed session.
            # This is the single biggest performance improvement: with 30+ symbols,
            # sequential download (threads=False) takes 20-30s, while parallel takes 3-5s.
            # Since all requests share the same session with crumb negotiation,
            # parallel downloads are safe and don't trigger additional rate limiting.
            return yf.download(
                symbols,
                period=period,
                auto_adjust=True,
                threads=True,
                progress=False,
                timeout=YFINANCE_TIMEOUT_BATCH,
                session=sess,
            )
        except Exception as exc:
            logger.warning("Batch download failed with exception: %s", exc)
            # Re-raise retriable errors for retry decorator
            if _is_yfinance_rate_limit_error(exc):
                backoff = app_state.mark_yf_429()
                logger.warning(
                    "yfinance rate limit detected for batch download; backing off %.0fs",
                    backoff,
                )
                return pd.DataFrame()
            exc_name = type(exc).__name__
            if "Timeout" in exc_name:
                raise
            return pd.DataFrame()

    @with_yfinance_retry(max_retries=2, base_delay=1.0, backoff_factor=2.0)
    def get_fast_info(self, symbol: str) -> dict:
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
                try:
                    currency = (t.info or {}).get("currency")
                except Exception:
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
                return cleaned
        except Exception as exc:
            logger.debug("yfinance ticker.fast_info failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                from app_state import app_state
                backoff = app_state.mark_yf_429()
                logger.warning(
                    "yfinance rate limit detected for fast_info symbol=%s; backing off %.0fs",
                    symbol,
                    backoff,
                )
                return {}
            exc_name = type(exc).__name__
            if "Timeout" in exc_name:
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
        except Exception as exc:
            logger.debug("yfinance ticker.info failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                from app_state import app_state
                backoff = app_state.mark_yf_429()
                logger.warning(
                    "yfinance rate limit detected for info symbol=%s; backing off %.0fs",
                    symbol,
                    backoff,
                )
                return {}
            exc_name = type(exc).__name__
            if "Timeout" in exc_name:
                raise
        return {}

    def _df_to_records(self, df: Optional[pd.DataFrame], limit: int = 0) -> list[dict]:
        """Convert a DataFrame to a list of dicts, handling DatetimeIndex and NaT."""
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return []
        try:
            if isinstance(df.index, pd.DatetimeIndex):
                df = df.copy()
                df.index = df.index.strftime("%Y-%m-%d %H:%M:%S")  # type: ignore[attr-defined]
            records = df.reset_index().to_dict("records")
            # Replace NaN/NaT with None for JSON serialization
            cleaned = []
            for r in records:
                cleaned.append({k: (None if pd.isna(v) else v) for k, v in r.items()})
            if limit > 0:
                cleaned = cleaned[:limit]
            return cleaned
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
                from app_state import app_state
                app_state.mark_yf_429()
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
        from app_state import app_state
        if app_state.is_yf_rate_limited():
            return []
        from app_state import yf_session_manager
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
                        "name": item.get("shortname")
                        or item.get("longname")
                        or "名称不明",
                        "exchange": item.get("exchange") or item.get("exchDisp") or "",
                    }
                )
            return results
        except Exception as exc:
            logger.error("yfinance Search failed (%s): %s", query, exc)
            if _is_yfinance_rate_limit_error(exc):
                backoff = app_state.mark_yf_429()
                logger.warning(
                    "yfinance rate limit detected for search query=%s; backing off %.0fs",
                    query,
                    backoff,
                )
                return []
            return []
