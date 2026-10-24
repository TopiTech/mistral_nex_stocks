"""シャットダウン。`routes.api_system` からの純粋な移動。

`_is_local_request` はテストで `patch(\"routes.api_system._is_local_request\")`
されるため、呼び出し時に `routes.api_system` 属性として解決する。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from flask import current_app, g, request

import routes.api_system
from app_state import app_state
from config_store import APP_DATA_DIR
from constants import BASE_DIR
from error_codes import ErrorCode
from route_helpers import rate_limit
from routes.system.blueprint import api_system_bp
from utils.env_helpers import _env_bool
from utils.networking import _is_allowed_shutdown_origin
from utils.stock_payload import error_response, ok_response
from utils.text_utils import _parse_json_request


def _terminate_current_process(logger: logging.Logger) -> None:
    """End the serving process after the shutdown response has been sent."""
    if os.name == "nt":
        # sys.exit() raised in the daemon shutdown thread only terminates that
        # thread. os._exit() is intentional here: all application cleanup has
        # already run in the caller and Windows has no SIGTERM equivalent.
        logger.info("Exiting process on Windows")
        try:
            logging.shutdown()
        except Exception:
            pass
        os._exit(0)

    try:
        logger.info("Sending SIGTERM to self for graceful shutdown")
        try:
            logging.shutdown()
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        try:
            logger.error(
                "Graceful shutdown failed: %s. Process must be terminated externally.",
                exc,
            )
        except Exception:
            pass
        try:
            logging.shutdown()
        except Exception:
            pass
        os._exit(1)


@api_system_bp.route("/api/shutdown", methods=["POST", "OPTIONS"])
# Matches /api/stocks/reset (the other destructive endpoint). A failed attempt
# writes a warning log line and touches the used-token marker file, so an
# unthrottled local caller could otherwise drive unbounded log/IO churn.
@rate_limit(max_requests=5, window_seconds=60)
def api_shutdown():
    """シャットダウンエンドポイント（ワンタイムトークン使用）"""
    if request.method == "OPTIONS":
        return ok_response()

    # Disable shutdown endpoint in production
    is_prod = _env_bool("MNS_PROD")
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
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
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

    if not routes.api_system._is_local_request(request):
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
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "confirm flag required"},
            status_code=400,
        )

    token_header = request.headers.get("X-MNS-Shutdown-Token")
    token_json = data.get("shutdown_token")
    # Preserve the established fallback from an empty header to the JSON field.
    token_value = token_header or token_json
    if token_value is not None and not isinstance(token_value, str):
        current_app.logger.warning("Shutdown request rejected: malformed shutdown token")
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "invalid shutdown request"},
            status_code=403,
        )
    provided_token = (token_value or "").strip()

    if not provided_token:
        current_app.logger.warning("Shutdown request rejected: missing shutdown token")
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "invalid shutdown request"},
            status_code=403,
        )

    if not app_state.consume_shutdown_token(provided_token):
        current_app.logger.warning(
            "Shutdown request rejected: invalid or already used shutdown token"
        )
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "invalid shutdown request"},
            status_code=403,
        )

    logger = current_app.logger
    logger.info("Valid shutdown token accepted, initiating shutdown sequence")

    # The token was atomically consumed above before any shutdown work. Rotate
    # only after consumption so concurrent requests cannot enter this block
    # with the same one-time token.
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

        # Remove PID file before exiting. R1: prefer the runtime-state copy
        # and also clean up a legacy project-root file written by older
        # versions so the source tree stops accumulating stale lock state.
        for pid_file in (APP_DATA_DIR / ".backend.pid", BASE_DIR / ".backend.pid"):
            try:
                if not pid_file.exists():
                    continue
                logger.info("Removing PID file %s", pid_file)
                removed = False
                for _ in range(2):
                    try:
                        pid_file.unlink()
                    except OSError:
                        time.sleep(0.1)
                    if not pid_file.exists():
                        removed = True
                        break
                if not removed:
                    logger.warning("PID file still exists after retry attempts: %s", pid_file)
                else:
                    logger.info("PID file removed successfully: %s", pid_file)
            except OSError as exc:
                logger.warning("Failed to remove pid file %s during shutdown: %s", pid_file, exc)

        try:
            from app_bg import _release_leader_lock

            _release_leader_lock()
        except Exception as exc:
            logger.debug("Failed to release leader lock during shutdown: %s", exc)

        # Brief delay to allow the HTTP 200 JSON response to be fully flushed over TCP
        time.sleep(0.3)
        _terminate_current_process(logger)

    shutdown_thread = threading.Thread(target=shutdown_server)
    shutdown_thread.daemon = True
    shutdown_thread.start()
    return ok_response({"message": "Shutting down..."})
