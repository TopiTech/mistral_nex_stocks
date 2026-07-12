"""
wsgi.py - WSGI entry point for Mistral NeX Stocks.

Usage:
    gunicorn --workers 1 --threads 8 wsgi:app
    uwsgi --module wsgi:app
    python -m wsgi

IMPORTANT: Multi-process (workers > 1) is NOT supported.
The application uses in-memory singletons (app_state, yf_session_manager) and
thread-local caches (yfinance_short_cache) that are NOT shared between processes.
Running with workers > 1 will cause:
- Duplicate background threads in each worker
- Cache inconsistency (yfinance requests multiplied by worker count -> 429/439)
- Race conditions on config file writes

Multi-worker validation is enabled by default. Set MNS_WORKER_VALIDATION=0
to disable (not recommended).

Tests can opt out of bootstrap by setting MNS_SKIP_BOOTSTRAP=1.
"""
import os

from app import create_app, bootstrap

# H-1: Enforce single-worker architecture. Multi-process mode is NOT supported
# due to in-memory singleton state (app_state) and thread-local caches
# (yfinance_short_cache) that do not synchronize across processes. Running with
# workers > 1 causes duplicate background threads in each worker, cache
# inconsistency (yfinance requests multiplied by worker count -> 429/439), and
# race conditions on config file writes.
#
# The previous implementation only printed a warning and continued, which meant
# a misconfigured gunicorn (e.g. `gunicorn --workers 4 wsgi:app`) would silently
# start in an unsupported mode and corrupt state. We now hard-fail at import
# time so the misconfiguration is impossible to miss.
#
# Set MNS_WORKER_VALIDATION=0 to disable this guard (NOT recommended; reserved
# for environments that have externalized all shared state, e.g. Redis).
if os.environ.get("MNS_WORKER_VALIDATION", "1") not in ("0", "false", "no"):
    _raw_worker_count = os.environ.get(
        "WEB_CONCURRENCY", os.environ.get("GUNICORN_WORKERS", "1")
    )
    try:
        _worker_count = int(_raw_worker_count)
    except (TypeError, ValueError):
        _worker_count = 1
    if _worker_count > 1:
        import sys
        print(
            f"FATAL: Multi-worker mode detected (workers={_worker_count}). "
            "This application uses in-memory singleton state and is only "
            "supported with a single worker process. Refuse to start. "
            "Use `gunicorn --workers 1 -k gthread wsgi:app` instead.",
            file=sys.stderr,
        )
        sys.exit(1)

# Create the Flask application instance (pure: no side effects).
app = create_app()

# Bootstrap runtime components (background threads, token init, data loading).
# Guarded by _app_bootstrap_lock in app.py so repeated calls are no-ops, and
# skipped entirely when MNS_SKIP_BOOTSTRAP is set (e.g. in tests).
if not os.environ.get("MNS_SKIP_BOOTSTRAP"):
    bootstrap(app)

if __name__ == "__main__":
    from constants import BACKEND_PORT

    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)
