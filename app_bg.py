# app_bg.py
"""Background synchronization, yfinance fetching, and SSE interpolation loop."""

import copy
import json
import logging
import random
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
    load_user_stocks,
    normalize_history_frame,
)
from app_state import app_state
from constants import (
    YFINANCE_MAX_RETRIES,
    YFINANCE_RETRY_WAIT,
)

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
        from app_state import yf_session_manager
        yf_session_manager.mark_rate_limited("yfinance", duration=120)
        logger.warning(
            "yfinance unauthorized/invalid crumb detected (401) for symbol=%s; rotated session/UA.",
            symbol,
        )
    elif "timeout" in exc_str_lower:
        logger.debug("yfinance timeout detected. symbol=%s", symbol)
    else:
        with app_state.yfinance_lock:
            app_state.yfinance_429_streak = 0


def fetch_stock(
    symbol: str,
    name_or_dict: Any,
    market: str,
    snapshot_ts_ms: Optional[int] = None,
) -> Optional[dict]:
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

        return build_stock_payload(
            symbol, name_or_dict, market, hist, snapshot_ts_ms=snapshot_ts_ms
        )
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

    results = []
    # バッチ失敗時や一部欠損時の過剰なフォールバックを制限（最大3銘柄まで）
    fallback_count = 0
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

        # Fallback to single query if batch extraction failed, but with strict limit
        if payload is None:
            if fallback_count < MAX_FALLBACKS:
                logger.info(
                    "Fallback single query triggered for %s (%d/%d)",
                    symbol,
                    fallback_count + 1,
                    MAX_FALLBACKS,
                )
                payload = fetch_stock(
                    symbol, name, market, snapshot_ts_ms=snapshot_ts_ms
                )
                fallback_count += 1
            else:
                logger.debug("Skipping fallback for %s: limit reached", symbol)

        results.append(payload)

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
                time.sleep(YFINANCE_RETRY_WAIT)
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
            app_state.current_indices_cache.get("USDJPY")
            if hasattr(app_state, "current_indices_cache")
            else None
        )
        if cached_usdjpy and cached_usdjpy.get("price") not in (None, "--", ""):
            logger.warning(
                "fetch_index_data failed for USDJPY; falling back to cached value."
            )
            return key, cached_usdjpy
        else:
            logger.warning(
                "fetch_index_data failed for USDJPY and no cached value exists; falling back to default 150.00."
            )
            return key, {
                "price": 150.00,
                "change": 0.00,
                "percent": 0.00,
                "open": 150.00,
                "high": 150.00,
                "low": 150.00,
                "volume": 0,
                "market_state": "CLOSED",
                "market": "idx",
            }

    return None


def interpolate_value(
    current,
    target,
    is_price=True,
    is_open=True,
    stock_market_state=None,
):
    """現在値と目標値の間を補間"""
    if target is None:
        return current

    try:
        target_float = float(target)
    except (ValueError, TypeError):
        return current

    if current is None:
        return target_float

    try:
        curr_float = float(current)
    except (ValueError, TypeError):
        return target_float

    if stock_market_state and stock_market_state != "REGULAR":
        return target_float

    diff = target_float - curr_float
    if abs(diff) < 1e-6:
        if is_open:
            # 目標値に到達している場合でも、わずかに動かす（リアルタイム演出）
            return curr_float + (curr_float * random.uniform(-0.0001, 0.0001))
        return target_float

    # 収束速度
    step = diff * 0.45
    min_step = 0.01 if is_price else 0.005

    if abs(step) < min_step:
        step = diff if abs(diff) < min_step else (min_step if diff > 0 else -min_step)

    return curr_float + step


def _round_if_numeric(value, digits=2):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def _update_c_item_static_fields(c_item: dict, t_item: dict) -> None:
    """静的フィールドをターゲットから同期"""
    static_keys = (
        "name",
        "market",
        "currency",
        "market_state",
        "shares",
        "avg_price",
        "portfolio_value",
        "portfolio_pl",
        "sector",
        "industry",
        "high",
        "low",
        "volume",
        "snapshot_ts_ms",
        "chart_data",
        "ohlc_data",
    )
    for k in static_keys:
        if k in t_item:
            c_item[k] = t_item[k]


def _interpolate_dynamic_fields(c_item: dict, t_item: dict, is_open: bool) -> None:
    """動的フィールド（株価関連）の補間"""
    market_state = t_item.get("market_state")
    if c_item.get("price") is not None and t_item.get("price") is not None:
        c_item["price"] = _round_if_numeric(
            interpolate_value(
                c_item["price"],
                t_item["price"],
                is_open=is_open,
                stock_market_state=market_state,
            )
        )
    if c_item.get("change") is not None and t_item.get("change") is not None:
        c_item["change"] = _round_if_numeric(
            interpolate_value(
                c_item["change"],
                t_item["change"],
                is_open=is_open,
                stock_market_state=market_state,
            )
        )
    if (
        c_item.get("change_percent") is not None
        and t_item.get("change_percent") is not None
    ):
        c_item["change_percent"] = _round_if_numeric(
            interpolate_value(
                c_item["change_percent"],
                t_item["change_percent"],
                is_price=False,
                is_open=is_open,
                stock_market_state=market_state,
            )
        )


def clone_structure_for_current(target_list, current_list, market="us", is_open=None):
    """ターゲット（目標値）から現在値を補間して新しいリストを作成"""
    if not target_list:
        return []

    if is_open is None:
        is_open = is_market_open(market)

    current_map = {
        item.get("symbol"): item
        for item in current_list
        if isinstance(item, dict) and "symbol" in item
    }
    new_current = []

    for t_item in target_list:
        if not t_item or not isinstance(t_item, dict):
            continue

        sym = t_item.get("symbol")
        # 既存アイテムがある場合は、不必要なdeepcopyを避けて必要なフィールドのみ更新
        if sym in current_map:
            c_item = current_map[sym].copy()
            _update_c_item_static_fields(c_item, t_item)
        else:
            # 新規アイテムのみ深いコピー（頻度は低い）
            c_item = copy.deepcopy(t_item)

        _interpolate_dynamic_fields(c_item, t_item, is_open)
        new_current.append(c_item)
    return new_current


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
    payload = {"us": [], "jp": [], "idx": []}
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


def bg_interpolate_loop():
    """継続的に全銘柄の現在値を補間してSSE配信（市場キャッシュ付き）"""
    last_market_check = time.time()
    market_check_interval = 60
    us_market_open = is_market_open("us")
    jp_market_open = is_market_open("jp")
    idx_market_open = is_market_open("idx")

    while not app_state.execution.shutdown_event.is_set():
        try:
            current_time = time.time()

            listener_count = app_state.sse_announcer.listener_count()
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(5.0)
                continue

            if current_time - last_market_check > market_check_interval:
                try:
                    us_market_open = is_market_open("us")
                    jp_market_open = is_market_open("jp")
                    idx_market_open = is_market_open("idx")
                    last_market_check = current_time
                except Exception as market_check_exc:
                    logger.debug("Market status check error: %s", market_check_exc)

            with app_state.sse_data_lock:
                target_us = list(app_state.target_stocks_cache.get("us", []))
                target_jp = list(app_state.target_stocks_cache.get("jp", []))
                target_idx = list(app_state.target_stocks_cache.get("idx", []))
                current_us = list(app_state.current_stocks_cache.get("us", []))
                current_jp = list(app_state.current_stocks_cache.get("jp", []))
                current_idx = list(app_state.current_stocks_cache.get("idx", []))

            new_current_stocks = {
                "us": clone_structure_for_current(
                    target_us, current_us, market="us", is_open=us_market_open
                ),
                "jp": clone_structure_for_current(
                    target_jp, current_jp, market="jp", is_open=jp_market_open
                ),
                "idx": clone_structure_for_current(
                    target_idx, current_idx, market="idx", is_open=idx_market_open
                ),
            }

            with app_state.sse_data_lock:
                app_state.current_stocks_cache = new_current_stocks
                app_state.current_indices_cache = app_state.target_indices_cache
                indices_copy = copy.copy(app_state.current_indices_cache)

            with app_state.yfinance_lock:
                yf_limited = app_state.is_yfinance_rate_limited and (
                    time.time() < app_state.yfinance_rate_limit_until
                )

            light_stocks = _build_sse_light_stocks_payload(new_current_stocks)
            payload = json.dumps(
                {
                    "stocks": light_stocks,
                    "indices": indices_copy,
                    "is_yfinance_rate_limited": yf_limited,
                }
            )
            app_state.sse_announcer.announce(f"data: {payload}\n\n")

            if not us_market_open and not jp_market_open:
                app_state.execution.shutdown_event.wait(10.0)
            else:
                app_state.execution.shutdown_event.wait(0.5)
        except Exception as e:
            logger.error("bg_interpolate_loop: %s", e)
            app_state.execution.shutdown_event.wait(0.5)


def _run_scheduled_sync_job():
    """スケジュールされた同期ジョブを実行"""
    try:
        sync_all_stocks_now()
    finally:
        with app_state.sync_schedule_lock:
            app_state.sync_scheduled = False
            pending = app_state.sync_pending
            if pending:
                app_state.sync_pending = False
        if pending:
            logger.info("Triggering pending stock sync.")
            schedule_sync_all_stocks_now()


def schedule_sync_all_stocks_now():
    """同期ジョブをスケジュール"""
    with app_state.is_syncing_lock:
        if app_state.is_syncing:
            with app_state.sync_schedule_lock:
                app_state.sync_pending = True
            return False

    with app_state.sync_schedule_lock:
        if app_state.sync_scheduled:
            app_state.sync_pending = True
            return False
        app_state.sync_scheduled = True

    try:
        app_state.sync_refresh_executor.submit(_run_scheduled_sync_job)
        return True
    except Exception as exc:
        with app_state.sync_schedule_lock:
            app_state.sync_scheduled = False
        logger.warning("Failed to schedule stock sync: %s", exc)
        return False


def _prepare_sync_items() -> List[Tuple[str, str, str]]:
    """Loads user stocks and default stocks, and prepares the items list for batch fetch."""
    load_user_stocks(force=True)

    us_open = is_market_open("us")
    jp_open = is_market_open("jp")

    us_cache_empty = not (isinstance(app_state.current_stocks_cache, dict) and app_state.current_stocks_cache.get("us"))
    jp_cache_empty = not (isinstance(app_state.current_stocks_cache, dict) and app_state.current_stocks_cache.get("jp"))

    fetch_us = us_open or us_cache_empty
    fetch_jp = jp_open or jp_cache_empty

    items = []
    with app_state.user_stocks_lock:
        user_us_snapshot = dict(app_state.user_us)
        user_jp_snapshot = dict(app_state.user_jp)
        user_idx_snapshot = dict(app_state.user_idx)

    user_us_set = set(user_us_snapshot.keys())
    user_jp_set = set(user_jp_snapshot.keys())
    user_idx_set = set(user_idx_snapshot.keys())

    if fetch_us:
        for s, n in user_us_snapshot.items():
            items.append((s, n, "us"))
    if fetch_jp:
        for s, n in user_jp_snapshot.items():
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

    with app_state.sse_data_lock:
        # Preserve previous cache if we skipped fetching that market
        prev_us = app_state.target_stocks_cache.get("us", []) if isinstance(app_state.target_stocks_cache, dict) else []
        prev_jp = app_state.target_stocks_cache.get("jp", []) if isinstance(app_state.target_stocks_cache, dict) else []
        prev_idx = app_state.target_stocks_cache.get("idx", []) if isinstance(app_state.target_stocks_cache, dict) else []

        new_us = us_res if us_res else prev_us
        new_jp = jp_res if jp_res else prev_jp
        new_idx = idx_res if idx_res else prev_idx

        app_state.target_stocks_cache = {"us": new_us, "jp": new_jp, "idx": new_idx}
        current_empty = not any(
            app_state.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
        )
        if current_empty:
            app_state.current_stocks_cache = copy.deepcopy(
                app_state.target_stocks_cache
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
        with app_state.sse_data_lock:
            app_state.current_indices_cache.update(new_header_data)

        with app_state.market_status_lock:
            if "N225" in new_header_data:
                app_state.market_status_cache["jp"] = new_header_data["N225"].get(
                    "market_state"
                )
            if "SP500" in new_header_data:
                st = new_header_data["SP500"].get("market_state")
                app_state.market_status_cache["us"] = st
                app_state.market_status_cache["idx"] = st


def sync_all_stocks_now():
    """Yahoo Financeから全銘柄を一括同期し、ターゲットキャッシュを更新する"""
    with app_state.is_syncing_lock:
        if app_state.is_syncing:
            logger.info("Sync already in progress, skipping.")
            return
        app_state.is_syncing = True

    try:
        with app_state.sse_data_lock:
            if getattr(app_state, "current_indices_cache", None) is None:
                app_state.current_indices_cache = {}
        items = _prepare_sync_items()

        snapshot_ts_ms = int(time.time() * 1000)
        fetched_items = fetch_stocks_batch(items, snapshot_ts_ms=snapshot_ts_ms)
        us_res, jp_res, idx_res = _process_fetched_stocks(fetched_items)

        if items and not (us_res or jp_res or idx_res):
            logger.warning(
                "Stock sync produced no valid items; preserving previous target cache."
            )
            return

        _update_indices_data(idx_res, us_res, jp_res)
        logger.info("Sync completed.")
    except Exception as e:
        logger.error("sync_all_stocks_now: %s", e)
        raise
    finally:
        with app_state.is_syncing_lock:
            app_state.is_syncing = False


def bg_yahoo_fetch_loop():
    """Yahoo Financeデータの定期取得ループ"""
    app_state.execution.shutdown_event.wait(0.5)
    consecutive_errors = 0
    max_consecutive_errors = 10

    while not app_state.execution.shutdown_event.is_set():
        try:
            sync_all_stocks_now()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.error(
                "sync_all_stocks_now failed (%d/%d): %s",
                consecutive_errors,
                max_consecutive_errors,
                e,
            )
            if consecutive_errors >= max_consecutive_errors:
                logger.critical(
                    "Too many consecutive errors, backing off for 5 minutes"
                )
                app_state.execution.shutdown_event.wait(300.0)
                consecutive_errors = 0

        try:
            listener_count = app_state.sse_announcer.listener_count()
            if listener_count == 0:
                app_state.execution.shutdown_event.wait(60.0)
            elif not is_market_open("us") and not is_market_open("jp"):
                app_state.execution.shutdown_event.wait(300.0)
            else:
                app_state.execution.shutdown_event.wait(30.0)
        except Exception as e:
            logger.error("Error in market check: %s", e)
            app_state.execution.shutdown_event.wait(60.0)


def _start_background_threads():
    """バックグラウンドスレッドを安全に開始（クラッシュ時に再起動）"""

    def wrapped_loop(func, name):
        consecutive_errors = 0
        max_consecutive_errors = 10
        while not app_state.execution.shutdown_event.is_set():
            try:
                func()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    "%s thread crashed (%d/%d): %s",
                    name,
                    consecutive_errors,
                    max_consecutive_errors,
                    e,
                )
                if consecutive_errors >= max_consecutive_errors:
                    logger.critical("%s thread stopped after too many errors", name)
                    break
                app_state.execution.shutdown_event.wait(min(2**consecutive_errors, 60))

    threading.Thread(
        target=wrapped_loop, args=(bg_yahoo_fetch_loop, "Yahoo"), daemon=True
    ).start()
    threading.Thread(
        target=wrapped_loop, args=(bg_interpolate_loop, "Interpolate"), daemon=True
    ).start()
