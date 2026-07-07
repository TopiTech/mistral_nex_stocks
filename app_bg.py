# app_bg.py
"""Background synchronization, yfinance fetching, and SSE interpolation loop."""

import copy
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from requests.exceptions import RequestException

from app_helpers import (
    _default_stock_names,
    _fmt,
    _fmt_vol,
    acquire_yfinance_slot,
    build_stock_payload,
    is_market_open,
    normalize_history_frame,
)
from utils.storage import load_user_stocks
from app_state import app_state
from constants import (
    YFINANCE_MAX_RETRIES,
    YFINANCE_RETRY_WAIT,
    SSE_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP,
    SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP,
    SSE_YAHOO_FETCH_NO_LISTENER_SLEEP,
)
from utils.storage import save_user_stocks

logger = logging.getLogger(__name__)


def _handle_yfinance_error(exc, symbol=""):
    """Handle exceptions from yfinance queries and increment/set rate limits if 429 is received."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    exc_str_lower = str(exc).lower()
    
    if status_code == 429 or "too many requests" in exc_str_lower:
        backoff_time = app_state.mark_yf_429()
        # mark_yf_429() already handles yf_session_manager UA rotation and cookie clearing
        logger.warning(
            "yfinance rate limit hit (429) for symbol=%s; backing off for %d seconds.",
            symbol,
            int(backoff_time),
        )
    elif status_code == 401 or "invalid crumb" in exc_str_lower or "unauthorized" in exc_str_lower:
        # Let the session manager rotate UA and handle the brief rate limit (5s) without a heavy process backoff.
        logger.warning(
            "yfinance unauthorized/invalid crumb detected (401) for symbol=%s; rotated session/UA and scheduling retry.",
            symbol,
        )
    elif "timeout" in exc_str_lower:
        logger.debug("yfinance timeout detected. symbol=%s", symbol)
    else:
        with app_state.market.yfinance_lock:
            app_state.market.yfinance_429_streak = 0


def fetch_stock(
    symbol: str,
    name_or_dict: Any,
    market: str,
    snapshot_ts_ms: Optional[int] = None,
) -> dict[str, Any] | None:
    """単一銘柄のデータを取得する"""
    if not acquire_yfinance_slot():
        if app_state.is_yf_rate_limited():
            logger.warning("yfinance is currently rate-limited. Sourcing cached/stale data for symbol=%s", symbol)
        return None

    try:
        hist = pd.DataFrame()
        for p in ["3mo", "5d", "1d"]:
            try:
                hist = app_state.stock_provider.get_history(symbol, period=p)
                if len(hist) >= 2:
                    break
            except (RequestException, ValueError, KeyError, IndexError) as e:
                logger.debug("Fetch failed for %s with period %s: %s", symbol, p, e)
                continue

        if 0 < len(hist) < 2:
            try:
                hist = app_state.stock_provider.get_history(symbol, period="1mo")
            except (RequestException, ValueError, KeyError, IndexError) as _hst_exc:
                logger.debug(
                    "Extended history fetch failed for %s: %s", symbol, _hst_exc
                )

        if hist.empty or "Close" not in hist.columns or len(hist) < 1:
            logger.warning(
                "No valid history data found for %s after multiple period attempts",
                symbol,
            )
            return None

        payload = build_stock_payload(
            symbol, name_or_dict, market, hist, snapshot_ts_ms=snapshot_ts_ms
        )
        if isinstance(payload, dict):
            # Persist successful fetch to disk so cold-start can serve recent data
            try:
                app_state.payload_disk_cache.set(
                    f"payload_{symbol}_{market}", payload
                )
            except Exception:
                pass
            return payload
        return None
    except Exception as exc:
        _handle_yfinance_error(exc, symbol)
        logger.error("Stock fetch failed (%s): %s", symbol, exc)
        return None


def extract_batch_history(downloaded, symbol, single_symbol=False):
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
                return normalize_history_frame(downloaded[symbol])
            except (KeyError, IndexError, ValueError):
                pass

            try:
                matching_cols = [
                    col
                    for col in downloaded.columns
                    if isinstance(col, tuple) and symbol in col
                ]
                if matching_cols:
                    extracted = downloaded[matching_cols].copy()
                    extracted.columns = [
                        next(part for part in col if part != symbol)
                        for col in matching_cols
                    ]
                    return normalize_history_frame(extracted)
            except (KeyError, IndexError, TypeError, StopIteration, ValueError):
                pass

            return pd.DataFrame()
        elif single_symbol:
            return normalize_history_frame(downloaded)
        else:
            return pd.DataFrame()
    except Exception as exc:
        logger.debug("extract_batch_history error for %s: %s", symbol, exc)
        return pd.DataFrame()


def fetch_stocks_batch(
    items: List[Tuple[str, str, str]], snapshot_ts_ms: Optional[int] = None
) -> List[Optional[dict]]:
    """複数銘柄をバッチで取得"""
    if not items:
        return []

    symbols = [item[0] for item in items]
    logger.info("Batch stock fetch starting: count=%d", len(symbols))

    # When rate-limited recently, use smaller batches to reduce load
    max_batch_size = len(symbols)
    if app_state.is_yf_rate_limited():
        max_batch_size = max(5, min(len(symbols), 10))
        if len(symbols) > max_batch_size:
            logger.info(
                "Rate limit active: reducing batch from %d to %d symbols",
                len(symbols), max_batch_size,
            )
            symbols = symbols[:max_batch_size]
            items = items[:max_batch_size]

    downloaded = None
    if acquire_yfinance_slot():
        try:
            downloaded = app_state.stock_provider.download_batch(symbols, period="3mo")
        except Exception as exc:
            _handle_yfinance_error(exc, "batch_fetch")
            logger.warning(
                "Batch fetch failed with exception: %s.",
                exc,
            )
    else:
        if app_state.is_yf_rate_limited():
            logger.warning("yfinance is currently rate-limited. Sourcing cached/stale data for batch fetch.")

    if downloaded is None or downloaded.empty:
        logger.warning(
            "Batch fetch completely failed or empty. Preserving previous state to avoid N+1 rate limiting."
        )
        return [None] * len(items)

    results_map = {}
    fallback_items = []
    MAX_FALLBACKS = 3

    for symbol, name, market in items:
        payload = None
        if downloaded is not None and not downloaded.empty:
            try:
                hist = extract_batch_history(
                    downloaded, symbol, single_symbol=(len(symbols) == 1)
                )
                if not hist.empty and len(hist) >= 2:
                    payload = build_stock_payload(
                        symbol, name, market, hist, snapshot_ts_ms=snapshot_ts_ms
                    )
            except Exception as extract_exc:
                logger.debug("Failed to extract %s from batch: %s", symbol, extract_exc)

        if payload is not None:
            results_map[symbol] = payload
        else:
            fallback_items.append((symbol, name, market))

    to_fetch = fallback_items[:MAX_FALLBACKS]
    skipped_items = fallback_items[MAX_FALLBACKS:]

    for symbol, _, _ in skipped_items:
        logger.debug("Skipping fallback for %s: limit reached", symbol)
        results_map[symbol] = None

    if to_fetch:
        import concurrent.futures
        futures_map = {}

        logger.info(
            "Fallback parallel single queries triggered for %d stocks (limit %d)",
            len(to_fetch),
            MAX_FALLBACKS,
        )

        for symbol, name, market in to_fetch:
            fut = app_state.execution.executor.submit(
                fetch_stock, symbol, name, market, snapshot_ts_ms
            )
            futures_map[fut] = symbol

        done, not_done = concurrent.futures.wait(
            futures_map.keys(),
            timeout=5.0,
        )

        for fut in done:
            symbol = futures_map[fut]
            try:
                payload = fut.result()
                results_map[symbol] = payload
            except Exception as exc:
                logger.warning("Parallel fallback fetch failed for %s: %s", symbol, exc)
                results_map[symbol] = None

        for fut in not_done:
            symbol = futures_map[fut]
            logger.warning("Parallel fallback fetch timed out for %s", symbol)
            results_map[symbol] = None

    results = [results_map.get(item[0]) for item in items]
    return results


def fetch_index_data(key: str, symbol: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """指数データ取得（タイムアウト・リトライ対策付き）"""
    max_retries = YFINANCE_MAX_RETRIES

    for attempt in range(max_retries):
        if not acquire_yfinance_slot():
            if app_state.is_yf_rate_limited():
                logger.warning("yfinance is currently rate-limited. Sourcing cached/stale data for index=%s", key)
            return None

        try:
            hist = pd.DataFrame()
            for p in ["3mo", "5d", "1d"]:
                try:
                    hist = app_state.stock_provider.get_history(symbol, period=p)
                    if len(hist) >= 2:
                        break
                except Exception:
                    continue

            if len(hist) < 2:
                hist = app_state.stock_provider.get_history(symbol, period="1mo")
                if len(hist) < 2:
                    continue

            last_row = hist.iloc[-1]
            prev_close = hist["Close"].iloc[-2]

            price = float(last_row["Close"])
            change = price - float(prev_close)
            pct = (change / float(prev_close) * 100) if prev_close else 0.0

            # Avoid using t.info which calls quoteSummary (causing 401 warnings)
            market_type = "jp" if key == "N225" else "us"
            is_open = is_market_open(market_type, bypass_cache=True)
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
        except Exception as exc:
            if attempt < max_retries - 1:
                logger.debug(
                    "Index fetch attempt %d failed for %s, retrying: %s",
                    attempt + 1,
                    key,
                    exc,
                )
                app_state.execution.shutdown_event.wait(YFINANCE_RETRY_WAIT)
            else:
                logger.error(
                    "Index fetch failed for %s after %d attempts: %s",
                    key,
                    max_retries,
                    exc,
                    exc_info=True,
                )

    try:
        logger.debug("Index final fallback to yf.download for %s", symbol)
        try:
            df_dl = app_state.stock_provider.download_batch([symbol], period="5d")
        except Exception as exc:
            logger.debug("yf.download failed for %s: %s", symbol, exc)
            df_dl = pd.DataFrame()
        if not df_dl.empty:
            df_norm = extract_batch_history(df_dl, symbol, single_symbol=True)
            if len(df_norm) >= 2:
                last_r = df_norm.iloc[-1]
                prev_c = df_norm["Close"].iloc[-2]
                val = float(last_r["Close"])
                chg = val - float(prev_c)
                pct = (chg / float(prev_c) * 100) if prev_c else 0.0
                return key, {
                    "price": _fmt(val),
                    "change": _fmt(chg),
                    "percent": _fmt(pct),
                    "high": _fmt(last_r.get("High")),
                    "low": _fmt(last_r.get("Low")),
                    "open": _fmt(last_r.get("Open")),
                    "volume": _fmt_vol(last_r.get("Volume")),
                }
    except Exception as dl_exc:
        logger.warning("Index absolute fallback failed for %s: %s", symbol, dl_exc)

    if key == "USDJPY" or symbol in ("USDJPY=X", "JPY=X"):
        # Attempt fallback
        cached_usdjpy = (
            app_state.market.current_indices_cache.get("USDJPY")
            if hasattr(app_state, "current_indices_cache")
            else None
        )
        if cached_usdjpy and cached_usdjpy.get("price") not in (None, "--", ""):
            logger.warning(
                "fetch_index_data failed for USDJPY; falling back to cached value."
            )
            return key, cached_usdjpy
        else:
            fallback_rate = app_state.market.last_usdjpy_rate
            logger.warning(
                "fetch_index_data failed for USDJPY and no cached value exists; falling back to stored rate %f.",
                fallback_rate
            )
            return key, {
                "price": fallback_rate,
                "change": 0.00,
                "percent": 0.00,
                "open": fallback_rate,
                "high": fallback_rate,
                "low": fallback_rate,
                "volume": 0,
                "market_state": "CLOSED",
                "market": "idx",
            }

    return None


def _build_sse_light_stocks_payload(stocks_by_market):
    """SSE配信用の軽量株価ペイロードを構築"""
    fields = (
        "symbol",
        "name",
        "market",
        "price",
        "change",
        "change_percent",
        "high",
        "low",
        "volume",
        "currency",
        "market_state",
        "shares",
        "avg_price",
        "avg_fx_rate",
        "portfolio_value",
        "portfolio_pl",
        "sector",
        "industry",
    )
    payload: dict[str, list[Any]] = {"us": [], "jp": [], "idx": []}
    for market in ("us", "jp", "idx"):
        rows = (
            stocks_by_market.get(market, [])
            if isinstance(stocks_by_market, dict)
            else []
        )
        out = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            row = {k: item.get(k) for k in fields if k in item}
            row["snapshot_ts_ms"] = item.get("snapshot_ts_ms")

            chart_rows = (
                item.get("chart_data")
                if isinstance(item.get("chart_data"), list)
                else []
            )
            if chart_rows:
                compact_chart = []
                for p in chart_rows[-24:]:
                    if not isinstance(p, dict):
                        continue
                    price = p.get("price")
                    if price is None:
                        continue
                    compact_chart.append(
                        {
                            "x": p.get("x"),
                            "price": price,
                        }
                    )
                if compact_chart:
                    row["chart_data"] = compact_chart

            out.append(row)
        payload[market] = out
    return payload


def announce_current_market_state() -> None:
    """現在のインメモリキャッシュ状態をシリアライズしてSSE配信する"""
    with app_state.cache.sse_data_lock:
        current_stocks_copy = copy.deepcopy(app_state.market.current_stocks_cache)
        indices_copy = copy.copy(app_state.market.current_indices_cache)
    yf_limited = app_state.is_yf_rate_limited()
    light_stocks = _build_sse_light_stocks_payload(current_stocks_copy)
    payload = json.dumps(
        {
            "stocks": light_stocks,
            "indices": indices_copy,
            "is_yfinance_rate_limited": yf_limited,
        }
    )
    app_state.sse_announcer.announce(f"data: {payload}\n\n")



def _run_scheduled_sync_job():
    """スケジュールされた同期ジョブを実行"""
    try:
        sync_all_stocks_now()
    finally:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
            pending = app_state.market.sync_pending
            if pending:
                app_state.market.sync_pending = False
        if pending:
            logger.info("Triggering pending stock sync.")
            schedule_sync_all_stocks_now()


def schedule_sync_all_stocks_now():
    """同期ジョブをスケジュール"""
    with app_state.market.is_syncing_lock:
        if app_state.market.is_syncing:
            with app_state.market.sync_schedule_lock:
                app_state.market.sync_pending = True
            return False

    with app_state.market.sync_schedule_lock:
        if app_state.market.sync_scheduled:
            app_state.market.sync_pending = True
            return False
        app_state.market.sync_scheduled = True

    try:
        app_state.execution.sync_refresh_executor.submit(_run_scheduled_sync_job)
        return True
    except Exception as exc:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = False
        logger.warning("Failed to schedule stock sync: %s", exc)
        return False


def _warm_payload_cache_from_disk() -> None:
    """Load cached stock payloads from disk into target cache on cold start.

    This allows the UI to display recent data immediately while the background
    thread fetches fresh data from yfinance.
    """
    try:
        # Ensure user stock data is loaded from file so we know which symbols to warm
        load_user_stocks(force=True)
        warmed = 0
        for market in ("us", "jp", "idx"):
            user_map = {}
            with app_state.market.user_stocks_lock:
                if market == "us":
                    user_map = dict(app_state.market.user_us)
                elif market == "jp":
                    user_map = dict(app_state.market.user_jp)
                elif market == "idx":
                    user_map = dict(app_state.market.user_idx)

            # Warm both user stocks and default stocks to populate the cache immediately on startup.
            symbols_to_warm = set(user_map.keys())
            for symbol in _default_stock_names(market).keys():
                symbols_to_warm.add(symbol)

            for symbol in symbols_to_warm:
                key = f"payload_{symbol}_{market}"
                # Set ignore_ttl=True to load cached payloads even if they are expired.
                # Background scheduler will refresh them asynchronously if market is open.
                cached = app_state.payload_disk_cache.get(key, ignore_ttl=True)
                if cached and isinstance(cached, dict) and cached.get("symbol"):
                    with app_state.cache.sse_data_lock:
                        target_list = app_state.market.target_stocks_cache.get(market, [])
                        if not any(
                            isinstance(s, dict) and s.get("symbol") == symbol
                            for s in target_list
                        ):
                            target_list.append(cached)
                            app_state.market.target_stocks_cache[market] = target_list
                    warmed += 1
        if warmed > 0:
            logger.info("Warmed %d stock payloads from disk cache (including defaults)", warmed)
            with app_state.cache.sse_data_lock:
                current_empty = not any(
                    app_state.market.current_stocks_cache.get(m)
                    for m in ("us", "jp", "idx")
                )
                if current_empty:
                    app_state.market.current_stocks_cache = copy.deepcopy(
                        app_state.market.target_stocks_cache
                    )
    except Exception as exc:
        logger.debug("Disk cache warm-up failed (non-critical): %s", exc)


def _prepare_sync_items(force_load: bool = True) -> List[Tuple[str, str, str]]:
    """Loads user stocks and default stocks, and prepares the items list for batch fetch."""
    if force_load:
        load_user_stocks(force=True)

    us_open = is_market_open("us")
    jp_open = is_market_open("jp")

    us_cache_empty = not (isinstance(app_state.market.current_stocks_cache, dict) and app_state.market.current_stocks_cache.get("us"))
    jp_cache_empty = not (isinstance(app_state.market.current_stocks_cache, dict) and app_state.market.current_stocks_cache.get("jp"))

    fetch_us = us_open or us_cache_empty
    fetch_jp = jp_open or jp_cache_empty

    def _placeholder_symbols(market):
        target_list = (
            app_state.market.target_stocks_cache.get(market, [])
            if isinstance(app_state.market.target_stocks_cache, dict)
            else []
        )
        return {
            s.get("symbol")
            for s in target_list
            if isinstance(s, dict) and s.get("price") in (None, "--", "")
        }

    us_placeholders = _placeholder_symbols("us") if not fetch_us else set()
    jp_placeholders = _placeholder_symbols("jp") if not fetch_jp else set()

    items = []
    with app_state.market.user_stocks_lock:
        user_us_snapshot = dict(app_state.market.user_us)
        user_jp_snapshot = dict(app_state.market.user_jp)
        user_idx_snapshot = dict(app_state.market.user_idx)

    user_us_set = set(user_us_snapshot.keys())
    user_jp_set = set(user_jp_snapshot.keys())
    user_idx_set = set(user_idx_snapshot.keys())

    if fetch_us:
        for s, n in user_us_snapshot.items():
            items.append((s, n, "us"))
    else:
        for s, n in user_us_snapshot.items():
            if s in us_placeholders:
                items.append((s, n, "us"))
    if fetch_jp:
        for s, n in user_jp_snapshot.items():
            items.append((s, n, "jp"))
    else:
        for s, n in user_jp_snapshot.items():
            if s in jp_placeholders:
                items.append((s, n, "jp"))
    for s, n in user_idx_snapshot.items():
        items.append((s, n, "idx"))

    for market_name, user_set in (
        ("us", user_us_set),
        ("jp", user_jp_set),
        ("idx", user_idx_set),
    ):
        if market_name == "us" and not fetch_us:
            continue
        if market_name == "jp" and not fetch_jp:
            continue

        for symbol, name in _default_stock_names(market_name).items():
            if symbol not in user_set:
                items.append((symbol, name, market_name))
    return items


def _process_fetched_stocks(
    fetched_items: List[Optional[dict]],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Splits fetched items into US, JP, and IDX results and updates caches."""
    us_res, jp_res, idx_res = [], [], []
    for item in fetched_items:
        if not item:
            continue
        m = item.get("market")
        if m == "us":
            us_res.append(item)
        elif m == "jp":
            jp_res.append(item)
        else:
            idx_res.append(item)

    with app_state.cache.sse_data_lock:
        # Preserve previous cache if we skipped fetching that market
        prev_us = app_state.market.target_stocks_cache.get("us", []) if isinstance(app_state.market.target_stocks_cache, dict) else []
        prev_jp = app_state.market.target_stocks_cache.get("jp", []) if isinstance(app_state.market.target_stocks_cache, dict) else []
        prev_idx = app_state.market.target_stocks_cache.get("idx", []) if isinstance(app_state.market.target_stocks_cache, dict) else []

        def merge_cache(prev_list, res_list):
            if not res_list:
                return prev_list
            res_dict = {item["symbol"]: item for item in res_list if item and "symbol" in item}
            merged = []
            seen = set()
            for item in prev_list:
                if not item or "symbol" not in item:
                    continue
                sym = item["symbol"]
                if sym in res_dict:
                    merged.append(res_dict[sym])
                    seen.add(sym)
                else:
                    merged.append(item)
            for item in res_list:
                if not item or "symbol" not in item:
                    continue
                sym = item["symbol"]
                if sym not in seen:
                    merged.append(item)
            return merged

        new_us = merge_cache(prev_us, us_res)
        new_jp = merge_cache(prev_jp, jp_res)
        new_idx = merge_cache(prev_idx, idx_res)

        app_state.market.target_stocks_cache = {"us": new_us, "jp": new_jp, "idx": new_idx}
        current_empty = not any(
            app_state.market.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if current_empty:
            app_state.market.current_stocks_cache = copy.deepcopy(
                app_state.market.target_stocks_cache
            )
    return new_us, new_jp, new_idx


def _update_indices_data(
    idx_res: List[dict], us_res: List[dict], jp_res: List[dict]
) -> None:
    """Updates the current indices cache and market status cache with fresh values."""
    header_mapping = {
        "^N225": "N225",
        "^DJI": "DJI",
        "USDJPY=X": "USDJPY",
        "JPY=X": "USDJPY",
        "EURJPY=X": "EURJPY",
        "^IXIC": "NASDAQ",
        "^GSPC": "SP500",
        "^VIX": "VIX",
    }
    new_header_data = {}
    for item in idx_res + us_res + jp_res:
        if not item:
            continue
        sym = item.get("symbol")
        if sym in header_mapping:
            h_key = header_mapping[sym]
            new_header_data[h_key] = {
                "price": item.get("price"),
                "change": item.get("change"),
                "percent": item.get("change_percent") or item.get("percent"),
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "volume": item.get("volume"),
                "market_state": item.get("market_state", "UNKNOWN"),
                "market": item.get("market"),
            }

    critical_indices = {
        "N225": "^N225",
        "DJI": "^DJI",
        "USDJPY": "USDJPY=X",
        "EURJPY": "EURJPY=X",
        "VIX": "^VIX",
        "NASDAQ": "^IXIC",
        "SP500": "^GSPC",
    }
    for key, sym in critical_indices.items():
        if key not in new_header_data or new_header_data[key].get("price") == "--":
            try:
                logger.debug(
                    "Safety net trigger: fetching %s (%s) individually",
                    key,
                    sym,
                )
                res = fetch_index_data(key, sym)
                if res and res[1]:
                    new_header_data[key] = res[1]
            except Exception as safety_exc:
                logger.warning("Safety net failed for %s: %s", key, safety_exc)

    if new_header_data:
        with app_state.cache.sse_data_lock:
            app_state.market.current_indices_cache.update(new_header_data)

        with app_state.market.market_status_lock:
            if "N225" in new_header_data:
                app_state.market.market_status_cache["jp"] = new_header_data["N225"].get(
                    "market_state"
                )
            if "SP500" in new_header_data:
                st = new_header_data["SP500"].get("market_state")
                app_state.market.market_status_cache["us"] = st
                app_state.market.market_status_cache["idx"] = st

        if "USDJPY" in new_header_data:
            rate_dict = new_header_data["USDJPY"]
            price_val: object = rate_dict.get("price")
            if price_val not in (None, "--", ""):
                try:
                    rate_float = float(price_val)  # type: ignore[arg-type]
                    if rate_float > 0:
                        app_state.market.last_usdjpy_rate = rate_float
                        save_user_stocks()
                except Exception as save_exc:
                    logger.debug("Failed to auto-save USDJPY rate: %s", save_exc)


def sync_all_stocks_now():
    """Yahoo Financeから全銘柄を一括同期し、ターゲットキャッシュを更新する"""
    with app_state.market.is_syncing_lock:
        if app_state.market.is_syncing:
            logger.info("Sync already in progress, skipping.")
            return
        app_state.market.is_syncing = True

    try:
        with app_state.cache.sse_data_lock:
            if getattr(app_state, "current_indices_cache", None) is None:
                app_state.market.current_indices_cache = {}

        # Cold-start: warm in-memory cache from disk before fetching
        target_empty = not any(
            app_state.market.target_stocks_cache.get(m)
            for m in ("us", "jp", "idx")
        )
        if target_empty:
            _warm_payload_cache_from_disk()

        items = _prepare_sync_items(force_load=not target_empty)

        snapshot_ts_ms = int(time.time() * 1000)
        fetched_items = fetch_stocks_batch(items, snapshot_ts_ms=snapshot_ts_ms)
        us_res, jp_res, idx_res = _process_fetched_stocks(fetched_items)

        if items and not (us_res or jp_res or idx_res):
            logger.warning(
                "Stock sync produced no valid items; preserving previous target cache."
            )
            return

        _update_indices_data(idx_res, us_res, jp_res)
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = copy.deepcopy(app_state.market.target_stocks_cache)
        announce_current_market_state()
        logger.info("Sync completed.")
    except Exception as e:
        logger.error("sync_all_stocks_now: %s", e)
        raise
    finally:
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = False


def bg_yahoo_fetch_loop():
    """Yahoo Financeデータの定期取得ループ"""
    app_state.execution.shutdown_event.wait(SSE_MARKET_OPEN_SLEEP)

    while not app_state.execution.shutdown_event.is_set():
        try:
            sync_all_stocks_now()
        except Exception as e:
            logger.error("sync_all_stocks_now failed: %s", e)
            # wrapped_loop in _start_background_threads handles crash recovery

        try:
            listener_count = app_state.sse_announcer.listener_count()
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_NO_LISTENER_SLEEP)
            elif not is_market_open("us") and not is_market_open("jp"):
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP)
            else:
                app_state.execution.shutdown_event.wait(SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP)
        except Exception as e:
            logger.error("Error in market check: %s", e)
            app_state.execution.shutdown_event.wait(60.0)


def _start_background_threads():
    """バックグラウンドスレッドを安全に開始（クラッシュ時に指数バックオフで再起動）"""

    def wrapped_loop(func, name):
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 20
        while not app_state.execution.shutdown_event.is_set():
            try:
                func()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors > MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        "%s thread stopped after %d consecutive errors.",
                        name,
                        MAX_CONSECUTIVE_ERRORS,
                    )
                    break
                sleep_time = min(2**consecutive_errors, 600)
                logger.error(
                    "%s thread crashed (consecutive=%d/%d). Retrying in %ds. Error: %s",
                    name,
                    consecutive_errors,
                    MAX_CONSECUTIVE_ERRORS,
                    sleep_time,
                    e,
                )
                app_state.execution.shutdown_event.wait(sleep_time)

    t1 = threading.Thread(
        target=wrapped_loop, args=(bg_yahoo_fetch_loop, "Yahoo"), daemon=True
    )
    app_state.execution.background_threads.append(t1)
    t1.start()
