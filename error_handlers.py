"""
error_handlers.py - Global Flask error handlers for Mistral NeX Stocks.

Extracted from app.py to reduce module complexity and pylint disable count.
All error responses follow a consistent JSON structure with 'ok', 'error',
'message' fields for uniform frontend handling.
"""

from flask import Flask, current_app, jsonify


def register_error_handlers(app: Flask) -> None:
    """Register all global error handlers on the Flask app.

    Call once after app creation. Each handler returns a JSON response
    that the frontend can parse uniformly.
    """

    @app.errorhandler(400)
    def bad_request_error(error):
        """Handle 400 Bad Request errors."""
        return jsonify(
            {
                "ok": False,
                "error": "Bad Request",
                "message": "The request was malformed or invalid.",
            }
        ), 400

    @app.errorhandler(403)
    def forbidden_error(error):
        """Handle 403 Forbidden errors."""
        return jsonify(
            {
                "ok": False,
                "error": "Forbidden",
                "message": "You do not have permission to access this resource.",
            }
        ), 403

    @app.errorhandler(404)
    def not_found_error(error):
        """Handle 404 Not Found errors."""
        return jsonify(
            {
                "ok": False,
                "error": "Not Found",
                "message": "The requested resource was not found.",
            }
        ), 404

    @app.errorhandler(405)
    def method_not_allowed_error(error):
        """Handle 405 Method Not Allowed errors."""
        return jsonify(
            {
                "ok": False,
                "error": "Method Not Allowed",
                "message": "The HTTP method is not allowed for this endpoint.",
            }
        ), 405

    @app.errorhandler(413)
    def payload_too_large_error(error):
        """Handle 413 Payload Too Large errors."""
        return jsonify(
            {
                "ok": False,
                "error": "Payload Too Large",
                "message": "The request payload exceeds the maximum allowed size.",
            }
        ), 413

    @app.errorhandler(429)
    def rate_limit_error(error):
        """Handle 429 Too Many Requests errors."""
        return jsonify(
            {
                "ok": False,
                "error": "Too Many Requests",
                "message": "Rate limit exceeded. Please try again later.",
            }
        ), 429

    @app.errorhandler(500)
    def internal_server_error(error):
        """Handle 500 Internal Server Error - never leak stack traces."""
        current_app.logger.error("Internal server error: %s", error, exc_info=True)
        return jsonify(
            {
                "ok": False,
                "error": "Internal Server Error",
                "message": "An unexpected error occurred. Please try again later.",
            }
        ), 500
