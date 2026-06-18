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
    stream_with_context,
)
from requests.exceptions import RequestException
from requests.exceptions import Timeout as RequestsTimeout
from werkzeug.exceptions import BadRequest

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
    require_trusted_state_changing_request,
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
    get_custom_ai_prompt,
    get_langsearch_api_key,
    get_mistral_api_key,
    get_model_badge,
    get_model_name,
    protect_data,
    save_api_credentials,
    set_custom_ai_prompt,
    unprotect_data,
)
from constants import (
    HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
    HISTORY_CIRCUIT_BREAKER_THRESHOLD,
    LANGSEARCH_API_KEY_MIN_LENGTH,
    MISTRAL_API_KEY_MIN_LENGTH,
    MISTRAL_API_TIMEOUT_SEC,
    MISTRAL_MIN_INTERVAL_SEC,
    NEWS_CONTEXT_WAIT_TIMEOUT,
    YFINANCE_TIMEOUT_BATCH,
    YFINANCE_TIMEOUT_SINGLE,
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
api_system_bp = Blueprint("api_system", __name__)


@api_system_bp.route("/api/credentials", methods=["GET", "POST", "DELETE", "OPTIONS"])
def api_credentials():
    """Handles API credential retrieval, updating, and removal."""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    if request.method in ("POST", "DELETE"):
        ok, reason = require_trusted_state_changing_request(request)
    else:
        ok, reason = _is_local_request(request), "forbidden"
    if not ok:
        current_app.logger.warning(
            "Credentials access denied id=%s reason=%s remote=%s",
            getattr(g, "request_id", "-"),
            reason,
            request.remote_addr,
        )
        return jsonify({"ok": False, "error": reason}), 403

    if request.method == "GET":
        current_app.logger.info(
            "Credentials state requested id=%s", getattr(g, "request_id", "-")
        )
        state = get_api_credential_state()
        state["custom_ai_prompt"] = get_custom_ai_prompt()
        return jsonify({"ok": True, **state})

    if request.method == "DELETE":
        clear_api_credentials()
        current_app.logger.info(
            "Credentials cleared id=%s", getattr(g, "request_id", "-")
        )
        return jsonify({"ok": True, **get_api_credential_state()})

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    mistral_api_key = data.get("mistral_api_key")
    langsearch_api_key = data.get("langsearch_api_key")

    if mistral_api_key is not None:
        mistral_api_key = mistral_api_key.strip()
        if not _is_valid_api_key(
            mistral_api_key, min_length=MISTRAL_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_mistral_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(mistral_api_key),
                MISTRAL_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.INVALID_API_KEY,
                details={
                    "fields": ["mistral_api_key"],
                    "min_length": MISTRAL_API_KEY_MIN_LENGTH,
                },
            )

    if langsearch_api_key is not None:
        langsearch_api_key = langsearch_api_key.strip()
        if langsearch_api_key and not _is_valid_api_key(
            langsearch_api_key, min_length=LANGSEARCH_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_langsearch_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(langsearch_api_key),
                LANGSEARCH_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.UNSAFE_INPUT,
                details={
                    "fields": ["langsearch_api_key"],
                    "min_length": LANGSEARCH_API_KEY_MIN_LENGTH,
                },
            )

    try:
        if mistral_api_key is not None or langsearch_api_key is not None:
            save_api_credentials(
                mistral_api_key=mistral_api_key,
                langsearch_api_key=langsearch_api_key,
            )
        if "custom_ai_prompt" in data:
            set_custom_ai_prompt(data["custom_ai_prompt"])
    except RuntimeError as exc:
        current_app.logger.warning(
            "Credentials save failed id=%s reason=%s",
            getattr(g, "request_id", "-"),
            str(exc)[:200],
        )
        return error_response(
            ErrorCode.CONFIG_ERROR,
            status_code=500,
            details={"reason": str(exc)},
        )

    current_app.logger.info(
        "Credentials/Settings saved id=%s mistral=%s langsearch=%s custom_prompt_len=%d",
        getattr(g, "request_id", "-"),
        _token_fingerprint(mistral_api_key),
        _token_fingerprint(langsearch_api_key),
        len(data.get("custom_ai_prompt", "")),
    )
    state = get_api_credential_state()
    state["custom_ai_prompt"] = get_custom_ai_prompt()
    return jsonify({"ok": True, **state})


@api_system_bp.route("/api/health", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=60, window_seconds=60)
def api_health():
    """ヘルスチェックエンドポイント"""
    with app_state.yfinance_lock:
        yf_limited = app_state.is_yfinance_rate_limited and (
            time.time() < app_state.yfinance_rate_limit_until
        )
        yf_until = (
            datetime.fromtimestamp(app_state.yfinance_rate_limit_until).isoformat()
            if app_state.is_yfinance_rate_limited
            else None
        )

    return jsonify(
        {
            "ok": True,
            "app": "Mistral NeX Stocks",
            "model": get_model_name(),
            "badge": get_model_badge(),
            "is_yfinance_rate_limited": yf_limited,
            "yfinance_rate_limit_until": yf_until,
            "extension_manifest_ok": app_state._extension_manifest_status.get(
                "ok", True
            ),
            "extension_manifest_error": app_state._extension_manifest_status.get(
                "error", ""
            ),
            **get_api_credential_state(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@api_system_bp.route("/api/cache-stats", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_cache_stats():
    """キャッシュ統計情報エンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    stats = app_state.cache.get_stats()
    with app_state.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.caches.items()}
    stats["cache_sizes"] = cache_sizes
    return jsonify({"ok": True, "cache_stats": stats})


@api_system_bp.route("/api/metrics", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_metrics():
    """Expose safe operational metrics for local troubleshooting.
    
    SECURITY: This endpoint is restricted to localhost only.
    Sensitive internal state is intentionally excluded.
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Only expose safe, non-sensitive operational metrics
    with app_state.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.caches.items()}
    
    with app_state.yfinance_lock:
        yfinance_metrics = {
            "rate_limited": bool(
                app_state.is_yfinance_rate_limited
                and time.time() < app_state.yfinance_rate_limit_until
            ),
            "rate_limit_clears_in_sec": _seconds_until(
                app_state.yfinance_rate_limit_until
            ),
        }
    
    with app_state.sse_data_lock:
        current_stock_counts = {
            market: len(items)
            for market, items in app_state.current_stocks_cache.items()
        }
        current_indices_count = len(app_state.current_indices_cache)
    
    with app_state.is_syncing_lock:
        is_syncing = app_state.is_syncing

    return jsonify(
        {
            "ok": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cache": {
                "sizes": cache_sizes,
            },
            "market_data": {
                "yfinance": yfinance_metrics,
                "is_syncing": is_syncing,
                "stock_counts": current_stock_counts,
                "indices_count": current_indices_count,
            },
            "sse": {
                "listeners": app_state.sse_announcer.listener_count()
                if app_state.sse_announcer
                else 0
            },
            "config": {
                "model": get_model_name(),
                "badge": get_model_badge(),
            },
        }
    )


@api_system_bp.route("/api/csp-report", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def api_csp_report():
    """CSP report receiver for Report-Only mode (accepts JSON POST)."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
        # Log up to 2KB of the report to avoid leaking large payloads
        current_app.logger.warning(
            "CSP report received: %s", json.dumps(payload, ensure_ascii=False)[:2000]
        )
    except (BadRequest, TypeError, ValueError) as exc:
        current_app.logger.debug("Failed to parse CSP report: %s", exc)
    # Return 204 No Content as recommended for CSP reports
    return ("", 204)


@api_system_bp.route("/api/shutdown", methods=["POST", "OPTIONS"])
def api_shutdown():
    """シャットダウンエンドポイント（ワンタイムトークン使用）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    if not _is_local_request(request):
        current_app.logger.warning(
            "Shutdown request rejected from non-local address: %s", request.remote_addr
        )
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if not _is_allowed_shutdown_origin(request):
        current_app.logger.warning("Shutdown request rejected from untrusted origin")
        return jsonify({"ok": False, "error": "untrusted origin"}), 403

    # JSON body validation
    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )

    if data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "confirm flag required"}), 400

    token_header = request.headers.get("X-MNS-Shutdown-Token")
    token_json = data.get("shutdown_token")
    provided_token = (token_header or token_json or "").strip()

    # Use single-use token validation only after all non-secret preconditions pass.
    from app import _consume_shutdown_token, _rotate_shutdown_token

    if not provided_token:
        current_app.logger.warning("Shutdown request rejected: missing shutdown token")
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403

    if not _consume_shutdown_token(provided_token):
        current_app.logger.warning(
            "Shutdown request rejected: invalid or already used shutdown token"
        )
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403

    logger = current_app.logger
    logger.info("Valid shutdown token consumed, initiating shutdown sequence")

    # Rotate token BEFORE spawning shutdown thread to prevent race condition
    # where a second request could reuse the old token during the shutdown delay
    try:
        _rotate_shutdown_token()
        logger.info("Shutdown token rotated for next session")
    except Exception as exc:
        logger.warning("Failed to rotate shutdown token before shutdown: %s", exc)

    def shutdown_server():
        logger.info("Shutdown thread started")
        time.sleep(1.0)

        try:
            app_state.shutdown_executors()
        except (RuntimeError, AttributeError, ValueError) as exc:
            logger.warning("Executor shutdown before process exit failed: %s", exc)

        # 終了前にPIDファイルを削除
        try:
            logger.info("Removing PID file")
            base_dir = Path(__file__).resolve().parent.parent
            pid_file = base_dir / ".backend.pid"
            if pid_file.exists():
                removed = False
                for _ in range(2):
                    try:
                        pid_file.unlink()
                    except (IOError, OSError):
                        time.sleep(0.1)
                    if not pid_file.exists():
                        removed = True
                        break
                if not removed:
                    logger.warning(
                        "PID file still exists after retry attempts: %s", pid_file
                    )
                else:
                    logger.info("PID file removed successfully")
        except (IOError, OSError) as exc:
            logger.warning("Failed to remove pid file during shutdown: %s", exc)

        try:
            logger.info("Shutting down logging")
            logging.shutdown()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # PIDファイルを使用してプロセスを終了
        try:
            import psutil

            current_pid = os.getpid()
            logger.info("Current PID: %s", current_pid)

            # 自分自身のプロセスを終了
            parent = psutil.Process(current_pid)
            parent.terminate()

            # タイムアウト後に強制終了
            def force_kill():
                try:
                    time.sleep(2.0)
                    if parent.is_running():
                        logger.warning("Process still running, forcing kill")
                        parent.kill()
                except psutil.NoSuchProcess:
                    pass

            threading.Thread(target=force_kill, daemon=True).start()
        except ImportError:
            logger.warning("psutil not available, using os._exit")
            os._exit(0)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Failed to terminate process: %s", exc)
            os._exit(0)

    # デーモンスレッドとして設定
    shutdown_thread = threading.Thread(target=shutdown_server)
    shutdown_thread.daemon = True
    shutdown_thread.start()
    return jsonify({"ok": True, "message": "Shutting down..."})
