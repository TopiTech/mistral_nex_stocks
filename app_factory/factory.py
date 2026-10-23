"""アプリケーションファクトリ (`create_app` 系)。`app` からの純粋な移動。

bootstrap完了フラグと `bootstrap()` 本体は `app` シムが所有・解決する
(テストが `app._app_bootstrap_done` を再束縛するため)。
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast

from flask import Flask, current_app

from app_factory.hooks import (
    _enforce_sec_fetch_site_check,
    _log_request_start,
    add_extension_cors_headers,
)
from app_factory.wiring import (
    _apply_proxy_fix,
    _configure_secret_key,
    _configure_static_cache_buster,
    _register_signal_handlers,
)
from error_handlers import register_error_handlers
from logging_config import init_logging
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_add_stock_ext, api_stocks_bp
from routes.api_system import api_csp_report, api_shutdown, api_system_bp
from routes.pages import pages_bp
from security_config import init_security

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


# H-2 guard: ensure bootstrap is called on first request if somehow missed.
# This prevents the app from running without background threads even when
# the entry point forgets to call bootstrap().
# Performance: the per-request flag check is O(1) after the first bootstrap
# completes (a single bool read). We intentionally do NOT remove this hook
# at runtime because mutating Flask's before_request_funcs mid-request is
# not thread-safe.
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
    # _env_bool は `patch.object(app, "_env_bool")` 対象のため app 経由で解決する。
    import app

    if current_app.config.get("MNS_SKIP_BOOTSTRAP", False) or app._env_bool(
        "MNS_SKIP_BOOTSTRAP"
    ):
        return
    if not app._app_bootstrap_done:
        _proxy: Any = current_app
        app.bootstrap(cast(Flask, _proxy._get_current_object()))
    return


def add_request_hooks(app: Flask) -> None:
    """Register request lifecycle hooks on a Flask instance.

    This is called internally by create_app() and should NOT be called
    externally as that would cause duplicate hook registration (causing
    double logging, duplicate CORS headers, etc.).

    Args:
        app: Flask application instance to register hooks on.
    """
    app.before_request(_ensure_bootstrap_called)
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
    # import_name は "app" で固定する。原文の Flask(__name__) と同一である
    # (テンプレート/static の root_path 解決が変わらないようにするため)。
    # このモジュールに移動したことによる __name__ 変化の影響を受けないこと。
    app = Flask("app")
    app.config["MNS_SKIP_BOOTSTRAP"] = skip_bootstrap

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


# #endregion
