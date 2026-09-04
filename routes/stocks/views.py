# routes/stocks/views.py
"""Screener, heatmap, and user watchlist mutation endpoints."""

from __future__ import annotations

import hashlib
import logging
import math
import queue
import secrets
import time
from typing import Any

from flask import Blueprint, current_app, g, jsonify, request

from app_state import app_state
from constants import (
    CACHE_DURATION_HEATMAP,
    CACHE_DURATION_SEARCH,
    POPULAR_JP,
    POPULAR_US,
)
from credential_manager import get_or_create_extension_api_token
from error_codes import ErrorCode
from route_helpers import (
    _parse_stock_request,
    _stock_display_name,
    ensure_stock_placeholder_in_caches,
    invalidate_stock_caches,
    rate_limit,
    remove_stock_from_caches,
)
from routes.stocks.common import (
    _announce_watchlist_state,
    _fetch_heatmap_cached,
    _get_api_stocks_attr,
    _stored_symbol_aliases,
    _sync_realtime_symbol,
    _watchlist_capacity_error,
    _watchlist_has_capacity,
    build_popular_symbol_items_dispatch,
    build_screener_base_rows_dispatch,
    build_screener_enrichment_dispatch,
    get_cached_dispatch,
    require_trusted_or_admin,
    resolve_stocks_for_response,
    save_user_stocks,
    schedule_sync_all_stocks_now,
)
from utils.caching import (
    CACHE_FETCHING,
    _get_cached_value,
    _has_cached_key,
    clear_cache_prefix,
)
from utils.normalization import (
    is_valid_symbol,
    normalize_market,
    normalize_symbol,
    normalize_symbol_for_market,
)
from utils.stock_payload import (
    _get_stock_container,
    _stock_is_default_or_user,
    error_response,
)
from utils.storage import UserStocksPersistError
from utils.text_utils import _parse_json_request

logger = logging.getLogger(__name__)

views_bp = Blueprint("views", __name__)


@views_bp.route("/api/screener")
@rate_limit(max_requests=60, window_seconds=60)
def api_screener() -> Any:
    """簡易株式スクリーナーAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    market_filter = (request.args.get("market") or "all").strip().lower()
    sector_filter = (request.args.get("sector") or "all").strip()
    q = (request.args.get("q") or "").strip().lower()
    if len(q) > 200:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "検索ワードは200文字以内で入力してください"},
        )
    sort_by = (request.args.get("sort_by") or "market_cap").strip().lower()
    sort_order = (request.args.get("sort_order") or "desc").strip().lower()

    if market_filter not in ("all", "us", "jp"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "market は all/us/jp のいずれかを指定してください"},
        )
    if sort_by not in (
        "market_cap",
        "price",
        "change_percent",
        "change_pct",
        "volume",
        "symbol",
        "pe_ratio",
        "pe",
    ):
        return error_response(ErrorCode.INVALID_INPUT, details={"reason": "sort_by の値が不正です"})
    if sort_order not in ("asc", "desc"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "sort_order は asc/desc のいずれかを指定してください"},
        )

    def _parse_strict_float(raw: Any, field_name: str) -> Any:
        if raw is None or str(raw).strip() == "":
            return None
        if isinstance(raw, bool) or type(raw).__name__ in ("bool_", "bool"):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"{field_name} は数値で指定してください",
                    "fields": [field_name],
                },
            )
        try:
            res = float(str(raw).strip())
        except (ValueError, TypeError):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"{field_name} は数値で指定してください",
                    "fields": [field_name],
                },
            )
        if not math.isfinite(res):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"{field_name} は有限数で指定してください",
                    "fields": [field_name],
                },
            )
        return res

    def _parse_screener_float(val: Any) -> float | None:
        if val is None or isinstance(val, bool) or type(val).__name__ in ("bool_", "bool"):
            return None
        try:
            candidate = float(val)
            return candidate if math.isfinite(candidate) else None
        except (ValueError, TypeError):
            return None

    min_price = _parse_strict_float(request.args.get("min_price"), "min_price")
    if isinstance(min_price, tuple):
        return min_price
    max_price = _parse_strict_float(request.args.get("max_price"), "max_price")
    if isinstance(max_price, tuple):
        return max_price
    min_change = _parse_strict_float(request.args.get("min_change"), "min_change")
    if isinstance(min_change, tuple):
        return min_change
    max_change = _parse_strict_float(request.args.get("max_change"), "max_change")
    if isinstance(max_change, tuple):
        return max_change
    min_market_cap = _parse_strict_float(request.args.get("min_market_cap"), "min_market_cap")
    if isinstance(min_market_cap, tuple):
        return min_market_cap
    max_market_cap = _parse_strict_float(request.args.get("max_market_cap"), "max_market_cap")
    if isinstance(max_market_cap, tuple):
        return max_market_cap
    min_pe = _parse_strict_float(request.args.get("min_pe"), "min_pe")
    if isinstance(min_pe, tuple):
        return min_pe
    max_pe = _parse_strict_float(request.args.get("max_pe"), "max_pe")
    if isinstance(max_pe, tuple):
        return max_pe

    for name, val in [
        ("min_price", min_price),
        ("max_price", max_price),
        ("min_market_cap", min_market_cap),
        ("max_market_cap", max_market_cap),
        ("min_pe", min_pe),
        ("max_pe", max_pe),
    ]:
        if val is not None and val < 0:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"{name} は0以上の数値である必要があります", "fields": [name]},
                status_code=400,
            )

    if min_price is not None and max_price is not None and min_price > max_price:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "min_price は max_price 以下である必要があります",
                "fields": ["min_price", "max_price"],
            },
            status_code=400,
        )
    if min_change is not None and max_change is not None and min_change > max_change:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "min_change は max_change 以下である必要があります",
                "fields": ["min_change", "max_change"],
            },
            status_code=400,
        )
    if (
        min_market_cap is not None
        and max_market_cap is not None
        and min_market_cap > max_market_cap
    ):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "min_market_cap は max_market_cap 以下である必要があります",
                "fields": ["min_market_cap", "max_market_cap"],
            },
            status_code=400,
        )
    if min_pe is not None and max_pe is not None and min_pe > max_pe:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "min_pe は max_pe 以下である必要があります",
                "fields": ["min_pe", "max_pe"],
            },
            status_code=400,
        )

    raw_limit = request.args.get("limit")
    limit_val = 150
    if raw_limit is not None and raw_limit.strip() != "":
        try:
            limit_val = int(raw_limit.strip())
            if limit_val < 1:
                limit_val = 1
            elif limit_val > 500:
                limit_val = 500
        except (ValueError, TypeError):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": "limit は整数で指定してください", "fields": ["limit"]},
            )

    stocks_data = resolve_stocks_for_response(include_portfolio=False)
    all_stocks = build_screener_base_rows_dispatch(stocks_data, market_filter)
    seen_symbols = {item["symbol"] for item in all_stocks}

    pop_us = _get_api_stocks_attr("POPULAR_US", POPULAR_US)
    pop_jp = _get_api_stocks_attr("POPULAR_JP", POPULAR_JP)
    pop_sources = []
    if market_filter in ("all", "us"):
        pop_sources.append(("us", pop_us))
    if market_filter in ("all", "jp"):
        pop_sources.append(("jp", pop_jp))

    pop_unseen_items = build_popular_symbol_items_dispatch(
        market_filter, q, seen_symbols, pop_sources
    )

    q_symbol = None
    raw_q_symbol = normalize_symbol(request.args.get("q"))
    if raw_q_symbol and is_valid_symbol(raw_q_symbol):
        mkt_q = "jp" if (raw_q_symbol.endswith(".T") or raw_q_symbol.isdigit()) else "us"
        q_symbol = normalize_symbol_for_market(raw_q_symbol, mkt_q)
        if market_filter == "all" or market_filter == mkt_q:
            if q_symbol not in seen_symbols:
                seen_symbols.add(q_symbol)
                pop_unseen_items.append((q_symbol, q_symbol, mkt_q))
        else:
            q_symbol = None

    if pop_unseen_items:
        _enrich_symbols = ",".join(sorted({sym for sym, _n, _m in pop_unseen_items}))
        enrich_key = (
            f"screener_enrich_{market_filter}_"
            f"q{hashlib.sha256(q.encode('utf-8')).hexdigest()}_"
            f"{hashlib.sha256(_enrich_symbols.encode('utf-8')).hexdigest()}"
        )
        enriched = get_cached_dispatch(
            enrich_key,
            lambda: build_screener_enrichment_dispatch(
                pop_unseen_items,
                q_symbol,
            ),
            duration=CACHE_DURATION_SEARCH,
        )
        if enriched is CACHE_FETCHING or not isinstance(enriched, dict):
            enriched = {}
        for sym, _fallback_name, _mkt in pop_unseen_items:
            row = enriched.get(sym)
            if row is not None:
                all_stocks.append(row)

    filtered = []
    for item in all_stocks:
        item_sec = str(item.get("sector") or "")
        if sector_filter != "all" and item_sec.lower() != sector_filter.lower():
            continue

        p_float = _parse_screener_float(item.get("price"))
        if min_price is not None and (p_float is None or p_float < min_price):
            continue
        if max_price is not None and (p_float is None or p_float > max_price or p_float <= 0):
            continue

        c_float = _parse_screener_float(item.get("change_percent"))
        if min_change is not None and (c_float is None or c_float < min_change):
            continue
        if max_change is not None and (c_float is None or c_float > max_change):
            continue

        mc_float = _parse_screener_float(item.get("market_cap"))
        if min_market_cap is not None and (mc_float is None or mc_float < min_market_cap):
            continue
        if max_market_cap is not None and (
            mc_float is None or mc_float > max_market_cap or mc_float <= 0
        ):
            continue

        pe_val = (
            item.get("pe_ratio")
            if item.get("pe_ratio") is not None
            else (item.get("pe") if item.get("pe") is not None else item.get("trailingPE"))
        )
        pe_float = _parse_screener_float(pe_val)
        if min_pe is not None and (pe_float is None or pe_float < min_pe):
            continue
        if max_pe is not None and (pe_float is None or pe_float > max_pe or pe_float <= 0):
            continue

        if q:
            sym_str = str(item.get("symbol") or "").lower()
            name_str = str(item.get("name") or "").lower()
            sec_str = item_sec.lower()
            matched_q = q in sym_str or q in name_str or q in sec_str
            if not matched_q:
                continue
        filtered.append(item)

    reverse = sort_order != "asc"

    def _safe_sort_key(item: dict[str, Any], field: str) -> Any:
        val = item.get(field)
        if field == "pe_ratio" and val is None:
            val = item.get("pe") if item.get("pe") is not None else item.get("trailingPE")
        if val is None or isinstance(val, bool) or type(val).__name__ in ("bool_", "bool"):
            return "" if field == "symbol" else -math.inf if reverse else math.inf
        if field == "symbol":
            return str(val)
        num = _parse_screener_float(val)
        if num is not None:
            return num
        return -math.inf if reverse else math.inf

    sort_field = {
        "change_pct": "change_percent",
        "pe": "pe_ratio",
    }.get(sort_by, sort_by)
    if sort_field not in ("price", "change_percent", "volume", "symbol", "market_cap", "pe_ratio"):
        sort_field = "market_cap"
    filtered.sort(key=lambda x: _safe_sort_key(x, sort_field), reverse=reverse)

    return jsonify(
        {
            "ok": True,
            "total": min(len(filtered), limit_val),
            "totalFiltered": len(filtered),
            "stocks": filtered[:limit_val],
        }
    )


@views_bp.route("/api/stocks/add", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def api_add_stock() -> Any:
    """銘柄追加APIエンドポイント"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    parsed, error = _parse_stock_request(data, require_name=True, default_market="")
    if error:
        return error
    if parsed is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"}
        )
    name = parsed["name"]
    market = parsed["market"]
    symbol = parsed["symbol"]

    with app_state.market.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        if _stock_is_default_or_user(symbol, market) or any(
            alias in container for alias in _stored_symbol_aliases(symbol, market)
        ):
            return error_response(ErrorCode.INVALID_INPUT, details={"reason": "既に追加済み"})
        if not _watchlist_has_capacity(market):
            return _watchlist_capacity_error(market)
        container[symbol] = name

        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            container.pop(symbol, None)
            current_app.logger.error("Failed to persist added stock %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
    invalidate_stock_caches(symbol)
    ensure_stock_placeholder_in_caches(symbol, name, market)

    _announce_watchlist_state()
    _sync_realtime_symbol(symbol, market, register=True)
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@views_bp.route("/api/stocks/delete", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def api_delete_stock() -> Any:
    """銘柄削除APIエンドポイント"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    parsed, error = _parse_stock_request(data, default_market="")
    if error:
        return error
    if parsed is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"}
        )
    market = parsed["market"]
    symbol = parsed["symbol"]
    storage_aliases = _stored_symbol_aliases(symbol, market)

    with app_state.market.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        previous_values = {
            stored_symbol: container.pop(stored_symbol)
            for stored_symbol in storage_aliases
            if stored_symbol in container
        }

        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            container.update(previous_values)
            current_app.logger.error("Failed to persist deleted stock %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
    for stored_symbol in storage_aliases:
        invalidate_stock_caches(stored_symbol)
        remove_stock_from_caches(stored_symbol, market)

    for stored_symbol in storage_aliases:
        _sync_realtime_symbol(stored_symbol, market, register=False)

    _announce_watchlist_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@views_bp.route("/api/stocks/add_ext", methods=["POST", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_add_stock_ext() -> Any:
    """拡張機能用銘柄追加APIエンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    from utils.env_helpers import _is_remote_api_enabled

    if _is_remote_api_enabled():
        current_app.logger.warning(
            "api_add_stock_ext rejected: not available in remote API mode id=%s",
            getattr(g, "request_id", "-"),
        )
        return error_response(
            ErrorCode.FORBIDDEN, details={"reason": "forbidden in remote API mode"}, status_code=403
        )

    import utils.networking

    if not utils.networking._is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)

    if request.headers.get("X-MNS-Extension-Request", "").strip().lower() != "true":
        current_app.logger.warning(
            "api_add_stock_ext: missing extension marker header id=%s",
            getattr(g, "request_id", "-"),
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"reason": "invalid or missing extension marker"},
            status_code=403,
        )

    raw_remote = request.environ.get("RAW_REMOTE_ADDR") or request.environ.get("REMOTE_ADDR", "")
    raw_remote = str(raw_remote).strip()

    if raw_remote and not utils.networking._is_loopback_ip(raw_remote):
        current_app.logger.warning(
            "Add-ext request rejected: WSGI REMOTE_ADDR %s is not loopback", raw_remote
        )
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "forbidden: remote address not loopback"},
            status_code=403,
        )

    if not utils.networking._is_allowed_shutdown_origin(request):
        current_app.logger.warning(
            "api_add_stock_ext: missing or untrusted origin id=%s remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "untrusted origin"}, status_code=403
        )

    # Origin validation must precede token initialization. The token helper
    # may create and persist a master key/token on first use; an untrusted
    # local caller must not be able to trigger that state change merely by
    # probing this CSRF-exempt endpoint.
    auth_header = request.headers.get("Authorization")
    get_token_fn = _get_api_stocks_attr(
        "get_or_create_extension_api_token", get_or_create_extension_api_token
    )
    expected_token = get_token_fn()

    is_valid_token = False
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
        is_valid_token = secrets.compare_digest(token, expected_token)

    if not is_valid_token:
        current_app.logger.warning(
            "api_add_stock_ext: security rejection id=%s remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"reason": "invalid or missing extension token"},
            status_code=403,
        )

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    parsed, error = _parse_stock_request(data, require_name=False)
    if error:
        return error
    if parsed is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "パース結果がありません"}
        )
    market = parsed["market"]
    symbol = parsed["symbol"]

    name = parsed["name"] or _stock_display_name(symbol, market)
    with app_state.market.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)
        if _stock_is_default_or_user(symbol, market) or any(
            alias in container for alias in _stored_symbol_aliases(symbol, market)
        ):
            return jsonify({"ok": True, "message": f"{symbol} already exists in {market}"})
        if not _watchlist_has_capacity(market):
            return _watchlist_capacity_error(market)
        container[symbol] = name

        try:
            save_fn = _get_api_stocks_attr("save_user_stocks", save_user_stocks)
            save_fn()
        except UserStocksPersistError as exc:
            container.pop(symbol, None)
            current_app.logger.error("Failed to persist extension-added stock %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
    invalidate_stock_caches(symbol)
    ensure_stock_placeholder_in_caches(symbol, name, market)

    _announce_watchlist_state()
    _sync_realtime_symbol(symbol, market, register=True)
    sync_fn = _get_api_stocks_attr("schedule_sync_all_stocks_now", schedule_sync_all_stocks_now)
    sync_fn()
    return jsonify({"ok": True, "message": f"Added {symbol} to {market}"})


@views_bp.route("/api/stocks/reset", methods=["POST"])
@rate_limit(max_requests=5, window_seconds=60)
def api_reset_stocks() -> Any:
    """銘柄リセットAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )

    with app_state.market.user_stocks_lock:
        previous_us = app_state.market.user_us
        previous_jp = app_state.market.user_jp
        previous_idx = app_state.market.user_idx
        app_state.market.user_us, app_state.market.user_jp, app_state.market.user_idx = {}, {}, {}
        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            (
                app_state.market.user_us,
                app_state.market.user_jp,
                app_state.market.user_idx,
            ) = previous_us, previous_jp, previous_idx
            current_app.logger.error("Failed to persist stock reset: %s", exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.current_indices_cache = {}
            app_state.market.target_indices_cache = {}
    for market, previous in (
        ("us", previous_us),
        ("jp", previous_jp),
        ("idx", previous_idx),
    ):
        for symbol in previous:
            _sync_realtime_symbol(symbol, market, register=False)
    try:
        app_state.payload_disk_cache.delete("indices_cache")
    except Exception as exc:
        current_app.logger.debug("Failed to delete indices_cache from disk cache: %s", exc)
    clear_cache_prefix("stocks")
    _announce_watchlist_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@views_bp.route("/api/heatmap")
@rate_limit(max_requests=30, window_seconds=60)
def api_heatmap() -> Any:
    """ヒートマップデータAPIエンドポイント"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)
    market = normalize_market(request.args.get("market"), default="us")
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if market not in ("us", "jp"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "heatmap market は us/jp のみ対応です"},
        )
    symbols = POPULAR_US if market == "us" else POPULAR_JP

    cache_key = f"heatmap_{market}"

    if _has_cached_key(cache_key, CACHE_DURATION_HEATMAP):
        cached = _get_cached_value(cache_key, CACHE_DURATION_HEATMAP)
        if cached:
            return jsonify(cached)

    disk_cached = None
    try:
        disk_cached = app_state.payload_disk_cache.get(cache_key, ignore_ttl=True)
    except Exception as exc:
        logger.debug("Failed to read heatmap disk cache: %s", exc)

    with app_state.heatmap_fetch_lock:
        now = time.time()
        if cache_key in app_state.heatmap_fetch_inflight:
            start_time = app_state.heatmap_fetch_start_times.get(cache_key, 0.0)
            if now - start_time > 30.0:
                app_state.heatmap_fetch_inflight.discard(cache_key)
                app_state.heatmap_fetch_start_times.pop(cache_key, None)

        already_fetching = cache_key in app_state.heatmap_fetch_inflight
        if not already_fetching:
            app_state.heatmap_fetch_inflight.add(cache_key)
            app_state.heatmap_fetch_start_times[cache_key] = now

    if not already_fetching:
        try:
            app_state.execution.data_executor.submit(
                _fetch_heatmap_cached, cache_key, market, symbols
            )
        except queue.Full:
            with app_state.heatmap_fetch_lock:
                app_state.heatmap_fetch_inflight.discard(cache_key)
                app_state.heatmap_fetch_start_times.pop(cache_key, None)
            current_app.logger.warning("Heatmap fetch queue is full market=%s", market)
            if not disk_cached:
                return error_response(
                    ErrorCode.TOO_MANY_REQUESTS,
                    details={
                        "reason": "ヒートマップ取得の処理容量を超えました。しばらくしてから再試行してください。"
                    },
                    status_code=503,
                )
        except Exception as exc:
            with app_state.heatmap_fetch_lock:
                app_state.heatmap_fetch_inflight.discard(cache_key)
                app_state.heatmap_fetch_start_times.pop(cache_key, None)
            logger.warning("Failed to submit heatmap fetch for %s: %s", market, exc)

    if disk_cached and isinstance(disk_cached, dict) and disk_cached.get("stocks"):
        return jsonify(disk_cached)

    return jsonify(
        {
            "stocks": [],
            "fetching": True,
            "message": "ヒートマップデータを取得中です。しばらくしてから再読み込みしてください。",
        }
    )
