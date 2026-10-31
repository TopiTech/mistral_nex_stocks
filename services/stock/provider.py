"""YFinance プロバイダ本体。`services.stock_provider` からの純粋な移動。

`yf_session_manager` と `_handle_yf_rate_limit` はテストで
`patch(\"services.stock_provider.X\")` (名前置換) されるため、呼び出し時に
`services.stock_provider` 属性として解決する。`yf` / `time` への patch は共有
モジュールオブジェクトに対するものなので直接 import でよい。
"""

from __future__ import annotations

import concurrent.futures
import io
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from constants import CurlRequestsTimeout, RequestsTimeout
from services.stock import _shim
from services.stock.errors import (
    _is_yfinance_invalid_symbol_error,
    _is_yfinance_rate_limit_error,
)
from services.stock.retry import with_yfinance_retry
from services.stock.sanitize import _finite_float, sanitize_fundamental_dict
from session_manager import register_auth_reset_listener
from utils.caching import history_short_cache_key
from utils.normalization import is_jp_stock_code, normalize_history_frame
from utils.tradingview_mapper import get_ticker_exchange, resolve_exchange_prefix

logger = logging.getLogger("services.stock_provider")


# Bounded per-process cache of live ``yf.Ticker`` instances, keyed by symbol.
#
# Why this exists: ``yf.Ticker`` construction is cheap, but the *first* use of a
# fresh instance (``history()`` / ``fast_info`` / ``get_history_metadata()``)
# resolves the exchange timezone via a v8/finance/chart HTTP request that is
# memoized per instance. Recreating a Ticker for every call therefore costs an
# extra network round-trip per symbol per call. Reusing a live instance lets
# yfinance's own per-instance caches (``_tz``, ``PriceHistory._history_cache``,
# ``_history_metadata``) absorb repeat lookups within the TTL window.
#
# Entries are validated against the session manager on every hit: if the
# embedded session was reclaimed (idle reaper / UA-rotation epoch sweep /
# close_all) the ticker is rebuilt with a fresh session.
#
# Tunables live in the `services.stock_provider` facade (single owner) and
# are resolved via `_shim()` at call time so writes through the facade keep
# working.


class BaseStockProvider(ABC):
    """Abstract Base Class for Stock Providers."""

    @abstractmethod
    def get_ticker(self, symbol: str) -> Any | None:
        """Wrap ticker object instantiation with defensive validation."""

    @abstractmethod
    def get_history(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch historical data for a specific stock ticker."""

    @abstractmethod
    def download_batch(
        self, symbols: list[str], period: str = "3mo", lightweight: bool = False
    ) -> pd.DataFrame:
        """Download historical series in batch for multiple tickers."""

    @abstractmethod
    def get_fast_info(self, symbol: str) -> dict:
        """Retrieve lightweight attributes for metadata caching."""

    @abstractmethod
    def get_info(self, symbol: str) -> dict:
        """Fetch full ticker info including fundamental data."""

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search for stocks/instruments by query string."""


class YFinanceProvider(BaseStockProvider):
    """Yahoo Finance API provider implementation."""

    def __init__(self, market_state: Any | None = None):
        self._market_state = market_state
        self._ticker_cache: dict[str, tuple[Any, Any, float]] = {}
        self._ticker_cache_lock = threading.Lock()
        # Automatically drop ticker cache when yfinance sessions/cookies are reset
        register_auth_reset_listener(self.clear_ticker_cache)

    def _get_market_state(self) -> Any:
        if self._market_state is not None:
            return self._market_state
        from app_state import app_state

        return app_state.market

    def clear_ticker_cache(self) -> None:
        """Drop all cached Ticker instances (session rotations / test resets)."""
        with self._ticker_cache_lock:
            self._ticker_cache.clear()

    def get_ticker(self, symbol: str) -> Any | None:
        m_state = self._get_market_state()
        if (
            hasattr(m_state, "is_negative_cached_symbol")
            and m_state.is_negative_cached_symbol(symbol) is True
        ):
            logger.debug("symbol %s is in negative cache; skipping get_ticker", symbol)
            return None
        if m_state.is_yf_rate_limited():
            return None
        sess = _shim().yf_session_manager.get_session()
        now = time.monotonic()
        with self._ticker_cache_lock:
            entry = self._ticker_cache.get(symbol)
            if entry is not None:
                cached, cached_sess, ts = entry
                if (
                    (now - ts) < _shim()._TICKER_CACHE_TTL_SEC
                    and cached_sess is sess
                    and _shim().yf_session_manager.is_session_alive(cached_sess)
                ):
                    # Sliding TTL: refresh the timestamp on hit so hot symbols
                    # keep their live Ticker (and its per-instance caches)
                    # across sync cycles, and eviction order reflects usage.
                    self._ticker_cache.pop(symbol, None)
                    self._ticker_cache[symbol] = (cached, cached_sess, now)
                    return cached
                self._ticker_cache.pop(symbol, None)
        try:
            ticker = yf.Ticker(symbol, session=sess)
        except (ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            logger.debug("yf.Ticker creation failed for %s: %s", symbol, exc)
            return None
        with self._ticker_cache_lock:
            self._ticker_cache.pop(symbol, None)
            self._ticker_cache[symbol] = (ticker, sess, now)
            if len(self._ticker_cache) > _shim()._TICKER_CACHE_MAX:
                # Evict the oldest (insertion-ordered) entry to bound memory.
                for key in list(self._ticker_cache):
                    if key != symbol:
                        del self._ticker_cache[key]
                        break
        return ticker

    def _infer_exchange_from_symbol(self, symbol: str) -> str | None:
        """Resolve a TradingView exchange prefix from the symbol alone.

        Pure heuristic — never performs network I/O. Keeps Japanese stocks
        (``.T``) and index tickers (``^``) out of the network-backed exchange
        lookup path used by ``_pre_warm_caches_from_history`` /
        ``get_fast_info``, which is a frequent source of extra
        v8/finance/chart requests on a cold start.
        """
        if not isinstance(symbol, str):
            return None
        s = symbol.strip().upper()
        if not s:
            return None
        if s.endswith(".T"):
            return "TSE"
        if s.startswith("^"):
            return "INDEX"
        return None

    @with_yfinance_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
    def get_history(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        from constants import YFINANCE_TIMEOUT_SINGLE
        from utils.normalization import history_circuit_key, normalize_history_frame

        m_state = self._get_market_state()
        # The same canonical key the route/service layer reports. Deriving it
        # from the symbol keeps callers without an explicit market (the sync
        # worker) in the same failure bucket.
        circuit_key = history_circuit_key(symbol)

        # Speedup: serve a short-lived in-memory copy to avoid duplicate fetches
        # of the same (symbol, period, interval) within one sync cycle. This is
        # deliberately checked *before* the availability gates: a cache hit
        # performs no network I/O, so an open circuit or a 429 window must not
        # turn a known-good cached frame into an empty result. The gates still
        # protect the network fetch below, which is where they are meaningful.
        cache_key = history_short_cache_key(symbol, period, interval)
        try:
            with m_state.yfinance_short_cache_lock:
                cached_hist = m_state.yfinance_short_cache.get(cache_key)
            if cached_hist is not None:
                logger.debug(
                    "yfinance history cache hit symbol=%s period=%s interval=%s",
                    symbol,
                    period,
                    interval,
                )
                if isinstance(cached_hist, pd.DataFrame):
                    return cached_hist.copy()
                return cached_hist
        except (AttributeError, KeyError, RuntimeError):
            cached_hist = None

        disk_key = f"hist_df_{symbol}_{period}"
        if m_state.is_circuit_open("yfinance_history", symbol=circuit_key):
            logger.info("stock-history circuit open symbol=%s", circuit_key)
            disk_hist = self._load_history_from_disk_cache(disk_key)
            if disk_hist is not None and not disk_hist.empty:
                return disk_hist
            return pd.DataFrame()
        if m_state.is_yf_rate_limited():
            logger.info("yfinance is rate-limited; skipping history fetch symbol=%s", symbol)
            disk_hist = self._load_history_from_disk_cache(disk_key)
            if disk_hist is not None and not disk_hist.empty:
                return disk_hist
            return pd.DataFrame()

        t = self.get_ticker(symbol)
        if not t:
            disk_hist = self._load_history_from_disk_cache(disk_key)
            if disk_hist is not None and not disk_hist.empty:
                return disk_hist
            return pd.DataFrame()

        try:
            result = t.history(
                period=period,
                interval=interval,
                auto_adjust=True,
                actions=False,
                timeout=YFINANCE_TIMEOUT_SINGLE,
            )
            m_state.report_circuit_result("yfinance_history", success=True, symbol=circuit_key)
            try:
                _shim().yf_session_manager.reset_consecutive_401_count()
            except (AttributeError, RuntimeError):
                pass
            normalized = normalize_history_frame(result)
            if not normalized.empty:
                try:
                    with m_state.yfinance_short_cache_lock:
                        m_state.yfinance_short_cache[cache_key] = normalized.copy()
                except (AttributeError, RuntimeError, TypeError) as cache_exc:
                    logger.debug("Failed to cache yfinance history for %s: %s", symbol, cache_exc)
            return normalized
        except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as timeout_exc:
            from constants import (
                HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
                HISTORY_CIRCUIT_BREAKER_THRESHOLD,
            )

            m_state.report_circuit_result(
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
            # Rate-limit detection/recording is owned by @with_yfinance_retry:
            # re-raising here lets the decorator apply ONE graduated backoff
            # instead of double-counting the same 429 (streak grew 2x per
            # failure before this was consolidated).
            if _is_yfinance_rate_limit_error(exc):
                raise
            exc_name = type(exc).__name__
            if "Timeout" in exc_name or "Connection" in exc_name:
                raise
            return pd.DataFrame()

    def _derive_quote_from_history(self, df: pd.DataFrame, symbol: str) -> dict | None:
        """history DataFrame の末尾行から最新の quote 相当データを合成する。

        v7/finance/quote エンドポイントは Yahoo 側で厳しく制限・廃止されつつ
        あり 429/439 の直接的な原因となるため、quote 取得は行わず、もともと
        取得済みの history から price / previousClose / volume / marketTime を
        合成する。これにより yfinance リクエスト数を大幅に削減できる。
        """
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        try:
            last_row = df.iloc[-1]
            price = last_row.get("Close")
            prev_close = None
            if len(df) >= 2:
                prev_close = df["Close"].iloc[-2]
            volume = last_row.get("Volume")
            open_p = last_row.get("Open")
            high = last_row.get("High")
            low = last_row.get("Low")

            market_time_sec = None
            idx = df.index[-1]
            try:
                ts_attr = getattr(idx, "timestamp", None)
                if callable(ts_attr):
                    # pandas.Timestamp exposes timestamp() as a method.
                    market_time_sec = float(ts_attr())  # type: ignore[arg-type, operator]
                elif ts_attr is not None:
                    market_time_sec = float(ts_attr)
            except (AttributeError, TypeError, ValueError):
                market_time_sec = None

            price_value = _finite_float(price)
            prev_close_value = _finite_float(prev_close)
            volume_value = _finite_float(volume, default=0.0)
            if (volume_value is None or volume_value <= 0.0) and "Volume" in df.columns:
                pos_vols = df["Volume"][df["Volume"] > 0]
                if not pos_vols.empty:
                    volume_value = _finite_float(pos_vols.iloc[-1], default=0.0)
            open_value = _finite_float(open_p)
            high_value = _finite_float(high)
            low_value = _finite_float(low)
            market_time_value = _finite_float(market_time_sec)
            quote = {
                "symbol": symbol,
                "regularMarketPrice": price_value,
                "regularMarketPreviousClose": prev_close_value,
                "regularMarketVolume": int(volume_value) if volume_value is not None else 0,
                "regularMarketOpen": open_value,
                "regularMarketDayHigh": high_value,
                "regularMarketDayLow": low_value,
                "regularMarketTime": market_time_value,
            }
            return quote
        except (KeyError, IndexError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            logger.debug("Failed to derive quote from history for %s: %s", symbol, exc)
            return None

    def _fetch_single_history(self, symbol: str, period: str, m_state: Any) -> pd.DataFrame:
        """単一銘柄の履歴を yfinance から取得し、キャッシュを更新する"""
        disk_key = f"hist_df_{symbol}_{period}"
        try:
            df = self.get_history(symbol, period=period)
            if df is not None and not df.empty:
                try:
                    data = {
                        "type": "dataframe",
                        "json_data": df.to_json(orient="split", date_format="iso"),
                    }
                    from app_state import app_state

                    app_state.stock_disk_cache.set(disk_key, data)
                except (OSError, TypeError, ValueError) as cache_exc:
                    logger.debug("Failed to save df to disk cache for %s: %s", symbol, cache_exc)
                return df
        except (
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
            AttributeError,
            OSError,
            TimeoutError,
        ) as exc:
            logger.warning("Failed to fetch history for %s: %s", symbol, exc)
        cached = self._load_history_from_disk_cache(disk_key)
        if cached is not None and not cached.empty:
            return cached
        return pd.DataFrame()

    @staticmethod
    def _load_history_from_disk_cache(disk_key: str) -> pd.DataFrame | None:
        """ディスクキャッシュから履歴DataFrameの読み出しを試みる"""
        try:
            from app_state import app_state

            data = app_state.stock_disk_cache.get(disk_key)
            if data and isinstance(data, dict) and data.get("type") == "dataframe":
                json_str = data.get("json_data")
                if json_str:
                    df = pd.read_json(io.StringIO(json_str), orient="split")
                    if not df.empty:
                        df.index = pd.to_datetime(df.index)
                        return df
        except (OSError, ValueError, KeyError, TypeError) as disk_exc:
            logger.debug("Disk cache retrieval failed for %s: %s", disk_key, disk_exc)
        return None

    def _merge_quote_into_history(self, df: pd.DataFrame, quote: dict, symbol: str) -> pd.DataFrame:
        """最新の一括 quote 情報を履歴 DataFrame にマージする"""
        if df is None or df.empty:
            return pd.DataFrame()

        price = _finite_float(quote.get("regularMarketPrice"))
        if price is None:
            return df

        df = df.copy()

        volume = _finite_float(quote.get("regularMarketVolume", 0), default=0.0)
        high = _finite_float(quote.get("regularMarketDayHigh"), default=price)
        low = _finite_float(quote.get("regularMarketDayLow"), default=price)
        open_p = _finite_float(quote.get("regularMarketOpen"), default=price)

        market_time_sec = _finite_float(quote.get("regularMarketTime"))

        is_jp = (
            symbol.endswith(".T")
            or is_jp_stock_code(symbol)
            or symbol in ("^N225", "^TOPX", "N225", "TOPX")
        )
        tz_str = "Asia/Tokyo" if is_jp else "America/New_York"
        try:
            local_tz = ZoneInfo(tz_str)
            dt = (
                datetime.fromtimestamp(market_time_sec, local_tz)
                if market_time_sec
                else datetime.now(local_tz)
            )
        except (ValueError, KeyError, OSError, OverflowError):
            try:
                dt = (
                    datetime.fromtimestamp(market_time_sec, tz=UTC)
                    if market_time_sec is not None
                    else datetime.now(tz=UTC)
                )
            except (ValueError, OSError, OverflowError):
                dt = datetime.now(tz=UTC)

        date_str = dt.strftime("%Y-%m-%d")

        df_tz = getattr(df.index, "tz", None)
        if isinstance(df.index, pd.DatetimeIndex):
            new_ts = pd.to_datetime(date_str)
            if df_tz:
                try:
                    new_idx = new_ts.tz_localize(df_tz)
                except Exception:
                    try:
                        new_idx = new_ts.tz_convert(df_tz)
                    except Exception:
                        new_idx = pd.Timestamp(date_str, tz=df_tz)
            else:
                new_idx = new_ts.tz_localize(None) if getattr(new_ts, "tz", None) else new_ts
        else:
            new_idx = date_str

        last_idx = df.index[-1]
        last_date_str = (
            last_idx.strftime("%Y-%m-%d") if hasattr(last_idx, "strftime") else str(last_idx)
        )

        row_data = {
            "Open": open_p if open_p is not None else price,
            "High": high if high is not None else price,
            "Low": low if low is not None else price,
            "Close": price,
            "Volume": int(volume) if volume is not None else 0,
        }

        for col in df.columns:
            if col not in row_data:
                if col in ("Dividends", "Stock Splits", "Capital Gains"):
                    row_data[col] = 0.0
                else:
                    row_data[col] = float(price)

        new_row = pd.Series(row_data, name=new_idx)

        if date_str == last_date_str:
            df.loc[last_idx] = new_row  # type: ignore[unsupported-operation]
        else:
            new_df = pd.DataFrame([new_row])
            df = pd.concat([df, new_df])
            df = df[~df.index.duplicated(keep="last")]  # type: ignore[unsupported-operation,bad-assignment]
            df = df.sort_index()

        return df

    def _pre_warm_caches_from_history(
        self, hist_by_symbol: dict[str, pd.DataFrame], m_state: Any
    ) -> None:
        """取得済み history から price / currency 等を合成し、軽量キャッシュに注入する。

        v7/finance/quote 等の別エンドポイントは呼ばず、すでに取得済みの history
        DataFrame から前日終値・currency を合成する。これにより yfinance への
        リクエスト数を最小化する（429/439 の根本原因を排除）。
        """
        for symbol, df in hist_by_symbol.items():
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                continue
            try:
                quote = self._derive_quote_from_history(df, symbol)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to derive quote for %s: %s", symbol, exc)
                quote = None
            if not quote:
                continue

            prev_close = quote.get("regularMarketPreviousClose")
            currency = self._infer_currency_from_symbol(symbol)

            # Try to extract exchange metadata from history_metadata / fast_info.
            # The extraction is skipped entirely once the exchange is already
            # known (register_ticker_exchange keeps a process-wide cache), so
            # the per-symbol network calls below (history_metadata / fast_info)
            # happen at most once per symbol per process. This preserves the
            # "minimize yfinance requests to avoid 429/439" design contract of
            # this pre-warm pass.
            exchange = get_ticker_exchange(symbol)
            if exchange is None:
                # Zero-I/O heuristic first: .T -> TSE, ^ -> INDEX.
                exchange = self._infer_exchange_from_symbol(symbol)
            if exchange is None:
                try:
                    t = self.get_ticker(symbol)
                    if t:
                        if hasattr(t, "fast_info"):
                            exchange = getattr(t.fast_info, "exchange", None)
                        if not exchange:
                            metadata = getattr(t, "history_metadata", None)
                            if metadata is None and hasattr(t, "get_history_metadata"):
                                metadata = t.get_history_metadata()
                            if isinstance(metadata, dict):
                                exchange = (
                                    metadata.get("exchangeName")
                                    or metadata.get("fullExchangeName")
                                    or metadata.get("exchange")
                                )
                except Exception as ex_exc:
                    if _is_yfinance_rate_limit_error(ex_exc):
                        _shim()._handle_yf_rate_limit(
                            ex_exc, m_state, context=f"pre-warm {symbol}"
                        )
                    logger.debug(
                        "Failed to extract exchange during pre-warm for %s: %s", symbol, ex_exc
                    )

            if exchange:
                from utils.tradingview_mapper import register_ticker_exchange

                register_ticker_exchange(symbol, exchange)

            # Normalize to the resolved TradingView prefix (e.g. "NasdaqGS" ->
            # "NASDAQ") so fastinfo_{symbol} and the payload exchange field are
            # consistent with the tv_symbol prefix.
            exchange = resolve_exchange_prefix(exchange)

            # fastinfo_{symbol} キャッシュの注入（get_fast_info の代替・補完）
            fast_cache_key = f"fastinfo_{symbol}"
            fast_data = {
                "regularMarketPreviousClose": prev_close,
                "previousClose": prev_close,
                "currency": currency,
                "exchange": exchange,
                "symbol": symbol,
            }
            fast_data = {k: v for k, v in fast_data.items() if v is not None}
            try:
                with m_state.yfinance_short_cache_lock:
                    m_state.yfinance_short_cache[fast_cache_key] = fast_data
            except (AttributeError, RuntimeError, TypeError):
                pass

    @with_yfinance_retry(max_retries=2, base_delay=3.0, backoff_factor=3.0)
    def download_batch(
        self, symbols: list[str], period: str = "3mo", lightweight: bool = False
    ) -> pd.DataFrame:
        from constants import YFINANCE_TIMEOUT_BATCH

        m_state = self._get_market_state()
        if m_state.is_yf_rate_limited():
            logger.info(
                "yfinance is rate-limited; skipping network batch download for %d symbols", len(symbols)
            )
            cached_dfs = []
            for sym in symbols:
                disk_key = f"hist_df_{sym}_{period}"
                cached = self._load_history_from_disk_cache(disk_key)
                if cached is not None and not cached.empty:
                    df_col = cached.copy()
                    df_col.columns = pd.MultiIndex.from_product([df_col.columns, [sym]])
                    cached_dfs.append(df_col)
            if not cached_dfs:
                return pd.DataFrame()
            if len(cached_dfs) == 1:
                return cached_dfs[0]
            try:
                return pd.concat(cached_dfs, axis=1)
            except Exception:
                return pd.DataFrame()

        # 各銘柄について、履歴データをキャッシュから引き出す、もしくは並行して取得する。
        # 注意: v7/finance/quote 等の別エンドポイントは使用しない。最新価格は
        # 取得済みの history から合成する（_derive_quote_from_history）。
        from app_state import app_state

        merged_dfs = []
        cache_miss_symbols = []
        hist_by_symbol = {}

        for symbol in symbols:
            # a. インメモリキャッシュをチェック
            cache_key = history_short_cache_key(symbol, period, "1d")
            try:
                with m_state.yfinance_short_cache_lock:
                    cached_hist = m_state.yfinance_short_cache.get(cache_key)
            except (AttributeError, RuntimeError):
                cached_hist = None

            if (
                cached_hist is not None
                and isinstance(cached_hist, pd.DataFrame)
                and not cached_hist.empty
            ):
                hist_by_symbol[symbol] = cached_hist.copy()
                continue

            # b. キャッシュにない場合は並行フェッチの対象にする
            cache_miss_symbols.append(symbol)

        # キャッシュミスの銘柄を一括ダウンロード
        if cache_miss_symbols:
            logger.info(
                "Cache miss for %d symbols; fetching in batch via yf.download",
                len(cache_miss_symbols),
            )
            try:
                sess = _shim().yf_session_manager.get_session()
                # threads is bounded by the session manager's concurrency
                # semaphore (YFINANCE_MAX_CONCURRENT_REQUESTS=3), so enabling a
                # small worker pool overlaps per-symbol requests within the same
                # pacing budget instead of serializing the whole batch. Peak
                # request rate is unchanged; wall-clock latency drops.
                batch_workers = min(3, len(cache_miss_symbols))
                batch_downloaded = yf.download(
                    tickers=cache_miss_symbols,
                    period=period,
                    interval="1d",
                    group_by="column",
                    session=sess,
                    threads=batch_workers,
                    auto_adjust=True,
                    progress=False,
                    timeout=YFINANCE_TIMEOUT_BATCH,
                )
                if not batch_downloaded.empty:
                    for sym in cache_miss_symbols:
                        try:
                            if isinstance(batch_downloaded.columns, pd.MultiIndex):
                                if sym in batch_downloaded.columns.get_level_values(1):
                                    sym_df = batch_downloaded.xs(sym, axis=1, level=1)
                                elif sym in batch_downloaded.columns.get_level_values(0):
                                    sym_df = batch_downloaded.xs(sym, axis=1, level=0)
                                else:
                                    continue
                            elif len(cache_miss_symbols) == 1:
                                sym_df = batch_downloaded.copy()
                            else:
                                continue
                            sym_df = normalize_history_frame(sym_df)
                            if not sym_df.empty:
                                hist_by_symbol[sym] = sym_df
                                # Cache it
                                cache_key = history_short_cache_key(sym, period, "1d")
                                disk_key = f"hist_df_{sym}_{period}"
                                with m_state.yfinance_short_cache_lock:
                                    m_state.yfinance_short_cache[cache_key] = sym_df.copy()
                                try:
                                    data = {
                                        "type": "dataframe",
                                        "json_data": sym_df.to_json(
                                            orient="split", date_format="iso"
                                        ),
                                    }
                                    app_state.stock_disk_cache.set(disk_key, data)
                                except Exception:
                                    logger.debug("Failed to cache history for %s", sym)
                        except Exception as e:
                            logger.debug("Failed to extract %s from yf.download: %s", sym, e)
            except Exception as exc:
                logger.warning(
                    "Batch yf.download failed: %s. Falling back to parallel individual fetches.",
                    exc,
                )
                if _is_yfinance_rate_limit_error(exc):
                    _shim()._handle_yf_rate_limit(
                        exc, m_state, context="batch yf.download"
                    )

            # Fallback for any symbols that still missed
            remaining_miss = [sym for sym in cache_miss_symbols if sym not in hist_by_symbol]
            if remaining_miss:
                if lightweight:
                    logger.info(
                        "Lightweight mode: skipping parallel fetches for %d missed symbols",
                        len(remaining_miss),
                    )
                elif m_state.is_yf_rate_limited():
                    # F-6: Skip parallel individual fetches when rate-limited to
                    # avoid a burst of requests that would worsen the 429 block.
                    logger.info(
                        "yfinance rate-limited; skipping parallel fallback fetches for %d symbols",
                        len(remaining_miss),
                    )
                else:
                    logger.info(
                        "Falling back to parallel fetches for %d remaining missed symbols",
                        len(remaining_miss),
                    )
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
                            except (
                                ValueError,
                                TypeError,
                                RuntimeError,
                                AttributeError,
                                OSError,
                                TimeoutError,
                            ) as e:
                                logger.warning(
                                    "Failed to fetch single history in batch for %s: %s", sym, e
                                )

            # Fallback to disk cache for any symbols that still failed network fetch
            for sym in cache_miss_symbols:
                if sym not in hist_by_symbol:
                    disk_key = f"hist_df_{sym}_{period}"
                    cached_disk = self._load_history_from_disk_cache(disk_key)
                    if cached_disk is not None:
                        hist_by_symbol[sym] = cached_disk

        # 3. 取得済み history から price / currency 等を合成し軽量キャッシュに注入。
        #    別エンドポイント(quote)は呼ばず、history から合成するため 429 を抑制。
        self._pre_warm_caches_from_history(hist_by_symbol, m_state)

        # 各銘柄の履歴 DataFrame を MultiIndex 列 (symbol レベル) で結合する
        for symbol in symbols:
            df = hist_by_symbol.get(symbol)
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                continue
            df_col = df.copy()
            df_col.columns = pd.MultiIndex.from_product([df_col.columns, [symbol]])
            merged_dfs.append(df_col)

        if not merged_dfs:
            return pd.DataFrame()
        if len(merged_dfs) == 1:
            return merged_dfs[0]

        try:
            return pd.concat(merged_dfs, axis=1)
        except (ValueError, TypeError, KeyError):
            logger.exception("Failed to concatenate merged dataframes in download_batch")
            return merged_dfs[0] if merged_dfs else pd.DataFrame()

    def _infer_currency_from_symbol(self, symbol: str) -> str | None:
        """Infer currency from symbol suffix — no yfinance call needed.

        Japanese stocks listed on TSE use the .T suffix and trade in JPY.
        Index tickers (^ prefix) are typically in USD.
        All others default to None (caller handles fallback).
        """
        if symbol.endswith(".T"):
            return "JPY"
        if symbol.endswith("=X") and len(symbol) >= 8:
            return symbol[-5:-2]

        INDEX_CURRENCY_MAP = {
            "^N225": "JPY",
            "^KS11": "KRW",
            "^HSI": "HKD",
            "^FTSE": "GBP",
            "^STOXX50E": "EUR",
            "^GDAXI": "EUR",
            "^FCHI": "EUR",
        }
        if symbol in INDEX_CURRENCY_MAP:
            return INDEX_CURRENCY_MAP[symbol]

        if symbol.startswith("^"):
            return "USD"
        if "." not in symbol:
            return "USD"
        return None

    @with_yfinance_retry(max_retries=2, base_delay=1.0, backoff_factor=2.0)
    def get_fast_info(self, symbol: str) -> dict:
        m_state = self._get_market_state()
        # Speedup: reuse cached fast_info (previous close, currency, etc.) which
        # rarely changes within a sync cycle. Serve it before the rate-limit
        # gate: a cache hit is not a network call, so a 429 must not discard
        # known-good metadata. The gate below still blocks the network path.
        cache_key = f"fastinfo_{symbol}"
        try:
            with m_state.yfinance_short_cache_lock:
                cached_fast = m_state.yfinance_short_cache.get(cache_key)
            if isinstance(cached_fast, dict):
                return dict(cached_fast)
        except (AttributeError, RuntimeError):
            cached_fast = None

        if m_state.is_yf_rate_limited():
            return {}

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
                    metadata = getattr(t, "history_metadata", None)
                    if metadata is None:
                        metadata = t.get_history_metadata()
                    if isinstance(metadata, dict) and metadata.get("currency"):
                        currency = metadata.get("currency")
                except Exception as exc:
                    logger.debug("Failed to retrieve history metadata for %s: %s", symbol, exc)
            if currency is None:
                # quoteSummary (t.info) は呼ばず、シンボル suffix から推測する。
                # t.info は Yahoo に制限されたエンドポイントで 429/439 の原因となる。
                currency = self._infer_currency_from_symbol(symbol)

            exchange = _fast_get(["exchange"]) or get_ticker_exchange(symbol)
            if exchange is None:
                # Zero-I/O heuristic first: .T -> TSE, ^ -> INDEX.
                exchange = self._infer_exchange_from_symbol(symbol)
            if exchange is None:
                # Only fall back to history metadata (a potential yfinance
                # request on a fresh Ticker) when the exchange is not already
                # registered in the TradingView symbol cache.
                try:
                    metadata = getattr(t, "history_metadata", None)
                    if metadata is None and hasattr(t, "get_history_metadata"):
                        metadata = t.get_history_metadata()
                    if isinstance(metadata, dict):
                        exchange = (
                            metadata.get("exchangeName")
                            or metadata.get("fullExchangeName")
                            or metadata.get("exchange")
                        )
                except Exception as exc:
                    logger.debug(
                        "Failed to retrieve history metadata exchange for %s: %s", symbol, exc
                    )
            # Normalize to the resolved TradingView prefix (e.g. "NasdaqGS" ->
            # "NASDAQ") so fastinfo_{symbol} and the payload exchange field are
            # consistent with the tv_symbol prefix.
            exchange = resolve_exchange_prefix(exchange)

            if exchange:
                from utils.tradingview_mapper import register_ticker_exchange

                register_ticker_exchange(symbol, exchange)

            mapped_info = {
                "shortName": None,
                "regularMarketPreviousClose": prev_close,
                "previousClose": prev_close,
                "currency": currency,
                "marketCap": _fast_get(["market_cap", "marketCap"]),
                "exchange": exchange,
                "quoteType": _fast_get(["quote_type", "quoteType"]),
                "symbol": symbol,
            }
            cleaned = {k: v for k, v in mapped_info.items() if v is not None}
            if cleaned:
                try:
                    with m_state.yfinance_short_cache_lock:
                        m_state.yfinance_short_cache[cache_key] = dict(cleaned)
                except Exception as cache_exc:
                    logger.debug("Failed to cache fast_info for %s: %s", symbol, cache_exc)
                return cleaned
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
            OSError,
            OverflowError,
        ) as exc:
            logger.debug("yfinance ticker.fast_info failed for %s: %s", symbol, exc)
            # @with_yfinance_retry owns rate-limit recording (single mark per 429).
            if _is_yfinance_rate_limit_error(exc):
                raise
            exc_name = type(exc).__name__
            if "Timeout" in exc_name or "Connection" in exc_name:
                raise
        return {}

    @with_yfinance_retry(max_retries=2, base_delay=2.0, backoff_factor=2.0)
    def get_info(self, symbol: str) -> dict:
        """Fetch full ticker info including fundamental data (P/E, dividend, etc.)."""
        m_state = self._get_market_state()
        if (
            hasattr(m_state, "is_negative_cached_symbol")
            and m_state.is_negative_cached_symbol(symbol) is True
        ):
            logger.debug("symbol %s is in negative cache; skipping get_info", symbol)
            return {}

        t = self.get_ticker(symbol)
        if not t:
            return {}
        try:
            info = t.info
            if not info or not isinstance(info, dict):
                return {}

            # Fields to extract from ticker.info for fundamental data
            fundamental_keys = [
                "trailingPE",
                "forwardPE",
                "priceToBook",
                "pegRatio",
                "dividendYield",
                "trailingAnnualDividendYield",
                "earningsPerShare",
                "epsForward",
                "revenuePerShare",
                "bookValue",
                "priceToSalesTrailing12Months",
                "enterpriseToEbitda",
                "enterpriseToRevenue",
                "profitMargins",
                "grossMargins",
                "operatingMargins",
                "returnOnEquity",
                "returnOnAssets",
                "totalRevenue",
                "revenueGrowth",
                "earningsGrowth",
                "totalCash",
                "totalDebt",
                "debtToEquity",
                "currentRatio",
                "quickRatio",
                "freeCashflow",
                "operatingCashflow",
                "targetMeanPrice",
                "targetHighPrice",
                "targetLowPrice",
                "recommendationMean",
                "recommendationKey",
                "numberOfAnalystOpinions",
                "beta",
                "fiftyTwoWeekHigh",
                "fiftyTwoWeekLow",
                "fiftyDayAverage",
                "twoHundredDayAverage",
                "shortName",
                "longName",
                "sector",
                "industry",
                "currency",
                "marketCap",
                "sharesOutstanding",
                "floatShares",
                "heldPercentInsiders",
                "heldPercentInstitutions",
                "shortRatio",
                "shortPercentOfFloat",
            ]

            result = {}
            for key in fundamental_keys:
                val = info.get(key)
                if val is not None:
                    result[key] = val

            return sanitize_fundamental_dict(result)
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
            OSError,
            OverflowError,
        ) as exc:
            logger.debug("yfinance ticker.info failed for %s: %s", symbol, exc)
            if _is_yfinance_invalid_symbol_error(exc):
                m_state.mark_negative_cached_symbol(symbol, str(exc))
            # @with_yfinance_retry owns rate-limit recording (single mark per 429).
            if _is_yfinance_rate_limit_error(exc):
                raise
            exc_name = type(exc).__name__
            if "Timeout" in exc_name or "Connection" in exc_name:
                raise
        return {}

    def _df_to_records(self, df: pd.DataFrame | None, limit: int = 0) -> list[dict]:
        """Convert a DataFrame to a list of dicts, handling DatetimeIndex and NaT."""
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return []
        try:
            df = df.copy()
            if isinstance(df.index, pd.DatetimeIndex):
                df.index = df.index.strftime("%Y-%m-%d %H:%M:%S")  # type: ignore[attr-defined]
            df = df.reset_index()
            # Replace inf/-inf and NaN/NaT with None for JSON serialization safely in Pandas 2.x
            df = df.astype(object)
            df = df.where(pd.notna(df), None)  # type: ignore[arg-type,bad-argument-type]
            records = df.to_dict("records")
            cleaned_records: list[dict[str, Any]] = []
            for row in records:
                clean_row: dict[str, Any] = {}
                for k, v in row.items():
                    key_str = str(k)
                    if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                        clean_row[key_str] = None
                    else:
                        clean_row[key_str] = v
                cleaned_records.append(clean_row)
            if limit > 0:
                cleaned_records = cleaned_records[:limit]
            return cleaned_records
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"earnings_dates symbol={symbol}"
                )
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"recommendations symbol={symbol}"
                )
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"institutional_holders symbol={symbol}"
                )
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"major_holders symbol={symbol}"
                )
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"analyst_targets symbol={symbol}"
                )
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
                            item.isoformat() if hasattr(item, "isoformat") else item for item in v
                        ]
                    else:
                        result[k] = v
                return result
            return {}
        except Exception as exc:
            logger.debug("yfinance calendar failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"calendar symbol={symbol}"
                )
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
                results.append(
                    {
                        "title": content.get("title", ""),
                        "summary": (content.get("summary") or "")[:200],
                        "pubDate": content.get("pubDate") or content.get("displayTime", ""),
                        "provider": provider.get("displayName", "")
                        if isinstance(provider, dict)
                        else "",
                        "providerUrl": provider.get("url", "")
                        if isinstance(provider, dict)
                        else "",
                        "thumbnailUrl": thumbnail.get("originalUrl", "")
                        if isinstance(thumbnail, dict)
                        else "",
                        "link": (content.get("canonicalUrl") or {}).get("url", "")
                        if isinstance(content.get("canonicalUrl"), dict)
                        else content.get("clickThroughUrl", {}).get("url", "")
                        if isinstance(content.get("clickThroughUrl"), dict)
                        else "",
                    }
                )
            return results
        except Exception as exc:
            logger.debug("yfinance news failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"news symbol={symbol}"
                )
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
            if (
                hasattr(chain, "calls")
                and isinstance(chain.calls, pd.DataFrame)
                and not chain.calls.empty
            ):
                result["calls"] = self._df_to_records(chain.calls)
            if (
                hasattr(chain, "puts")
                and isinstance(chain.puts, pd.DataFrame)
                and not chain.puts.empty
            ):
                result["puts"] = self._df_to_records(chain.puts)
            result["available_dates"] = list(options_dates[:12])
            return result
        except Exception as exc:
            logger.debug("yfinance option_chain failed for %s: %s", symbol, exc)
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"option_chain symbol={symbol}"
                )
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"revenue_estimate symbol={symbol}"
                )
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"earnings_estimate symbol={symbol}"
                )
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
            if _is_yfinance_rate_limit_error(exc):
                m_state = self._get_market_state()
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"valuation_measures symbol={symbol}"
                )
            return []

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search for stocks/instruments via yfinance Search."""
        if not query or len(query.strip()) < 2:
            return []
        m_state = self._get_market_state()
        if m_state.is_yf_rate_limited():
            return []

        # yfinance.Search was added in 0.2.51; fall back to direct API call
        # for older yfinance versions.
        # Use getattr with None check instead of hasattr so that
        # @patch.object(yf, "Search", None) in tests is correctly detected.
        if getattr(yf, "Search", None) is None:
            logger.warning(
                "yfinance.Search not available in yfinance %s, using fallback",
                getattr(yf, "__version__", "<unknown>"),
            )
            return self._search_fallback(query, max_results, m_state)

        try:
            sess = _shim().yf_session_manager.get_session()
            s = yf.Search(query, session=sess)
            quotes = getattr(s, "quotes", []) or []
            results = []
            for item in quotes[:max_results]:
                sym = item.get("symbol")
                if not sym:
                    continue
                ex_val = item.get("exchange") or item.get("exchDisp") or ""
                if ex_val:
                    from utils.tradingview_mapper import register_ticker_exchange

                    register_ticker_exchange(sym, ex_val)
                results.append(
                    {
                        "symbol": sym,
                        # L-8: Return empty string rather than a hardcoded Japanese UI string.
                        # Display fallback ("名称不明" etc.) should be handled by the frontend.
                        "name": item.get("shortname") or item.get("longname") or "",
                        "exchange": ex_val,
                    }
                )
            return results
        except Exception as exc:
            logger.error("yfinance Search failed (%s): %s", query, exc)
            if _is_yfinance_rate_limit_error(exc):
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"search query={query}"
                )
                return []
            return []

    def _search_fallback(self, query: str, max_results: int, m_state: Any) -> list[dict]:
        """Fallback search when yfinance.Search is not available (yfinance < 0.2.51).

        Makes a direct HTTP request to the Yahoo Finance v1 search endpoint.
        This mirrors the internal implementation of yfinance.Search.
        """
        try:
            sess = _shim().yf_session_manager.get_session()
            url = "https://query2.finance.yahoo.com/v1/finance/search"
            params = {
                "q": query,
                "quotesCount": max_results,
                "newsCount": 0,
            }
            resp = sess.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            quotes = data.get("quotes", []) or []
            results = []
            for item in quotes[:max_results]:
                sym = item.get("symbol")
                if not sym:
                    continue
                ex_val = item.get("exchange") or item.get("exchDisp") or ""
                if ex_val:
                    from utils.tradingview_mapper import register_ticker_exchange

                    register_ticker_exchange(sym, ex_val)
                results.append(
                    {
                        "symbol": sym,
                        "name": item.get("shortname") or item.get("longname") or "",
                        "exchange": ex_val,
                    }
                )
            return results
        except Exception as exc:
            logger.error("yfinance search fallback failed (%s): %s", query, exc)
            if _is_yfinance_rate_limit_error(exc):
                _shim()._handle_yf_rate_limit(
                    exc, m_state, context=f"search_fallback query={query}"
                )
            return []
