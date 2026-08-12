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

from app import app, bootstrap
from utils.env_helpers import _env_bool

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
# Heroku/PaaS conventions), so the guard must also inspect GUNICORN_CMD_ARGS and
# the raw CLI arguments to catch `gunicorn --workers 4 wsgi:app` invocations that
# run without gunicorn.conf.py (whose on_starting hook is the other guard).
if os.environ.get("MNS_WORKER_VALIDATION", "1") not in ("0", "false", "no"):
    import shlex
    import sys

    def _detect_worker_count() -> int:
        for env_name in ("WEB_CONCURRENCY", "GUNICORN_WORKERS"):
            raw = os.environ.get(env_name, "")
            if raw.strip():
                try:
                    return max(1, int(raw.strip()))
                except (TypeError, ValueError):
                    pass
        tokens = []
        if os.environ.get("GUNICORN_CMD_ARGS", ""):
            tokens.extend(shlex.split(os.environ["GUNICORN_CMD_ARGS"]))
        tokens.extend(sys.argv[1:])
        for i, tok in enumerate(tokens):
            worker_value: str | None = None
            if tok in ("--workers", "-w") and i + 1 < len(tokens):
                worker_value = tokens[i + 1]
            elif tok.startswith("--workers="):
                worker_value = tok.partition("=")[2]
            elif tok.startswith("-w") and len(tok) > 2:
                # Gunicorn accepts the compact short form as well (e.g. -w4).
                worker_value = tok[2:]
            if worker_value is not None:
                try:
                    return max(1, int(worker_value))
                except (TypeError, ValueError):
                    pass
        return 1

    _worker_count = _detect_worker_count()
    if _worker_count > 1:
        print(
            f"FATAL: Multi-worker mode detected (workers={_worker_count}). "
            "This application uses in-memory singleton state and is only "
            "supported with a single worker process. Refuse to start. "
            "Use `gunicorn --workers 1 -k gthread wsgi:app` instead.",
            file=sys.stderr,
        )
        sys.exit(1)

# Bootstrap runtime components (background threads, token init, data loading).
# Guarded by _app_bootstrap_lock in app.py so repeated calls are no-ops, and
# skipped entirely when MNS_SKIP_BOOTSTRAP is set (e.g. in tests).
if not _env_bool("MNS_SKIP_BOOTSTRAP"):
    bootstrap(app)

if __name__ == "__main__":
    from constants import BACKEND_PORT

    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)
