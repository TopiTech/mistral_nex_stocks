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

from requests.exceptions import Timeout as RequestsTimeout
try:
    from curl_cffi.requests.exceptions import Timeout as CurlRequestsTimeout
except ImportError:
    CurlRequestsTimeout = RequestsTimeout  # type: ignore[misc,assignment,unused-ignore]

logger = logging.getLogger(__name__)

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
                        delay = base_delay * (backoff_factor ** attempt)
                        jitter = delay * random.uniform(-0.25, 0.25)
                        total_delay = delay + jitter
                        _target = getattr(args[0], "symbol", None) if args else None
                        logger.debug(
                            "yfinance retry %d/%d for %s after timeout, waiting %.2fs",
                            attempt + 1, max_retries, _target or str(args),
                            total_delay,
                        )
                        time.sleep(total_delay)
                except (ConnectionError, OSError) as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        delay = base_delay * (backoff_factor ** attempt)
                        jitter = delay * random.uniform(-0.25, 0.25)
                        time.sleep(delay + jitter)
                except Exception as exc:
                    # Non-retriable errors: re-raise immediately
                    # Check for yfinance rate limit errors
                    exc_name = type(exc).__name__
                    if "RateLimit" in exc_name or "YFRateLimit" in exc_name:
                        last_exception = exc
                        if attempt < max_retries:
                            delay = base_delay * (backoff_factor ** attempt) * 2
                            jitter = delay * random.uniform(-0.1, 0.1)
                            logger.warning(
                                "yfinance rate limited (%s), retry %d/%d after %.1fs",
                                exc_name, attempt + 1, max_retries, delay + jitter,
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
            if "RateLimit" in type(exc).__name__:
                raise  # Let retry decorator handle rate limits
            return pd.DataFrame()

    @with_yfinance_retry(max_retries=2, base_delay=3.0, backoff_factor=3.0)
    def download_batch(self, symbols: List[str], period: str = "3mo") -> pd.DataFrame:
        from constants import YFINANCE_TIMEOUT_BATCH
        from app_state import yf_session_manager
        try:
            sess = yf_session_manager.get_session()
            return yf.download(
                symbols,
                period=period,
                auto_adjust=True,
                threads=False,
                progress=False,
                timeout=YFINANCE_TIMEOUT_BATCH,
                session=sess,
            )
        except Exception as exc:
            logger.warning("Batch download failed with exception: %s", exc)
            # Re-raise retriable errors for retry decorator
            exc_name = type(exc).__name__
            if any(kw in exc_name for kw in ("Timeout", "RateLimit")):
                raise
            return pd.DataFrame()

    @with_yfinance_retry(max_retries=2, base_delay=1.0, backoff_factor=2.0)
    def get_fast_info(self, symbol: str) -> dict:
        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            fast = t.fast_info
            prev_close = (
                getattr(fast, "previous_close", None)
                or getattr(fast, "regular_market_previous_close", None)
                or getattr(fast, "previousClose", None)
            )
            if prev_close is not None:
                mapped_info = {
                    "shortName": None,
                    "regularMarketPreviousClose": prev_close,
                    "previousClose": prev_close,
                    "currency": getattr(fast, "currency", None),
                    "marketCap": getattr(fast, "market_cap", None)
                    or getattr(fast, "marketCap", None),
                    "exchange": getattr(fast, "exchange", None),
                    "quoteType": getattr(fast, "quote_type", None)
                    or getattr(fast, "quoteType", None),
                    "symbol": symbol,
                }
                return {k: v for k, v in mapped_info.items() if v is not None}
        except Exception as exc:
            logger.debug("yfinance ticker.fast_info failed for %s: %s", symbol, exc)
            exc_name = type(exc).__name__
            if "Timeout" in exc_name or "RateLimit" in exc_name:
                raise
        return {}

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search for stocks/instruments via yfinance Search."""
        if not query or len(query.strip()) < 2:
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
            return []
