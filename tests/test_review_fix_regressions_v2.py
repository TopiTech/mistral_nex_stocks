"""Regression tests for review fixes (factory bootstrap hooks, error schema symmetry)."""

from unittest.mock import patch

from flask import Flask

import app as app_module
from error_codes import ErrorCode
from error_handlers import _build_error_response
from utils.stock_payload import error_response


def test_factory_app_has_bootstrap_hook():
    """Verify that apps created via create_app() have _ensure_bootstrap_called registered."""
    test_app = app_module.create_app(skip_bootstrap=True)
    # Check that _ensure_bootstrap_called is in before_request hooks
    before_funcs = test_app.before_request_funcs.get(None, [])
    hook_names = [fn.__name__ for fn in before_funcs]
    assert "_ensure_bootstrap_called" in hook_names


def test_ensure_bootstrap_called_triggers_bootstrap_on_request():
    """Verify that _ensure_bootstrap_called invokes bootstrap when not done and not skipped."""
    test_app = app_module.create_app(skip_bootstrap=False)
    test_app.config["TESTING"] = True
    test_app.config["MNS_SKIP_BOOTSTRAP"] = False

    with patch.object(app_module, "_app_bootstrap_done", False):
        with patch.object(app_module, "_env_bool", return_value=False):
            with patch.object(app_module, "bootstrap") as mock_bootstrap:
                with test_app.test_client() as client:
                    resp = client.get("/api/health")
                    mock_bootstrap.assert_called_once()
                    assert resp.status_code == 200


def test_ensure_bootstrap_called_skips_when_configured():
    """Verify that _ensure_bootstrap_called skips bootstrap when MNS_SKIP_BOOTSTRAP is True."""
    test_app = app_module.create_app(skip_bootstrap=True)
    test_app.config["TESTING"] = True

    with patch.object(app_module, "_app_bootstrap_done", False):
        with patch.object(app_module, "bootstrap") as mock_bootstrap:
            with test_app.test_client() as client:
                client.get("/api/health")
                mock_bootstrap.assert_not_called()


def test_error_response_schema_parity():
    """Verify that error_response from stock_payload and _build_error_response have identical key sets."""
    test_app = Flask(__name__)
    with test_app.test_request_context():
        resp_sp, code_sp = error_response(
            ErrorCode.INVALID_SYMBOL, status_code=400, details={"field": "symbol"}
        )
        resp_eh, code_eh = _build_error_response(
            message="無効なシンボルです",
            status_code=400,
            error_code=ErrorCode.INVALID_SYMBOL,
            details={"field": "symbol"},
        )

        data_sp = resp_sp.get_json()
        data_eh = resp_eh.get_json()

        assert code_sp == code_eh == 400
        assert set(data_sp.keys()) == set(data_eh.keys())
        assert set(data_sp.keys()) == {
            "ok",
            "error",
            "error_flag",
            "code",
            "error_code",
            "message",
            "details",
        }
        assert data_sp["ok"] is False
        assert data_sp["error_flag"] is True
        assert data_sp["error_code"] == int(ErrorCode.INVALID_SYMBOL)
        assert data_sp["code"] == str(ErrorCode.INVALID_SYMBOL)
