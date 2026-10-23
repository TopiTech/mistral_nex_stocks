"""ランタイムブートストラップ。`app` からの純粋な移動。

完了フラグ (`_app_bootstrap_done` / `_app_bootstrap_lock`) は `app` シムが
所有し、ここでは呼び出し時に `app` 属性として解決する (テストが再束縛する
ため)。`_start_background_threads` / `schedule_sync_all_stocks_now` /
`schedule_news_warmup` も `patch(\"app.X\")` 対象のため同様に `app` 経由で
解決する。
"""

from __future__ import annotations

import logging
import os
import time

from flask import Flask

import app as app_module
from app_state import app_state
from utils.env_helpers import _env_bool, _env_int
from utils.storage import load_user_stocks

logger = logging.getLogger("app")


# H-1/H-2 improvement: runtime bootstrap for threads and config-less startup.
def bootstrap(app: Flask) -> None:
    """Initialize runtime-only components after app creation.

    This separates wiring-time side effects from runtime side effects,
    allowing WSGI/import usage without unintended disk/network activity.

    Core initialization is fail-closed: on failure ``_app_bootstrap_done``
    stays False so a corrected environment can retry, and
    ``app_state.bootstrap_ready`` is not set.
    """
    if _env_bool("MNS_SKIP_BOOTSTRAP"):
        logger.info("Skipping runtime bootstrap per MNS_SKIP_BOOTSTRAP")
        return

    with app_module._app_bootstrap_lock:
        if app_module._app_bootstrap_done:
            return

        # H-6: Fail closed when remote API access is enabled without an admin token.
        # MNS_ALLOW_REMOTE_API expands the credentials / local-request surface; without
        # MNS_ADMIN_TOKEN a misconfigured reverse-proxy deployment would leave key
        # management reachable by any caller that can hit the proxy.
        # Checked BEFORE marking bootstrap complete so a misconfigured start can
        # still be corrected (env fix + retry) without leaving a half-booted flag.
        _allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
        _use_proxy_fix = _env_bool("MNS_PROXY_FIX")
        if _allow_remote and not _use_proxy_fix:
            raise RuntimeError(
                "FATAL: MNS_ALLOW_REMOTE_API requires MNS_PROXY_FIX=1. "
                "Refuse to start. Configure MNS_PROXY_FIX=1 or disable remote API access."
            )
        _admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
        if _allow_remote and len(_admin_token) < 32:
            raise RuntimeError(
                "FATAL: MNS_ALLOW_REMOTE_API requires MNS_ADMIN_TOKEN with at least 32 characters. "
                "Refuse to start. Configure a strong token or disable remote API access."
            )
        if _allow_remote:
            _proxy_hops = _env_int("MNS_PROXY_FIX_X_FOR", 1, min_value=0)
            if _proxy_hops < 1:
                raise RuntimeError(
                    "FATAL: MNS_ALLOW_REMOTE_API requires MNS_PROXY_FIX_X_FOR >= 1. "
                    "The trusted proxy hop count must be at least 1 when remote mode is enabled."
                )

        # Keep the first-request fallback and direct ``app:app`` imports under
        # the same fail-closed process guard as wsgi.py.  In particular, uWSGI
        # ini files expose their process count through the native ``uwsgi``
        # module rather than Gunicorn-style environment variables.
        from utils.worker_validation import enforce_single_worker

        try:
            enforce_single_worker()
        except RuntimeError as exc:
            raise RuntimeError(f"FATAL: {exc}") from exc

        # Runtime-only: initialize shutdown token, user stocks, and background loops.
        # These are intentionally removed from ``create_app`` to prevent import-time
        # side effects and make thread startup explicit.
        # Keep the entire critical path under the lock so concurrent callers cannot
        # double-start background threads or mark a half-initialized runtime ready.
        try:
            app_state.get_or_create_shutdown_token()
            app_state.initialize_yfinance_cache()
            # Older releases persisted portfolio fields in the reusable payload
            # cache. Scrub them in place so cold-start market data is retained
            # without leaving personal holdings in plaintext.
            from utils.stock_payload import _PORTFOLIO_RESPONSE_FIELDS

            try:
                migrated_payloads = app_state.payload_disk_cache.remove_fields_recursive(
                    _PORTFOLIO_RESPONSE_FIELDS
                )
                if migrated_payloads:
                    logger.info(
                        "Removed portfolio fields from %d legacy payload cache entries",
                        migrated_payloads,
                    )
            except (OSError, TypeError) as cache_exc:
                # Market-data cache is disposable and must not make core
                # application bootstrap fail.
                logger.warning("Could not migrate legacy payload cache: %s", cache_exc)
            load_user_stocks(force=True)
        except Exception as exc:
            logger.error("Bootstrap initialization failed: %s", exc)
            app_state.bootstrap_ready.clear()
            raise RuntimeError(f"Bootstrap initialization failed: {exc}") from exc

        app_module._start_background_threads()

        def _schedule_sync() -> None:
            try:
                app_module.schedule_sync_all_stocks_now()
            except Exception:
                logger.exception("Initial stock sync scheduling failed")

        def _schedule_news() -> None:
            try:
                app_module.schedule_news_warmup()
            except Exception:
                logger.exception("Initial news warmup scheduling failed")

        for _executor_name, _job in (
            ("sync_refresh_executor", _schedule_sync),
            ("news_executor", _schedule_news),
        ):
            _submitted = app_state.execution.safe_submit(_executor_name, _job)
            if not _submitted:
                # Backpressure: retry once after a short bounded delay so a
                # cold-start transient saturation does not silently drop the
                # initial sync/warmup while _app_bootstrap_done is marked True.
                time.sleep(0.5)
                _retry_ok = app_state.execution.safe_submit(_executor_name, _job)
                if not _retry_ok:
                    logger.warning(
                        "Failed to submit initial %s job after retry (queue full/shutdown)",
                        _executor_name,
                    )

        # Mark complete only after successful core init + thread start.
        app_module._app_bootstrap_done = True
        app_state.bootstrap_ready.set()
