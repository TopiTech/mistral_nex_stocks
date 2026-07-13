"""
gunicorn.conf.py - Gunicorn configuration for Mistral NeX Stocks.

IMPORTANT: This application uses in-memory singleton state (app_state,
yf_session_manager) and thread-local caches that are NOT shared between
OS processes.  Always run with a single worker.

Recommended invocation:
    gunicorn -c gunicorn.conf.py wsgi:app
"""

# ---------------------------------------------------------------------------
# Worker configuration
# ---------------------------------------------------------------------------
# MUST remain 1.  See wsgi.py (H-1) for a full explanation.
workers = 1

# gthread mode lets Flask serve multiple requests concurrently without spawning
# additional processes (all threads share the same in-memory state).
worker_class = "gthread"

# 8 threads matches the ThreadPoolExecutor sizes in execution_state.py.
threads = 8

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
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
