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
from app_state import (
    KeyringError,
    app_state,
    yf_session_manager,
)
from constants import (
    BACKEND_PORT,
    CACHE_DURATION_NEWS,
    NEGATIVE_CACHE_TTL,
    STATIC_MTIME_CACHE_TTL,
)
from credential_manager import get_langsearch_api_key, get_tavily_api_key
from error_handlers import register_error_handlers
from logging_config import DETAILED_API_LOG_PATHS, LOG_LEVEL, init_logging
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_add_stock_ext, api_stocks_bp
from routes.api_system import api_csp_report, api_shutdown, api_system_bp
from routes.pages import pages_bp
from security_config import init_security
from utils.caching import get_cached_context_with_negative_cache
from utils.env_helpers import _env_bool, _env_int
from utils.networking import _is_allowed_shutdown_origin, get_allowed_cors_origins
from utils.storage import load_user_stocks
from utils.text_utils import _short_text

logger = logging.getLogger(__name__)
from services.search_service import (
    _determine_search_strategy,
    collect_market_news_context,
    collect_market_trending_titles,
)


# Ensure global HTTP sessions, SQLite connections, and managers are closed on process
# exit to avoid ResourceWarning / WAL corruption. This is the last-resort cleanup
# that fires after all teardown_appcontext hooks have run.
def _cleanup_on_exit():
    # R9 fix: consolidated single atexit handler. Previously two separate
    # handlers (_cleanup_on_exit + app_state.shutdown_executors) were registered
    # with no guaranteed ordering; now executors shut down first, then DB
    # connections are closed.
    try:
        app_state.shutdown_executors()
    except Exception as exc:
        logger.debug("Cleanup of executors: %s", exc)

    try:
        yf_session_manager.close_all()
    except Exception as exc:
        logger.debug("Cleanup of yfinance sessions: %s", exc)

    try:
        if hasattr(app_state, "ai") and hasattr(app_state.ai, "chat_history"):
            chat_store = app_state.ai.chat_history
            if hasattr(chat_store, "close_all"):
                chat_store.close_all()
            else:
                chat_store.close()
    except Exception as exc:
        logger.debug("Cleanup of chat database: %s", exc)


atexit.register(_cleanup_on_exit)

# Signal handlers are registered in _register_signal_handlers() inside
# create_app() so that they include executor cleanup. See #endregion below.
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


def create_app(config_override: dict | None = None, skip_bootstrap: bool = False) -> Flask:
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

    # -- Logging --
    init_logging(app)

    # R3: Local rate-limit bypass is a conscious relaxation for personal use,
    # but it removes the guard that keeps runaway frontend loops from hammering
    # the local server. Surface it at startup so the operator can notice the
    # setting and verify it is intentional.
    if os.environ.get("MNS_DISABLE_LOCAL_RATE_LIMIT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        app.logger.warning(
            "MNS_DISABLE_LOCAL_RATE_LIMIT is enabled: local requests are limited only by the "
            "high MNS_LOCAL_RATE_LIMIT_CEILING backstop (default 600/window). "
            "This is intended for controlled personal environments only."
        )

    # -- Shutdown handlers --
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

    Core initialization is fail-closed: on failure ``_app_bootstrap_done``
    stays False so a corrected environment can retry, and
    ``app_state.bootstrap_ready`` is not set.
    """
    if _env_bool("MNS_SKIP_BOOTSTRAP"):
        logger.info("Skipping runtime bootstrap per MNS_SKIP_BOOTSTRAP")
        return

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
        if _allow_remote and len(_admin_token) < 32:
            raise RuntimeError(
                "FATAL: MNS_ALLOW_REMOTE_API requires MNS_ADMIN_TOKEN with at least 32 characters. "
                "Refuse to start. Configure a strong token or disable remote API access."
            )

        # Keep the first-request fallback and direct ``app:app`` imports under
        # the same fail-closed process guard as wsgi.py.  In particular, uWSGI
        # ini files expose their process count through the native ``uwsgi``
        # module rather than Gunicorn-style environment variables.
        from utils.worker_validation import enforce_single_worker

        try:
            enforce_single_worker()
        except RuntimeError as exc:
            raise RuntimeError(f"FATAL: {exc}") from exc

        # Runtime-only: initialize shutdown token, user stocks, and background loops.
        # These are intentionally removed from ``create_app`` to prevent import-time
        # side effects and make thread startup explicit.
        # Keep the entire critical path under the lock so concurrent callers cannot
        # double-start background threads or mark a half-initialized runtime ready.
        try:
            app_state.get_or_create_shutdown_token()
            app_state.initialize_yfinance_cache()
            # Older releases persisted portfolio fields in the reusable payload
            # cache. Scrub them in place so cold-start market data is retained
            # without leaving personal holdings in plaintext.
            from utils.stock_payload import _PORTFOLIO_RESPONSE_FIELDS

            try:
                migrated_payloads = app_state.payload_disk_cache.remove_fields_recursive(
                    _PORTFOLIO_RESPONSE_FIELDS
                )
                if migrated_payloads:
                    logger.info(
                        "Removed portfolio fields from %d legacy payload cache entries",
                        migrated_payloads,
                    )
            except (OSError, TypeError) as cache_exc:
                # Market-data cache is disposable and must not make core
                # application bootstrap fail.
                logger.warning("Could not migrate legacy payload cache: %s", cache_exc)
            load_user_stocks(force=True)
        except Exception as exc:
            logger.error("Bootstrap initialization failed: %s", exc)
            app_state.bootstrap_ready.clear()
            raise RuntimeError(f"Bootstrap initialization failed: {exc}") from exc

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

        # Mark complete only after successful core init + thread start.
        _app_bootstrap_done = True
        app_state.bootstrap_ready.set()


class RawRemoteAddressMiddleware:
    """WSGI middleware to backup the raw REMOTE_ADDR before downstream modifications."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        environ["RAW_REMOTE_ADDR"] = environ.get("REMOTE_ADDR", "")
        return self.wsgi_app(environ, start_response)


def _apply_proxy_fix(app: Flask) -> None:
    """Apply ProxyFix middleware if MNS_PROXY_FIX is enabled.

    The raw-address backup MUST wrap ProxyFix (i.e. sit closest to the WSGI
    server): ProxyFix rewrites REMOTE_ADDR from X-Forwarded-For, so any code
    that reads REMOTE_ADDR *after* ProxyFix sees attacker-influenced data when
    the proxy headers are not actually trusted. Capturing the socket address
    before ProxyFix runs guarantees RAW_REMOTE_ADDR is the true peer address.
    """
    _allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    _use_proxy_fix = os.environ.get("MNS_PROXY_FIX", "").strip().lower() in ("1", "true", "yes")
    if _allow_remote and not _use_proxy_fix:
        logger.warning(
            "MNS_ALLOW_REMOTE_API is enabled but MNS_PROXY_FIX is not set. "
            "Remote API mode requires both MNS_ALLOW_REMOTE_API=1 and MNS_PROXY_FIX=1. "
            "Requests behind a proxy will be rejected as non-local unless MNS_PROXY_FIX=1 is configured."
        )
    if _use_proxy_fix:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app,
            x_for=_env_int("MNS_PROXY_FIX_X_FOR", 1, min_value=0),
            x_proto=_env_int("MNS_PROXY_FIX_X_PROTO", 1, min_value=0),
            x_host=_env_int("MNS_PROXY_FIX_X_HOST", 1, min_value=0),
            x_port=_env_int("MNS_PROXY_FIX_X_PORT", 0, min_value=0),
            x_prefix=_env_int("MNS_PROXY_FIX_X_PREFIX", 0, min_value=0),
        )
    # Wrap ProxyFix (or the plain app when disabled) with the raw-address
    # backup so RAW_REMOTE_ADDR always reflects the real peer socket address.
    app.wsgi_app = RawRemoteAddressMiddleware(app.wsgi_app)  # type: ignore[method-assign]


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
    template rendered through this app (including ones rendered outside a
    normal request context in tests) can use ``static_url()`` without it being
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
    if _env_bool("MNS_SKIP_BOOTSTRAP") or "pytest" in sys.modules:
        return

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


_CROSS_SITE_COSTLY_GET_PATHS = frozenset(
    {
        "/api/indices",
        "/api/stocks",
        "/api/stock-details",
        "/api/stock-history",
        "/api/search",
        "/api/heatmap",
        "/api/trending",
        "/api/screener",
        "/api/stocks/stream",
        "/api/news",
    }
)


def _enforce_sec_fetch_site_check():
    """Enforce Sec-Fetch-Site metadata checks to block cross-site request forgery.

    Mutating methods are always protected. A small allowlisted set of GET API
    routes can trigger external work or allocate streaming resources, so those
    routes also reject browser requests explicitly marked ``cross-site``.
    """
    is_mutating = request.method in ("POST", "DELETE", "PUT", "PATCH")
    is_costly_get = request.method == "GET" and request.path in _CROSS_SITE_COSTLY_GET_PATHS
    if not is_mutating and not is_costly_get:
        return None

    if request.path == "/api/csp-report":
        return None

    sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    # M-7: "cross-site" is the only metadata value that reliably indicates a
    # cross-site request forgery attempt and is blocked.
    # "none" means the request was not initiated by a same-site page context
    # (e.g. direct navigation, bookmark, or a non-browser client such as the
    # native host, curl, or an extension background page issuing fetch). Many
    # legitimate local-only POSTs send Sec-Fetch-Site: none, so blocking it
    # would reject valid same-machine requests. We therefore allow "none" and
    # rely on the trusted-origin / loopback gate for those requests. (REV-03)
    #
    # Cross-site from a TRUSTED origin (chrome-extension://) is also permitted
    # by design: the browser's CORS enforcement already gates these requests,
    # and Flask-WTF's CSRF protection (which runs before this hook) blocks
    # cross-site POSTs to non-exempt endpoints. The three CSRF-exempt POST
    # endpoints (csp-report, shutdown, add_ext) are each protected by their own
    # token mechanism (none / shutdown token / extension Bearer token), so
    # allowing cross-site from a known extension origin does not weaken the
    # security model. (REV-04)
    if sec_fetch_site == "cross-site":
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
        from utils.networking import mask_sensitive_url

        app.logger.info(
            "REQ start id=%s method=%s path=%s remote=%s origin=%s ua=%s",
            g.request_id,
            request.method,
            mask_sensitive_url(request.full_path),
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
        "Content-Type, X-CSRFToken, X-CSRF-Token, X-MNS-Shutdown-Token, "
        "X-MNS-Admin-Token, X-MNS-Extension-Request, Authorization"
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
    from utils.networking import mask_sensitive_url

    _masked_path = mask_sensitive_url(request.full_path)
    if status_code >= 400:
        if status_code == 404:
            # 頻繁に発生し、かつ対応不要な404エラーをINFOレベルに下げる（本番ログのノイズ防止）
            ignored_404_paths = (
                "favicon.ico",
                "apple-touch-icon.png",
                "apple-touch-icon-precomposed.png",
                "robots.txt",
                ".map",
                "com.chrome.devtools.json",
            )
            if any(path in _masked_path for path in ignored_404_paths):
                logger.info(
                    "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
                    req_id,
                    request.method,
                    _masked_path,
                    status_code,
                    elapsed_ms,
                )
            else:
                logger.warning(
                    "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
                    req_id,
                    request.method,
                    _masked_path,
                    status_code,
                    elapsed_ms,
                )
        else:
            logger.warning(
                "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
                req_id,
                request.method,
                _masked_path,
                status_code,
                elapsed_ms,
            )
    elif LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        logger.info(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            _masked_path,
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
        results = {}
        # 各ウォームアップステップを個別にtry/exceptし、1つの失敗が他に影響しないようにする
        try:
            results["us_context"] = get_cached_context_with_negative_cache(
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
        except (OSError, RuntimeError, RequestException, ValueError, json.JSONDecodeError) as exc:
            logger.warning("News warmup (us context) failed: %s", exc)
            results["us_context"] = None

        try:
            results["jp_context"] = get_cached_context_with_negative_cache(
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
        except (OSError, RuntimeError, RequestException, ValueError, json.JSONDecodeError) as exc:
            logger.warning("News warmup (jp context) failed: %s", exc)
            results["jp_context"] = None

        try:
            results["us_trends"] = collect_market_trending_titles(
                "us", 8, langsearch_api_key, tavily_api_key
            )
        except (OSError, RuntimeError, RequestException, ValueError) as exc:
            logger.warning("News warmup (us trends) failed: %s", exc)
            results["us_trends"] = None

        try:
            results["jp_trends"] = collect_market_trending_titles(
                "jp", 8, langsearch_api_key, tavily_api_key
            )
        except (OSError, RuntimeError, RequestException, ValueError) as exc:
            logger.warning("News warmup (jp trends) failed: %s", exc)
            results["jp_trends"] = None

        success_count = sum(1 for v in results.values() if v is not None)
        total_count = len(results)
        if success_count < total_count:
            logger.info(
                "News warmup completed: %d/%d steps successful (partial)",
                success_count,
                total_count,
            )

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
    if _env_bool("MNS_SKIP_BOOTSTRAP"):
        return
    if not _app_bootstrap_done:
        bootstrap(app)
    return


# #endregion

# #region Startup Configuration

LANGSEARCH_BASE_URL = os.environ.get("LANGSEARCH_BASE_URL", "https://api.langsearch.com")
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"

NEWS_PARSE_LOG_SNIPPET_CHARS = _env_int("MNS_NEWS_PARSE_LOG_SNIPPET_CHARS", 1200, 0, 10000)


if __name__ == "__main__":
    # Use wsgi.py as the canonical entry point instead of running this file directly.
    # This is kept for backward compatibility.
    if not _env_bool("MNS_SKIP_BOOTSTRAP"):
        bootstrap(app)
    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)

# #endregion
