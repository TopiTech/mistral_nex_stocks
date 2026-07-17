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
from utils.caching import get_cached_context_with_negative_cache
from utils.networking import _is_allowed_shutdown_origin, get_allowed_cors_origins
from utils.text_utils import _short_text
from app_state import (
    KeyringError,
    app_state,
    yf_session_manager,
)
from credential_manager import get_langsearch_api_key, get_tavily_api_key
from utils.env_helpers import _env_int
from constants import (
    BACKEND_PORT,
    BASE_DIR,
    CACHE_DURATION_NEWS,
    NEGATIVE_CACHE_TTL,
    STATIC_MTIME_CACHE_TTL,
)
from error_handlers import register_error_handlers
from logging_config import DETAILED_API_LOG_PATHS, LOG_LEVEL, init_logging
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_add_stock_ext, api_stocks_bp
from routes.api_system import api_csp_report, api_shutdown, api_system_bp
from routes.pages import pages_bp
from security_config import init_security
from utils.storage import load_user_stocks

logger = logging.getLogger(__name__)
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


def _close_current_thread_chat_db(exception=None):
    """Close the current thread's SQLite chat history connection on teardown."""
    try:
        from app_state import app_state

        if hasattr(app_state, "ai") and hasattr(app_state.ai, "chat_history"):
            app_state.ai.chat_history.close()
    except Exception as exc:
        fallback_logger = logging.getLogger(__name__)
        # WARNING level: teardown-time SQLite errors (lock, corruption) indicate
        # a resource leak that would otherwise go undetected at DEBUG level.
        fallback_logger.warning("Failed to close chat database connection: %s", exc)


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
    app.teardown_appcontext(_close_current_thread_chat_db)


def create_app(config_override: Optional[dict] = None, skip_bootstrap: bool = False) -> Flask:
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
        skip_bootstrap: If True, skip auto-bootstrap on first request (for testing).
    """
    if skip_bootstrap:
        os.environ["MNS_SKIP_BOOTSTRAP"] = "1"
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
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11+ is required for this application")

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
    # api_credentials is intentionally NOT exempted: it writes/deletes the user's
    # API keys, so it must carry a CSRF token like any other state-changing
    # endpoint. The frontend (setup.js/settings.js) already sends X-CSRFToken
    # via csrfFetch. The local-origin check remains as defense-in-depth.
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

        # H-6: Fail closed when remote API access is enabled without an admin token.
        # MNS_ALLOW_REMOTE_API expands the credentials / local-request surface; without
        # MNS_ADMIN_TOKEN a misconfigured reverse-proxy deployment would leave key
        # management reachable by any caller that can hit the proxy.
        # Checked BEFORE marking bootstrap complete so a misconfigured start can
        # still be corrected (env fix + retry) without leaving a half-booted flag.
        _allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        _admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
        if _allow_remote and not _admin_token:
            raise RuntimeError(
                "FATAL: MNS_ALLOW_REMOTE_API is enabled but MNS_ADMIN_TOKEN is not set. "
                "Refuse to start. Configure a strong MNS_ADMIN_TOKEN or disable remote API access."
            )

        _app_bootstrap_done = True

    # Runtime-only: initialize shutdown token, user stocks, and background loops.
    # These are intentionally removed from ``create_app`` to prevent import-time
    # side effects and make thread startup explicit.
    try:
        app_state.get_or_create_shutdown_token()
        app_state.initialize_yfinance_cache()
        load_user_stocks()
    except Exception as exc:
        logger.warning("Bootstrap initialization failed: %s", exc)

    _start_background_threads()

    def _schedule_sync() -> None:
        try:
            schedule_sync_all_stocks_now()
        except Exception:
            logger.exception("Initial stock sync scheduling failed")

    def _schedule_news() -> None:
        try:
            schedule_news_warmup()
        except Exception:
            logger.exception("Initial news warmup scheduling failed")

    try:
        app_state.execution.sync_refresh_executor.submit(_schedule_sync)
    except RuntimeError as exc:
        logger.warning("Failed to submit initial sync job: %s", exc)

    try:
        app_state.execution.news_executor.submit(_schedule_news)
    except RuntimeError as exc:
        logger.warning("Failed to submit initial news warmup job: %s", exc)

    # Signal that bootstrap is complete (components can wait on this Event)
    app_state.bootstrap_ready.set()


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
    from utils.env_helpers import _is_production_env

    _is_prod_env = _is_production_env()
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
        from credential_manager import get_or_create_flask_secret_key

        logger.warning(
            "FLASK_SECRET_KEY not set in environment. Using auto-generated key for development. "
            "For production, set a strong unique FLASK_SECRET_KEY."
        )
        app.secret_key = get_or_create_flask_secret_key()


def _configure_static_cache_buster(app: Flask) -> None:
    """Configure template context with cache-busted static URL helper.

    Registered as a Jinja global (not just a context processor) so that any
    template rendered through this app  Eincluding ones rendered outside a
    normal request context in tests  Ecan use ``static_url()`` without it being
    undefined.
    """
    _static_mtime_cache: dict[str, tuple[float, int]] = {}
    _static_mtime_cache_lock = threading.Lock()

    def static_url(filename: str) -> str:
        from flask import url_for

        now = time.time()
        with _static_mtime_cache_lock:
            cached = _static_mtime_cache.get(filename)
            if cached and (now - cached[0]) < STATIC_MTIME_CACHE_TTL:
                return url_for("static", filename=filename) + f"?v={cached[1]}"
        file_path = os.path.join(app.static_folder or "", filename)
        try:
            mtime = int(os.path.getmtime(file_path))
            with _static_mtime_cache_lock:
                _static_mtime_cache[filename] = (now, mtime)
            return url_for("static", filename=filename) + f"?v={mtime}"
        except (OSError, ValueError):
            return url_for("static", filename=filename)

    app.jinja_env.globals["static_url"] = static_url  # type: ignore


def _register_signal_handlers(app: Flask) -> None:
    """Register OS signal handlers for graceful shutdown."""

    def _handle_shutdown_signal(signum, frame):
        logger.info("Received termination signal %s. Shutting down...", signum)
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
    """Enforce Sec-Fetch-Site metadata checks to block cross-site request forgery.

    Only runs on mutating HTTP methods (POST, DELETE, PUT, PATCH) to avoid
    unnecessary header parsing on GET/HEAD requests (health checks, static files).
    """
    # Fast-path: skip for non-mutating methods to avoid per-request overhead
    if request.method not in ("POST", "DELETE", "PUT", "PATCH"):
        return None

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
            logger.warning(
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

    vary_values = [v.strip() for v in str(response.headers.get("Vary", "")).split(",") if v.strip()]
    if "origin" not in {v.lower() for v in vary_values}:
        vary_values.append("Origin")
    response.headers["Vary"] = ", ".join(vary_values) if vary_values else "Origin"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-CSRFToken, X-CSRF-Token, X-MNS-Shutdown-Token, X-MNS-Admin-Token"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Access-Control-Max-Age"] = "600"

    # M-2: Reporting-Endpoints header for CSP Level 3 report-to directive.
    # Injected here (alongside the CSP report-uri fallback) so that both
    # old and new browser CSP reporting modes are covered by a single
    # after_request handler rather than split across registrations.
    response.headers["Reporting-Endpoints"] = 'csp-endpoint="/api/csp-report"'

    req_id = getattr(g, "request_id", "-")
    response.headers["X-MNS-Request-Id"] = req_id
    response.headers["Access-Control-Expose-Headers"] = "X-MNS-Request-Id"

    started = getattr(g, "request_start_ts", None)
    elapsed_ms = int((time.time() - started) * 1000) if isinstance(started, (int, float)) else -1
    status_code = int(response.status_code or 0)
    if status_code >= 400:
        logger.warning(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            request.path,
            status_code,
            elapsed_ms,
        )
    elif LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        logger.info(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            request.path,
            status_code,
            elapsed_ms,
        )

    return response


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
                lambda: collect_market_news_context(
                    "us",
                    langsearch_api_key=langsearch_api_key,
                    tavily_api_key=tavily_api_key,
                ),
                CACHE_DURATION_NEWS,
                NEGATIVE_CACHE_TTL,
                True,
            )
            get_cached_context_with_negative_cache(
                f"market_news_context_jp_{strategy}",
                lambda: collect_market_news_context(
                    "jp",
                    langsearch_api_key=langsearch_api_key,
                    tavily_api_key=tavily_api_key,
                ),
                CACHE_DURATION_NEWS,
                NEGATIVE_CACHE_TTL,
                True,
            )
            collect_market_trending_titles("us", 8, langsearch_api_key, tavily_api_key)
            collect_market_trending_titles("jp", 8, langsearch_api_key, tavily_api_key)
        except (
            IOError,
            OSError,
            RuntimeError,
            RequestException,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            logger.warning("News warmup failed: %s", exc)

    try:
        app_state.execution.news_executor.submit(_job)
    except (RuntimeError, AttributeError, ValueError) as exc:
        logger.warning("Failed to schedule news warmup: %s", exc)


# NOTE: Do NOT call bootstrap() at import time. WSGI servers (gunicorn wsgi:app)
# import this module to obtain `app`, so running bootstrap here would start
# background threads *and* a second bootstrap would run in wsgi.py, producing
# duplicate apps / threads sharing the single app_state singleton. Bootstrap is
# performed exactly once by the entry point (wsgi.py for gunicorn, or the
# __main__ block below for `python app.py`). Tests opt out via MNS_SKIP_BOOTSTRAP.
app = create_app()


# H-2 guard: ensure bootstrap is called on first request if somehow missed.
# This prevents the app from running without background threads even when
# the entry point forgets to call bootstrap().
# Performance: the per-request flag check is O(1) after the first bootstrap
# completes (a single bool read). We intentionally do NOT remove this hook
# at runtime because mutating Flask's before_request_funcs mid-request is
# not thread-safe.
@app.before_request
def _ensure_bootstrap_called():
    """Auto-bootstrap on first request if bootstrap() was never called.

    This is a safety net for misconfigured WSGI entry points. Under normal
    operation the entry point (wsgi.py or ``python app.py``) calls bootstrap()
    before the first request arrives, so this guard is a no-op on the first
    request after bootstrap completes. Tests opt out via MNS_SKIP_BOOTSTRAP.

    Unlike the previous implementation, this hook does NOT attempt to remove
    itself from the before_request chain at runtime. Modifying Flask's internal
    before_request_funcs during request processing is not thread-safe. Instead,
    the guard simply checks the ``_app_bootstrap_done`` flag on every request,
    which is a fast O(1) read after the first bootstrap completes.
    """
    if os.environ.get("MNS_SKIP_BOOTSTRAP"):
        return None
    if not _app_bootstrap_done:
        bootstrap(app)
    return None


# #endregion

# #region Startup Configuration

LANGSEARCH_BASE_URL = os.environ.get("LANGSEARCH_BASE_URL", "https://api.langsearch.com")
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"
USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")

NEWS_PARSE_LOG_SNIPPET_CHARS = _env_int("MNS_NEWS_PARSE_LOG_SNIPPET_CHARS", 1200, 0, 10000)


if __name__ == "__main__":
    # Use wsgi.py as the canonical entry point instead of running this file directly.
    # This is kept for backward compatibility.
    if not os.environ.get("MNS_SKIP_BOOTSTRAP"):
        bootstrap(app)
    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)

# #endregion
