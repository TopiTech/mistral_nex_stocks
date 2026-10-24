"""観測系 (health/cache-stats/metrics)。`routes.api_system` からの純粋な移動。

`get_api_credential_state` / `_is_local_request` /
`_require_admin_token_if_remote` はテストで
`patch(\"routes.api_system.X\")` されるため、呼び出し時に
`routes.api_system` 属性として解決する。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from flask import current_app, request

import routes.api_system
from app_state import app_state
from credential_manager import get_ai_provider, get_model_badge, get_model_name, is_preview_mode
from error_codes import ErrorCode
from route_helpers import _seconds_until, rate_limit
from routes.system.blueprint import api_system_bp
from utils.env_helpers import _env_bool
from utils.networking import is_allowed_trusted_origin
from utils.stock_payload import error_response, ok_response


@api_system_bp.route("/api/health", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=60, window_seconds=60)
def api_health():
    """ヘルスチェックエンドポイント"""
    if request.method == "OPTIONS":
        return ok_response()
    ok, denied = routes.api_system._require_admin_token_if_remote(request)
    if not ok:
        return denied
    yf_limited = app_state.market.is_yf_rate_limited()
    yf_until = None
    if yf_limited:
        from app_state import yf_session_manager

        rl_until = yf_session_manager.get_rate_limit_until("yfinance")
        if rl_until:
            yf_until = datetime.fromtimestamp(rl_until, tz=UTC).isoformat()

    bootstrap_skipped = _env_bool("MNS_SKIP_BOOTSTRAP")
    bootstrap_ready = app_state.bootstrap_ready.is_set() and not bootstrap_skipped
    with app_state._extension_origins_cache_lock:
        manifest_ok = app_state._extension_manifest_status.get("ok", True)
        manifest_error = app_state._extension_manifest_status.get("error", "")

    health_data = {
        "ok": True,
        "ready": bootstrap_ready,
        "bootstrap_skipped": bootstrap_skipped,
        "app": "Mistral NeX Stocks",
        "model": get_model_name(),
        "badge": get_model_badge(),
        "ai_provider": get_ai_provider(),
        "preview_mode": is_preview_mode(),
        "is_yfinance_rate_limited": yf_limited,
        "yfinance_rate_limit_until": yf_until,
        "extension_manifest_ok": manifest_ok,
        "extension_manifest_error": manifest_error,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    # APIキーの設定状態はローカルリクエストかつOriginが信頼できる場合のみ暴露（リモートモードや未信頼Originでは非開示）
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
    if not allow_remote and routes.api_system._is_local_request(request):
        origin = request.headers.get("Origin")
        if not origin or is_allowed_trusted_origin(request):
            health_data.update(routes.api_system.get_api_credential_state())

    return ok_response(health_data)


@api_system_bp.route("/api/cache-stats", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_cache_stats():
    """キャッシュ統計情報エンドポイント"""
    if request.method == "OPTIONS":
        return ok_response()
    ok, denied = routes.api_system._require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
    if not allow_remote and not routes.api_system._is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)
    if not allow_remote and request.headers.get("Origin") and not is_allowed_trusted_origin(request):
        return error_response(
            ErrorCode.FORBIDDEN, details={"reason": "untrusted origin"}, status_code=403
        )
    stats = app_state.cache.get_stats()
    with app_state.cache.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.cache.caches.items()}
    stats["cache_sizes"] = cache_sizes
    # Include disk cache statistics
    try:
        stats.update(app_state.stock_disk_cache.stats())
    except Exception as exc:
        current_app.logger.debug("Failed to read disk cache stats: %s", exc)
    try:
        stats.update(app_state.payload_disk_cache.stats())
    except Exception as exc:
        current_app.logger.debug("Failed to read payload disk cache stats: %s", exc)
    return ok_response({"cache_stats": stats})


@api_system_bp.route("/api/metrics", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_metrics():
    """Expose safe operational metrics for local troubleshooting.
    SECURITY: This endpoint is restricted to localhost only.
    Sensitive internal state is intentionally excluded.
    """
    if request.method == "OPTIONS":
        return ok_response()
    ok, denied = routes.api_system._require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
    if not allow_remote and not routes.api_system._is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)
    if not allow_remote and request.headers.get("Origin") and not is_allowed_trusted_origin(request):
        return error_response(
            ErrorCode.FORBIDDEN, details={"reason": "untrusted origin"}, status_code=403
        )

    # Only expose safe, non-sensitive operational metrics
    with app_state.cache.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.cache.caches.items()}

    with app_state.market.yfinance_lock:
        yfinance_metrics = {
            "rate_limited": (
                app_state.market.is_yfinance_rate_limited
                and time.time() < app_state.market.yfinance_rate_limit_until
            ),
            "rate_limit_clears_in_sec": _seconds_until(app_state.market.yfinance_rate_limit_until),
        }

    with app_state.market.scraper_block_lock:
        scraper_metrics = {
            "blocked": app_state.market.is_scraper_blocked(),
            "block_clears_in_sec": app_state.market.scraper_block_clears_in(),
        }

    with app_state.cache.sse_data_lock:
        current_stock_counts = {
            market: len(items) for market, items in app_state.market.current_stocks_cache.items()
        }
        current_indices_count = len(app_state.market.current_indices_cache)

    with app_state.market.is_syncing_lock:
        is_syncing = app_state.market.is_syncing

    # Expose thread-pool saturation so operators can see when the AI-bound
    # `executor` or the market-data `data_executor` are backing up (H3/M6).
    executors = {
        "ai": app_state.execution.executor_stats(app_state.execution.executor),
        "data": app_state.execution.executor_stats(app_state.execution.data_executor),
        "news": app_state.execution.executor_stats(app_state.execution.news_executor),
        "sync": app_state.execution.executor_stats(app_state.execution.sync_refresh_executor),
    }

    # Realtime engine diagnostics so producer-level failures (dead WS, blocked
    # scrapers, stale market_store) are visible in one screen. All reads are
    # defensive: the engine is optional and its threads may not be started.
    try:
        from services.realtime_engine import realtime_market_engine as _rt_engine

        _latest_ts = 0.0
        try:
            for _p in _rt_engine.get_market_snapshot().values():
                _ts = _p.get("updated_at") or 0.0
                _latest_ts = max(_latest_ts, _ts)
        except Exception:
            _latest_ts = 0.0

        engine_metrics: dict[str, Any] = {
            "running": bool(getattr(_rt_engine, "running", False)),
            "market_store_count": len(_rt_engine.get_market_snapshot()),
            "pts_store_count": len(_rt_engine.get_pts_snapshot()),
            "last_update_at": _latest_ts or None,
            "tv_ws_connected": bool(getattr(_rt_engine.tv_client, "connected", False)),
            "tv_last_connected_at": (
                getattr(_rt_engine.tv_client, "last_connected_at", 0.0) or None
            ),
            "tv_subscribed_symbols": len(getattr(_rt_engine.tv_client, "symbols", set())),
            "jp_scraper_symbols": len(getattr(_rt_engine.yahoojp_scraper, "symbols", set())),
        }
        # Thread liveness reads the inner ``.thread`` attribute of each producer.
        engine_metrics["tv_thread_alive"] = bool(
            getattr(getattr(_rt_engine.tv_client, "thread", None), "is_alive", lambda: False)()
        )
        engine_metrics["jp_scraper_thread_alive"] = bool(
            getattr(
                getattr(_rt_engine.yahoojp_scraper, "thread", None), "is_alive", lambda: False
            )()
        )
        engine_metrics["pts_thread_alive"] = bool(
            getattr(getattr(_rt_engine, "pts_thread", None), "is_alive", lambda: False)()
        )
    except Exception as exc:
        current_app.logger.debug(
            "Failed to collect realtime engine metrics error_type=%s", type(exc).__name__
        )
        engine_metrics = {"error": "unavailable"}

    with app_state.market.scraper_block_lock:
        engine_metrics["scraper_blocked"] = app_state.market.is_scraper_blocked()
        engine_metrics["scraper_block_clears_in_sec"] = app_state.market.scraper_block_clears_in()
    with app_state.market.yfinance_lock:
        engine_metrics["yf_rate_limited"] = app_state.market.is_yf_rate_limited()

    return ok_response(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "cache": {
                "sizes": cache_sizes,
                **app_state.stock_disk_cache.stats(),
                **app_state.payload_disk_cache.stats(),
            },
            "market_data": {
                "yfinance": yfinance_metrics,
                "scraper": scraper_metrics,
                "is_syncing": is_syncing,
                "stock_counts": current_stock_counts,
                "indices_count": current_indices_count,
            },
            "sse": {
                # Admission reservations include a response returned by Flask
                # but not yet iterated by the WSGI server, so this is the
                # authoritative global-capacity metric.  Per-mode figures
                # remain queue-level observability only.
                "listeners": app_state.sse_listener_limiter.listener_count(),
                "mode1_listeners": app_state.sse_announcer_mode1.listener_count(),
                "mode2_listeners": app_state.sse_announcer_mode2.listener_count(),
                "mode1_announced": app_state.sse_announcer_mode1.stats()["announced"],
                "mode1_dropped": app_state.sse_announcer_mode1.stats()["dropped"],
                "mode2_announced": app_state.sse_announcer_mode2.stats()["announced"],
                "mode2_dropped": app_state.sse_announcer_mode2.stats()["dropped"],
            },
            "engine": engine_metrics,
            "executors": executors,
            "mistral": {
                **app_state.ai.mistral_usage_stats(),
                "circuit_open": app_state.market.is_circuit_open("mistral"),
                "next_allowed_ts": app_state.ai.mistral_next_allowed_ts,
                "response_cache_size": app_state.ai.response_cache_size(),
                "clients_cached": app_state.ai.clients_cached_count(),
            },
            "config": {
                "model": get_model_name(),
                "badge": get_model_badge(),
            },
        }
    )
