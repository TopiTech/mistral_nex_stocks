# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-return-statements,too-many-arguments,too-many-positional-arguments
"""Backend application for Mistral NeX Stocks."""

# #region Imports

import atexit
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from cachetools import TTLCache
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect

try:
    from mistralai.client import Mistral  # type: ignore[attr-defined,no-redef]
except ImportError:
    try:
        from mistralai import Mistral  # type: ignore[attr-defined,no-redef]
    except ImportError:
        try:
            from mistralai.client.sdk import (
                Mistral,  # type: ignore[attr-defined,no-redef]
            )
        except ImportError:
            # Fallback/mock if mistralai is not installed in some test contexts
            class Mistral:  # type: ignore[no-redef]
                def __init__(self, api_key: str, **kwargs: Any):
                    self.api_key = api_key
                    self.kwargs = kwargs


from requests.exceptions import RequestException
from werkzeug.exceptions import BadRequest

import trend_sources as ts
from app_bg import (
    _build_sse_light_stocks_payload,
    _handle_yfinance_error,
    _round_if_numeric,
    _run_scheduled_sync_job,
    _start_background_threads,
    bg_interpolate_loop,
    bg_yahoo_fetch_loop,
    clone_structure_for_current,
    extract_batch_history,
    fetch_index_data,
    fetch_stock,
    fetch_stocks_batch,
    interpolate_value,
    schedule_sync_all_stocks_now,
    sync_all_stocks_now,
)
from app_helpers import (
    _default_stock_names,
    _fmt,
    _fmt_vol,
    _get_cached_value,
    _get_stock_container,
    _has_cached_key,
    _has_ready_indices_snapshot,
    _has_ready_stocks_snapshot,
    _is_allowed_shutdown_origin,
    _is_local_request,
    _parse_json_request,
    _resolve_indices_for_response,
    _resolve_stocks_for_response,
    _sanitize_error_message,
    _set_cached_value,
    _short_text,
    _stock_is_default_or_user,
    _token_fingerprint,
    _token_mask,
    _wait_for_initial_market_snapshot,
    acquire_yfinance_slot,
    build_stock_payload,
    error_response,
    get_allowed_cors_origins,
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

# #endregion Imports
# #region Imports from Migrated Modules
from app_state import (
    AIState,
    AppState,
    BackendLogFilter,
    CacheState,
    ExecutionState,
    KeyringError,
    MarketDataState,
    MessageAnnouncer,
    NewsFormatter,
    NewsSummaryModel,
    PollingFilter,
    StockAnalysis,
    YFinanceSessionManager,
    app_state,
    yf_session_manager,
)
from config_utils import (
    _env_float,
    _env_int,
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
from constants import (
    ANALYZE_RESEARCH_CONTEXT_MAX_CHARS,
    BACKEND_PORT,
    CACHE_DURATION,
    HISTORY_CIRCUIT_BREAKER_OPEN_SEC,
    HISTORY_CIRCUIT_BREAKER_THRESHOLD,
    LANGSEARCH_API_KEY_MIN_LENGTH,
    LANGSEARCH_TIMEOUT,
    MAX_JSON_SIZE,
    MISTRAL_API_KEY_MIN_LENGTH,
    MISTRAL_API_TIMEOUT_SEC,
    MISTRAL_MIN_INTERVAL_SEC,
    NEWS_CONTEXT_WAIT_TIMEOUT,
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
    PORTFOLIO_TOTAL_VALUE_MAX,
    YFINANCE_MAX_RETRIES,
    YFINANCE_RETRY_WAIT,
    YFINANCE_TIMEOUT_BATCH,
    YFINANCE_TIMEOUT_SINGLE,
)
from error_codes import ErrorCode, get_error_message
from route_helpers import (
    _cleanup_rate_limit_store,
    _extract_text_from_mistral_content,
    _parse_stock_request,
    _rate_limit_env_name,
    _resolve_rate_limit,
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


# Ensure global HTTP sessions and managers are closed on process exit to avoid ResourceWarning
def _cleanup_on_exit():
    try:
        yf_session_manager.close_all()
    except Exception as exc:
        app.logger.debug("Cleanup of yfinance sessions: %s", exc)


atexit.register(_cleanup_on_exit)
# #endregion Imports from Migrated Modules

# #region Logging Configuration

app = Flask(__name__)

# セッション暗号化用のシークレットキー（個人利用向けに環境変数または自動生成）
# 本番環境では環境変数 FLASK_SECRET_KEY を設定することを強く推奨
_flask_secret = os.environ.get("FLASK_SECRET_KEY")
if _flask_secret:
    if len(_flask_secret) < 32:
        raise ValueError("FLASK_SECRET_KEY must be at least 32 characters for security")
    app.secret_key = _flask_secret
else:
    from config_utils import get_or_create_flask_secret_key

    app.logger.info(
        "FLASK_SECRET_KEY not set in environment. Using persistent auto-generated key from secure storage."
    )
    app.secret_key = get_or_create_flask_secret_key()

# セッション設定の強化（個人利用向け）
# SESSION_COOKIE_SECURE: デフォルトは環境変数で制御
#   MNS_COOKIE_SECURE=1 で明示的に有効化、MNS_PROD=1 でも自動有効化
#   個人利用のlocalhost環境ではHTTP接続のためデフォルトはFalse
_cookie_secure = os.environ.get("MNS_COOKIE_SECURE", "").lower() in (
    "1",
    "true",
    "yes",
) or os.environ.get("MNS_PROD", "").lower() in ("1", "true", "yes")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,  # JavaScriptからアクセス不可
    SESSION_COOKIE_SAMESITE="Lax",  # CSRF対策
    SESSION_COOKIE_SECURE=_cookie_secure,  # MNS_COOKIE_SECURE=1 or MNS_PROD=1 で有効化
    SESSION_COOKIE_PARTITIONED=_cookie_secure,  # Flask 3.1+: Partitioned cookies (CHIPS) 対応
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=3600),  # 1時間で期限切れ
    WTF_CSRF_TIME_LIMIT=3600,  # CSRFトークンの有効期限（1時間）
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16MB: DoS対策のコンテンツ長制限
    # Flask 3.1+ security defaults for form parsing
    MAX_FORM_MEMORY_SIZE=512 * 1024,  # 512KB: フォームデータのメモリ制限
    MAX_FORM_PARTS=1000,  # 最大フォームパーツ数制限
)

# CSRF保護の初期化
csrf = CSRFProtect(app)

# Content Security Policy: default to Enforce for maximum security.
# 'unsafe-inline' removed for enhanced XSS protection; use nonces instead.
CSP_DEFAULT_POLICY = os.environ.get(
    "CSP_DEFAULT_POLICY",
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data: https:; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self' http://localhost:* http://127.0.0.1:* https://api.mistral.ai https://api.langsearch.com; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /api/csp-report;",
)
CSP_ENFORCE = os.environ.get("CSP_ENFORCE", "true").lower() in ("1", "true", "yes")

# Flask-Talismanによるセキュリティヘッダの一元管理
talisman = Talisman(
    app,
    content_security_policy=CSP_DEFAULT_POLICY,
    content_security_policy_nonce_in=["script-src", "style-src"],
    force_https=False,  # localhost開発のためFalse。本番相当のHSTSは手動で追加可能
    frame_options="DENY",
    strict_transport_security=True if CSP_ENFORCE else False,
    session_cookie_secure=_cookie_secure,  # MNS_COOKIE_SECURE=1 or MNS_PROD=1 で有効化
    session_cookie_http_only=True,
    referrer_policy="strict-origin-when-cross-origin",
)

if not CSP_ENFORCE:
    # デバッグ/診断モード用のReport-Only設定
    app.config["TALISMAN_CONTENT_SECURITY_POLICY_REPORT_ONLY"] = True


@app.context_processor
def inject_csp_nonce():
    """Inject the CSP nonce into the template context. Supports both manual and Talisman-generated nonces."""
    # Flask-Talisman stores the per-request nonce on request.csp_nonce.
    # Keep g.csp_nonce as a compatibility fallback for manually set values.
    nonce = getattr(request, "csp_nonce", None) or getattr(g, "csp_nonce", "")
    if nonce:
        g.csp_nonce = nonce
    return dict(csp_nonce=nonce)


if sys.version_info < (3, 9):
    raise RuntimeError("Python 3.9+ is required for this application")

# --- ログローテーション設定 (5MB × 最大3ファイル) ---
LOG_LEVEL_NAME = (os.environ.get("BACKEND_LOG_LEVEL", "INFO") or "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

# --- セキュリティ設定 ---
# 本番環境では必ず環境変数から強力なシークレットキーを設定することを推奨します
# 個人利用向け: config_utils を通じて自動生成キーがセキュアに永続化されます


# The following log filters and constants were moved to app_state.py


_log_file = Path(__file__).resolve().parent / "backend.log"

# JSONフォーマッターの設定（構造化ログ）
# Supports python-json-logger v2.x and v3.x (module path changed in v3)
try:
    from pythonjsonlogger.json import JsonFormatter as _JsonFormatter

    _use_json_format = os.environ.get("LOG_FORMAT", "json").lower() == "json"
except ImportError:
    try:
        # python-json-logger 2.x fallback
        from pythonjsonlogger import (
            jsonlogger as _jsonlogger_compat,  # type: ignore[import-untyped]
        )

        _JsonFormatter = _jsonlogger_compat.JsonFormatter  # type: ignore[assignment,misc]
        _use_json_format = os.environ.get("LOG_FORMAT", "json").lower() == "json"
    except ImportError:
        _use_json_format = False
        _JsonFormatter = None  # type: ignore[assignment,misc]


class SanitizedFormatter(logging.Formatter):
    def format(self, record):
        formatted = super().format(record)
        return _sanitize_error_message(formatted)


if _use_json_format and _JsonFormatter is not None:
    # JSON形式のログフォーマッター
    class CustomJsonFormatter(_JsonFormatter):  # type: ignore[misc]
        def add_fields(self, log_record, record, message_dict):
            super().add_fields(log_record, record, message_dict)
            log_record["level"] = record.levelname
            log_record["logger"] = record.name
            log_record["timestamp"] = self.formatTime(record, self.datefmt)

    class SanitizedJsonFormatter(CustomJsonFormatter):
        def format(self, record):
            formatted = super().format(record)
            return _sanitize_error_message(formatted)

    _log_formatter: logging.Formatter = SanitizedJsonFormatter(
        "%(timestamp)s %(level)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
else:
    # 従来のテキスト形式（開発・個人利用向けに最適化）
    # タイムスタンプを短縮し、レベルを固定幅にして視認性を向上
    _log_formatter = SanitizedFormatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

_rotating_handler = RotatingFileHandler(
    str(_log_file),
    maxBytes=5 * 1024 * 1024,  # 5MB
    backupCount=3,
    encoding="utf-8",
)
_rotating_handler.setLevel(LOG_LEVEL)
_rotating_handler.addFilter(BackendLogFilter())
_rotating_handler.setFormatter(_log_formatter)
logging.getLogger().addHandler(_rotating_handler)

# --- Dedicated Error Log (ERROR and above) ---
_error_log_file = Path(__file__).resolve().parent / "error.log"
_error_handler = RotatingFileHandler(
    str(_error_log_file),
    maxBytes=2 * 1024 * 1024,  # 2MB
    backupCount=5,
    encoding="utf-8",
)
_error_handler.setLevel(logging.ERROR)
_error_handler.setFormatter(_log_formatter)
logging.getLogger().addHandler(_error_handler)

logging.getLogger().setLevel(LOG_LEVEL)
app.logger.addHandler(_rotating_handler)
app.logger.addHandler(_error_handler)
app.logger.setLevel(LOG_LEVEL)
app.logger.propagate = False


logging.getLogger("werkzeug").addFilter(PollingFilter())


# --- Application State Groups (Imported from app_state) ---
atexit.register(app_state.shutdown_executors)


def _handle_shutdown_signal(signum, frame):
    app.logger.info("Received termination signal %s. Shutting down...", signum)
    app_state.shutdown_executors()
    if (
        not sys.is_finalizing()
        and threading.current_thread() is threading.main_thread()
    ):
        sys.exit(0)


try:
    import signal

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
except (ValueError, ImportError, AttributeError):
    # May fail if not called from the main thread
    pass


DETAILED_API_LOG_PATHS = {
    "/api/chat",
    "/api/news",
    "/api/analyze-v2",
    "/api/credentials",
    "/api/shutdown",
}


app_state.get_or_create_shutdown_token()


@app.before_request
def _enforce_sec_fetch_site_check():
    """Enforce Sec-Fetch-Site metadata checks to block cross-site request forgery."""
    if request.method in ("POST", "DELETE", "PUT", "PATCH"):
        if request.path == "/api/csp-report":
            return None

        sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_fetch_site in ("cross-site", "none"):
            allowed = _is_allowed_shutdown_origin(request)
            if not allowed:
                app.logger.warning(
                    "Block cross-site request to %s: Origin/Referer not allowed. Sec-Fetch-Site=%s",
                    request.path,
                    sec_fetch_site,
                )
                return jsonify(
                    {"ok": False, "error": "forbidden cross-site request"}
                ), 403


@app.before_request
def _log_request_start():
    """Log the start of an incoming request with a unique request ID."""
    g.request_start_ts = time.time()
    g.request_id = uuid.uuid4().hex[:10]

    # Quiet by default: only emit request traces when INFO logging is explicitly enabled.
    if LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        app.logger.info(
            "REQ start id=%s method=%s path=%s remote=%s origin=%s ua=%s",
            g.request_id,
            request.method,
            request.path,
            request.remote_addr,
            _short_text(request.headers.get("Origin"), 80),
            _short_text(request.headers.get("User-Agent"), 120),
        )


_BASE_ALLOWED_CORS_ORIGINS = {
    f"http://localhost:{BACKEND_PORT}",
    f"http://127.0.0.1:{BACKEND_PORT}",
}


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9._\-^=]{0,14}$")


@app.after_request
def add_extension_cors_headers(response):
    """Inject CORS and security headers into outgoing responses."""
    allowed_origins = {origin.rstrip("/") for origin in get_allowed_cors_origins()}
    origin = (request.headers.get("Origin") or "").strip().rstrip("/")
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin

    vary_values = [
        v.strip() for v in str(response.headers.get("Vary", "")).split(",") if v.strip()
    ]
    if "origin" not in {v.lower() for v in vary_values}:
        vary_values.append("Origin")
    response.headers["Vary"] = ", ".join(vary_values) if vary_values else "Origin"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-LangSearch-Key, X-CSRFToken, X-CSRF-Token, X-MNS-Shutdown-Token"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"

    # Note: Flask-Talisman handles CSP, HSTS, X-Frame-Options, etc.
    # We only add headers that Talisman might not cover or that need manual enforcement.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Access-Control-Max-Age"] = "600"

    req_id = getattr(g, "request_id", "-")
    response.headers["X-MNS-Request-Id"] = req_id
    response.headers["Access-Control-Expose-Headers"] = "X-MNS-Request-Id"

    started = getattr(g, "request_start_ts", None)
    elapsed_ms = (
        int((time.time() - started) * 1000) if isinstance(started, (int, float)) else -1
    )
    status_code = int(response.status_code or 0)
    if status_code >= 400:
        app.logger.warning(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            request.path,
            status_code,
            elapsed_ms,
        )
    elif LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        app.logger.info(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            request.path,
            status_code,
            elapsed_ms,
        )

    return response


# ------------------------------
# Base Directory & Settings
# ------------------------------
BASE_DIR = Path(__file__).resolve().parent


LANGSEARCH_BASE_URL = os.environ.get(
    "LANGSEARCH_BASE_URL", "https://api.langsearch.com"
)
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"
USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")
# Executor は AppState 側で一元管理し、終了処理の漏れを防ぐ


# Per-key events to prevent cache stampede (multiple threads fetching same key simultaneously)
# --- エラーレスポンスヘルパー ---





# #region Constants
NEWS_PARSE_LOG_SNIPPET_CHARS = _env_int(
    "MNS_NEWS_PARSE_LOG_SNIPPET_CHARS", 1200, 0, 10000
)
# #endregion Constants


def schedule_news_warmup():
    """Warm up news/trends caches in background to reduce first refresh failures after startup."""
    try:
        langsearch_api_key = get_langsearch_api_key() or ""
    except (KeyringError, RuntimeError):
        langsearch_api_key = ""
    search_source_hint = "ls" if langsearch_api_key else "ddgs"

    def _job():
        try:
            get_cached_context_with_negative_cache(
                f"market_news_context_us_{search_source_hint}",
                lambda: collect_market_news_context(
                    "us", langsearch_api_key=langsearch_api_key
                ),
                300,
                90,
                True,
            )
            get_cached_context_with_negative_cache(
                f"market_news_context_jp_{search_source_hint}",
                lambda: collect_market_news_context(
                    "jp", langsearch_api_key=langsearch_api_key
                ),
                300,
                90,
                True,
            )
            collect_market_trending_titles("us", 8, langsearch_api_key)
            collect_market_trending_titles("jp", 8, langsearch_api_key)
        except (IOError, RuntimeError, RequestException) as exc:
            app.logger.warning("News warmup failed: %s", exc)

    try:
        app_state.execution.news_executor.submit(_job)
    except (RuntimeError, AttributeError) as exc:
        app.logger.warning("Failed to schedule news warmup: %s", exc)


# ------------------------------
# Cache Utilities
# ------------------------------


# ------------------------------
# User Stock Save/Load
# ------------------------------


load_user_stocks()


def _warn_insecure_plaintext_mode():
    """Log a prominent warning at startup when plaintext secret storage is enabled."""
    if os.environ.get("MNS_ALLOW_PLAINTEXT_SECRETS", "").lower() in ("1", "true", "yes"):
        app.logger.critical(
            "SECURITY WARNING: MNS_ALLOW_PLAINTEXT_SECRETS is enabled. "
            "API keys are stored as plaintext in config.json. "
            "This is INSECURE and should only be used for development. "
            "Set up keyring or DPAPI for secure credential storage."
        )
    if os.environ.get("ALLOW_PLAINTEXT_SECRETS", "").lower() in ("1", "true", "yes"):
        app.logger.critical(
            "SECURITY WARNING: ALLOW_PLAINTEXT_SECRETS is enabled (legacy). "
            "Use MNS_ALLOW_PLAINTEXT_SECRETS instead. "
            "API keys are stored as plaintext in config.json."
        )


_warn_insecure_plaintext_mode()


# ------------------------------
# Mistral API Callers
# ------------------------------


# ------------------------------
# Stock Info Helpers
# ------------------------------


# ------------------------------
# #region AI Integration Routes & Logic


# #region Health & System Utility


def _is_loopback_host(host: str) -> bool:
    """ホストがループバックアドレスか判定"""
    if not host:
        return False
    host = host.strip()
    if host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


# #endregion Health & System Utility

# #region Market Hours Logic


# #region Real-Time SSE Engine


# ------------------------------
# 銘柄追加・削除時に同期をキックするように既存ルートを修正
# ------------------------------
# (メモ: 既存の api_add_stock, api_delete_stock 等の関数内で sync_all_stocks_now() を呼ぶように修正)


# ------------------------------
# Run
# ------------------------------


from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_add_stock_ext, api_stocks_bp
from routes.api_system import api_credentials, api_csp_report, api_shutdown, api_system_bp
from routes.pages import pages_bp

app.register_blueprint(pages_bp)
app.register_blueprint(api_system_bp)
app.register_blueprint(api_stocks_bp)
app.register_blueprint(api_analysis_bp)

# CSRF exemptions for API endpoints
csrf.exempt(api_csp_report)
csrf.exempt(api_shutdown)
csrf.exempt(api_add_stock_ext)
csrf.exempt(api_credentials)


# --- Global Error Handlers ---
@app.errorhandler(400)
def bad_request_error(error):
    """Handle 400 Bad Request errors."""
    return jsonify(
        {
            "ok": False,
            "error": "Bad Request",
            "message": "The request was malformed or invalid.",
        }
    ), 400


@app.errorhandler(403)
def forbidden_error(error):
    """Handle 403 Forbidden errors."""
    return jsonify(
        {
            "ok": False,
            "error": "Forbidden",
            "message": "You do not have permission to access this resource.",
        }
    ), 403


@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 Not Found errors."""
    return jsonify(
        {
            "ok": False,
            "error": "Not Found",
            "message": "The requested resource was not found.",
        }
    ), 404


@app.errorhandler(405)
def method_not_allowed_error(error):
    """Handle 405 Method Not Allowed errors."""
    return jsonify(
        {
            "ok": False,
            "error": "Method Not Allowed",
            "message": "The HTTP method is not allowed for this endpoint.",
        }
    ), 405


@app.errorhandler(413)
def payload_too_large_error(error):
    """Handle 413 Payload Too Large errors."""
    return jsonify(
        {
            "ok": False,
            "error": "Payload Too Large",
            "message": "The request payload exceeds the maximum allowed size.",
        }
    ), 413


@app.errorhandler(429)
def rate_limit_error(error):
    """Handle 429 Too Many Requests errors."""
    return jsonify(
        {
            "ok": False,
            "error": "Too Many Requests",
            "message": "Rate limit exceeded. Please try again later.",
        }
    ), 429


@app.errorhandler(500)
def internal_server_error(error):
    """Handle 500 Internal Server Error - never leak stack traces."""
    app.logger.error("Internal server error: %s", error, exc_info=True)
    return jsonify(
        {
            "ok": False,
            "error": "Internal Server Error",
            "message": "An unexpected error occurred. Please try again later.",
        }
    ), 500


if __name__ == "__main__":
    # スクリプト直接実行時のみ常駐スレッドを開始
    _start_background_threads()
    schedule_sync_all_stocks_now()
    schedule_news_warmup()
    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)
