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

# H-1: Enforce single-worker architecture. Multi-process mode is not supported
# due to in-memory singleton state (app_state) and thread-local caches
# (yfinance_short_cache) that do not synchronize across processes.
# Default: enabled. Set MNS_WORKER_VALIDATION=0 to disable (not recommended).
if os.environ.get("MNS_WORKER_VALIDATION", "1") not in ("0", "false", "no"):
    # gunicorn exposes preload flag; detect workers > 1 via env
    _gunicorn_workers = os.environ.get("PRELOAD_WORKERS", "")
    _gunicorn_worker_count = os.environ.get("GUNICORN_WORKERS", "1")
    if _gunicorn_worker_count not in ("", "1"):
        raise RuntimeError(
            "H-1: Multi-worker mode is not supported. "
            "Set GUNICORN_WORKERS=1 or remove the environment variable. "
            "See wsgi.py docstring for details."
        )

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
