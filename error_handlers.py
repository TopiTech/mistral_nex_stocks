"""
error_handlers.py - Global Flask error handlers for Mistral NeX Stocks.

Provides a unified error response format:
    {"ok": False, "error": "...", "code": "...", "error_code": int}

Usage:
    from error_handlers import AppError, register_error_handlers
    raise AppError("Invalid input", status_code=400, error_code="INVALID_INPUT")
"""

from typing import Optional

from flask import Flask, current_app, jsonify


class AppError(Exception):
    """Application-level error with structured JSON response."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        error_code: Optional[str] = None,
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details or {}


def register_error_handlers(app: Flask) -> None:
    """Register all global error handlers on the Flask app."""

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError):
        return jsonify({
            "ok": False,
            "error": error.message,
            "code": error.error_code,
            "error_code": -1,
            "message": error.message,
            "details": error.details,
        }), error.status_code

    @app.errorhandler(400)
    def bad_request_error(error):
        return jsonify({
            "ok": False,
            "error": "Bad Request",
            "message": "The request was malformed or invalid.",
        }), 400

    @app.errorhandler(403)
    def forbidden_error(error):
        return jsonify({
            "ok": False,
            "error": "Forbidden",
            "message": "You do not have permission to access this resource.",
        }), 403

    @app.errorhandler(404)
    def not_found_error(error):
        return jsonify({
            "ok": False,
            "error": "Not Found",
            "message": "The requested resource was not found.",
        }), 404

    @app.errorhandler(405)
    def method_not_allowed_error(error):
        return jsonify({
            "ok": False,
            "error": "Method Not Allowed",
            "message": "The HTTP method is not allowed for this endpoint.",
        }), 405

    @app.errorhandler(413)
    def payload_too_large_error(error):
        return jsonify({
            "ok": False,
            "error": "Payload Too Large",
            "message": "The request payload exceeds the maximum allowed size.",
        }), 413

    @app.errorhandler(429)
    def rate_limit_error(error):
        return jsonify({
            "ok": False,
            "error": "Too Many Requests",
            "message": "Rate limit exceeded. Please try again later.",
        }), 429

    @app.errorhandler(500)
    def internal_server_error(error):
        current_app.logger.error("Internal server error: %s", error, exc_info=True)
        return jsonify({
            "ok": False,
            "error": "Internal Server Error",
            "message": "An unexpected error occurred. Please try again later.",
        }), 500
