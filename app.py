# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-return-statements,too-many-arguments,too-many-positional-arguments
"""Backend application for Mistral NeX Stocks."""

# #region Imports

import atexit
import json
import logging
import os
import sys
import threading
import time
import uuid
from flask import (
    Flask,
    g,
    jsonify,
    request,
)


from requests.exceptions import RequestException


from app_bg import (
    _start_background_threads,
    schedule_sync_all_stocks_now,
)
from app_helpers import (
    _is_allowed_shutdown_origin,
    _sanitize_error_message,
    _short_text,
    get_allowed_cors_origins,
    get_cached_context_with_negative_cache,
    load_user_stocks,
)

# #endregion Imports
# #region Imports from Migrated Modules
from app_state import (
    KeyringError,
    app_state,
    yf_session_manager,
)
from config_utils import (
    _env_int,
    get_langsearch_api_key,
    get_tavily_api_key,
)
from constants import BACKEND_PORT, BASE_DIR
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_add_stock_ext, api_stocks_bp
from routes.api_system import api_csp_report, api_shutdown, api_system_bp
from routes.pages import pages_bp

from logging_config import init_logging, LOG_LEVEL, DETAILED_API_LOG_PATHS
from security_config import init_security
from services.search_service import (
    _determine_search_strategy,
    collect_market_news_context,
    collect_market_trending_titles,
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

# --- ProxyFix: Reverse Proxy 環境での正しいクライアントIP/スキーマ取得 ---
# 環境変数 MNS_PROXY_FIX=1 で有効化（デフォルト: localhost前提で無効）
# 本番環境で nginx / caddy 等のリバースプロキシ配下で実行する場合は有効にすること。
# x_for=1: X-Forwarded-For の最初のエントリを client IP として信頼
# x_proto=1: X-Forwarded-Proto を信頼（http→httpsの判定を正しく行う）
# x_host=1: X-Forwarded-Host を信頼
_use_proxy_fix = os.environ.get("MNS_PROXY_FIX", "").lower() in ("1", "true", "yes")
if _use_proxy_fix:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
        app.wsgi_app,
        x_for=int(os.environ.get("MNS_PROXY_FIX_X_FOR", "1")),
        x_proto=int(os.environ.get("MNS_PROXY_FIX_X_PROTO", "1")),
        x_host=int(os.environ.get("MNS_PROXY_FIX_X_HOST", "1")),
        x_port=int(os.environ.get("MNS_PROXY_FIX_X_PORT", "0")),
        x_prefix=int(os.environ.get("MNS_PROXY_FIX_X_PREFIX", "0")),
    )

# セッション暗号化用のシークレットキーの検証と設定
# 本番環境では環境変数 FLASK_SECRET_KEY を設定することを必須（強制）とします
_is_prod_env = os.environ.get("MNS_PROD", "").lower() in ("1", "true", "yes") or os.environ.get("MNS_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
_flask_secret = os.environ.get("FLASK_SECRET_KEY")

if _flask_secret:
    if len(_flask_secret) < 32:
        raise ValueError("FLASK_SECRET_KEY must be at least 32 characters for security")
    app.secret_key = _flask_secret
else:
    if _is_prod_env:
        raise ValueError(
            "Security Risk: FLASK_SECRET_KEY environment variable is required in production environment."
        )
    
    from config_utils import get_or_create_flask_secret_key

    app.logger.warning(
        "FLASK_SECRET_KEY not set in environment. Using persistent auto-generated key from secure storage for development. "
        "For production deployment, please set a strong unique FLASK_SECRET_KEY environment variable."
    )
    app.secret_key = get_or_create_flask_secret_key()

# --- セキュリティ設定の初期化（security_config.py に委譲） ---
# Session設定、CSP、Talisman、CSRF保護を一括設定
csrf = init_security(app)

if sys.version_info < (3, 9):
    raise RuntimeError("Python 3.9+ is required for this application")

# --- ログ設定の初期化（logging_config.py に委譲） ---
init_logging(app)

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
        "Content-Type, X-LangSearch-Key, X-Tavily-Key, X-CSRFToken, X-CSRF-Token, X-MNS-Shutdown-Token"
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
        tavily_api_key = get_tavily_api_key() or ""
    except (KeyringError, RuntimeError, ValueError):
        langsearch_api_key = ""
        tavily_api_key = ""

    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)

    def _job():
        try:
            get_cached_context_with_negative_cache(
                f"market_news_context_us_{strategy}",
                lambda: collect_market_news_context(
                    "us", langsearch_api_key=langsearch_api_key, tavily_api_key=tavily_api_key
                ),
                300,
                90,
                True,
            )
            get_cached_context_with_negative_cache(
                f"market_news_context_jp_{strategy}",
                lambda: collect_market_news_context(
                    "jp", langsearch_api_key=langsearch_api_key, tavily_api_key=tavily_api_key
                ),
                300,
                90,
                True,
            )
            collect_market_trending_titles("us", 8, langsearch_api_key, tavily_api_key)
            collect_market_trending_titles("jp", 8, langsearch_api_key, tavily_api_key)
        except (IOError, OSError, RuntimeError, RequestException, ValueError, json.JSONDecodeError) as exc:
            app.logger.warning("News warmup failed: %s", exc)

    try:
        app_state.execution.news_executor.submit(_job)
    except (RuntimeError, AttributeError, ValueError) as exc:
        app.logger.warning("Failed to schedule news warmup: %s", exc)


# ------------------------------
# Cache Utilities
# ------------------------------


# ------------------------------
# User Stock Save/Load
# ------------------------------


load_user_stocks()


# プレーンテキスト保存はセキュリティ強化のため削除されました。
# _warn_insecure_plaintext_mode は廃止されました。


# ------------------------------
# Mistral API Callers
# ------------------------------


# ------------------------------
# Stock Info Helpers
# ------------------------------


# ------------------------------
# #region AI Integration Routes & Logic


# #region Health & System Utility


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




app.register_blueprint(pages_bp)
app.register_blueprint(api_system_bp)
app.register_blueprint(api_stocks_bp)
app.register_blueprint(api_analysis_bp)

# CSRF exemptions for API endpoints
# ────────────────────────────────────────────────────────────
# Security model for each exempt endpoint:
#
# 1. api_csp_report     : CSP violation reports are sent by the browser automatically
#                         (via report-uri). No session/cookie state is changed, and
#                         the payload is purely diagnostic. Exempt is safe.
#
# 2. api_shutdown       : Protected by single-use shutdown token (X-MNS-Shutdown-Token).
#                         Additionally requires _is_local_request() + Origin validation.
#                         Token is rotated on each use. Exempt is justified.
#
# 3. api_add_stock_ext  : Chrome Extension専用エンドポイント。
#                         3重防御: (a) _is_local_request() — localhost限定
#                         (b) X-MNS-Extension-Request カスタムヘッダー必須
#                         (c) _is_allowed_shutdown_origin() — Origin/Referer許可リスト検証
#                         Chrome拡張機能の fetch() は同一オリジンでないため
#                         csrf.js の自動 CSRF token 付与が効かないため、exempt が必要。
#                         上記3重防御で CSRF の代替を担保している。
csrf.exempt(api_csp_report)
csrf.exempt(api_shutdown)
csrf.exempt(api_add_stock_ext)


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
