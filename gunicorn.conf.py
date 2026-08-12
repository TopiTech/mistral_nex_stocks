"""
gunicorn.conf.py - Gunicorn configuration for Mistral NeX Stocks.

!!! WARNING - SINGLE WORKER ONLY !!!
This application uses in-memory singleton state (app_state,
yf_session_manager) and thread-local caches that are NOT shared
between OS processes. Multi-worker mode WILL cause data corruption,
duplicate background threads, and split SSE connections.

DO NOT change ``workers`` to anything other than 1.
See wsgi.py for details.

Recommended invocation:
    gunicorn -c gunicorn.conf.py wsgi:app
"""

import os


def _env_int(name, default, minimum, maximum):
    """Read a bounded integer without importing the application at config load."""
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))

# ---------------------------------------------------------------------------
# Worker configuration
# ---------------------------------------------------------------------------
# ⚠️  WARNING: MUST remain 1. See docstring above for rationale.
# Do NOT change this value. Multi-worker mode is UNSUPPORTED.
workers = 1

# gthread mode lets Flask serve multiple requests concurrently without spawning
# additional processes (all threads share the same in-memory state).
worker_class = "gthread"

# Each SSE connection occupies one gthread worker for its lifetime.  Derive the
# thread pool from the same bounded environment setting as constants.py so a
# raised MNS_MAX_SSE_LISTENERS cannot silently exhaust every request thread
# before its advertised limit is reached.  Keep six slots for normal API,
# reconnect, and shutdown requests.
sse_listener_limit = _env_int("MNS_MAX_SSE_LISTENERS", 64, 1, 1000)
sse_non_stream_thread_reserve = 6
required_threads = sse_listener_limit + sse_non_stream_thread_reserve
threads = required_threads

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
# Localhost only: the app is designed for personal use behind a browser.
# For reverse-proxy deployment, set MNS_ALLOW_REMOTE_API=1 and MNS_PROXY_FIX=1
# with MNS_ADMIN_TOKEN configured (see README).
bind = "127.0.0.1:5000"

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
# News + AI analysis can take 90-120 s on the first cold fetch.
timeout = 120

# Keep-alive for SSE connections (streaming diff updates).
keepalive = 65

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
loglevel = "info"
accesslog = "-"  # stdout
errorlog = "-"  # stderr
# Do not log query strings or Referer headers. Remote SSE authentication may
# carry the admin token in the query string because EventSource cannot set headers.
access_log_format = '%({x-forwarded-for}i)s %(l)s %(u)s [%(t)s] "%(m)s %(U)s %(H)s" %(s)s %(b)s'


def on_starting(server):
    """Validate the single-worker and SSE-capacity contracts before forking."""
    import sys

    # Respect the documented MNS_WORKER_VALIDATION=0 opt-out so this hook stays
    # consistent with the guard in wsgi.py (reserved for environments that have
    # externalized all shared state).
    if (
        server.num_workers > 1
        and os.environ.get("MNS_WORKER_VALIDATION", "1") not in ("0", "false", "no")
    ):
        sys.stderr.write(
            f"FATAL: Multi-worker mode is not supported (configured workers: {server.num_workers}).\n"
            "This application relies on in-memory singleton state.\n"
            "Please start Gunicorn with the bundled capacity settings: "
            "`gunicorn -c gunicorn.conf.py wsgi:app`.\n"
        )
        sys.exit(1)

    # Gunicorn command-line flags can override this file's ``threads`` value.
    # Reject an override that would let accepted SSE connections consume all
    # gthread workers and starve ordinary API calls.
    configured_threads = None
    try:
        # Real Gunicorn exposes ``server.cfg.threads``.  Access is guarded so
        # minimal test/dry-run server objects stay compatible while wsgi.py
        # still enforces the process-count invariant independently.
        configured_threads = int(server.cfg.threads)
    except (AttributeError, TypeError, ValueError):
        # Missing ``cfg`` or ``threads`` on a minimal test/dry-run server
        # object: wsgi.py still enforces the process-count invariant.
        pass
    if configured_threads is not None and configured_threads < required_threads:
        sys.stderr.write(
            "FATAL: Gunicorn thread count is too low for the configured SSE limit "
            f"(threads={configured_threads}, MNS_MAX_SSE_LISTENERS={sse_listener_limit}, "
            f"required>={required_threads}).\n"
            "Increase --threads or lower MNS_MAX_SSE_LISTENERS; reserve request threads "
            "must remain available alongside SSE connections.\n"
        )
        sys.exit(1)
