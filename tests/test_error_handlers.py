"""Tests for error_handlers.py — verifies AppError and registered handlers."""
import pytest
from flask import Flask, jsonify
from werkzeug.exceptions import TooManyRequests, RequestEntityTooLarge

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
