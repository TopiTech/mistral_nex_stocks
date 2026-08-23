# routes/stocks/ai_portfolio.py
"""AI portfolio generation, rebalancing, persistence, and portfolio copy endpoints."""

from __future__ import annotations

import logging
import math
import queue
import re
import secrets
import threading
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any, TypedDict

from cachetools import TTLCache
from flask import Blueprint, current_app, g, jsonify, request, session

from app_state import app_state
from constants import (
    AI_PORTFOLIO_MARKETS,
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
)
from error_codes import ErrorCode
from route_helpers import (
    _stock_display_name,
    ensure_stock_placeholder_in_caches,
    extract_api_key,
    invalidate_stock_caches,
    rate_limit,
)
from routes.stocks.common import (
    _announce_watchlist_state,
    _get_api_stocks_attr,
    _stored_symbol_aliases,
    _sync_realtime_symbol,
    _watchlist_capacity_error,
    _watchlist_has_capacity,
    require_trusted_or_admin,
    save_user_stocks,
    schedule_sync_all_stocks_now,
)
from services.ai_portfolio_service import (
    DEFAULT_PRESET_CONFIGS,
    MAX_AI_PORTFOLIO_ITEMS,
    VIRTUAL_INITIAL_CAPITAL_JPY,
    delete_custom_ai_portfolio,
    generate_ai_portfolio_by_theme,
    load_saved_ai_portfolios,
    sanitize_ai_portfolio,
    save_custom_ai_portfolio,
)
from utils.normalization import is_valid_symbol, normalize_symbol_for_market
from utils.stock_payload import (
    _get_stock_container,
    error_response,
    get_current_usdjpy_rate,
)
from utils.text_utils import _parse_json_request

logger = logging.getLogger(__name__)

ai_portfolio_bp = Blueprint("ai_portfolio", __name__)


class FetchJob(TypedDict):
    result: Any
    error: BaseException | None
    done: threading.Event


ai_portfolio_fetch_lock = threading.Lock()
ai_portfolio_fetch_inflight: dict[str, Any] = {}

AI_PORTFOLIO_RESULT_CACHE_TTL = 300.0
ai_portfolio_result_cache: TTLCache[str, tuple[float, Any, BaseException | None]] = TTLCache(
    maxsize=128, ttl=AI_PORTFOLIO_RESULT_CACHE_TTL
)

_OPERATION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _get_conversation_scope() -> str:
    scope = session.get("mns_analysis_conversation")
    if not isinstance(scope, str) or not _OPERATION_TOKEN_RE.fullmatch(scope):
        scope = secrets.token_urlsafe(24)
        session["mns_analysis_conversation"] = scope
    return scope


@ai_portfolio_bp.route("/api/ai-portfolio", methods=["GET"])
@rate_limit(max_requests=60, window_seconds=60)
def api_get_ai_portfolios() -> Any:
    """AIポートフォリオ一覧（プリセットおよび保存済みカスタムテーマ）を取得"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    saved = load_saved_ai_portfolios()
    return jsonify(
        {
            "ok": True,
            "presets": DEFAULT_PRESET_CONFIGS,
            "saved": saved,
        }
    )


@ai_portfolio_bp.route("/api/ai-portfolio/generate", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_generate_ai_portfolio() -> Any:
    """テーマに基づいてAIポートフォリオを生成"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request()
    if data is None or not isinstance(data, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_theme = data.get("theme")
    if raw_theme is None:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["theme"]}, status_code=400
        )
    if not isinstance(raw_theme, str):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "themeは文字列で指定してください", "fields": ["theme"]},
            status_code=400,
        )
    theme = raw_theme.strip()
    if not theme:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["theme"]}, status_code=400
        )
    if len(theme) > 120:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "themeは120文字以内で指定してください"},
            status_code=400,
        )

    api_key = extract_api_key(request)

    conversation_scope = _get_conversation_scope()
    inflight_key = f"generate:{conversation_scope}:{theme}"
    with ai_portfolio_fetch_lock:
        cached = ai_portfolio_result_cache.get(inflight_key)
    if cached is not None:
        _cached_ts, cached_result, cached_err = cached
        if cached_err is not None:
            return error_response(
                ErrorCode.INTERNAL_SERVER_ERROR,
                details={"reason": "AI ポートフォリオの生成に失敗しました"},
                status_code=500,
            )
        if cached_result is not None:
            return jsonify({"ok": True, "portfolio": cached_result})

    with ai_portfolio_fetch_lock:
        if inflight_key in ai_portfolio_fetch_inflight:
            result_holder = ai_portfolio_fetch_inflight[inflight_key]
            already_fetching = True
        else:
            new_result_holder: FetchJob = {
                "result": None,
                "error": None,
                "done": threading.Event(),
            }
            ai_portfolio_fetch_inflight[inflight_key] = new_result_holder
            result_holder = new_result_holder
            already_fetching = False

    if not already_fetching:

        def _run_ai_portfolio_job() -> None:
            try:
                gen_fn = _get_api_stocks_attr("generate_ai_portfolio_by_theme", generate_ai_portfolio_by_theme)
                res = gen_fn(theme, api_key=api_key)
                result_holder["result"] = res
            except Exception as exc:
                result_holder["error"] = exc
            finally:
                with ai_portfolio_fetch_lock:
                    ai_portfolio_fetch_inflight.pop(inflight_key, None)
                    if result_holder["error"] is None and result_holder["result"] is not None:
                        ai_portfolio_result_cache[inflight_key] = (
                            time.time(),
                            result_holder["result"],
                            None,
                        )
                result_holder["done"].set()

        try:
            import route_helpers
            submit_fn = _get_api_stocks_attr("_submit_in_app_context", route_helpers._submit_in_app_context)
            submit_fn(app_state.execution.executor, _run_ai_portfolio_job)
        except queue.Full as exc:
            current_app.logger.warning(
                "AI portfolio job queue is full id=%s: %s", getattr(g, "request_id", "-"), exc
            )
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            return error_response(
                ErrorCode.TOO_MANY_REQUESTS,
                details={
                    "reason": "サーバーの処理容量を超えました。しばらくしてから再試行してください。"
                },
                status_code=503,
            )
        except Exception as exc:
            current_app.logger.error("Failed to schedule AI portfolio job: %s", exc)
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    finished = result_holder["done"].wait(timeout=3.0)
    if not finished:
        return jsonify({"fetching": True})

    if result_holder["error"] is not None:
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    return jsonify({"ok": True, "portfolio": result_holder["result"]})


@ai_portfolio_bp.route("/api/ai-portfolio/rebalance", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_rebalance_ai_portfolio() -> Any:
    """指定されたAIポートフォリオのリバランス（再評価）を実行"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request()
    if data is None or not isinstance(data, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_theme = data.get("theme")
    if raw_theme is not None and not isinstance(raw_theme, str):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "themeは文字列で指定してください", "fields": ["theme"]},
            status_code=400,
        )
    theme = raw_theme.strip() if isinstance(raw_theme, str) else ""
    if not theme:
        theme = "tech"
    if len(theme) > 120:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "テーマは120文字以内で入力してください"},
            status_code=400,
        )

    api_key = extract_api_key(request)

    conversation_scope = _get_conversation_scope()
    inflight_key = f"rebalance:{conversation_scope}:{theme}"
    with ai_portfolio_fetch_lock:
        cached = ai_portfolio_result_cache.pop(inflight_key, None)
    if cached is not None:
        _cached_ts, cached_result, cached_err = cached
        if cached_err is not None:
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)
        if cached_result is not None:
            return jsonify(
                {"ok": True, "portfolio": cached_result, "message": "リバランスが完了しました"}
            )

    with ai_portfolio_fetch_lock:
        if inflight_key in ai_portfolio_fetch_inflight:
            result_holder = ai_portfolio_fetch_inflight[inflight_key]
            already_fetching = True
        else:
            new_result_holder: FetchJob = {
                "result": None,
                "error": None,
                "done": threading.Event(),
            }
            ai_portfolio_fetch_inflight[inflight_key] = new_result_holder
            result_holder = new_result_holder
            already_fetching = False

    if not already_fetching:

        def _run_ai_rebalance_job() -> None:
            try:
                gen_fn = _get_api_stocks_attr("generate_ai_portfolio_by_theme", generate_ai_portfolio_by_theme)
                res = gen_fn(theme, force_rebalance=True, api_key=api_key)
                result_holder["result"] = res
            except Exception as exc:
                result_holder["error"] = exc
            finally:
                with ai_portfolio_fetch_lock:
                    ai_portfolio_fetch_inflight.pop(inflight_key, None)
                    if result_holder["error"] is None and result_holder["result"] is not None:
                        ai_portfolio_result_cache[inflight_key] = (
                            time.time(),
                            result_holder["result"],
                            None,
                        )
                result_holder["done"].set()

        try:
            import route_helpers
            submit_fn = _get_api_stocks_attr("_submit_in_app_context", route_helpers._submit_in_app_context)
            submit_fn(app_state.execution.executor, _run_ai_rebalance_job)
        except queue.Full as exc:
            current_app.logger.warning(
                "AI rebalance job queue is full id=%s: %s", getattr(g, "request_id", "-"), exc
            )
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            return error_response(
                ErrorCode.TOO_MANY_REQUESTS,
                details={
                    "reason": "サーバーの処理容量を超えました。しばらくしてから再試行してください。"
                },
                status_code=503,
            )
        except Exception as exc:
            current_app.logger.error("Failed to schedule AI rebalance job: %s", exc)
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    finished = result_holder["done"].wait(timeout=3.0)
    if not finished:
        return jsonify({"fetching": True})

    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache.pop(inflight_key, None)

    if result_holder["error"] is not None:
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    return jsonify(
        {"ok": True, "portfolio": result_holder["result"], "message": "リバランスが完了しました"}
    )


@ai_portfolio_bp.route("/api/ai-portfolio/save", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_save_ai_portfolio() -> Any:
    """カスタムAIポートフォリオを永続化保存"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request() or {}
    portfolio = data.get("portfolio")
    if not isinstance(portfolio, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "無効なポートフォリオデータです"},
            status_code=400,
        )

    canonical_portfolio = sanitize_ai_portfolio(portfolio)
    if not canonical_portfolio.get("title"):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "無効なポートフォリオデータです（タイトルがありません）"},
            status_code=400,
        )
    if portfolio.get("items") is not None and not canonical_portfolio.get("items"):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "有効な銘柄データがありません"},
            status_code=400,
        )

    save_fn = _get_api_stocks_attr("save_custom_ai_portfolio", save_custom_ai_portfolio)
    success = save_fn(canonical_portfolio)
    if not success:
        return error_response(
            ErrorCode.INTERNAL_SERVER_ERROR,
            details={"reason": "保存に失敗しました"},
            status_code=500,
        )

    return jsonify({"ok": True, "portfolio": canonical_portfolio})


@ai_portfolio_bp.route("/api/ai-portfolio/custom", methods=["DELETE"])
@rate_limit(max_requests=20, window_seconds=60)
def api_delete_ai_portfolio() -> Any:
    """保存済みカスタムAIポートフォリオを削除"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request()
    if data is None or not isinstance(data, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_id = data.get("id")
    if raw_id is None:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["id"]}, status_code=400
        )
    if not isinstance(raw_id, str):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "idは文字列で指定してください", "fields": ["id"]},
            status_code=400,
        )
    portfolio_id = raw_id.strip()
    if not portfolio_id:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["id"]}, status_code=400
        )
    if len(portfolio_id) > 256:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "IDは256文字以内で指定してください"},
            status_code=400,
        )

    delete_fn = _get_api_stocks_attr("delete_custom_ai_portfolio", delete_custom_ai_portfolio)
    success = delete_fn(portfolio_id)
    if not success:
        return error_response(
            ErrorCode.NOT_FOUND,
            details={"reason": "対象ポートフォリオが見つかりません"},
            status_code=404,
        )

    return jsonify({"ok": True, "id": portfolio_id})


@ai_portfolio_bp.route("/api/ai-portfolio/copy-to-my", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_copy_ai_portfolio_to_my() -> Any:
    """AIポートフォリオの構成銘柄をユーザーのマイポートフォリオへ複製"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request()
    if data is None or not isinstance(data, dict):
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "itemsリストが必要です"}, status_code=400
        )
    if len(items) > MAX_AI_PORTFOLIO_ITEMS:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": f"items は最大 {MAX_AI_PORTFOLIO_ITEMS} 件まで指定できます"},
            status_code=400,
        )

    parsed_items: list[tuple[str, str, float, float]] = []
    raw_validated_items: list[tuple[str, str, float, float]] = []
    total_weight_pct = 0.0
    stale_warning: str | None = None
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return error_response(
                ErrorCode.MALFORMED_INPUT,
                details={"reason": f"items[{idx}] が無効です"},
                status_code=400,
            )
        market = str(item.get("market") or "us").strip().lower()
        symbol = normalize_symbol_for_market(item.get("symbol"), market)
        if market not in AI_PORTFOLIO_MARKETS or not is_valid_symbol(symbol):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] のシンボルまたは市場が無効です"},
                status_code=400,
            )
        target_raw = item.get("target_price")
        weight_raw = item.get("weight_pct")
        if isinstance(target_raw, bool) or isinstance(weight_raw, bool):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] の数値が無効です（真偽値は不可）"},
                status_code=400,
            )
        try:
            if target_raw is None or str(target_raw).strip() == "":
                target_price = 100.0
            else:
                target_price = float(target_raw)
            weight_pct = float(weight_raw or 0.0)
        except (TypeError, ValueError):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] の数値が無効です"},
                status_code=400,
            )
        if (
            not math.isfinite(target_price)
            or not math.isfinite(weight_pct)
            or target_price <= 0
            or target_price > PORTFOLIO_AVG_PRICE_MAX
        ):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"items[{idx}] の target_price は 0 より大きく {PORTFOLIO_AVG_PRICE_MAX:,.0f} 以下の値が必要です"
                },
                status_code=400,
            )
        if not (0.0 < weight_pct <= 100.0):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] の weight_pct は 0〜100 の範囲が必要です"},
                status_code=400,
            )
        total_weight_pct += weight_pct
        raw_validated_items.append((symbol, market, target_price, weight_pct))

    if total_weight_pct > 100.5:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "items の weight_pct 合計は100以下である必要があります"},
            status_code=400,
        )

    has_us_items = any(m == "us" for _, m, _, _ in raw_validated_items)
    resolved_usdjpy_rate = 150.0
    if has_us_items:
        rate_is_valid = True
        try:
            resolved_usdjpy_rate = float(getattr(app_state.market, "last_usdjpy_rate", 150.0))
        except (TypeError, ValueError):
            resolved_usdjpy_rate = 150.0
            rate_is_valid = False
        if not math.isfinite(resolved_usdjpy_rate) or resolved_usdjpy_rate <= 0:
            resolved_usdjpy_rate = 150.0
            rate_is_valid = False
        try:
            usdjpy_rate_ts = float(getattr(app_state.market, "last_usdjpy_rate_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            usdjpy_rate_ts = 0.0
        now = time.time()
        is_stale = (
            not rate_is_valid
            or not math.isfinite(usdjpy_rate_ts)
            or usdjpy_rate_ts <= 0.0
            or usdjpy_rate_ts > now + 300.0
            or (now - usdjpy_rate_ts) > 24 * 3600
        )

        if is_stale:
            try:
                resolved_fx, is_est = get_current_usdjpy_rate(default_rate=150.0)
                if not is_est and math.isfinite(resolved_fx) and resolved_fx > 0:
                    resolved_usdjpy_rate = resolved_fx
                    usdjpy_rate_ts = now
                    is_stale = False
                    app_state.market.last_usdjpy_rate = resolved_usdjpy_rate
                    app_state.market.last_usdjpy_rate_ts = usdjpy_rate_ts
                else:
                    resolved_usdjpy_rate = resolved_fx
                    app_state.market.last_usdjpy_rate = resolved_usdjpy_rate
                    app_state.market.last_usdjpy_rate_ts = now - 24 * 3600 + 60.0
            except Exception as fx_exc:
                current_app.logger.debug(
                    "Failed to dynamically resolve USDJPY rate: %s", fx_exc
                )

        if is_stale:
            stale_warning = (
                "ドル円為替レートの更新日時が古いか確認できません（デフォルトレート 1ドル=150.0円 を適用しました）。"
                "最新データでの再計算をお勧めします。"
            )

    norm_divisor = max(100.0, total_weight_pct)
    for idx, (symbol, market, target_price, weight_pct) in enumerate(raw_validated_items):
        allocated_val = VIRTUAL_INITIAL_CAPITAL_JPY * (weight_pct / norm_divisor)
        if market == "us":
            allocated_val_usd = allocated_val / resolved_usdjpy_rate
            raw_shares = allocated_val_usd / target_price
        else:
            raw_shares = allocated_val / target_price

        if not math.isfinite(raw_shares) or raw_shares > PORTFOLIO_SHARES_MAX + 0.01:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"items[{idx}] の計算株数が上限（{PORTFOLIO_SHARES_MAX:,.0f}）を超過しています"
                },
                status_code=400,
            )

        shares = float(Decimal(str(raw_shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if shares < 0.01:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": f"items[{idx}] の割当額では最小株数（0.01株）を購入できません"},
                status_code=400,
            )

        if shares > PORTFOLIO_SHARES_MAX:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"items[{idx}] の計算株数が上限（{PORTFOLIO_SHARES_MAX:,.0f}）を超過しています"
                },
                status_code=400,
            )
        parsed_items.append((symbol, market, target_price, shares))

    added_count = 0
    skipped_symbols: list[str] = []
    added_symbols: list[tuple[str, str]] = []
    with app_state.market.user_stocks_lock:
        new_by_market: dict[str, int] = {}
        for symbol, market, _target, _shares in parsed_items:
            capacity_container = _get_stock_container(market)
            if capacity_container is None:
                continue
            if any(
                alias in capacity_container
                for alias in _stored_symbol_aliases(symbol, market)
            ):
                continue
            new_by_market[market] = new_by_market.get(market, 0) + 1
        for market, new_count in new_by_market.items():
            if not _watchlist_has_capacity(market, extra=new_count):
                return _watchlist_capacity_error(market)

        for symbol, market, target_price, shares in parsed_items:
            container = _get_stock_container(market)
            if container is None:
                continue
            if any(alias in container for alias in _stored_symbol_aliases(symbol, market)):
                skipped_symbols.append(f"{symbol} ({market})")
                continue
            holding_entry: dict[str, Any] = {
                "name": symbol,
                "shares": shares,
                "avg_price": target_price,
            }
            if market == "us":
                holding_entry["avg_fx_rate"] = resolved_usdjpy_rate
            container[symbol] = holding_entry
            added_symbols.append((symbol, market))
            added_count += 1

        if added_count > 0:
            try:
                save_user_stocks()
            except Exception as exc:
                logger.warning("save_user_stocks during copy-to-my: %s", exc)
                for symbol, market in added_symbols:
                    container = _get_stock_container(market)
                    if container is not None:
                        container.pop(symbol, None)
                return error_response(
                    ErrorCode.FILE_ERROR,
                    details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                    status_code=503,
                )

            if added_symbols:
                with app_state.cache.sse_data_lock:
                    for sym, mkt in added_symbols:
                        invalidate_stock_caches(sym)
                        ensure_stock_placeholder_in_caches(sym, _stock_display_name(sym, mkt), mkt)
                        container = _get_stock_container(mkt)
                        holding_info = container.get(sym) if container else None
                        if holding_info and isinstance(holding_info, dict):
                            shares_val = holding_info.get("shares")
                            avg_price_val = holding_info.get("avg_price")
                            avg_fx_val = holding_info.get("avg_fx_rate")
                            for cache in (
                                app_state.market.current_stocks_cache,
                                app_state.market.target_stocks_cache,
                            ):
                                if mkt not in cache:
                                    cache[mkt] = []
                                target_list = cache.get(mkt, [])
                                for s in target_list:
                                    if isinstance(s, dict) and s.get("symbol") == sym:
                                        if shares_val is not None:
                                            s["shares"] = shares_val
                                        if avg_price_val is not None:
                                            s["avg_price"] = avg_price_val
                                        if avg_fx_val is not None:
                                            s["avg_fx_rate"] = avg_fx_val
                                        break

    if added_symbols:
        for sym, mkt in added_symbols:
            _sync_realtime_symbol(sym, mkt, register=True)

        _announce_watchlist_state()
        schedule_sync_all_stocks_now()

    message = f"{added_count} 銘柄をマイポートフォリオに反映しました"
    if skipped_symbols:
        message += f"（既存保有のためスキップ: {', '.join(skipped_symbols[:5])}）"
    payload: dict = {
        "ok": True,
        "added_count": added_count,
        "skipped": skipped_symbols,
        "message": message,
    }
    if stale_warning:
        payload["stale_warning"] = stale_warning
    return jsonify(payload)
