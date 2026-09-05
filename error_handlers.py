"""
error_handlers.py - Global Flask error handlers for Mistral NeX Stocks.

Provides a unified error response format:
    {"ok": False, "error": "...", "code": "...", "error_code": int}

Usage:
    from error_handlers import AppError, register_error_handlers
    raise AppError("Invalid input", status_code=400, error_code="INVALID_INPUT")
"""

from typing import Any

from flask import Flask, current_app, jsonify
from werkzeug.exceptions import HTTPException


class AppError(Exception):
    """Application-level error with structured JSON response.

    All API endpoints should raise AppError rather than calling
    error_response() directly to ensure consistent error format.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        error_code: Any | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details or {}


def _build_error_response(
    message: str,
    status_code: int,
    error_code: Any | None = None,
    details: dict | None = None,
) -> tuple:
    """Build a unified error response dict.

    Shared by AppError handler and error_response() so both paths
    produce identical JSON shapes.
    """
    from error_codes import ErrorCode

    ec_int = int(ErrorCode.UNKNOWN)
    if error_code is not None:
        try:
            ec_int = int(error_code)
        except (ValueError, TypeError):
            pass
    return jsonify(
        {
            "ok": False,
            "error": message,
            "error_flag": True,
            "code": str(error_code) if error_code is not None else None,
            "error_code": ec_int,
            "message": message,
            "details": details or {},
        }
    ), status_code


def register_error_handlers(app: Flask) -> None:
    """Register all global error handlers on the Flask app."""
    from error_codes import ErrorCode

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError):
        return _build_error_response(
            message=error.message,
            status_code=error.status_code,
            error_code=error.error_code,
            details=error.details,
        )

    @app.errorhandler(400)
    def bad_request_error(error):
        # Do not forward Werkzeug's HTTPException.description to clients:
        # it can reveal framework internals (e.g. "The browser (or proxy)
        # sent a request that this server could not understand"). The server
        # log still records the full error for diagnosis.
        current_app.logger.debug("400 error description: %s", getattr(error, "description", None))
        return _build_error_response(
            message="Bad Request",
            status_code=400,
            error_code=ErrorCode.BAD_REQUEST,
            details={"reason": None},
        )

    @app.errorhandler(403)
    def forbidden_error(error):
        return _build_error_response(
            message="Forbidden",
            status_code=403,
            error_code=ErrorCode.FORBIDDEN,
        )

    @app.errorhandler(404)
    def not_found_error(error):
        return _build_error_response(
            message="Not Found",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
        )

    @app.errorhandler(405)
    def method_not_allowed_error(error):
        return _build_error_response(
            message="Method Not Allowed",
            status_code=405,
            error_code=ErrorCode.METHOD_NOT_ALLOWED,
        )

    @app.errorhandler(413)
    def payload_too_large_error(error):
        return _build_error_response(
            message="Payload Too Large",
            status_code=413,
            error_code=ErrorCode.PAYLOAD_TOO_LARGE,
        )

    @app.errorhandler(429)
    def rate_limit_error(error):
        return _build_error_response(
            message="Too Many Requests",
            status_code=429,
            error_code=ErrorCode.TOO_MANY_REQUESTS,
        )

    @app.errorhandler(500)
    def internal_server_error(error):
        current_app.logger.error("Internal server error: %s", error)
        return _build_error_response(
            message="Internal Server Error",
            status_code=500,
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
        )

    @app.errorhandler(TimeoutError)
    def handle_timeout_error(error):
        current_app.logger.warning("Timeout error: %s", error)
        return _build_error_response(
            message="Service Unavailable (Timeout)",
            status_code=503,
            error_code=ErrorCode.TIMEOUT_ERROR,
        )

    @app.errorhandler(Exception)
    def handle_exception(error):
        """Catch-all exception handler to prevent stack trace leakage in production."""
        if isinstance(error, HTTPException):
            code_mapping = {
                400: ErrorCode.BAD_REQUEST,
                403: ErrorCode.FORBIDDEN,
                404: ErrorCode.NOT_FOUND,
                405: ErrorCode.METHOD_NOT_ALLOWED,
                413: ErrorCode.PAYLOAD_TOO_LARGE,
                429: ErrorCode.TOO_MANY_REQUESTS,
            }
            # Do not forward Werkzeug's HTTPException.description to clients:
            # it can reveal framework internals. The server log still records
            # the full error for diagnosis.
            current_app.logger.debug(
                "HTTPException caught: code=%s description=%s",
                error.code,
                error.description,
            )
            return _build_error_response(
                message=error.name or "HTTP Error",
                status_code=error.code or 500,
                error_code=code_mapping.get(error.code) if error.code is not None else None,
                details={"reason": None},
            )

        # The response body is always generic; the stack trace must still reach
        # the server-side log in every environment so production failures can
        # be diagnosed from the log files.
        current_app.logger.error("Unhandled exception: %s", error, exc_info=True)
        return _build_error_response(
            message="Internal Server Error",
            status_code=500,
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
        )
