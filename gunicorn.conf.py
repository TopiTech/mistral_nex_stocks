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

# ---------------------------------------------------------------------------
# Worker configuration
# ---------------------------------------------------------------------------
# ⚠️  WARNING: MUST remain 1. See docstring above for rationale.
# Do NOT change this value. Multi-worker mode is UNSUPPORTED.
workers = 1

# gthread mode lets Flask serve multiple requests concurrently without spawning
# additional processes (all threads share the same in-memory state).
worker_class = "gthread"

# 8 threads matches the ThreadPoolExecutor sizes in execution_state.py.
threads = 8

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
accesslog = "-"   # stdout
errorlog = "-"    # stderr


def on_starting(server):
    """Ensure that Gunicorn cannot be started with more than 1 worker."""
    if server.num_workers > 1:
        import sys
        sys.stderr.write(
            f"FATAL: Multi-worker mode is not supported (configured workers: {server.num_workers}).\n"
            "This application relies on in-memory singleton state.\n"
            "Please start Gunicorn with exactly 1 worker: `gunicorn --workers 1 ...`.\n"
        )
        sys.exit(1)

