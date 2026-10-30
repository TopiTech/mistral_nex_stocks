"""Flask インスタンスの配線 (ProxyFix/secret/static/signals)。`app` からの純粋な移動。"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import sys
import threading
import time

from flask import Flask
from werkzeug.security import safe_join

from app_state import app_state
from constants import STATIC_MTIME_CACHE_TTL
from utils.env_helpers import _env_bool, _env_int, _is_testing

logger = logging.getLogger("app")


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
    _allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
    _use_proxy_fix = _env_bool("MNS_PROXY_FIX")
    if _allow_remote and not _use_proxy_fix:
        logger.warning(
            "MNS_ALLOW_REMOTE_API is enabled but MNS_PROXY_FIX is not set. "
            "Remote API mode requires both env vars to be set. "
            "Requests behind a proxy will be rejected as non-local unless MNS_PROXY_FIX=1."
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
        try:
            app.secret_key = get_or_create_flask_secret_key()
        except Exception as exc:
            # CORE-2 (Medium): Persisting the auto-generated key to the config
            # file can fail (read-only APP_DATA_DIR, disk full, Windows lock
            # contention). That must not make the app unable to start. Fall
            # back to an in-memory key so startup continues; the key only lives
            # for this process, so sessions do not survive a restart, but the
            # app remains usable. Production fail-closed behavior above is
            # unchanged.
            logger.warning(
                "Could not persist auto-generated FLASK_SECRET_KEY (%s). "
                "Using an in-memory key for this process; sessions will not "
                "survive restart.",
                exc,
            )
            app.secret_key = secrets.token_hex(32)


def _configure_static_cache_buster(app: Flask) -> None:
    """Configure template context with cache-busted static URL helper.

    Registered as a Jinja global (not just a context processor) so that any
    template rendered through this app (including ones rendered outside a
    normal request context in tests) can use ``static_url()`` without it being
    undefined.
    """
    _static_mtime_cache: dict[str, tuple[float, int]] = {}
    _static_mtime_cache_lock = threading.Lock()
    _static_mtime_cache_max_entries = 256

    def static_url(filename: str) -> str:
        from flask import url_for

        safe_path = safe_join(app.static_folder or "", filename) if app.static_folder else None
        if safe_path is None:
            return url_for("static", filename=filename)

        now = time.time()
        with _static_mtime_cache_lock:
            cached = _static_mtime_cache.get(filename)
            if cached and (now - cached[0]) < STATIC_MTIME_CACHE_TTL:
                return url_for("static", filename=filename) + f"?v={cached[1]}"
        try:
            mtime = int(os.path.getmtime(safe_path))
            with _static_mtime_cache_lock:
                if len(_static_mtime_cache) >= _static_mtime_cache_max_entries:
                    _static_mtime_cache.clear()
                _static_mtime_cache[filename] = (now, mtime)
            return url_for("static", filename=filename) + f"?v={mtime}"
        except (OSError, ValueError):
            return url_for("static", filename=filename)

    app.jinja_env.globals["static_url"] = static_url  # type: ignore


def _register_signal_handlers(app: Flask) -> None:
    """Register OS signal handlers for graceful shutdown."""
    if _is_testing():
        return

    def _handle_shutdown_signal(signum, frame):
        logger.info("Received termination signal %s. Shutting down...", signum)
        try:
            app_state.shutdown_executors()
        except Exception as exc:
            logger.error("Shutdown executor failure, continuing cleanup: %s", exc)
        if not sys.is_finalizing() and threading.current_thread() is threading.main_thread():
            sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    except (ValueError, AttributeError):
        # In non-main threads or some environments, signal handling cannot be set
        pass
