"""Tests for error_handlers.py — verifies AppError and registered handlers."""

import pytest
from flask import Flask, jsonify
from werkzeug.exceptions import RequestEntityTooLarge, TooManyRequests

from error_codes import ErrorCode
from error_handlers import AppError, register_error_handlers


@pytest.fixture
def app():
    app = Flask(__name__)
    register_error_handlers(app)

    @app.route("/raise-app-error")
    def _raise_app_error():
        raise AppError(
            "bad input",
            status_code=400,
            error_code="INVALID_INPUT",
            details={"field": "symbol"},
        )

    @app.route("/raise-value")
    def _raise_value():
        raise ValueError("boom")

    @app.route("/raise-timeout")
    def _raise_timeout():
        raise TimeoutError("simulated timeout")

    @app.route("/raise-429")
    def _raise_429():
        raise TooManyRequests()

    @app.route("/raise-413")
    def _raise_413():
        raise RequestEntityTooLarge()

    @app.route("/post-only", methods=["POST"])
    def _post_only():
        return jsonify({"ok": True})

    return app


def test_app_error_defaults():
    err = AppError("msg")
    assert err.status_code == 400
    assert err.message == "msg"
    assert err.details == {}


def test_app_error_handler_structure(app):
    client = app.test_client()
    resp = client.get("/raise-app-error")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error_flag"] is True
    assert data["code"] == "INVALID_INPUT"
    assert data["error_code"] == int(data["error_code"])
    assert data["details"] == {"field": "symbol"}


def test_not_found_handler(app):
    client = app.test_client()
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.get_json()["error_flag"] is True


def test_method_not_allowed_handler(app):
    client = app.test_client()
    resp = client.get("/post-only")  # GET not allowed -> 405
    assert resp.status_code == 405
    assert resp.get_json()["error_flag"] is True


def test_too_many_requests_handler(app):
    client = app.test_client()
    resp = client.get("/raise-429")
    assert resp.status_code == 429
    assert resp.get_json()["error_flag"] is True


def test_payload_too_large_handler(app):
    client = app.test_client()
    resp = client.get("/raise-413")
    assert resp.status_code == 413
    assert resp.get_json()["error_flag"] is True


def test_internal_server_error_handler(app):
    client = app.test_client()
    resp = client.get("/raise-value")
    assert resp.status_code == 500
    assert resp.get_json()["error_flag"] is True


def test_timeout_error_handler(app):
    """TimeoutError must map to 503 with the timeout error code (not a 500 crash)."""
    client = app.test_client()
    resp = client.get("/raise-timeout")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["error_flag"] is True
    assert data["error_code"] == int(ErrorCode.TIMEOUT_ERROR)
    assert "Timeout" in data["message"]


def test_http_exception_description_not_leaked(app):
    """HTTPException.description must never be forwarded to clients.

    Werkzeug's HTTPException.description can contain framework internals
    (e.g. "The browser (or proxy) sent a request that this server could
    not understand"). The error handler must suppress it and return a
    generic reason instead.
    """
    from werkzeug.exceptions import BadRequest

    @app.route("/raise-bad-request")
    def _raise_bad_request():
        raise BadRequest()

    client = app.test_client()
    resp = client.get("/raise-bad-request")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    # The description must NOT leak framework details to the client
    reason = data.get("details", {}).get("reason")
    assert reason is None, f"HTTPException.description leaked: {reason!r}"


def test_catch_all_http_exception_description_not_leaked(app):
    """The catch-all HTTPException handler must also suppress descriptions."""
    from werkzeug.exceptions import Forbidden

    @app.route("/raise-forbidden")
    def _raise_forbidden():
        raise Forbidden()

    client = app.test_client()
    resp = client.get("/raise-forbidden")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["ok"] is False
    # The description must NOT leak to the client
    reason = data.get("details", {}).get("reason")
    assert reason is None, f"HTTPException.description leaked: {reason!r}"
