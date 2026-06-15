import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
from concurrent.futures import wait
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
)
from requests.exceptions import RequestException
from requests.exceptions import Timeout as RequestsTimeout

from app_bg import (
    fetch_stock,
    fetch_stocks_batch,
    schedule_sync_all_stocks_now,
)
from app_helpers import (
    ANALYZE_RESEARCH_CONTEXT_MAX_CHARS,
    CACHE_DURATION,
    DEFAULT_IDX,
    DEFAULT_JP,
    DEFAULT_US,
    MAX_STOCK_NAME_LENGTH,
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
    VALID_HISTORY_PERIODS,
    VALID_MARKETS,
    _default_stock_names,
    _ensure_cache_bucket,
    _fmt,
    _fmt_vol,
    _get_cached_value,
    _get_stock_container,
    _has_cached_key,
    _has_ready_indices_snapshot,
    _has_ready_stocks_snapshot,
    _is_allowed_shutdown_origin,
    _is_local_request,
    _is_valid_api_key,
    _parse_json_request,
    _resolve_indices_for_response,
    _resolve_stocks_for_response,
    _set_cached_value,
    _short_text,
    _stock_is_default_or_user,
    _token_fingerprint,
    _wait_for_initial_market_snapshot,
    acquire_yfinance_slot,
    build_stock_payload,
    choose_display_name,
    clear_cache_prefix,
    error_response,
    get_cached,
    get_cached_context_with_negative_cache,
    get_default_symbols,
    get_stock_info_cached,
    is_market_open,
    is_valid_symbol,
    load_user_stocks,
    normalize_history_frame,
    normalize_market,
    normalize_optional_number,
    normalize_symbol,
    normalize_symbol_for_market,
    normalize_text,
    parse_non_negative_float,
    safe_get_ticker,
    sanitize_cache_key,
    save_user_stocks,
)
from app_state import NewsFormatter, NewsSummaryModel, StockAnalysis, app_state
from config_utils import (
    clear_api_credentials,
    get_api_credential_state,
    get_langsearch_api_key,
    get_mistral_api_key,
    get_model_badge,
    get_model_name,
    protect_data,
    save_api_credentials,
    unprotect_data,
)
from error_codes import ErrorCode, get_error_message
from route_helpers import (
    POPULAR_JP,
    POPULAR_US,
    _extract_text_from_mistral_content,
    _parse_stock_request,
    _seconds_until,
    _stock_display_name,
    cleanup_history_circuit_state,
    ensure_stock_placeholder_in_caches,
    extract_api_key,
    extract_langsearch_api_key,
    invalidate_stock_caches,
    rate_limit,
    remove_stock_from_caches,
)
from services.ai_service import (
    call_mistral_chat,
    repair_analysis_json_with_llm,
    repair_news_json_with_llm,
)
from services.search_service import (
    _build_market_trending_titles,
    _get_market_trending_titles,
    _market_trends_cache_key,
    _schedule_market_trends_refresh_async,
    collect_market_news_context,
    collect_market_trending_titles,
    collect_symbol_research_context,
    ddgs_news_search,
    ddgs_text_search,
    langsearch_search,
)
from utils.formatting import _parse_datetime_to_utc, build_fallback_analysis_result
from utils.validators import (
    extract_chat_content,
    extract_json_payload,
    normalize_analysis_result,
    validate_analysis_result,
    validate_portfolio_input,
)

try:
    from curl_cffi.requests.exceptions import Timeout as CurlRequestsTimeout
except ImportError:
    CurlRequestsTimeout = RequestsTimeout  # type: ignore[misc,assignment]
try:
    from mistralai.models import AssistantMessage, SystemMessage, UserMessage
except ImportError:
    try:
        from mistralai.client.models import AssistantMessage, SystemMessage, UserMessage
    except ImportError:

        def SystemMessage(content):  # type: ignore[no-redef]
            return {"role": "system", "content": content}

        def UserMessage(content):  # type: ignore[no-redef]
            return {"role": "user", "content": content}

        def AssistantMessage(content):  # type: ignore[no-redef]
            return {"role": "assistant", "content": content}

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/favicon.ico")
def favicon():
    """favicon.ico の直接参照を許可する"""
    root_favicon = Path(current_app.root_path) / "favicon.ico"
    if root_favicon.exists():
        return send_from_directory(current_app.root_path, "favicon.ico")
    return send_from_directory(current_app.static_folder, "favicon.ico")


@pages_bp.route("/")
@pages_bp.route("/setup")
def setup():
    """セットアップページを表示する"""
    return render_template(
        "setup.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@pages_bp.route("/main")
def main_page():
    """メインページを表示する"""
    return render_template(
        "index.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@pages_bp.route("/heatmap")
def heatmap_page():
    """ヒートマップページを表示する"""
    return render_template(
        "heatmap.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@pages_bp.route("/settings")
def settings_page():
    """設定ページを表示する"""
    return render_template(
        "settings.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )
