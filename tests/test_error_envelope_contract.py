"""Contract tests for the unified error envelope (H-3).

``error_response`` is the single owner of the error envelope. These tests lock
the frontend-consumed keys (``ok``/``error``/``message``/``error_code``/``valid``)
and the compatibility fields existing clients and tests depend on (``code`` name
strings such as ``"INVALID_API_KEY"``, ``latency_ms``), so unifying the
generation path behind one function cannot silently change the response
contract. They also pin that the route path (``error_response``) and the
exception path (``error_handlers._build_error_response``) produce identical
shapes from a single implementation.
"""

from unittest.mock import patch

import pytest
from flask import Flask

from app import create_app
from app_factory.hooks import _forbidden_cross_site_response
from error_codes import ErrorCode
from error_handlers import AppError, _build_error_response, register_error_handlers
from utils.stock_payload import error_response


@pytest.fixture
def app():
    app = Flask(__name__)
    register_error_handlers(app)

    @app.route("/raise-enum-code")
    def _raise_enum_code():
        raise AppError("Bad Request", status_code=400, error_code=ErrorCode.BAD_REQUEST)

    return app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


# --- 正常系: the standard envelope ------------------------------------------


def test_error_response_envelope_shape(app):
    with app.test_request_context():
        resp, status = error_response(ErrorCode.INVALID_API_KEY, 400)
    assert status == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error_flag"] is True
    assert data["code"] == str(ErrorCode.INVALID_API_KEY)
    assert data["error_code"] == int(ErrorCode.INVALID_API_KEY)
    assert data["message"] == data["error"]
    assert data["details"] == {}


def test_error_response_message_override_and_compat_keys(app):
    """A caller-specific message + compat keys must survive unification."""
    with app.test_request_context():
        resp, status = error_response(
            ErrorCode.INVALID_API_KEY,
            400,
            message="Mistral APIキーが指定されていません。",
            extra_fields={"code": "INVALID_API_KEY", "valid": False, "latency_ms": 12},
        )
    assert status == 400
    data = resp.get_json()
    # Frontend/test-consumed keys preserved exactly.
    assert data["ok"] is False
    assert data["error"] == "Mistral APIキーが指定されていません。"
    assert data["message"] == data["error"]
    assert data["valid"] is False
    assert data["code"] == "INVALID_API_KEY"  # compat name string preserved
    assert data["error_code"] == ErrorCode.INVALID_API_KEY.value
    assert data["latency_ms"] == 12
    # Unified envelope fields contributed by the single owner.
    assert data["error_flag"] is True
    assert data["details"] == {}


# --- 異常系・境界値: code fallbacks and sanitization -----------------------


def test_error_response_error_code_none_falls_back(app):
    with app.test_request_context():
        resp, _ = error_response(None, 500, message="oops")
    data = resp.get_json()
    assert data["code"] is None
    assert data["error_code"] == int(ErrorCode.UNKNOWN)
    assert data["error"] == "oops"


def test_error_response_non_numeric_code_preserves_string(app):
    """A custom string code keeps its literal while error_code falls back."""
    with app.test_request_context():
        resp, _ = error_response("CUSTOM_CODE", 400, message="custom")
    data = resp.get_json()
    assert data["code"] == "CUSTOM_CODE"
    assert data["error_code"] == int(ErrorCode.UNKNOWN)
    assert data["error"] == "custom"


def test_error_response_sanitizes_details_secrets(app):
    with app.test_request_context():
        resp, _ = error_response(
            ErrorCode.INVALID_API_KEY,
            400,
            details={"reason": "api_key=sk-secret-should-not-leak"},
        )
    data = resp.get_json()
    assert "sk-secret-should-not-leak" not in str(data)
    assert data["details"]["reason"] != "api_key=sk-secret-should-not-leak"


# --- Single-owner: route path and exception path must agree ----------------


def test_route_and_exception_paths_produce_identical_shape(app):
    with app.test_request_context():
        route_body = error_response(
            ErrorCode.BAD_REQUEST, 400, message="Bad Request"
        )[0].get_json()
        exc_body = _build_error_response(
            message="Bad Request", status_code=400, error_code=ErrorCode.BAD_REQUEST
        )[0].get_json()
    assert route_body == exc_body
    assert route_body["code"] == str(ErrorCode.BAD_REQUEST)
    assert route_body["error_code"] == int(ErrorCode.BAD_REQUEST)


def test_app_error_handler_matches_error_response(app):
    """The registered AppError handler must equal error_response's output."""
    client = app.test_client()
    resp = client.get("/raise-enum-code")
    assert resp.status_code == 400
    data = resp.get_json()
    with app.test_request_context():
        expected = error_response(
            ErrorCode.BAD_REQUEST, 400, message="Bad Request"
        )[0].get_json()
    assert data == expected


# --- Route-level contracts that existing tests/clients depend on -----------


def test_credentials_verify_error_contract(client):
    """The verify error keeps its exact compat keys while using error_response."""
    res = client.post(
        "/api/credentials/verify",
        json={},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert data["valid"] is False
    assert data["code"] == "INVALID_API_KEY"
    assert data["error_code"] == ErrorCode.INVALID_API_KEY.value
    assert data["error"] == "Mistral APIキーが指定されていません。"
    assert data["message"] == data["error"]
    assert data["error_flag"] is True


def test_credentials_verify_provider_error_no_leak(client):
    """Site 4 returns a fixed message and never reflects provider diagnostics."""
    internal_error = "upstream trace=private-456 api_key=should-not-leak"
    with patch(
        "routes.api_system.app_state.ai.get_or_create_mistral_client",
        side_effect=RuntimeError(internal_error),
    ):
        res = client.post(
            "/api/credentials/verify",
            json={"mistral_api_key": "test-valid-key"},
            headers={"Origin": "http://localhost:5000"},
        )
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert data["valid"] is False
    assert data["code"] == "INVALID_API_KEY"
    assert data["error"] == "APIキーの検証に失敗しました。設定と接続を確認してください。"
    assert data["message"] == data["error"]
    assert isinstance(data["latency_ms"], int)
    assert "private-456" not in str(data)
    assert "should-not-leak" not in str(data)


def test_csrf_reject_uses_unified_envelope(app):
    """The Sec-Fetch-Site rejection must carry the unified error envelope."""
    with app.test_request_context():
        resp, status = _forbidden_cross_site_response()
    assert status == 403
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "forbidden cross-site request"
    assert data["error_flag"] is True
    assert data["message"] == data["error"]
