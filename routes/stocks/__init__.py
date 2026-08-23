# routes/stocks/__init__.py
"""Stocks route package combining quotes, views, portfolio, stream, and AI portfolio handlers."""

from __future__ import annotations

import logging

from flask import Blueprint

from routes.stocks.ai_portfolio import (
    AI_PORTFOLIO_RESULT_CACHE_TTL,
    FetchJob,
    ai_portfolio_bp,
    ai_portfolio_fetch_inflight,
    ai_portfolio_fetch_lock,
    ai_portfolio_result_cache,
    api_copy_ai_portfolio_to_my,
    api_delete_ai_portfolio,
    api_generate_ai_portfolio,
    api_get_ai_portfolios,
    api_rebalance_ai_portfolio,
    api_save_ai_portfolio,
)
from routes.stocks.common import (
    _WATCHLIST_MARKET_LABELS,
    _announce_watchlist_state,
    _fetch_heatmap_cached,
    _json_safe,
    _parse_last_event_id,
    _replay_frame_for_entry,
    _stored_symbol_aliases,
    _sync_realtime_symbol,
    _watchlist_capacity_error,
    _watchlist_has_capacity,
)
from routes.stocks.portfolio import (
    api_portfolio_snapshot,
    api_update_portfolio,
    portfolio_bp,
)
from routes.stocks.quotes import (
    _run_async_info_fetch,
    _submit_async_history_fetch,
    _submit_async_info_fetch,
    api_indices,
    api_search,
    api_stock_details,
    api_stock_history,
    api_stocks,
    quotes_bp,
)
from routes.stocks.stream import (
    api_create_sse_ticket,
    api_stocks_stream,
    stream_bp,
)
from routes.stocks.views import (
    api_add_stock,
    api_add_stock_ext,
    api_delete_stock,
    api_heatmap,
    api_reset_stocks,
    api_screener,
    views_bp,
)

logger = logging.getLogger(__name__)

# Master Blueprint representing api_stocks
api_stocks_bp = Blueprint("api_stocks", __name__)

# Register all sub-blueprints into the master Blueprint
# (Alternatively, sub-blueprints are registered directly, but to preserve full url_for / endpoint
# compatibility like 'api_stocks.api_stocks', we attach routes to api_stocks_bp or register them).
for sub_bp in (quotes_bp, views_bp, portfolio_bp, stream_bp, ai_portfolio_bp):
    api_stocks_bp.register_blueprint(sub_bp)

__all__ = [
    "AI_PORTFOLIO_RESULT_CACHE_TTL",
    "_WATCHLIST_MARKET_LABELS",
    "FetchJob",
    "_announce_watchlist_state",
    "_fetch_heatmap_cached",
    "_json_safe",
    "_parse_last_event_id",
    "_replay_frame_for_entry",
    "_run_async_info_fetch",
    "_stored_symbol_aliases",
    "_submit_async_history_fetch",
    "_submit_async_info_fetch",
    "_sync_realtime_symbol",
    "_watchlist_capacity_error",
    "_watchlist_has_capacity",
    "ai_portfolio_bp",
    "ai_portfolio_fetch_inflight",
    "ai_portfolio_fetch_lock",
    "ai_portfolio_result_cache",
    "api_add_stock",
    "api_add_stock_ext",
    "api_copy_ai_portfolio_to_my",
    "api_create_sse_ticket",
    "api_delete_ai_portfolio",
    "api_delete_stock",
    "api_generate_ai_portfolio",
    "api_get_ai_portfolios",
    "api_heatmap",
    "api_indices",
    "api_portfolio_snapshot",
    "api_rebalance_ai_portfolio",
    "api_reset_stocks",
    "api_save_ai_portfolio",
    "api_screener",
    "api_search",
    "api_stock_details",
    "api_stock_history",
    "api_stocks",
    "api_stocks_bp",
    "api_stocks_stream",
    "api_update_portfolio",
    "portfolio_bp",
    "quotes_bp",
    "stream_bp",
    "views_bp",
]
