# bg/sync/fetch.py
"""Single/batch/index fetching from yfinance with fallback and rate-limit handling.

Pure move from `bg.sync_worker`. `acquire_yfinance_slot` is resolved at call
time through the `bg.sync_worker` facade so `patch("bg.sync_worker.X")`
keeps working in tests.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
from typing import Any

import pandas as pd
from requests.exceptions import RequestException

from app_state import app_state
from bg.sync import _shim
from bg.sync.state import (
    _BATCH_INVALID_MARKER,
    _get_app_bg_attr,
    _get_logger,
)
from services.stock_provider import YFTickerMissingError
from utils.http_utils import parse_retry_after
from utils.market_utils import is_market_open
from utils.normalization import (
    _fmt,
    _fmt_vol,
    normalize_history_frame,
)
from utils.stock_payload import (
    _strip_portfolio_fields,
    build_stock_payload,
)

logger = logging.getLogger("bg.sync_worker")



def _handle_yfinance_error(exc: Exception, symbol: str = "") -> None:
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    exc_str_lower = str(exc).lower()

    if (
        status_code in (401, 429, 402, 439)
        or "too many requests" in exc_str_lower
        or "payment required" in exc_str_lower
        or "invalid crumb" in exc_str_lower
        or "unauthorized" in exc_str_lower
    ):
        backoff_time = app_state.market.mark_yf_429(retry_after=parse_retry_after(exc))
        logger.warning(
            "yfinance rate limit / block hit (%s) for symbol=%s; backing off for %d seconds.",
            status_code if status_code else "unknown",
            symbol,
            int(backoff_time),
        )
    elif "timeout" in exc_str_lower:
        logger.debug("yfinance timeout detected. symbol=%s", symbol)
    else:
        with app_state.market.yfinance_lock:
            app_state.market.yfinance_429_streak = 0


def _invalid_tuple_if_applicable(symbol: str, exc: Exception) -> Any:
    from services.stock_provider import _is_yfinance_invalid_symbol_error

    if _is_yfinance_invalid_symbol_error(exc):
        return (_BATCH_INVALID_MARKER, symbol)
    return None


def _is_batch_result_invalid(result: Any) -> bool:
    return isinstance(result, tuple) and len(result) == 2 and result[0] == _BATCH_INVALID_MARKER


def fetch_stock(
    symbol: str,
    name_or_dict: Any,
    market: str,
    snapshot_ts_ms: int | None = None,
) -> dict[str, Any] | tuple[str, str] | None:
    """単一銘柄のデータを取得する"""
    if not _shim().acquire_yfinance_slot():
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for symbol=%s",
                symbol,
            )
        try:
            cached_payload = app_state.payload_disk_cache.get(f"payload_{symbol}_{market}")
            if isinstance(cached_payload, dict) and cached_payload:
                return cached_payload
        except Exception:
            pass
        return None

    try:
        period = "3mo" if is_market_open(market) else "1mo"
        hist = pd.DataFrame()
        try:
            hist = app_state.stock_provider.get_history(symbol, period=period)
        except (
            RequestException,
            ValueError,
            KeyError,
            IndexError,
            OSError,
            YFTickerMissingError,
        ) as e:
            logger.debug("Fetch failed for %s with period %s: %s", symbol, period, e)
            invalid = _invalid_tuple_if_applicable(symbol, e)
            if invalid is not None:
                return invalid

        if hist.empty or "Close" not in hist.columns or len(hist) < 1:
            logger.warning(
                "No valid history data found for %s after period %s",
                symbol,
                period,
            )
            try:
                cached_payload = app_state.payload_disk_cache.get(f"payload_{symbol}_{market}")
                if isinstance(cached_payload, dict) and cached_payload:
                    return cached_payload
            except Exception:
                pass
            return None

        build_fn = _get_app_bg_attr("build_stock_payload", build_stock_payload)
        payload = build_fn(symbol, name_or_dict, market, hist, snapshot_ts_ms=snapshot_ts_ms)
        if isinstance(payload, dict):
            try:
                app_state.payload_disk_cache.set(
                    f"payload_{symbol}_{market}", _strip_portfolio_fields(payload)
                )
            except (OSError, TypeError):
                logger.debug("Failed to cache payload for %s", symbol)
            return payload
        return None
    except (
        RequestException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OSError,
        YFTickerMissingError,
    ) as exc:
        _handle_yfinance_error(exc, symbol)
        logger.exception("Stock fetch failed (%s)", symbol)
        invalid = _invalid_tuple_if_applicable(symbol, exc)
        if invalid is not None:
            return invalid
        return None


def extract_batch_history(
    downloaded: Any, symbol: str, single_symbol: bool = False
) -> pd.DataFrame:
    """バッチ取得されたDataFrameから単一銘柄の履歴を抽出"""
    if downloaded is None or getattr(downloaded, "empty", True):
        return pd.DataFrame()
    try:
        if not isinstance(downloaded, pd.DataFrame):
            return pd.DataFrame()

        if isinstance(downloaded.columns, pd.MultiIndex):
            try:
                return normalize_history_frame(downloaded.xs(symbol, axis=1, level=1))
            except (KeyError, IndexError, ValueError):
                pass

            try:
                return normalize_history_frame(downloaded.xs(symbol, axis=1, level=0))
            except (KeyError, IndexError, ValueError):
                pass

            try:
                return normalize_history_frame(downloaded[symbol])
            except (KeyError, IndexError, ValueError):
                pass

            try:
                matching_cols = [
                    col for col in downloaded.columns if isinstance(col, tuple) and symbol in col
                ]
                if matching_cols:
                    extracted = downloaded[matching_cols].copy()
                    extracted.columns = [
                        next(part for part in col if part != symbol) for col in matching_cols
                    ]
                    return normalize_history_frame(extracted)
            except (KeyError, IndexError, TypeError, StopIteration, ValueError):
                pass

            return pd.DataFrame()
        elif single_symbol or ("Close" in downloaded.columns or "close" in downloaded.columns):
            return normalize_history_frame(downloaded)
        else:
            return pd.DataFrame()
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        logger.debug("extract_batch_history error for %s: %s", symbol, exc)
        return pd.DataFrame()


def fetch_stocks_batch(
    items: list[tuple[str, str, str]],
    snapshot_ts_ms: int | None = None,
    lightweight: bool = False,
    period: str = "3mo",
) -> list[Any]:
    """複数銘柄をバッチで取得。"""
    target = _get_app_bg_attr("fetch_stocks_batch", None)
    if target is not None and target is not fetch_stocks_batch:
        return target(items, snapshot_ts_ms=snapshot_ts_ms, lightweight=lightweight, period=period)

    if not items:
        return []

    from utils.disk_cache import is_disk_cache_degraded

    if is_disk_cache_degraded():
        logger.warning("Disk cache is in degraded state; skipping batch stock fetch")
        return [None] * len(items)

    symbols = [item[0] for item in items]
    logger.info("Batch stock fetch starting: count=%d", len(symbols))

    max_batch_size = len(symbols)
    if app_state.market.is_yf_rate_limited():
        max_batch_size = max(5, min(len(symbols), 10))
        if len(symbols) > max_batch_size:
            logger.info(
                "Rate limit active: reducing batch from %d to %d symbols",
                len(symbols),
                max_batch_size,
            )
            symbols = symbols[:max_batch_size]
            items = items[:max_batch_size]

    downloaded = None
    if _shim().acquire_yfinance_slot():
        try:
            downloaded = app_state.stock_provider.download_batch(
                symbols, period=period, lightweight=lightweight
            )
        except (RequestException, ValueError, TypeError, KeyError, OSError) as exc:
            _handle_yfinance_error(exc, "batch_fetch")
            logger.warning(
                "Batch fetch failed with exception: %s.",
                exc,
                exc_info=True,
            )
    else:
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for batch fetch."
            )

    if downloaded is None or downloaded.empty:
        logger.warning("Batch fetch completely failed or empty. Preserving previous state.")
        return [None] * len(items)

    results_map: dict[str, Any] = {}
    fallback_items = []
    max_fallbacks = 2

    for symbol, name, market in items:
        payload = None
        if downloaded is not None and not downloaded.empty:
            try:
                extract_fn = _get_app_bg_attr("extract_batch_history", extract_batch_history)
                hist = extract_fn(downloaded, symbol, single_symbol=(len(symbols) == 1))
                if not hist.empty and len(hist) >= 1:
                    build_fn = _get_app_bg_attr("build_stock_payload", build_stock_payload)
                    payload = build_fn(
                        symbol,
                        name,
                        market,
                        hist,
                        snapshot_ts_ms=snapshot_ts_ms,
                        lightweight=lightweight,
                    )
            except (KeyError, IndexError, ValueError, TypeError) as extract_exc:
                logger.debug("Failed to extract %s from batch: %s", symbol, extract_exc)

        if payload is not None:
            results_map[symbol] = payload
        else:
            fallback_items.append((symbol, name, market))

    if lightweight:
        logger.debug("Lightweight mode: skipping all %d fallbacks", len(fallback_items))
        for symbol, _name, _market in fallback_items:
            results_map[symbol] = None
        results = [results_map.get(item[0]) for item in items]
        return results

    if app_state.market.is_yf_rate_limited():
        logger.warning(
            "yfinance rate-limited: skipping %d batch fallbacks.",
            len(fallback_items),
        )
        results = [results_map.get(item[0]) for item in items]
        return results

    to_fetch = fallback_items[:max_fallbacks]
    skipped_items = fallback_items[max_fallbacks:]

    for symbol, _, _ in skipped_items:
        logger.debug("Skipping fallback for %s: limit reached", symbol)
        results_map[symbol] = None

    if to_fetch:
        futures_map: dict[concurrent.futures.Future[Any], str] = {}

        logger.info(
            "Fallback parallel single queries triggered for %d stocks (limit %d)",
            len(to_fetch),
            max_fallbacks,
        )

        for symbol, name, market in to_fetch:
            try:
                fut = app_state.execution.data_executor.submit(
                    fetch_stock, symbol, name, market, snapshot_ts_ms
                )
            except (queue.Full, RuntimeError) as submit_exc:
                logger.warning("Fallback fetch submission skipped for %s: %s", symbol, submit_exc)
                results_map[symbol] = None
                continue
            futures_map[fut] = symbol
        from utils.env_helpers import _env_float

        fallback_timeout = _env_float("MNS_FALLBACK_FETCH_TIMEOUT", 10.0, 1.0, 60.0)
        done, not_done = concurrent.futures.wait(
            futures_map.keys(),
            timeout=fallback_timeout,
        )

        for fut in done:
            symbol = futures_map[fut]
            try:
                payload = fut.result()
                results_map[symbol] = payload
            except (RequestException, ValueError, TypeError, RuntimeError) as exc:
                logger.warning("Parallel fallback fetch failed for %s: %s", symbol, exc)
                results_map[symbol] = _invalid_tuple_if_applicable(symbol, exc)
        for fut in not_done:
            symbol = futures_map[fut]
            fut.cancel()
            logger.warning("Parallel fallback fetch timed out for %s", symbol)
            results_map[symbol] = None

            def _log_late_failure(f: Any, _sym: str = symbol) -> None:
                try:
                    exc = f.exception()
                except Exception as log_exc:
                    exc = log_exc
                if exc is not None:
                    _get_logger().warning(
                        "Parallel fallback fetch failed late for %s: %s", _sym, exc
                    )

            fut.add_done_callback(_log_late_failure)

    results = [results_map.get(item[0]) for item in items]
    return results


def fetch_index_data(key: str, symbol: str) -> tuple[str, dict[str, Any]] | None:
    """指数データ取得（シングルピリオド、フォールバック無し）"""
    if not _shim().acquire_yfinance_slot():
        if app_state.market.is_yf_rate_limited():
            logger.warning(
                "yfinance is currently rate-limited. Sourcing cached/stale data for index=%s", key
            )
        return None

    try:
        hist = app_state.stock_provider.get_history(symbol, period="1mo")

        if len(hist) < 2:
            return None

        last_row = hist.iloc[-1]
        prev_close = hist["Close"].iloc[-2]

        price = float(last_row["Close"])
        change = price - float(prev_close)
        pct = (change / float(prev_close) * 100) if prev_close else 0.0

        market_type = "jp" if key == "N225" else "us"
        is_open = is_market_open(market_type)
        market_state = "REGULAR" if is_open else "CLOSED"

        return key, {
            "price": _fmt(price),
            "change": _fmt(change),
            "percent": _fmt(pct),
            "high": _fmt(last_row.get("High")),
            "low": _fmt(last_row.get("Low")),
            "open": _fmt(last_row.get("Open")),
            "volume": _fmt_vol(last_row.get("Volume")),
            "market_state": market_state,
        }
    except (RequestException, ValueError, TypeError, KeyError, IndexError, OSError):
        logger.exception("Index fetch failed for %s", key)
        return None
