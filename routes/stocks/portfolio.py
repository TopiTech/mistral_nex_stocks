# routes/stocks/portfolio.py
"""Portfolio holdings update, snapshot, and management endpoints."""

from __future__ import annotations

import copy
import logging
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from app_bg import (
    _invalidate_sse_payload_cache,
    announce_current_market_state,
    schedule_sync_all_stocks_now,
)
from app_state import app_state
from constants import (
    PORTFOLIO_AVG_FX_RATE_MAX,
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
)
from error_codes import ErrorCode
from route_helpers import (
    _parse_stock_request,
    _stock_display_name,
    ensure_stock_placeholder_in_caches,
    invalidate_stock_caches,
    rate_limit,
)
from routes.stocks.common import (
    _stored_symbol_aliases,
    require_trusted_or_admin,
    resolve_stocks_for_response,
    save_user_stocks,
)
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    error_response,
)
from utils.storage import UserStocksPersistError
from utils.text_utils import _parse_json_request, parse_non_negative_float
from utils.validators import validate_portfolio_input

logger = logging.getLogger(__name__)

portfolio_bp = Blueprint("portfolio", __name__)


@portfolio_bp.route("/api/stocks/portfolio", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_update_portfolio() -> Any:
    """ポートフォリオ更新APIエンドポイント"""
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

    try:
        shares_raw = data.get("shares")
        avg_price_raw = data.get("avg_price")
        avg_fx_rate_raw = data.get("avg_fx_rate")
        if shares_raw is None or str(shares_raw).strip() == "":
            return error_response(ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["shares"]})
        if avg_price_raw is None or str(avg_price_raw).strip() == "":
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["avg_price"]}
            )

        shares = parse_non_negative_float(shares_raw, "shares", max_value=PORTFOLIO_SHARES_MAX)
        avg_price = parse_non_negative_float(
            avg_price_raw, "avg_price", max_value=PORTFOLIO_AVG_PRICE_MAX
        )
        avg_fx_rate = None
        if avg_fx_rate_raw is not None and str(avg_fx_rate_raw).strip():
            avg_fx_rate = parse_non_negative_float(
                avg_fx_rate_raw, "avg_fx_rate", max_value=PORTFOLIO_AVG_FX_RATE_MAX
            )

        portfolio_errors = validate_portfolio_input(shares, avg_price, avg_fx_rate)
        if portfolio_errors:
            return error_response(ErrorCode.INVALID_INPUT, details={"reason": portfolio_errors[0]})
    except ValueError as exc:
        return error_response(ErrorCode.INVALID_INPUT, details={"reason": str(exc)})

    with app_state.market.user_stocks_lock:
        container = _get_stock_container(market)
        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)

        matching_symbol = next(
            (alias for alias in _stored_symbol_aliases(symbol, market) if alias in container),
            None,
        )
        if matching_symbol is None:
            if symbol not in _default_stock_names(market):
                current_app.logger.warning(
                    "Portfolio update rejected: symbol %s not in %s watch list", symbol, market
                )
                return error_response(
                    ErrorCode.SYMBOL_NOT_FOUND,
                    details={"reason": "symbol not in watch list; add it before setting holdings"},
                    status_code=404,
                )
            previous_value = None
            val: Any = {
                "name": _stock_display_name(symbol, market),
                "shares": shares,
                "avg_price": avg_price,
            }
        else:
            previous_value = copy.deepcopy(container.get(matching_symbol))
            val = container.pop(matching_symbol)
            if isinstance(val, str):
                val = {
                    "name": val,
                    "shares": shares,
                    "avg_price": avg_price,
                }
            else:
                val["shares"] = shares
                val["avg_price"] = avg_price

        if market == "jp":
            is_jp_like = symbol.endswith(".T")
            if not is_jp_like:
                existing_fx = None
                if isinstance(previous_value, dict):
                    existing_fx = previous_value.get("avg_fx_rate")
                if existing_fx is not None:
                    current_app.logger.warning(
                        "R3: market/symbol mismatch rejected: market=jp symbol=%s holds avg_fx_rate=%s; preserving FX",
                        symbol,
                        existing_fx,
                    )
                else:
                    current_app.logger.warning(
                        "R3: market/symbol mismatch rejected: market=jp symbol=%s not JP-like (expected *.T)",
                        symbol,
                    )
                if matching_symbol is not None and previous_value is not None:
                    container[matching_symbol] = previous_value
                return error_response(
                    ErrorCode.INVALID_MARKET,
                    details={
                        "reason": "market/symbol mismatch: JP market requires JP symbol (e.g., 7203.T)"
                    },
                    status_code=400,
                )
            val.pop("avg_fx_rate", None)
        elif market == "us":
            if symbol.endswith(".T"):
                if matching_symbol is not None and previous_value is not None:
                    container[matching_symbol] = previous_value
                return error_response(
                    ErrorCode.INVALID_MARKET,
                    details={
                        "reason": "market/symbol mismatch: US market cannot accept JP symbol (e.g., 7203.T)"
                    },
                    status_code=400,
                )
            if avg_fx_rate is not None:
                val["avg_fx_rate"] = avg_fx_rate
            else:
                val.pop("avg_fx_rate", None)
        elif avg_fx_rate is not None:
            val["avg_fx_rate"] = avg_fx_rate
        else:
            val.pop("avg_fx_rate", None)

        container[symbol] = val

        try:
            save_user_stocks()
        except UserStocksPersistError as exc:
            container.pop(symbol, None)
            if matching_symbol is not None and previous_value is not None:
                container[matching_symbol] = previous_value
            current_app.logger.error("Failed to persist portfolio update for %s: %s", symbol, exc)
            return error_response(
                ErrorCode.FILE_ERROR,
                details={"reason": "ポートフォリオの保存に失敗しました。再試行してください。"},
                status_code=503,
            )

        invalidate_stock_caches(symbol)
        ensure_stock_placeholder_in_caches(symbol, _stock_display_name(symbol, market), market)
        with app_state.cache.sse_data_lock:
            for cache in (
                app_state.market.target_stocks_cache,
                app_state.market.current_stocks_cache,
            ):
                if market not in cache:
                    cache[market] = []
                target_list = cache.get(market, [])
                for s in target_list:
                    if s.get("symbol") == symbol:
                        s["shares"] = shares
                        s["avg_price"] = avg_price
                        if market == "jp":
                            s.pop("avg_fx_rate", None)
                        elif avg_fx_rate is not None:
                            s["avg_fx_rate"] = avg_fx_rate
                        else:
                            s.pop("avg_fx_rate", None)
                        break

    _invalidate_sse_payload_cache()
    announce_current_market_state()
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


@portfolio_bp.route("/api/stocks/portfolio/snapshot", methods=["POST"])
@rate_limit(max_requests=30, window_seconds=60)
def api_portfolio_snapshot() -> Any:
    """Return holdings to the CSRF-protected local UI."""
    ok, reason = require_trusted_or_admin(request)
    if not ok:
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": reason},
            status_code=403,
        )
    stocks = resolve_stocks_for_response(include_portfolio=True)
    return jsonify({"stocks": stocks})
