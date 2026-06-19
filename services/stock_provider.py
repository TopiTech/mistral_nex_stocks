# services/stock_provider.py
"""Stock Data Provider Abstraction Layer for Mistral NeX Stocks.

Provides uniform interface for retrieving stock ticker data, historical series,
batch downloads, and fast attributes.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
import logging
import time
import pandas as pd
import yfinance as yf

from requests.exceptions import Timeout as RequestsTimeout
try:
    from curl_cffi.requests.exceptions import Timeout as CurlRequestsTimeout
except ImportError:
    CurlRequestsTimeout = RequestsTimeout  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


class BaseStockProvider(ABC):
    """Abstract Base Class for Stock Providers."""

    @abstractmethod
    def get_ticker(self, symbol: str) -> Optional[Any]:
        """Wrap ticker object instantiation with defensive validation."""
        pass

    @abstractmethod
    def get_history(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch historical data for a specific stock ticker."""
        pass

    @abstractmethod
    def download_batch(self, symbols: List[str], period: str = "3mo") -> pd.DataFrame:
        """Download historical series in batch for multiple tickers."""
        pass

    @abstractmethod
    def get_fast_info(self, symbol: str) -> dict:
        """Retrieve lightweight attributes for metadata caching."""
        pass


class YFinanceProvider(BaseStockProvider):
    """Yahoo Finance API provider implementation."""

    def get_ticker(self, symbol: str) -> Optional[Any]:
        try:
            return yf.Ticker(symbol)
        except (ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            logger.debug("yf.Ticker creation failed for %s: %s", symbol, exc)
            return None

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
            return pd.DataFrame()
        except Exception as exc:
            logger.debug("stock-history error symbol=%s err=%s", symbol, exc)
            return pd.DataFrame()

    def download_batch(self, symbols: List[str], period: str = "3mo") -> pd.DataFrame:
        from constants import YFINANCE_TIMEOUT_BATCH
        try:
            return yf.download(
                symbols,
                period=period,
                auto_adjust=True,
                threads=False,
                progress=False,
                timeout=YFINANCE_TIMEOUT_BATCH,
            )
        except Exception as exc:
            logger.warning("Batch download failed with exception: %s", exc)
            return pd.DataFrame()

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
        return {}
