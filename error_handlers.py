"""
error_handlers.py - Global Flask error handlers for Mistral NeX Stocks.

Provides a unified error response format:
    {"ok": False, "error": "...", "code": "...", "error_code": int}

Usage:
    from error_handlers import AppError, register_error_handlers
    raise AppError("Invalid input", status_code=400, error_code="INVALID_INPUT")
"""

from typing import Any, Optional

from flask import Flask, current_app, jsonify


class AppError(Exception):
    """Application-level error with structured JSON response.

    All API endpoints should raise AppError rather than calling
    error_response() directly to ensure consistent error format.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        error_code: Optional[Any] = None,
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details or {}


def _build_error_response(
    message: str,
    status_code: int,
    error_code: Optional[Any] = None,
    details: Optional[dict] = None,
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
        return _build_error_response(
            message="Bad Request",
            status_code=400,
            error_code=ErrorCode.BAD_REQUEST,
            details={"reason": str(error) if error.description != "Bad Request" else None},
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
        current_app.logger.error("Internal server error: %s", error, exc_info=True)
        return _build_error_response(
            message="Internal Server Error",
            status_code=500,
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
        )

    @app.errorhandler(Exception)
    def handle_exception(error):
        """Catch-all exception handler to prevent stack trace leakage in production."""
        from utils.env_helpers import _is_production_env

        _is_prod = _is_production_env()
        current_app.logger.error("Unhandled exception: %s", error, exc_info=not _is_prod)
        return _build_error_response(
            message="Internal Server Error",
            status_code=500,
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
        )
