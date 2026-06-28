import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request
from app_state import app_state
from config_utils import (
    get_api_credential_state,
    clear_api_credentials,
    save_api_credentials,
    set_custom_ai_prompt,
    get_custom_ai_prompt,
    get_model_name,
    get_model_badge,
)
from error_codes import ErrorCode
from constants import (
    MISTRAL_API_KEY_MIN_LENGTH,
    LANGSEARCH_API_KEY_MIN_LENGTH,
    TAVILY_API_KEY_MIN_LENGTH,
)
from route_helpers import rate_limit, _seconds_until

from app_helpers import (
    _is_local_request,
    _parse_json_request,
    error_response,
    _is_valid_api_key,
    _token_fingerprint,
    _is_allowed_shutdown_origin,
    require_trusted_state_changing_request,
)
from werkzeug.exceptions import BadRequest

api_system_bp = Blueprint("api_system", __name__)


@api_system_bp.route("/api/credentials", methods=["GET", "POST", "DELETE", "OPTIONS"])
def api_credentials():
    """Handles API credential retrieval, updating, and removal."""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    if request.method in ("POST", "DELETE"):
        ok, reason = require_trusted_state_changing_request(request)
    else:
        ok, reason = _is_local_request(request), "forbidden"
    if not ok:
        current_app.logger.warning(
            "Credentials access denied id=%s reason=%s remote=%s",
            getattr(g, "request_id", "-"),
            reason,
            request.remote_addr,
        )
        return jsonify({"ok": False, "error": reason}), 403


    if request.method == "GET":
        current_app.logger.info(
            "Credentials state requested id=%s", getattr(g, "request_id", "-")
        )
        state = get_api_credential_state()
        state["custom_ai_prompt"] = get_custom_ai_prompt()
        return jsonify({"ok": True, **state})

    if request.method == "DELETE":
        clear_api_credentials()
        current_app.logger.info(
            "Credentials cleared id=%s", getattr(g, "request_id", "-")
        )
        return jsonify({"ok": True, **get_api_credential_state()})

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    mistral_api_key = data.get("mistral_api_key")
    langsearch_api_key = data.get("langsearch_api_key")
    tavily_api_key = data.get("tavily_api_key")

    if mistral_api_key is not None:
        mistral_api_key = mistral_api_key.strip()
        if not _is_valid_api_key(
            mistral_api_key, min_length=MISTRAL_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_mistral_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(mistral_api_key),
                MISTRAL_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.INVALID_API_KEY,
                details={
                    "fields": ["mistral_api_key"],
                    "min_length": MISTRAL_API_KEY_MIN_LENGTH,
                },
            )

    if langsearch_api_key is not None:
        langsearch_api_key = langsearch_api_key.strip()
        if langsearch_api_key and not _is_valid_api_key(
            langsearch_api_key, min_length=LANGSEARCH_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_langsearch_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(langsearch_api_key),
                LANGSEARCH_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.UNSAFE_INPUT,
                details={
                    "fields": ["langsearch_api_key"],
                    "min_length": LANGSEARCH_API_KEY_MIN_LENGTH,
                },
            )

    if tavily_api_key is not None:
        tavily_api_key = tavily_api_key.strip()
        if tavily_api_key and not _is_valid_api_key(
            tavily_api_key, min_length=TAVILY_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_tavily_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(tavily_api_key),
                TAVILY_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.UNSAFE_INPUT,
                details={
                    "fields": ["tavily_api_key"],
                    "min_length": TAVILY_API_KEY_MIN_LENGTH,
                },
            )

    try:
        if mistral_api_key is not None or langsearch_api_key is not None or tavily_api_key is not None:
            save_api_credentials(
                mistral_api_key=mistral_api_key,
                langsearch_api_key=langsearch_api_key,
                tavily_api_key=tavily_api_key,
            )
        if "custom_ai_prompt" in data:
            prompt_value = str(data.get("custom_ai_prompt") or "").strip()
            if len(prompt_value) > 5000:
                return error_response(
                    ErrorCode.UNSAFE_INPUT,
                    details={"reason": "カスタムプロンプトは5000文字以内で入力してください"},
                )
            set_custom_ai_prompt(prompt_value)
    except RuntimeError as exc:
        current_app.logger.warning(
            "Credentials save failed id=%s reason=%s",
            getattr(g, "request_id", "-"),
            str(exc)[:200],
        )
        return error_response(
            ErrorCode.CONFIG_ERROR,
            status_code=500,
            details={"reason": str(exc)},
        )

    current_app.logger.info(
        "Credentials/Settings saved id=%s mistral=%s langsearch=%s tavily=%s custom_prompt_len=%d",
        getattr(g, "request_id", "-"),
        _token_fingerprint(mistral_api_key),
        _token_fingerprint(langsearch_api_key),
        _token_fingerprint(tavily_api_key),
        len(str(data.get("custom_ai_prompt") or "")),
    )
    state = get_api_credential_state()
    state["custom_ai_prompt"] = get_custom_ai_prompt()
    return jsonify({"ok": True, **state})


@api_system_bp.route("/api/health", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=60, window_seconds=60)
def api_health():
    """ヘルスチェックエンドポイント"""
    with app_state.yfinance_lock:
        yf_limited = app_state.is_yfinance_rate_limited and (
            time.time() < app_state.yfinance_rate_limit_until
        )
        yf_until = (
            datetime.fromtimestamp(app_state.yfinance_rate_limit_until).isoformat()
            if app_state.is_yfinance_rate_limited
            else None
        )

    health_data = {
        "ok": True,
        "app": "Mistral NeX Stocks",
        "model": get_model_name(),
        "badge": get_model_badge(),
        "is_yfinance_rate_limited": yf_limited,
        "yfinance_rate_limit_until": yf_until,
        "extension_manifest_ok": app_state._extension_manifest_status.get(
            "ok", True
        ),
        "extension_manifest_error": app_state._extension_manifest_status.get(
            "error", ""
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # APIキーの設定状態はローカルリクエストのみに暴露
    if _is_local_request(request):
        health_data.update(get_api_credential_state())

    return jsonify(health_data)


@api_system_bp.route("/api/cache-stats", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_cache_stats():
    """キャッシュ統計情報エンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    stats = app_state.cache.get_stats()
    with app_state.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.caches.items()}
    stats["cache_sizes"] = cache_sizes
    return jsonify({"ok": True, "cache_stats": stats})


@api_system_bp.route("/api/metrics", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_metrics():
    """Expose safe operational metrics for local troubleshooting.
    
    SECURITY: This endpoint is restricted to localhost only.
    Sensitive internal state is intentionally excluded.
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Only expose safe, non-sensitive operational metrics
    with app_state.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.caches.items()}
    
    with app_state.yfinance_lock:
        yfinance_metrics = {
            "rate_limited": (
                app_state.is_yfinance_rate_limited
                and time.time() < app_state.yfinance_rate_limit_until
            ),
            "rate_limit_clears_in_sec": _seconds_until(
                app_state.yfinance_rate_limit_until
            ),
        }
    
    with app_state.sse_data_lock:
        current_stock_counts = {
            market: len(items)
            for market, items in app_state.current_stocks_cache.items()
        }
        current_indices_count = len(app_state.current_indices_cache)
    
    with app_state.is_syncing_lock:
        is_syncing = app_state.is_syncing

    return jsonify(
        {
            "ok": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cache": {
                "sizes": cache_sizes,
            },
            "market_data": {
                "yfinance": yfinance_metrics,
                "is_syncing": is_syncing,
                "stock_counts": current_stock_counts,
                "indices_count": current_indices_count,
            },
            "sse": {
                "listeners": app_state.sse_announcer.listener_count()
            },
            "config": {
                "model": get_model_name(),
                "badge": get_model_badge(),
            },
        }
    )


@api_system_bp.route("/api/csp-report", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def api_csp_report():
    """CSP report receiver for Report-Only mode (accepts JSON POST)."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
        # Sanitize: remove potentially sensitive fields before logging
        safe_keys = {"document-uri", "violated-directive", "effective-directive",
                     "original-policy", "disposition", "blocked-uri",
                     "line-number", "column-number", "source-file", "status-code",
                     "referrer", "script-sample"}
        sanitized = {k: v for k, v in payload.items() if k in safe_keys}
        # Truncate URI values to avoid leaking sensitive query params
        for key in ("document-uri", "blocked-uri", "source-file", "referrer"):
            if key in sanitized and isinstance(sanitized[key], str):
                sanitized[key] = sanitized[key][:200]
        current_app.logger.warning(
            "CSP report received: %s", json.dumps(sanitized, ensure_ascii=False)[:2000]
        )
    except (BadRequest, TypeError, ValueError) as exc:
        current_app.logger.debug("Failed to parse CSP report: %s", exc)
    # Return 204 No Content as recommended for CSP reports
    return ("", 204)


@api_system_bp.route("/api/shutdown", methods=["POST", "OPTIONS"])
def api_shutdown():
    """シャットダウンエンドポイント（ワンタイムトークン使用）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    if not _is_local_request(request):
        current_app.logger.warning(
            "Shutdown request rejected from non-local address: %s", request.remote_addr
        )
        return error_response(ErrorCode.UNSAFE_INPUT, details={"reason": "forbidden"}, status_code=403)

    if not _is_allowed_shutdown_origin(request):
        current_app.logger.warning("Shutdown request rejected from untrusted origin")
        return error_response(ErrorCode.UNSAFE_INPUT, details={"reason": "untrusted origin"}, status_code=403)

    # JSON body validation
    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )

    if data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "confirm flag required"}), 400

    token_header = request.headers.get("X-MNS-Shutdown-Token")
    token_json = data.get("shutdown_token")
    provided_token = (token_header or token_json or "").strip()

    if not provided_token:
        current_app.logger.warning("Shutdown request rejected: missing shutdown token")
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403

    if not app_state.consume_shutdown_token(provided_token):
        current_app.logger.warning(
            "Shutdown request rejected: invalid or already used shutdown token"
        )
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403

    logger = current_app.logger
    logger.info("Valid shutdown token consumed, initiating shutdown sequence")

    # Rotate token BEFORE spawning shutdown thread to prevent race condition
    # where a second request could reuse the old token during the shutdown delay
    try:
        app_state.rotate_shutdown_token()
        logger.info("Shutdown token rotated for next session")
    except Exception as exc:
        logger.warning("Failed to rotate shutdown token before shutdown: %s", exc)

    def shutdown_server():
        logger.info("Shutdown thread started")
        time.sleep(1.0)

        try:
            app_state.shutdown_executors()
        except (RuntimeError, AttributeError, ValueError) as exc:
            logger.warning("Executor shutdown before process exit failed: %s", exc)

        # 終了前にPIDファイルを削除
        try:
            logger.info("Removing PID file")
            base_dir = Path(__file__).resolve().parent.parent
            pid_file = base_dir / ".backend.pid"
            if pid_file.exists():
                removed = False
                for _ in range(2):
                    try:
                        pid_file.unlink()
                    except (IOError, OSError):
                        time.sleep(0.1)
                    if not pid_file.exists():
                        removed = True
                        break
                if not removed:
                    logger.warning(
                        "PID file still exists after retry attempts: %s", pid_file
                    )
                else:
                    logger.info("PID file removed successfully")
        except (IOError, OSError) as exc:
            logger.warning("Failed to remove pid file during shutdown: %s", exc)

        try:
            logger.info("Shutting down logging")
            logging.shutdown()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # PIDファイルを使用してプロセスを終了
        try:
            import psutil

            current_pid = os.getpid()
            logger.info("Current PID: %s", current_pid)

            # 自分自身のプロセスを終了
            parent = psutil.Process(current_pid)
            parent.terminate()

            # タイムアウト後に強制終了
            def force_kill():
                try:
                    time.sleep(2.0)
                    if parent.is_running():
                        logger.warning("Process still running, forcing kill")
                        parent.kill()
                except psutil.NoSuchProcess:
                    pass

            threading.Thread(target=force_kill, daemon=True).start()
        except ImportError:
            logger.warning("psutil not available, using os._exit")
            os._exit(0)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Failed to terminate process: %s", exc)
            os._exit(0)

    # デーモンスレッドとして設定
    shutdown_thread = threading.Thread(target=shutdown_server)
    shutdown_thread.daemon = True
    shutdown_thread.start()
    return jsonify({"ok": True, "message": "Shutting down..."})
