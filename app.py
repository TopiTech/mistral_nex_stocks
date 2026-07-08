# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-arguments,too-many-positional-arguments
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
from typing import Optional

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
    _short_text,
    get_allowed_cors_origins,
    get_cached_context_with_negative_cache,
)
from utils.storage import load_user_stocks

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
from constants import (
    BACKEND_PORT,
    BASE_DIR,
    STATIC_MTIME_CACHE_TTL,
    NEGATIVE_CACHE_TTL,
    CACHE_DURATION_NEWS,
)
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_add_stock_ext, api_stocks_bp
from routes.api_system import api_csp_report, api_shutdown, api_system_bp
from routes.pages import pages_bp

from error_handlers import register_error_handlers
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
        logger = logging.getLogger(__name__)
        logger.debug("Cleanup of yfinance sessions: %s", exc)


atexit.register(_cleanup_on_exit)
# #endregion

# #region Application Factory


def add_request_hooks(app: Flask) -> None:
    """Register request lifecycle hooks on a Flask instance.

    This is called internally by create_app() and should NOT be called
    externally as that would cause duplicate hook registration (causing
    double logging, duplicate CORS headers, etc.).

    Args:
        app: Flask application instance to register hooks on.
    """
    app.before_request(_enforce_sec_fetch_site_check)
    app.before_request(_log_request_start)
    app.after_request(add_extension_cors_headers)


def create_app(config_override: Optional[dict] = None) -> Flask:
    """Create and configure the Flask application.

    Application Factory pattern for improved testability and modularity.
    Call once to get the configured Flask instance.

    Note:
        This function focuses on application wiring only. It does not
        perform side effects like background thread startup or disk I/O.
        Use :func:`bootstrap` explicitly after creating the app to
        initialize runtime components.

    Args:
        config_override: Optional dict to override app.config values.
    """
    app = Flask(__name__)

    # -- ProxyFix --
    _apply_proxy_fix(app)

    # -- Secret Key --
    _configure_secret_key(app)

    # -- Security --
    csrf = init_security(app)

    # -- Static file cache buster --
    _configure_static_cache_buster(app)

    # -- Python version check --
    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9+ is required for this application")

    # -- Logging --
    init_logging(app)

    # -- Shutdown handlers --
    atexit.register(app_state.shutdown_executors)
    _register_signal_handlers(app)

    # Request lifecycle hooks.
    add_request_hooks(app)

    # -- Blueprints --
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_system_bp)
    app.register_blueprint(api_stocks_bp)
    app.register_blueprint(api_analysis_bp)

    # -- CSRF exemptions --
    csrf.exempt(api_csp_report)
    csrf.exempt(api_shutdown)
    csrf.exempt(api_add_stock_ext)

    # -- Error handlers --
    register_error_handlers(app)

    # -- Apply config overrides --
    if config_override:
        app.config.update(config_override)

    return app


# H-1/H-2 improvement: runtime bootstrap for threads and config-less startup.
_app_bootstrap_done = False
_app_bootstrap_lock = threading.Lock()


def bootstrap(app: Flask) -> None:
    """Initialize runtime-only components after app creation.

    This separates wiring-time side effects from runtime side effects,
    allowing WSGI/import usage without unintended disk/network activity.
    """
    global _app_bootstrap_done
    with _app_bootstrap_lock:
        if _app_bootstrap_done:
            return
        _app_bootstrap_done = True

    # Runtime-only: initialize shutdown token, user stocks, and background loops.
    # These are intentionally removed from ``create_app`` to prevent import-time
    # side effects and make thread startup explicit.
    try:
        app_state.get_or_create_shutdown_token()
        load_user_stocks()
    except Exception as exc:
        app.logger.warning("Bootstrap initialization failed: %s", exc)

    _start_background_threads()

    def _schedule_sync() -> None:
        try:
            schedule_sync_all_stocks_now()
        except Exception as exc:
            app.logger.warning("Initial stock sync scheduling failed: %s", exc)

    def _schedule_news() -> None:
        try:
            schedule_news_warmup()
        except Exception as exc:
            app.logger.warning("Initial news warmup scheduling failed: %s", exc)

    try:
        app_state.execution.sync_refresh_executor.submit(_schedule_sync)
    except RuntimeError as exc:
        app.logger.warning("Failed to submit initial sync job: %s", exc)

    try:
        app_state.execution.news_executor.submit(_schedule_news)
    except RuntimeError as exc:
        app.logger.warning("Failed to submit initial news warmup job: %s", exc)


class RawRemoteAddressMiddleware:
    """WSGI middleware to backup the raw REMOTE_ADDR before downstream modifications."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        environ["RAW_REMOTE_ADDR"] = environ.get("REMOTE_ADDR", "")
        return self.wsgi_app(environ, start_response)


def _apply_proxy_fix(app: Flask) -> None:
    """Apply ProxyFix middleware if MNS_PROXY_FIX is enabled."""
    app.wsgi_app = RawRemoteAddressMiddleware(app.wsgi_app)  # type: ignore[method-assign]
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


def _configure_secret_key(app: Flask) -> None:
    """Configure Flask secret key from env or auto-generated store."""
    _is_prod_env = os.environ.get("MNS_PROD", "").lower() in ("1", "true", "yes") or \
        os.environ.get("MNS_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
    _flask_secret = os.environ.get("FLASK_SECRET_KEY")

    if _flask_secret:
        if len(_flask_secret) < 32:
            raise ValueError("FLASK_SECRET_KEY must be at least 32 characters for security")
        app.secret_key = _flask_secret
    else:
        if _is_prod_env:
            raise ValueError(
                "Security Risk: FLASK_SECRET_KEY environment variable is required in production."
            )
        from config_utils import get_or_create_flask_secret_key
        app.logger.warning(
            "FLASK_SECRET_KEY not set in environment. Using auto-generated key for development. "
            "For production, set a strong unique FLASK_SECRET_KEY."
        )
        app.secret_key = get_or_create_flask_secret_key()


def _configure_static_cache_buster(app: Flask) -> None:
    """Configure template context with cache-busted static URL helper."""
    _static_mtime_cache: dict[str, tuple[float, int]] = {}
    _static_mtime_cache_lock = threading.Lock()

    @app.context_processor
    def inject_static_url():
        from flask import url_for
        _static_folder = app.static_folder or ""
        now = time.time()

        def static_url(filename: str) -> str:
            with _static_mtime_cache_lock:
                cached = _static_mtime_cache.get(filename)
                if cached and (now - cached[0]) < STATIC_MTIME_CACHE_TTL:
                    return url_for("static", filename=filename) + f"?v={cached[1]}"
            file_path = os.path.join(_static_folder, filename)
            try:
                mtime = int(os.path.getmtime(file_path))
                with _static_mtime_cache_lock:
                    _static_mtime_cache[filename] = (now, mtime)
                return url_for("static", filename=filename) + f"?v={mtime}"
            except (OSError, ValueError):
                return url_for("static", filename=filename)

        return dict(static_url=static_url)


def _register_signal_handlers(app: Flask) -> None:
    """Register OS signal handlers for graceful shutdown."""
    def _handle_shutdown_signal(signum, frame):
        app.logger.info("Received termination signal %s. Shutting down...", signum)
        app_state.shutdown_executors()
        if not sys.is_finalizing() and threading.current_thread() is threading.main_thread():
            sys.exit(0)

    try:
        import signal
        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    except (ValueError, ImportError, AttributeError):
        pass


# #region Global Flask Instance (backward compatibility)


def _enforce_sec_fetch_site_check():
    """Enforce Sec-Fetch-Site metadata checks to block cross-site request forgery."""
    if request.method in ("POST", "DELETE", "PUT", "PATCH"):
        if request.path == "/api/csp-report":
            return None

        sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        # M-7: "cross-site" is blocked as expected.
        # "none" means the request came from a direct navigation (address bar,
        # bookmark, etc.) rather than from a same-site page. Mutating requests
        # (POST/DELETE/PUT/PATCH) initiated via direct navigation are unusual
        # and may indicate a CSRF attack (e.g. form submitted via a saved link).
        # We allow exceptions only for known extension origins.
        if sec_fetch_site in ("cross-site", "none"):
            allowed = _is_allowed_shutdown_origin(request)
            if not allowed:
                app.logger.warning(
                    "Block cross-site request to %s: Origin/Referer not allowed. Sec-Fetch-Site=%s",
                    request.path,
                    sec_fetch_site,
                )
                return jsonify({"ok": False, "error": "forbidden cross-site request"}), 403


def _log_request_start():
    """Log the start of an incoming request with a unique request ID."""
    g.request_start_ts = time.time()
    g.request_id = uuid.uuid4().hex[:10]

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
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Access-Control-Max-Age"] = "600"

    req_id = getattr(g, "request_id", "-")
    response.headers["X-MNS-Request-Id"] = req_id
    response.headers["Access-Control-Expose-Headers"] = "X-MNS-Request-Id"

    started = getattr(g, "request_start_ts", None)
    elapsed_ms = int((time.time() - started) * 1000) if isinstance(started, (int, float)) else -1
    status_code = int(response.status_code or 0)
    if status_code >= 400:
        app.logger.warning(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id, request.method, request.path, status_code, elapsed_ms,
        )
    elif LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        app.logger.info(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id, request.method, request.path, status_code, elapsed_ms,
        )

    return response


app = create_app()

# #endregion

# #region Startup Configuration

LANGSEARCH_BASE_URL = os.environ.get("LANGSEARCH_BASE_URL", "https://api.langsearch.com")
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"
USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")

NEWS_PARSE_LOG_SNIPPET_CHARS = _env_int("MNS_NEWS_PARSE_LOG_SNIPPET_CHARS", 1200, 0, 10000)


def schedule_news_warmup():
    """Warm up news/trends caches in background."""
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
                lambda: collect_market_news_context("us",
                    langsearch_api_key=langsearch_api_key, tavily_api_key=tavily_api_key),
                CACHE_DURATION_NEWS, NEGATIVE_CACHE_TTL, True,
            )
            get_cached_context_with_negative_cache(
                f"market_news_context_jp_{strategy}",
                lambda: collect_market_news_context("jp",
                    langsearch_api_key=langsearch_api_key, tavily_api_key=tavily_api_key),
                CACHE_DURATION_NEWS, NEGATIVE_CACHE_TTL, True,
            )
            collect_market_trending_titles("us", 8, langsearch_api_key, tavily_api_key)
            collect_market_trending_titles("jp", 8, langsearch_api_key, tavily_api_key)
        except (IOError, OSError, RuntimeError, RequestException, ValueError, json.JSONDecodeError) as exc:
            app.logger.warning("News warmup failed: %s", exc)

    try:
        app_state.execution.news_executor.submit(_job)
    except (RuntimeError, AttributeError, ValueError) as exc:
        app.logger.warning("Failed to schedule news warmup: %s", exc)


if __name__ == "__main__":
    _start_background_threads()
    schedule_sync_all_stocks_now()
    schedule_news_warmup()
    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)

# #endregion
