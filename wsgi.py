"""
wsgi.py - WSGI entry point for Mistral NeX Stocks.

Usage:
    gunicorn wsgi:app
    uwsgi --module wsgi:app
    python -m wsgi

This is the canonical application entry point. It builds the Flask app via the
factory in app.py (app.py no longer bootstraps at import time) and then runs the
runtime bootstrap exactly once (background threads, token init, data loading).
Tests can opt out by setting MNS_SKIP_BOOTSTRAP=1.
"""
import os

from app import create_app, bootstrap

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
