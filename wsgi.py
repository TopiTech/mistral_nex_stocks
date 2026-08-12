"""
wsgi.py - WSGI entry point for Mistral NeX Stocks.

Usage:
    gunicorn -c gunicorn.conf.py wsgi:app
    uwsgi --processes 1 --enable-threads --module wsgi:app
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

import sys

from utils.worker_validation import MultiWorkerConfigurationError, enforce_single_worker

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
#
# Gunicorn does not export WEB_CONCURRENCY/GUNICORN_WORKERS itself (both are
# Heroku/PaaS conventions), and uWSGI commonly keeps ``processes`` in its
# native module when started from an ini file.  Validate before importing the
# Flask app so an invalid deployment cannot initialize runtime configuration
# before it is rejected.
try:
    enforce_single_worker()
except MultiWorkerConfigurationError as exc:
    print(
        f"FATAL: {exc} Refuse to start. "
        "Use `gunicorn -c gunicorn.conf.py wsgi:app` or "
        "`uwsgi --processes 1 --enable-threads --module wsgi:app` instead.",
        file=sys.stderr,
    )
    sys.exit(1)


from app import app, bootstrap
from utils.env_helpers import _env_bool

# Bootstrap runtime components (background threads, token init, data loading).
# Guarded by _app_bootstrap_lock in app.py so repeated calls are no-ops, and
# skipped entirely when MNS_SKIP_BOOTSTRAP is set (e.g. in tests).
if not _env_bool("MNS_SKIP_BOOTSTRAP"):
    bootstrap(app)

if __name__ == "__main__":
    from constants import BACKEND_PORT

    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)
