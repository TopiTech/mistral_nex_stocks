import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
import secrets

from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.exceptions import BadRequest

from utils.networking import (
    _is_allowed_shutdown_origin,
    _is_local_request,
    require_trusted_state_changing_request,
)
from utils.stock_payload import error_response
from utils.text_utils import _is_valid_api_key, _parse_json_request, _token_fingerprint
from app_state import app_state
from credential_manager import (
    clear_api_credentials,
    get_api_credential_state,
    get_custom_ai_prompt,
    get_model_badge,
    get_model_name,
    save_api_credentials,
    set_custom_ai_prompt,
)
from constants import (
    BASE_DIR,
    LANGSEARCH_API_KEY_MIN_LENGTH,
    MISTRAL_API_KEY_MIN_LENGTH,
    TAVILY_API_KEY_MIN_LENGTH,
)
from error_codes import ErrorCode
from route_helpers import _seconds_until, rate_limit

api_system_bp = Blueprint("api_system", __name__)




def _require_admin_token_if_remote(request_obj):
    """Require the admin token when the app is exposed beyond loopback."""
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    if allow_remote and len(admin_token) < 32:
        return False, (
            jsonify(
                {
                    "ok": False,
                    "error": "MNS_ADMIN_TOKEN must contain at least 32 characters when MNS_ALLOW_REMOTE_API is enabled",
                }
            ),
            503,
        )

    if not allow_remote:
        return True, None

    provided_token = request_obj.headers.get("X-MNS-Admin-Token", "").strip()
    if not provided_token or not secrets.compare_digest(provided_token, admin_token):
        return False, (jsonify({"ok": False, "error": "invalid admin token"}), 403)
    return True, None


@api_system_bp.route("/api/credentials", methods=["GET", "POST", "DELETE", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_credentials():
    """Handles API credential retrieval, updating, and removal.

    Personal / local-first defaults:
      * localhost + CSRF (+ trusted Origin on writes) is enough for GET/POST/DELETE.

    Hardened remote mode:
      * When ``MNS_ALLOW_REMOTE_API`` is enabled, ``MNS_ADMIN_TOKEN`` is mandatory
        for all methods. Without it the endpoint fails closed (503) so a
        misconfigured remote deployment cannot silently expose or mutate keys.
      * When an admin token IS configured, every method must present a matching
        ``X-MNS-Admin-Token`` header (constant-time compare).
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    provided_token = request.headers.get("X-MNS-Admin-Token", "").strip()

    # Fail closed: remote deployments must configure an admin token before any
    # credential endpoint is usable.
    if allow_remote and len(admin_token) < 32:
        current_app.logger.error(
            "Credentials access denied id=%s reason=admin_token_required_for_remote remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return jsonify(
            {
                "ok": False,
                "error": "MNS_ADMIN_TOKEN must contain at least 32 characters when MNS_ALLOW_REMOTE_API is enabled",
            }
        ), 503

    # When an admin token is configured, every credentials request must present it.
    # Local personal use typically leaves MNS_ADMIN_TOKEN unset so the existing
    # setup/settings UI continues to work with CSRF + local-origin only.
    if admin_token:
        if not provided_token or not secrets.compare_digest(provided_token, admin_token):
            current_app.logger.warning(
                "Credentials access denied id=%s reason=invalid_admin_token remote=%s",
                getattr(g, "request_id", "-"),
                request.remote_addr,
            )
            return jsonify({"ok": False, "error": "invalid admin token"}), 403

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
        current_app.logger.info("Credentials state requested id=%s", getattr(g, "request_id", "-"))
        state = get_api_credential_state()
        state["custom_ai_prompt"] = get_custom_ai_prompt()
        return jsonify({"ok": True, **state})

    if request.method == "DELETE":
        failed_keys = clear_api_credentials()
        if failed_keys:
            current_app.logger.warning(
                "Credentials cleared but failed to remove from OS Keyring for: %s, id=%s",
                failed_keys,
                getattr(g, "request_id", "-")
            )
            return jsonify({
                "ok": False,
                "error": "設定ファイルから資格情報を削除しましたが、OSのセキュアストア（Keyring）からの削除に一部失敗しました。",
                "failed_keys": failed_keys,
                **get_api_credential_state()
            }), 200
        current_app.logger.info("Credentials cleared id=%s", getattr(g, "request_id", "-"))
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
        if mistral_api_key and not _is_valid_api_key(
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
        # Validate prompt length BEFORE any side effects to prevent
        # partial state update (credentials saved but prompt rejected).
        if "custom_ai_prompt" in data:
            prompt_value = str(data.get("custom_ai_prompt") or "").strip()
            if len(prompt_value) > 5000:
                return error_response(
                    ErrorCode.UNSAFE_INPUT,
                    details={"reason": "カスタムプロンプトは5000文字以内で入力してください"},
                )
        if (
            mistral_api_key is not None
            or langsearch_api_key is not None
            or tavily_api_key is not None
        ):
            save_api_credentials(
                mistral_api_key=mistral_api_key,
                langsearch_api_key=langsearch_api_key,
                tavily_api_key=tavily_api_key,
            )
        if "custom_ai_prompt" in data:
            set_custom_ai_prompt(prompt_value)
    except RuntimeError as exc:
        current_app.logger.warning(
            "Credentials save failed id=%s reason=%s",
            getattr(g, "request_id", "-"),
            str(exc)[:200],
        )
        exc_msg = str(exc)
        if "MNS_EPHEMERAL_FALLBACK" in exc_msg or "keyring" in exc_msg or "DPAPI" in exc_msg:
            reason_msg = "セキュアストレージ (keyring/DPAPI) が利用できません。ヘッドレス環境やDocker環境の場合は、環境変数 MNS_EPHEMERAL_FALLBACK=1 を設定して再起動してください。"
        else:
            reason_msg = "設定の保存に失敗しました。再試行してください。"
        return error_response(
            ErrorCode.CONFIG_ERROR,
            status_code=500,
            details={"reason": reason_msg},
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
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    yf_limited = app_state.market.is_yf_rate_limited()
    yf_until = None
    if yf_limited:
        from app_state import yf_session_manager

        rl_until = yf_session_manager.get_rate_limit_until("yfinance")
        if rl_until:
            yf_until = datetime.fromtimestamp(rl_until).isoformat()

    health_data = {
        "ok": True,
        "app": "Mistral NeX Stocks",
        "model": get_model_name(),
        "badge": get_model_badge(),
        "is_yfinance_rate_limited": yf_limited,
        "yfinance_rate_limit_until": yf_until,
        "extension_manifest_ok": app_state._extension_manifest_status.get("ok", True),
        "extension_manifest_error": app_state._extension_manifest_status.get("error", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # APIキーの設定状態はローカルリクエストのみに暴露
    if _is_local_request(request) and os.environ.get(
        "MNS_ALLOW_REMOTE_API", ""
    ).strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        health_data.update(get_api_credential_state())

    return jsonify(health_data)


@api_system_bp.route("/api/cache-stats", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_cache_stats():
    """キャッシュ統計情報エンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_remote and not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403
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
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_remote and not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

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

    return jsonify(
        {
            "ok": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cache": {
                "sizes": cache_sizes,
                **app_state.stock_disk_cache.stats(),
                **app_state.payload_disk_cache.stats(),
            },
            "market_data": {
                "yfinance": yfinance_metrics,
                "is_syncing": is_syncing,
                "stock_counts": current_stock_counts,
                "indices_count": current_indices_count,
            },
            "sse": {"listeners": app_state.sse_announcer.listener_count()},
            "executors": executors,
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
        safe_keys = {
            "document-uri",
            "violated-directive",
            "effective-directive",
            "original-policy",
            "disposition",
            "blocked-uri",
            "line-number",
            "column-number",
            "source-file",
            "status-code",
            "referrer",
            "script-sample",
        }
        sanitized = {k: v for k, v in payload.items() if k in safe_keys}
        # Truncate URI values and strip control characters to prevent log injection
        for key in ("document-uri", "blocked-uri", "source-file", "referrer"):
            if key in sanitized and isinstance(sanitized[key], str):
                sanitized[key] = sanitized[key][:200]
        for key in sanitized:
            if isinstance(sanitized[key], str):
                sanitized[key] = "".join(
                    c for c in sanitized[key] if ord(c) >= 0x20 or c in ("\t", "\n")
                )
        current_app.logger.info(
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

    # Disable shutdown endpoint in production
    is_prod = os.environ.get("MNS_PROD", "").strip().lower() in ("1", "true", "yes")
    if is_prod:
        current_app.logger.warning("Shutdown request rejected: disabled in production environment")
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "shutdown is disabled in production"},
            status_code=403,
        )

    # F-4: Block shutdown in remote/proxy mode. Shutdown is a local-only
    # operation; remote callers should not be able to terminate the server
    # even with a valid admin token + shutdown token.
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if allow_remote:
        current_app.logger.warning(
            "Shutdown request rejected: not available in remote API mode id=%s",
            getattr(g, "request_id", "-"),
        )
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "shutdown is not available in remote API mode"},
            status_code=403,
        )

    if not _is_local_request(request):
        current_app.logger.warning(
            "Shutdown request rejected from non-local address: %s", request.remote_addr
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "forbidden"}, status_code=403
        )

    # Double check connection raw remote IP to resist any proxy-override headers spoofing
    raw_remote = request.environ.get("RAW_REMOTE_ADDR") or request.environ.get("REMOTE_ADDR", "")
    raw_remote = str(raw_remote).strip()
    from utils.networking import _is_loopback_ip

    if raw_remote and not _is_loopback_ip(raw_remote):
        current_app.logger.warning(
            "Shutdown request rejected: WSGI REMOTE_ADDR %s is not loopback", raw_remote
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "forbidden"}, status_code=403
        )

    if not _is_allowed_shutdown_origin(request):
        current_app.logger.warning("Shutdown request rejected from untrusted origin")
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "untrusted origin"}, status_code=403
        )

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

    if not app_state.validate_shutdown_token(provided_token):
        current_app.logger.warning(
            "Shutdown request rejected: invalid or already used shutdown token"
        )
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403

    logger = current_app.logger
    logger.info("Valid shutdown token accepted, initiating shutdown sequence")

    # Consume the validated token FIRST (mark as used), then rotate to
    # generate a fresh token for the next session. Order matters:
    # commit → rotate prevents a race window where rotate() resets
    # shutdown_token_used=False before commit marks it, leaving the
    # new token temporarily usable by a concurrent request.
    app_state.commit_shutdown_token()
    try:
        app_state.rotate_shutdown_token()
        logger.info("Shutdown token rotated for next session")
    except RuntimeError as exc:
        logger.warning("Failed to rotate shutdown token before shutdown: %s", exc)
        # Token is already consumed; rotation failure does not revert that.
        # The server shutdown proceeds regardless; the next startup will
        # generate a fresh token from scratch.

    def shutdown_server():
        logger.info("Shutdown thread started")

        # No sleep — shutdown should be as fast as possible
        try:
            app_state.shutdown_executors()
        except (RuntimeError, AttributeError, ValueError) as exc:
            logger.warning("Executor shutdown before process exit failed: %s", exc)

        # Remove PID file before exiting
        try:
            logger.info("Removing PID file")
            pid_file = BASE_DIR / ".backend.pid"
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
                    logger.warning("PID file still exists after retry attempts: %s", pid_file)
                else:
                    logger.info("PID file removed successfully")
        except (IOError, OSError) as exc:
            logger.warning("Failed to remove pid file during shutdown: %s", exc)

        try:
            logger.info("Shutting down logging")
            logging.shutdown()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Logging shutdown failed: %s", exc)

        # Send SIGTERM to self for graceful shutdown.
        try:
            logger.info("Sending SIGTERM to self for graceful shutdown")
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as exc:
            logger.error(
                "Graceful shutdown failed: %s. Process must be terminated externally.",
                exc,
            )
    shutdown_thread = threading.Thread(target=shutdown_server)
    shutdown_thread.daemon = True
    shutdown_thread.start()
    return jsonify({"ok": True, "message": "Shutting down..."})
