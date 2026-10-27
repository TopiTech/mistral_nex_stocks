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

import httpx
import requests
from cachetools import TTLCache
from flask import Blueprint, current_app, g, jsonify, request, session

from app_state import app_state
from constants import PORTFOLIO_SHARES_MAX
from error_codes import ErrorCode
from route_helpers import (
    _parse_ai_copy_to_my_request,
    _parse_ai_delete_request,
    _parse_ai_generate_request,
    _parse_ai_rebalance_request,
    _parse_ai_save_request,
    _stock_display_name,
    ensure_stock_placeholder_in_caches,
    extract_api_key,
    invalidate_stock_caches,
    rate_limit,
)
from routes.stocks import common as _common
from routes.stocks.common import (
    _stored_symbol_aliases,
    _watchlist_capacity_error,
    _watchlist_has_capacity,
    require_trusted_or_admin,
    save_user_stocks,
    schedule_sync_all_stocks_now,
)
from services.ai_portfolio_service import (
    DEFAULT_PRESET_CONFIGS,
    VIRTUAL_INITIAL_CAPITAL_JPY,
    PortfolioStorageError,
    delete_custom_ai_portfolio,
    generate_ai_portfolio_by_theme,
    load_saved_ai_portfolios,
    sanitize_ai_portfolio,
    save_custom_ai_portfolio,
)
from services.ai_service import ProviderAPIError
from utils.stock_payload import (
    _get_stock_container,
    error_response,
    get_current_usdjpy_rate,
    ok_response,
)
from utils.text_utils import _parse_json_request

logger = logging.getLogger(__name__)

ai_portfolio_bp = Blueprint("ai_portfolio", __name__)


class FetchJob(TypedDict, total=False):
    result: Any
    error: BaseException | None
    done: threading.Event
    step: str
    start_time: float
    message: str


def _ai_portfolio_error_response(
    job_err: BaseException,
    flask_g: Any = None,
    default_reason: str = "AI ポートフォリオの生成に失敗しました",
) -> tuple[Any, int]:
    effective_g = flask_g if flask_g is not None else g
    req_id = getattr(effective_g, "request_id", "-")
    err_type = type(job_err).__name__
    err_msg = str(job_err)

    if isinstance(job_err, ProviderAPIError):
        current_app.logger.error(
            "AI portfolio provider error id=%s status=%d: %s",
            req_id,
            job_err.status_code,
            err_msg,
        )
        return error_response(
            ErrorCode.API_SERVICE_ERROR,
            details={"reason": err_msg, "error_type": err_type, **job_err.details},
            status_code=job_err.status_code,
        )

    if isinstance(job_err, (httpx.TimeoutException, requests.Timeout)):
        current_app.logger.error(
            "AI portfolio timeout id=%s error_type=%s: %s", req_id, err_type, err_msg
        )
        return error_response(
            ErrorCode.API_SERVICE_ERROR,
            details={
                "reason": "AIモデルの応答がタイムアウトしました。サーバーの負荷状況を確認して再試行してください。",
                "error_type": err_type,
            },
            status_code=504,
        )

    if isinstance(job_err, httpx.HTTPStatusError) and job_err.response is not None:
        status = job_err.response.status_code
        current_app.logger.error(
            "AI portfolio HTTP status error id=%s status=%d: %s", req_id, status, err_msg
        )
        return error_response(
            ErrorCode.API_SERVICE_ERROR,
            details={"reason": f"AIサービスエラー (HTTP {status}): {err_msg}", "error_type": err_type},
            status_code=status,
        )

    if isinstance(job_err, (requests.ConnectionError, ConnectionError, OSError, httpx.HTTPError)):
        current_app.logger.error(
            "AI portfolio network error id=%s error_type=%s: %s", req_id, err_type, err_msg
        )
        return error_response(
            ErrorCode.API_SERVICE_ERROR,
            details={"reason": f"AIサービス通信エラー: {err_msg}", "error_type": err_type},
            status_code=503,
        )

    if isinstance(job_err, PortfolioStorageError):
        current_app.logger.error(
            "AI portfolio storage error id=%s error_type=%s: %s", req_id, err_type, err_msg
        )
        return error_response(
            ErrorCode.API_SERVICE_ERROR,
            details={
                "reason": "ポートフォリオの生成または保存処理が完了しませんでした。しばらくしてから再試行してください。",
                "error_type": err_type,
            },
            status_code=getattr(job_err, "status_code", 503),
        )

    current_app.logger.error(
        "AI portfolio unexpected error id=%s error_type=%s: %s", req_id, err_type, err_msg
    )
    return error_response(
        ErrorCode.INTERNAL_SERVER_ERROR,
        details={"reason": default_reason, "error_type": err_type},
        status_code=500,
    )


ai_portfolio_fetch_lock = threading.Lock()
ai_portfolio_fetch_inflight: dict[str, Any] = {}

AI_PORTFOLIO_RESULT_CACHE_TTL = 300.0
ai_portfolio_result_cache: TTLCache[str, tuple[float, Any, BaseException | None]] = TTLCache(
    maxsize=128, ttl=AI_PORTFOLIO_RESULT_CACHE_TTL
)

_OPERATION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _get_conversation_scope(data: dict[str, Any] | None = None) -> str:
    """Return a validated conversation scope token for AI portfolio operations.

    Fallback chain:
    1. Header: ``X-Conversation-Scope``
    2. Request data / body / query parameter: ``conversation_scope``
    3. Cookie session: ``session['mns_analysis_conversation']``
    4. Auto-generated token via ``secrets.token_urlsafe(24)``
    """
    # 1. Header fallback
    header_scope = request.headers.get("X-Conversation-Scope") if request else None
    if isinstance(header_scope, str) and _OPERATION_TOKEN_RE.fullmatch(header_scope.strip()):
        valid_scope = header_scope.strip()
        try:
            session["mns_analysis_conversation"] = valid_scope
        except Exception:
            pass
        return valid_scope

    # 2. Request data fallback (passed dict, parsed json, or query args)
    req_scope: Any = None
    if isinstance(data, dict):
        req_scope = data.get("conversation_scope")
    if not req_scope and request:
        try:
            json_data = request.get_json(silent=True)
            if isinstance(json_data, dict):
                req_scope = json_data.get("conversation_scope")
        except Exception:
            pass
    if not req_scope and request:
        req_scope = request.args.get("conversation_scope")

    if isinstance(req_scope, str) and _OPERATION_TOKEN_RE.fullmatch(req_scope.strip()):
        valid_scope = req_scope.strip()
        try:
            session["mns_analysis_conversation"] = valid_scope
        except Exception:
            pass
        return valid_scope

    # 3. Session fallback
    try:
        scope = session.get("mns_analysis_conversation")
        if isinstance(scope, str) and _OPERATION_TOKEN_RE.fullmatch(scope):
            return scope
    except Exception:
        pass

    # 4. Generate new scope
    scope = secrets.token_urlsafe(24)
    try:
        session["mns_analysis_conversation"] = scope
    except Exception:
        pass
    return scope


@ai_portfolio_bp.route("/api/ai-portfolio", methods=["GET"])
@rate_limit(max_requests=60, window_seconds=60)
def api_get_ai_portfolios() -> Any:
    """AIポートフォリオ一覧（プリセットおよび保存済みカスタムテーマ）を取得"""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    saved = load_saved_ai_portfolios()
    return ok_response(
        {
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
    theme, err = _parse_ai_generate_request(data)
    if err is not None:
        return err
    if theme is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "themeが無効です"}, status_code=400
        )

    api_key = extract_api_key(request)

    conversation_scope = _get_conversation_scope(data)
    inflight_key = f"generate:{conversation_scope}:{theme}"
    with ai_portfolio_fetch_lock:
        cached = ai_portfolio_result_cache.get(inflight_key)
    if cached is not None:
        _cached_ts, cached_result, cached_err = cached
        if cached_err is not None:
            return _ai_portfolio_error_response(cached_err, g)
        if cached_result is not None:
            return ok_response({"portfolio": cached_result})

    with ai_portfolio_fetch_lock:
        if inflight_key in ai_portfolio_fetch_inflight:
            result_holder = ai_portfolio_fetch_inflight[inflight_key]
            already_fetching = True
        else:
            new_result_holder: FetchJob = {
                "result": None,
                "error": None,
                "done": threading.Event(),
                "step": "generating",
                "start_time": time.time(),
                "message": f"AIポートフォリオ（テーマ: {theme}）を生成中...",
            }
            ai_portfolio_fetch_inflight[inflight_key] = new_result_holder
            result_holder = new_result_holder
            already_fetching = False

    req_id = getattr(g, "request_id", "-")
    if not already_fetching:

        def _run_ai_portfolio_job() -> None:
            current_app.logger.info("Starting background AI portfolio generation job id=%s theme=%s", req_id, theme)
            try:
                res = generate_ai_portfolio_by_theme(theme, api_key=api_key)
                result_holder["result"] = res
                result_holder["step"] = "done"
                result_holder["message"] = "AIポートフォリオの生成が完了しました"
            except Exception as exc:
                current_app.logger.exception(
                    "AI portfolio generate job failed id=%s theme=%s", req_id, theme
                )
                result_holder["error"] = exc
                result_holder["step"] = "error"
                result_holder["message"] = f"AIポートフォリオ生成エラー: {exc}"
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

            route_helpers._submit_in_app_context(app_state.execution.executor, _run_ai_portfolio_job)
        except queue.Full as exc:
            current_app.logger.warning(
                "AI portfolio job queue is full id=%s: %s", getattr(g, "request_id", "-"), exc
            )
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            result_holder["error"] = exc
            result_holder["done"].set()
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
            result_holder["error"] = exc
            result_holder["done"].set()
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    finished = result_holder["done"].wait(timeout=3.0)
    if not finished:
        elapsed = round(time.time() - result_holder.get("start_time", time.time()), 1)
        return jsonify({
            "fetching": True,
            "step": result_holder.get("step", "generating"),
            "message": result_holder.get("message", "AIポートフォリオを生成中..."),
            "elapsed_sec": elapsed,
        })

    if result_holder["error"] is not None:
        return _ai_portfolio_error_response(result_holder["error"], g)

    return ok_response({"portfolio": result_holder["result"]})


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
    theme, err = _parse_ai_rebalance_request(data)
    if err is not None:
        return err
    if theme is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "themeが無効です"}, status_code=400
        )

    api_key = extract_api_key(request)

    conversation_scope = _get_conversation_scope(data)
    inflight_key = f"rebalance:{conversation_scope}:{theme}"
    with ai_portfolio_fetch_lock:
        cached = ai_portfolio_result_cache.pop(inflight_key, None)
    if cached is not None:
        _cached_ts, cached_result, cached_err = cached
        if cached_err is not None:
            return _ai_portfolio_error_response(
                cached_err, g, default_reason="AI ポートフォリオのリバランスに失敗しました"
            )
        if cached_result is not None:
            return ok_response(
                {"portfolio": cached_result, "message": "リバランスが完了しました"}
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
                "step": "rebalancing",
                "start_time": time.time(),
                "message": f"AIポートフォリオ（テーマ: {theme}）をリバランス中...",
            }
            ai_portfolio_fetch_inflight[inflight_key] = new_result_holder
            result_holder = new_result_holder
            already_fetching = False

    req_id = getattr(g, "request_id", "-")
    if not already_fetching:

        def _run_ai_rebalance_job() -> None:
            current_app.logger.info("Starting background AI portfolio rebalance job id=%s theme=%s", req_id, theme)
            try:
                res = generate_ai_portfolio_by_theme(theme, force_rebalance=True, api_key=api_key)
                result_holder["result"] = res
                result_holder["step"] = "done"
                result_holder["message"] = "リバランスが完了しました"
            except Exception as exc:
                current_app.logger.exception(
                    "AI portfolio rebalance job failed id=%s theme=%s", req_id, theme
                )
                result_holder["error"] = exc
                result_holder["step"] = "error"
                result_holder["message"] = f"AIリバランスエラー: {exc}"
            finally:
                with ai_portfolio_fetch_lock:
                    ai_portfolio_fetch_inflight.pop(inflight_key, None)
                    if result_holder["error"] is None and result_holder["result"] is not None:
                        ai_portfolio_result_cache[inflight_key] = (
                            time.time(),
                            result_holder["result"],
                            None,
                        )
                        generate_key = f"generate:{conversation_scope}:{theme}"
                        ai_portfolio_result_cache[generate_key] = (
                            time.time(),
                            result_holder["result"],
                            None,
                        )
                result_holder["done"].set()

        try:
            import route_helpers

            route_helpers._submit_in_app_context(app_state.execution.executor, _run_ai_rebalance_job)
        except queue.Full as exc:
            current_app.logger.warning(
                "AI rebalance job queue is full id=%s: %s", getattr(g, "request_id", "-"), exc
            )
            with ai_portfolio_fetch_lock:
                ai_portfolio_fetch_inflight.pop(inflight_key, None)
            result_holder["error"] = exc
            result_holder["done"].set()
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
            result_holder["error"] = exc
            result_holder["done"].set()
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    finished = result_holder["done"].wait(timeout=3.0)
    if not finished:
        elapsed = round(time.time() - result_holder.get("start_time", time.time()), 1)
        return jsonify({
            "fetching": True,
            "step": result_holder.get("step", "rebalancing"),
            "message": result_holder.get("message", "AIポートフォリオをリバランス中..."),
            "elapsed_sec": elapsed,
        })

    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache.pop(inflight_key, None)

    if result_holder["error"] is not None:
        return _ai_portfolio_error_response(
            result_holder["error"], g, default_reason="AI ポートフォリオのリバランスに失敗しました"
        )

    return ok_response(
        {"portfolio": result_holder["result"], "message": "リバランスが完了しました"}
    )


@ai_portfolio_bp.route("/api/ai-portfolio/save", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_save_ai_portfolio() -> Any:
    """カスタムAIポートフォリオを永続化保存"""
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
    save_req, err = _parse_ai_save_request(data)
    if err is not None:
        return err
    if save_req is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "無効なポートフォリオデータです"},
            status_code=400,
        )
    portfolio = save_req.portfolio

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

    success = save_custom_ai_portfolio(canonical_portfolio)
    if not success:
        return error_response(
            ErrorCode.INTERNAL_SERVER_ERROR,
            details={"reason": "保存に失敗しました"},
            status_code=500,
        )

    target_id = canonical_portfolio.get("id")
    target_theme = canonical_portfolio.get("theme")
    with ai_portfolio_fetch_lock:
        keys_to_pop = [
            k
            for k in ai_portfolio_result_cache
            if (target_id and f":{target_id}" in k) or (target_theme and f":{target_theme}" in k)
        ]
        for k in keys_to_pop:
            ai_portfolio_result_cache.pop(k, None)

    return ok_response({"portfolio": canonical_portfolio})


@ai_portfolio_bp.route("/api/ai-portfolio/custom", methods=["DELETE", "POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_delete_ai_portfolio() -> Any:
    """保存済みカスタムAIポートフォリオを削除"""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_json_request()
    if data is None or not isinstance(data, dict):
        id_from_args = request.args.get("id")
        if id_from_args is not None:
            data = {"id": id_from_args}
        else:
            return error_response(
                ErrorCode.MALFORMED_INPUT,
                details={"reason": "JSON形式が不正です"},
                status_code=400,
            )
    elif "id" not in data or data.get("id") is None:
        id_from_args = request.args.get("id")
        if id_from_args is not None:
            data = dict(data)
            data["id"] = id_from_args

    portfolio_id, err = _parse_ai_delete_request(data)
    if err is not None:
        return err
    if portfolio_id is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": "idが無効です"}, status_code=400
        )


    # Resolve the theme BEFORE deletion so the generate-result cache can be
    # invalidated for both the id-keyed and theme-keyed entries; after deletion
    # the theme would no longer be recoverable and a stale generate cache
    # (300s TTL) would resurrect the deleted portfolio.
    deleted_theme: str | None = None
    try:
        for saved in load_saved_ai_portfolios():
            if isinstance(saved, dict) and saved.get("id") == portfolio_id:
                theme_val = saved.get("theme")
                if isinstance(theme_val, str) and theme_val.strip():
                    deleted_theme = theme_val.strip()
                break
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to resolve theme before AI portfolio delete: %s", exc)
    success = delete_custom_ai_portfolio(portfolio_id)
    if not success:
        return error_response(
            ErrorCode.NOT_FOUND,
            details={"reason": "対象ポートフォリオが見つかりません"},
            status_code=404,
        )

    with ai_portfolio_fetch_lock:
        keys_to_pop = [
            k
            for k in ai_portfolio_result_cache
            if f":{portfolio_id}" in k or (deleted_theme and f":{deleted_theme}" in k)
        ]
        for k in keys_to_pop:
            ai_portfolio_result_cache.pop(k, None)

    return ok_response({"id": portfolio_id})


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
    parsed_copy, copy_error = _parse_ai_copy_to_my_request(data)
    if copy_error is not None:
        return copy_error
    if parsed_copy is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "無効なポートフォリオデータです"},
            status_code=400,
        )

    parsed_items: list[tuple[str, str, float, float]] = []
    raw_validated_items: list[tuple[str, str, float, float]] = parsed_copy
    total_weight_pct = sum(weight for _, _, _, weight in raw_validated_items)
    stale_warning: str | None = None

    has_us_items = any(m == "us" for _, m, _, _ in raw_validated_items)
    resolved_usdjpy_rate = 150.0
    if has_us_items:
        rate_is_valid = True
        raw_rate = getattr(app_state.market, "last_usdjpy_rate", 150.0)
        if isinstance(raw_rate, bool) or type(raw_rate).__name__ in ("bool_", "bool"):
            resolved_usdjpy_rate = 150.0
            rate_is_valid = False
        else:
            try:
                resolved_usdjpy_rate = float(raw_rate)
            except (TypeError, ValueError):
                resolved_usdjpy_rate = 150.0
                rate_is_valid = False
        if not math.isfinite(resolved_usdjpy_rate) or resolved_usdjpy_rate <= 0:
            resolved_usdjpy_rate = 150.0
            rate_is_valid = False
        raw_ts = getattr(app_state.market, "last_usdjpy_rate_ts", 0.0)
        if isinstance(raw_ts, bool) or type(raw_ts).__name__ in ("bool_", "bool"):
            usdjpy_rate_ts = 0.0
        else:
            try:
                usdjpy_rate_ts = float(raw_ts or 0.0)
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
                current_app.logger.debug("Failed to dynamically resolve USDJPY rate: %s", fx_exc)

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
    added_symbols: list[tuple[str, str, float, float, float | None]] = []
    with app_state.market.user_stocks_lock:
        new_by_market: dict[str, int] = {}
        for symbol, market, _target, _shares in parsed_items:
            capacity_container = _get_stock_container(market)
            if capacity_container is None:
                continue
            if any(alias in capacity_container for alias in _stored_symbol_aliases(symbol, market)):
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
                "name": _stock_display_name(symbol, market),
                "shares": shares,
                "avg_price": target_price,
            }
            avg_fx_val = resolved_usdjpy_rate if market == "us" else None
            if avg_fx_val is not None:
                holding_entry["avg_fx_rate"] = avg_fx_val
            container[symbol] = holding_entry
            added_symbols.append((symbol, market, target_price, shares, avg_fx_val))
            added_count += 1

    if added_count > 0:
        try:
            save_user_stocks()
        except Exception as exc:
            logger.warning("save_user_stocks during copy-to-my: %s", exc)
            with app_state.market.user_stocks_lock:
                for symbol, market, _, _, _ in added_symbols:
                    container = _get_stock_container(market)
                    if container is not None:
                        container.pop(symbol, None)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "銘柄設定の保存に失敗しました。再試行してください。"},
                status_code=503,
            )

    if added_symbols:
        for sym, mkt, target_price, shares, fx_rate in added_symbols:
            invalidate_stock_caches(sym)
            ensure_stock_placeholder_in_caches(
                sym,
                _stock_display_name(sym, mkt),
                mkt,
                shares=shares,
                avg_price=target_price,
                avg_fx_rate=fx_rate,
            )

        for sym, mkt, _, _, _ in added_symbols:
            _common._sync_realtime_symbol(sym, mkt, register=True)

        _common._announce_watchlist_state()
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
    return ok_response(payload)
